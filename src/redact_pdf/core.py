"""Core redaction, verification and flattening logic.

All functions operate on file paths and never modify the input file.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from redact_pdf.metadata import MetadataReport, strip_metadata

__all__ = [
    "DEFAULT_LABEL",
    "EMAIL_PATTERN",
    "FLATTENED_SUFFIX",
    "REDACTED_SUFFIX",
    "RedactionError",
    "RedactionReport",
    "default_output_path",
    "flatten_pdf",
    "load_terms",
    "redact_pdf",
    "verify_pdf",
]

logger = logging.getLogger(__name__)

DEFAULT_LABEL = "REDACTED"
REDACTED_SUFFIX = "_REDACTED"
FLATTENED_SUFFIX = "_FLATTENED"
LABEL_FONT = "helv"
MIN_LABEL_FONTSIZE = 4.0
"""Below this size the label is unreadable, so a plain black box is drawn instead."""

EMAIL_PATTERN = re.compile(
    r"""
    (?<![\w%+-])                     # not in the middle of a longer token
    [\w%+-]{1,64}(?:\.[\w%+-]{1,64}){0,15}   # local part: dot-separated, no double dots
    @
    (?:[\w-]{1,63}\.){1,10}          # domain labels: mail. example.
    [^\W\d_]{2,63}                   # top-level domain: letters only (excludes 1.2@3.4)
    (?![\w-])                        # TLD must end here
    """,
    re.VERBOSE,
)
"""Pragmatic email matcher: covers real-world addresses, not every RFC 5322 edge case.

Repetitions are bounded (using RFC length limits) so matching stays linear-time,
even on adversarial text such as long dot-separated runs without an ``@``.
"""

# Blank only the image pixels under a redaction box. Defined dynamically by PyMuPDF,
# so it is invisible to type checkers; 2 is its documented value.
REDACT_IMAGE_PIXELS: int = getattr(pymupdf, "PDF_REDACT_IMAGE_PIXELS", 2)

BLACK = (0.0, 0.0, 0.0)
WHITE = (1.0, 1.0, 1.0)

PageHits = dict[str, list["pymupdf.Quad"]]


class RedactionError(Exception):
    """Raised when a document cannot be processed safely."""


@dataclass
class RedactionReport:
    """Summary of a redaction run.

    Attributes:
        output: Path of the written PDF.
        hits: ``{page_number: {matched_text: count}}`` (1-based page numbers).
        image_pages: ``{page_number: image_count}``. Text inside images is not
            detected, so these pages should be reviewed manually.
        leftovers: ``{page_number: [matched_text, ...]}`` found when re-checking
            the output. Should be empty.
        bookmarks: Number of bookmark (outline) titles that were redacted.
        metadata: What was removed by :func:`redact_pdf.metadata.strip_metadata`.
    """

    output: Path
    hits: dict[int, dict[str, int]] = field(default_factory=dict)
    image_pages: dict[int, int] = field(default_factory=dict)
    leftovers: dict[int, list[str]] = field(default_factory=dict)
    bookmarks: int = 0
    metadata: MetadataReport = field(default_factory=MetadataReport)

    @property
    def total(self) -> int:
        """Total number of redacted areas."""
        return sum(sum(page.values()) for page in self.hits.values())

    @property
    def is_clean(self) -> bool:
        """True if no search terms remain in the output's text layer."""
        return not self.leftovers


def default_output_path(input_path: str | Path, suffix: str = REDACTED_SUFFIX) -> Path:
    """``contract.pdf`` -> ``contract_REDACTED.pdf`` in the same directory."""
    src = Path(input_path)
    return src.with_name(f"{src.stem}{suffix}.pdf")


