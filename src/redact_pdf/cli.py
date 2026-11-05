"""Command-line interface: ``redact-pdf {redact,verify,apply-boxes,remove-pages,flatten}``."""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from redact_pdf import __version__
from redact_pdf.boxes import APPLIED_SUFFIX, BoxReport, apply_boxes
from redact_pdf.core import (
    DEFAULT_LABEL,
    DOCUMENT_LEVEL,
    FLATTENED_SUFFIX,
    REDACTED_SUFFIX,
    RedactionError,
    RedactionReport,
    default_output_path,
    flatten_pdf,
    load_terms,
    redact_pdf,
    verify_pdf,
)
from redact_pdf.pages import TRIMMED_SUFFIX, PageRemovalReport, remove_pages

EXIT_OK = 0
EXIT_LEFTOVERS = 1
EXIT_ERROR = 2


def _regex(value: str) -> re.Pattern[str]:
    try:
        return re.compile(value)
    except re.error as exc:
        raise argparse.ArgumentTypeError(f"invalid regex {value!r}: {exc}") from exc


def _add_match_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("what to match")
    group.add_argument(
        "-t", "--term", action="append", default=[], metavar="TEXT",
        help="literal text to match as whole words, case-insensitive (repeatable)",
    )  # fmt: skip
    group.add_argument(
        "-f", "--terms-file", type=Path, metavar="FILE",
        help="file with one term per line; '#' starts a comment",
    )  # fmt: skip
    group.add_argument(
        "-r", "--regex", action="append", default=[], type=_regex, metavar="PATTERN",
        help="regular expression to match (repeatable)",
    )  # fmt: skip
    group.add_argument(
        "--no-email", dest="emails", action="store_false",
        help="do not match email addresses (matched by default)",
    )  # fmt: skip


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="redact-pdf",
        description="True redaction of known strings and patterns in PDFs.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "redact",
        help="redact matches and write a new PDF",
        description=(
            "Redact matches and strip metadata. Email addresses are redacted by default. "
            f"OUTPUT defaults to <input>{REDACTED_SUFFIX}.pdf next to the input; "
            "INPUT may be a directory for batch mode."
        ),
    )
    p.add_argument("input", type=Path, help="PDF file or directory of PDFs")
    p.add_argument(
        "output", type=Path, nargs="?",
        help=f"output file or directory (default: <input>{REDACTED_SUFFIX}.pdf)",
    )  # fmt: skip
    _add_match_args(p)
    p.add_argument(
        "--no-email-domains", dest="email_domains", action="store_false",
        help="do not redact organisation names taken from email domains (acme from x@acme.com)",
    )  # fmt: skip
    p.add_argument(
        "--no-email-names", dest="email_names", action="store_false",
        help="do not redact person names taken from email addresses (display names, address parts)",
    )  # fmt: skip
    p.add_argument(
        "--label", default=DEFAULT_LABEL, help=f"text inside each box (default: {DEFAULT_LABEL})"
    )
    p.add_argument("--no-label", action="store_true", help="plain black boxes without text")
    p.add_argument(
        "--no-scrub", action="store_true", help="keep links, attachments, scripts and hidden text"
    )
    p.add_argument(
        "--keep-annotations", action="store_true",
        help="keep comments and form fields interactive (their text is then not redacted)",
    )  # fmt: skip

    p = sub.add_parser("verify", help="check a PDF's text layer for remaining matches")
    p.add_argument("input", type=Path, help="PDF file to check")
    _add_match_args(p)
    p.add_argument("--label", default=DEFAULT_LABEL, help="label used during redaction")

    p = sub.add_parser(
        "apply-boxes",
        help="turn boxes drawn during review into real redactions",
        description=(
            "Delete the text, image pixels and drawings under boxes drawn during manual "
            "review (filled shapes, e.g. from Xournal++, or annotations from a PDF viewer). "
            "Pages are not converted to images. "
            f"OUTPUT defaults to <input>{APPLIED_SUFFIX}.pdf."
        ),
    )
    p.add_argument("input", type=Path, help="the reviewed PDF")
    p.add_argument(
        "output", type=Path, nargs="?",
        help=f"output PDF file (default: <input>{APPLIED_SUFFIX}.pdf)",
    )  # fmt: skip
    p.add_argument(
        "--no-scrub", action="store_true", help="keep links, attachments, scripts and hidden text"
    )

    p = sub.add_parser(
        "remove-pages",
        help="remove pages completely; the remaining pages follow on directly",
        description=(
            "Remove pages and everything only they use, including bookmarks to them. "
            "Metadata is stripped as in redact. "
            f"OUTPUT defaults to <input>{TRIMMED_SUFFIX}.pdf."
        ),
    )
    p.add_argument("input", type=Path, help="PDF file")
    p.add_argument(
        "pages", help="pages to remove, numbered from 1: 92-100, 1,3,92-100 or 92- (to the end)"
    )
    p.add_argument(
        "output", type=Path, nargs="?",
        help=f"output PDF file (default: <input>{TRIMMED_SUFFIX}.pdf)",
    )  # fmt: skip
    p.add_argument(
        "--no-scrub", action="store_true", help="keep links, attachments, scripts and hidden text"
    )

    p = sub.add_parser("flatten", help="burn in manual boxes by converting pages to images")
    p.add_argument("input", type=Path, help="PDF file to flatten")
    p.add_argument(
        "output", type=Path, nargs="?",
        help=f"output PDF file (default: <input>{FLATTENED_SUFFIX}.pdf)",
    )  # fmt: skip
    p.add_argument("--dpi", type=int, default=200, help="render resolution (default: 200)")

    return parser


def _collect_terms(args: argparse.Namespace) -> list[str]:
    terms: list[str] = list(args.term)
    if args.terms_file:
        terms.extend(load_terms(args.terms_file))
    return terms


def _where(page: int) -> str:
    return "outside page text" if page == DOCUMENT_LEVEL else f"page {page}"


def _counts(items: dict[str, int]) -> str:
    return ", ".join(f"{text!r} x{n}" for text, n in items.items())


def _print_report(report: RedactionReport) -> None:
    print(f"{report.output}: {report.total} redaction(s)")
    if report.domain_terms:
        found = ", ".join(f"{t!r} (from {e})" for t, e in report.domain_terms.items())
        print(f"  email domains: {found}")
    for address, names in report.name_terms.items():
        print(f"  names from {address}: {', '.join(map(repr, names))}")
    for page, items in sorted(report.hits.items()):
        print(f"  page {page}: {_counts(items)}")
    if report.bookmarks:
        print(f"  bookmarks: {report.bookmarks} title(s) redacted")
    if report.hidden_text:
        print(f"  outside page text: {_counts(report.hidden_text)}")
    if report.metadata:
        print(f"  metadata removed: {report.metadata.summary()}")
    if report.hidden_images:
        print(f"  image areas no page shows, blanked: {report.hidden_images} image(s)")
    if report.image_pages:
        pages = ", ".join(f"{p} ({n} img)" for p, n in sorted(report.image_pages.items()))
        print(f"  review manually, pages with images: {pages}")
    for page, remaining in sorted(report.leftovers.items()):
        print(f"  WARNING {_where(page)}: still found {', '.join(remaining)}")


def _is_redacted_output(path: Path) -> bool:
    return path.stem.upper().endswith(REDACTED_SUFFIX.upper())


def _redact_jobs(src: Path, dst: Path | None) -> list[tuple[Path, Path]]:
    """Pair each input with its output path."""
    if not src.is_dir():
        if dst is None:
            return [(src, default_output_path(src))]
        if dst.is_dir():
            return [(src, dst / default_output_path(src).name)]
        return [(src, dst)]

    if dst is not None and dst.exists() and not dst.is_dir():
        raise RedactionError("When INPUT is a directory, OUTPUT must be a directory.")
    # Skip earlier outputs, so running twice on the same folder is safe.
    files = sorted(
        p for p in src.iterdir() if p.suffix.lower() == ".pdf" and not _is_redacted_output(p)
    )
    if not files:
        raise RedactionError(f"No PDF files found in {src}")
    out_dir = dst if dst is not None else src
    return [(f, out_dir / default_output_path(f).name) for f in files]