def load_terms(path: str | Path) -> list[str]:
    """Read one term per line, ignoring blank lines and ``#`` comments."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [s for line in lines if (s := line.strip()) and not s.startswith("#")]


def _normalise_terms(terms: Iterable[str]) -> list[str]:
    """Deduplicate and sort longest-first, so 'John Smith' wins over 'John'."""
    unique = {t.strip() for t in terms if t and t.strip()}
    return sorted(unique, key=lambda t: (-len(t), t))


def _build_patterns(
    patterns: Iterable[str | re.Pattern[str]], *, emails: bool
) -> list[re.Pattern[str]]:
    compiled = [p if isinstance(p, re.Pattern) else re.compile(p) for p in patterns]
    if emails:
        compiled.append(EMAIL_PATTERN)
    return compiled


def _open(path: Path) -> pymupdf.Document:
    if not path.is_file():
        raise FileNotFoundError(path)
    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # pymupdf raises several exception types
        raise RedactionError(f"Could not open {path}: {exc}") from exc
    if doc.needs_pass:
        doc.close()
        raise RedactionError(f"{path} is password protected; decrypt it first.")
    return doc


def _check_paths(input_path: Path, output_path: Path) -> None:
    if input_path.resolve() == output_path.resolve():
        raise RedactionError("Output must differ from input; the original is never overwritten.")
    output_path.parent.mkdir(parents=True, exist_ok=True)


def _find_hits(
    page: pymupdf.Page, terms: Sequence[str], patterns: Sequence[re.Pattern[str]]
) -> PageHits:
    """Return ``{matched_text: [quads]}`` for literal terms and regex matches.

    Regex matches are found in the extracted page text and then located on the
    page with a literal search. ``search_for`` is case-insensitive for ASCII.
    """
    needles = list(terms)
    if patterns:
        text = page.get_text("text")
        for pattern in patterns:
            needles.extend(m.group(0) for m in pattern.finditer(text) if m.group(0).strip())

    hits: PageHits = {}
    for needle in dict.fromkeys(needles):  # dedupe, keep order
        quads = page.search_for(needle, quads=True)
        if quads:
            hits[needle] = quads
    return hits


def _label_fontsize(rect: pymupdf.Rect, label: str) -> float:
    """Largest font size at which ``label`` fits inside ``rect``."""
    width_at_1pt = pymupdf.get_text_length(label, fontname=LABEL_FONT, fontsize=1)
    return float(min(rect.height * 0.7, rect.width * 0.95 / width_at_1pt))


def _mostly_inside(inner: pymupdf.Rect, outer: pymupdf.Rect, threshold: float = 0.5) -> bool:
    area = inner.get_area()
    return area > 0 and (inner & outer).get_area() / area >= threshold


def redact_pdf(
    input_path: str | Path,
    output_path: str | Path | None = None,
    terms: Iterable[str] = (),
    patterns: Iterable[str | re.Pattern[str]] = (),
    *,
    emails: bool = True,
    label: str | None = DEFAULT_LABEL,
    scrub: bool = True,
    keep_annotations: bool = False,
) -> RedactionReport:
    """Permanently remove terms and regex matches from a PDF.

    Matched text is deleted from the content stream (not just covered) and
    replaced by a black box containing ``label``. Image pixels under a box are
    blanked, matching bookmark titles are rewritten, and all metadata is
    removed (see :mod:`redact_pdf.metadata`).

    By default, annotations and form fields are first merged into the page
    ("baked"): their visible text becomes searchable and redactable, and their
    hidden data (comment text, authors, timestamps) disappears.

    Args:
        input_path: Source PDF. Never modified.
        output_path: Destination PDF. Defaults to ``<input>_REDACTED.pdf``.
        terms: Literal strings to remove (case-insensitive).
        patterns: Regular expressions to remove.
        emails: Also remove email addresses (on by default).
        label: Text shown in each box, or ``None`` for plain black boxes.
        scrub: Also remove links, attachments, scripts and other hidden content.
        keep_annotations: Keep annotations and form fields interactive instead
            of baking them. Their visible text is then not redacted.

    Returns:
        A :class:`RedactionReport`.

    Raises:
        ValueError: If there is nothing to match (no terms, no patterns, emails off).
        FileNotFoundError: If the input does not exist.
        RedactionError: If the input is unreadable, encrypted, or equals the output.
    """
    src = Path(input_path)
    dst = Path(output_path) if output_path is not None else default_output_path(src)
    term_list = _normalise_terms(terms)
    pattern_list = _build_patterns(patterns, emails=emails)
    if not term_list and not pattern_list:
        raise ValueError("Nothing to match: provide terms or patterns, or enable emails.")
    _check_paths(src, dst)

    report = RedactionReport(output=dst)
    with _open(src) as doc:
        if not keep_annotations:
            doc.bake(annots=True, widgets=True)

        for page in doc:
            page_no = page.number + 1
            for needle, quads in _find_hits(page, term_list, pattern_list).items():
                for quad in quads:
                    _add_box(page, quad, label)
                report.hits.setdefault(page_no, {})[needle] = len(quads)

            # Deletes text under the boxes and blanks overlapping image pixels.
            page.apply_redactions(images=REDACT_IMAGE_PIXELS)

            if n_images := len(page.get_images(full=True)):
                report.image_pages[page_no] = n_images

        report.bookmarks = _redact_outline(doc, term_list, pattern_list, label)
        # Serialise once to drop objects orphaned by baking and redaction, so the
        # metadata pass below only sees (and reports) what is really in the file.
        data = doc.tobytes(garbage=4, deflate=True)

    with pymupdf.open("pdf", data) as doc:
        if scrub:
            # Metadata is handled more thoroughly by strip_metadata().
            doc.scrub(metadata=False, xml_metadata=False)
        report.metadata = strip_metadata(doc)
        # Full rewrite (not incremental) so no earlier revision survives in the file.
        doc.save(dst, garbage=4, deflate=True, clean=True)

    logger.info("Wrote %s with %d redaction(s)", dst, report.total)
    report.leftovers = verify_pdf(dst, term_list, pattern_list, emails=False, label=label)
    if report.leftovers:
        logger.info("Terms still present in %s: %s", dst, report.leftovers)
    return report


def _redact_outline(
    doc: pymupdf.Document,
    terms: Sequence[str],
    patterns: Sequence[re.Pattern[str]],
    label: str | None,
) -> int:
    """Replace matches in bookmark titles. Returns the number of changed titles."""
    toc = doc.get_toc(simple=False)
    if not toc:
        return 0
    replacement = label or "***"
    matchers = list(patterns)
    if terms:
        matchers.insert(0, re.compile("|".join(map(re.escape, terms)), re.IGNORECASE))

    changed = 0
    for entry in toc:
        title = original = entry[1]
        for matcher in matchers:
            title = matcher.sub(replacement, title)
        if title != original:
            entry[1] = title
            changed += 1
    if changed:
        doc.set_toc(toc)
    return changed


def _add_box(page: pymupdf.Page, quad: pymupdf.Quad, label: str | None) -> None:
    fontsize = _label_fontsize(quad.rect, label) if label else 0.0
    use_label = label is not None and fontsize >= MIN_LABEL_FONTSIZE
    page.add_redact_annot(
        quad,
        text=label if use_label else None,
        fontname=LABEL_FONT,
        fontsize=max(fontsize, MIN_LABEL_FONTSIZE),
        align=pymupdf.TEXT_ALIGN_CENTER,
        fill=BLACK,
        text_color=WHITE,
        cross_out=False,
    )


def verify_pdf(
    path: str | Path,
    terms: Iterable[str] = (),
    patterns: Iterable[str | re.Pattern[str]] = (),
    *,
    emails: bool = True,
    label: str | None = DEFAULT_LABEL,
) -> dict[int, list[str]]:
    """Search a PDF's text layer for terms or patterns.

    Email addresses are checked too unless ``emails=False``. Matches that fall
    inside a redaction ``label`` (e.g. a term like "act"
    matching inside "REDACTED") are ignored. Text inside images is not checked.

    Returns:
        ``{page_number: [matched_text, ...]}``; empty if clean.
    """
    term_list = _normalise_terms(terms)
    pattern_list = _build_patterns(patterns, emails=emails)
    found: dict[int, list[str]] = {}
    with _open(Path(path)) as doc:
        for page in doc:
            label_rects = page.search_for(label) if label else []
            remaining = [
                needle
                for needle, quads in _find_hits(page, term_list, pattern_list).items()
                if any(not any(_mostly_inside(q.rect, lr) for lr in label_rects) for q in quads)
            ]
            if remaining:
                found[page.number + 1] = sorted(remaining)
    return found


def flatten_pdf(
    input_path: str | Path, output_path: str | Path | None = None, *, dpi: int = 200
) -> Path:
    """Render every page, including annotations, into an image-only PDF.

    Use this after a manual sweep with an annotation tool: drawn boxes are
    burned in and no text layer, hidden content or metadata survives. The
    result is not text-searchable and is larger than the input.

    Args:
        input_path: Source PDF. Never modified.
        output_path: Destination PDF. Defaults to ``<input>_FLATTENED.pdf``.
        dpi: Render resolution.

    Returns:
        The path of the written PDF.
    """
    if not 50 <= dpi <= 1200:
        raise ValueError("dpi must be between 50 and 1200.")
    src = Path(input_path)
    dst = (
        Path(output_path) if output_path is not None else default_output_path(src, FLATTENED_SUFFIX)
    )
    _check_paths(src, dst)

    with _open(src) as doc, pymupdf.open() as out:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi, annots=True)
            new_page = out.new_page(width=page.rect.width, height=page.rect.height)
            new_page.insert_image(new_page.rect, pixmap=pix)
        strip_metadata(out)
        out.save(dst, garbage=4, deflate=True)
    logger.info("Flattened %s -> %s at %d dpi", src, dst, dpi)
    return dst