def _cmd_redact(args: argparse.Namespace) -> int:
    terms = _collect_terms(args)
    label = None if args.no_label else args.label
    exit_code = EXIT_OK
    for src, dst in _redact_jobs(args.input, args.output):
        report = redact_pdf(
            src, dst, terms, args.regex,
            emails=args.emails, email_domains=args.email_domains,
            email_names=args.email_names,
            label=label, scrub=not args.no_scrub,
            keep_annotations=args.keep_annotations,
        )  # fmt: skip
        _print_report(report)
        if not report.is_clean:
            exit_code = EXIT_LEFTOVERS
    return exit_code


def _cmd_verify(args: argparse.Namespace) -> int:
    terms = _collect_terms(args)
    if not terms and not args.regex and not args.emails:
        raise ValueError("Nothing to match: provide terms or patterns, or enable emails.")
    found = verify_pdf(args.input, terms, args.regex, emails=args.emails, label=args.label)
    if not found:
        print(f"{args.input}: clean (text, and image areas no page shows)")
        return EXIT_OK
    print(f"{args.input}: matches remain")
    for page, items in sorted(found.items()):
        print(f"  {_where(page)}: {', '.join(items)}")
    return EXIT_LEFTOVERS


def _ranges(pages: list[int]) -> str:
    """``[1, 2, 3, 7]`` -> ``"1-3, 7"``."""
    runs: list[list[int]] = []
    for n in pages:
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def _print_removal_report(report: PageRemovalReport) -> None:
    print(
        f"{report.output}: removed {len(report.removed)} page(s) ({_ranges(report.removed)}), "
        f"{report.pages_left} left"
    )
    if report.bookmarks:
        print(f"  bookmarks to removed pages dropped: {report.bookmarks}")
    if report.metadata:
        print(f"  metadata removed: {report.metadata.summary()}")
    if report.leftovers:
        print(f"  WARNING {report.leftovers} removed page object(s) still in the file")


def _cmd_remove_pages(args: argparse.Namespace) -> int:
    report = remove_pages(args.input, args.pages, args.output, scrub=not args.no_scrub)
    _print_removal_report(report)
    return EXIT_OK if report.is_clean else EXIT_LEFTOVERS


def _cmd_flatten(args: argparse.Namespace) -> int:
    output = flatten_pdf(args.input, args.output, dpi=args.dpi)
    print(f"{output}: flattened at {args.dpi} dpi")
    return EXIT_OK


def _pages(counts: dict[int, int]) -> str:
    return ", ".join(f"{page} ({n})" for page, n in sorted(counts.items()))


def _print_box_report(report: BoxReport) -> None:
    print(f"{report.output}: {report.total} box(es) applied")
    if report.drawn:
        print(f"  drawn boxes, pages: {_pages(report.drawn)}")
    if report.annotations:
        print(f"  annotation boxes, pages: {_pages(report.annotations)}")
    if report.metadata:
        print(f"  metadata removed: {report.metadata.summary()}")
    if report.hidden_images:
        print(f"  image areas no page shows, blanked: {report.hidden_images} image(s)")
    if not report.total:
        print("  WARNING no boxes found: highlight, draw or box what should go, then save")
    for page, n in sorted(report.ignored.items()):
        print(
            f"  WARNING page {page}: {n} shape(s) over text or images that do not hide them "
            "(see-through fill, highlighter or pen); not applied"
        )
    for page, n in sorted(report.leftovers.items()):
        print(f"  WARNING page {page}: text still under {n} box(es)")
    for page, n in sorted(report.image_leftovers.items()):
        print(f"  WARNING page {page}: {n} image(s) still have content no page shows")


def _cmd_apply_boxes(args: argparse.Namespace) -> int:
    report = apply_boxes(args.input, args.output, scrub=not args.no_scrub)
    _print_box_report(report)
    return EXIT_OK if report.is_clean else EXIT_LEFTOVERS


COMMANDS = {
    "redact": _cmd_redact,
    "verify": _cmd_verify,
    "apply-boxes": _cmd_apply_boxes,
    "remove-pages": _cmd_remove_pages,
    "flatten": _cmd_flatten,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )
    try:
        return COMMANDS[args.command](args)
    except (RedactionError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
