"""Core redaction, verification and flattening logic.

All functions operate on file paths and never modify the input file.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from redact_pdf.images import HIDDEN_IMAGE, blank_hidden_image_areas, hidden_image_pages
from redact_pdf.matching import (
    EMAIL_PATTERN,
    Matcher,
    PageText,
    build_matchers,
    email_domain_term,
    find_in_text,
    redact_text,
    select,
    term_matcher,
)
from redact_pdf.metadata import MetadataReport, strip_metadata
from redact_pdf.names import email_name_terms
from redact_pdf.pdfstrings import (
    iter_object_strings,
    redact_object_strings,
    strip_marked_content_text,
)

__all__ = [
    "DEFAULT_LABEL",
    "DOCUMENT_LEVEL",
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
DOCUMENT_LEVEL = 0
"""Page number used in leftovers for text found outside the pages' text."""

# Blank only the image pixels under a redaction box. Defined dynamically by PyMuPDF,
# so it is invisible to type checkers; 2 is its documented value.
REDACT_IMAGE_PIXELS: int = getattr(pymupdf, "PDF_REDACT_IMAGE_PIXELS", 2)

BLACK = (0.0, 0.0, 0.0)
WHITE = (1.0, 1.0, 1.0)


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
            the output. Page :data:`DOCUMENT_LEVEL` (0) collects matches outside
            the page text, e.g. in layer names. Should be empty.
        bookmarks: Number of bookmark (outline) titles that were redacted.
        hidden_text: ``{matched_text: count}`` redacted in strings outside the
            pages: layer names, page labels, link targets, form values, comments.
        domain_terms: ``{term: email}``, organisation names taken from the
            domains of email addresses found in the document, and redacted
            as whole words.
        name_terms: ``{email: [name, ...]}``, person names taken from the
            email addresses found in the document (see :mod:`redact_pdf.names`).
        metadata: What was removed by :func:`redact_pdf.metadata.strip_metadata`,
            plus alternate text in marked content.
        hidden_images: Number of images whose areas that no page shows
            (cropped off, or off the page) were blanked.
    """

    output: Path
    hits: dict[int, dict[str, int]] = field(default_factory=dict)
    image_pages: dict[int, int] = field(default_factory=dict)
    leftovers: dict[int, list[str]] = field(default_factory=dict)
    bookmarks: int = 0
    hidden_text: dict[str, int] = field(default_factory=dict)
    domain_terms: dict[str, str] = field(default_factory=dict)
    name_terms: dict[str, list[str]] = field(default_factory=dict)
    metadata: MetadataReport = field(default_factory=MetadataReport)
    hidden_images: int = 0

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


def _label_fontsize(rect: pymupdf.Rect, label: str) -> float:
    """Largest font size at which ``label`` fits inside ``rect``."""
    width_at_1pt = pymupdf.get_text_length(label, fontname=LABEL_FONT, fontsize=1)
    return float(min(rect.height * 0.7, rect.width * 0.95 / width_at_1pt))


def redact_pdf(
    input_path: str | Path,
    output_path: str | Path | None = None,
    terms: Iterable[str] = (),
    patterns: Iterable[str | re.Pattern[str]] = (),
    *,
    emails: bool = True,
    email_domains: bool = True,
    email_names: bool = True,
    label: str | None = DEFAULT_LABEL,
    scrub: bool = True,
    keep_annotations: bool = False,
) -> RedactionReport:
    """Permanently remove terms and regex matches from a PDF.

    Matched text is deleted from the content stream (not just covered) and
    replaced by a black box containing ``label``. Image pixels under a box are
    blanked, as are image areas that no page shows (cropped off, or off the
    page; see :mod:`redact_pdf.images`). Matches are also replaced in
    bookmark titles and other text strings outside the pages (layer names,
    page labels, link targets).
    Alternate text that would reveal redacted words and all metadata are
    removed (see :mod:`redact_pdf.metadata`).

    By default, annotations and form fields are first merged into the page
    ("baked"): their visible text becomes searchable and redactable, and their
    hidden data (comment text, authors, timestamps) disappears.

    Args:
        input_path: Source PDF. Never modified.
        output_path: Destination PDF. Defaults to ``<input>_REDACTED.pdf``.
        terms: Literal strings to remove as whole words (case-insensitive).
        patterns: Regular expressions to remove, applied as written (use
            ``(?i)`` for case-insensitive matching).
        emails: Also remove email addresses (on by default).
        email_domains: With ``emails``, also remove the organisation name in
            each address's domain as a whole word, everywhere in the document
            (``john@acme.com`` -> "acme"). Mail providers such as gmail.com are
            skipped. See :func:`redact_pdf.matching.email_domain_term`.
        email_names: With ``emails``, also remove person names belonging to
            each address, as whole words everywhere in the document: the
            display name in ``Lukas Körnich <lk@acme.ch>`` and the parts of
            the address itself (``lukas.koernich@`` -> "lukas", "koernich",
            matched accent-insensitively). Best effort; see :mod:`redact_pdf.names`.
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
    matchers = build_matchers(terms, patterns, emails=emails)
    if not matchers:
        raise ValueError("Nothing to match: provide terms or patterns, or enable emails.")
    _check_paths(src, dst)

    report = RedactionReport(output=dst)
    with _open(src) as doc:
        if not keep_annotations:
            doc.bake(annots=True, widgets=True)

        if emails and (email_domains or email_names):
            # First pass over the whole document, so a name or domain seen on
            # the last page is also redacted on the first.
            doc_text = _document_text(doc)
            if email_names:
                names = email_name_terms(doc_text)
                report.name_terms = names.by_address
                matchers += names.matchers
            if email_domains:
                report.domain_terms = _domain_terms(doc_text)
                matchers += [term_matcher(t, whole_word_only=True) for t in report.domain_terms]

        for page in doc:
            page_no = page.number + 1
            text = PageText(page)
            counts, covered = select(text.find(matchers))
            for rect in text.rects(covered):
                _add_box(page, rect, label)
            if counts:
                report.hits[page_no] = dict(counts)

            # Deletes text under the boxes and blanks overlapping image pixels.
            page.apply_redactions(images=REDACT_IMAGE_PIXELS)

            if n_images := len(page.get_images(full=True)):
                report.image_pages[page_no] = n_images

        report.bookmarks = _redact_outline(doc, matchers, label)
        # After redaction: a box blanks pixels only in that page's copy of an image.
        report.hidden_images = blank_hidden_image_areas(doc)
        # Serialise once to drop objects orphaned by baking and redaction, so the
        # passes below only see (and report) what is really in the file.
        data = doc.tobytes(garbage=4, deflate=True)

    replacement = label or "***"
    report.metadata, hidden = clean_and_save(
        data, dst, scrub=scrub, strings=lambda s: redact_text(s, matchers, replacement)
    )
    report.hidden_text = dict(hidden)

    logger.info("Wrote %s with %d redaction(s)", dst, report.total)
    report.leftovers = _verify(dst, matchers, label)
    if report.leftovers:
        logger.info("Terms still present in %s: %s", dst, report.leftovers)
    return report


def _document_text(doc: pymupdf.Document) -> str:
    """The text of all pages in order, then bookmark titles.

    Pages follow each other directly, so a name at the bottom of one page
    stays next to an address at the top of the next. Hyphen-joined variants
    come after all pages, so they don't come between two pages.
    """
    variants = [PageText(page).variants for page in doc.pages()]
    texts = [v[0][0] for v in variants]
    texts += [text for v in variants for text, _ in v[1:]]
    texts += [entry[1] for entry in doc.get_toc()]
    return "\n".join(texts)


def _domain_terms(text: str) -> dict[str, str]:
    """``{organisation name: first email it came from}``."""
    terms: dict[str, str] = {}
    for m in EMAIL_PATTERN.finditer(text):
        if (term := email_domain_term(m.group(0))) is not None:
            terms.setdefault(term, m.group(0))
    return terms


def clean_and_save(
    data: bytes,
    dst: Path,
    *,
    scrub: bool,
    strings: Callable[[str], tuple[str, Counter[str]]] | None = None,
) -> tuple[MetadataReport, Counter[str]]:
    """Remove hidden content and metadata from a serialised PDF and save it.

    Runs ``scrub`` (links, attachments, scripts...), :func:`strip_metadata`
    and removes alternate text in page content. ``strings``, if given,
    rewrites text strings outside the pages. The file is fully rewritten, so
    no earlier revision survives. Returns what was removed and the string
    matches.
    """
    with pymupdf.open("pdf", data) as doc:
        if scrub:
            # Metadata is handled more thoroughly by strip_metadata().
            doc.scrub(metadata=False, xml_metadata=False)
        metadata = strip_metadata(doc)
        if n := strip_marked_content_text(doc):
            metadata.removed["alternate text in page content"] += n
        # Drop what scrubbing and the tag removal orphaned, so only strings
        # that end up in the output are rewritten and reported.
        data = doc.tobytes(garbage=4, deflate=True)

    hidden: Counter[str] = Counter()
    with pymupdf.open("pdf", data) as doc:
        if strings is not None:
            hidden = redact_object_strings(doc, strings)
        doc.save(dst, garbage=4, deflate=True, clean=True)
    return metadata, hidden


def _redact_outline(doc: pymupdf.Document, matchers: Sequence[Matcher], label: str | None) -> int:
    """Replace matches in bookmark titles. Returns the number of changed titles."""
    toc = doc.get_toc(simple=False)
    replacement = label or "***"
    changed = 0
    for entry in toc:
        title, counts = redact_text(entry[1], matchers, replacement)
        if counts:
            entry[1] = title
            changed += 1
    if changed:
        doc.set_toc(toc)
    return changed


def _add_box(page: pymupdf.Page, rect: pymupdf.Rect, label: str | None) -> None:
    fontsize = _label_fontsize(rect, label) if label else 0.0
    use_label = label is not None and fontsize >= MIN_LABEL_FONTSIZE
    page.add_redact_annot(
        rect,
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
    """Search a PDF for terms or patterns.

    Checks the text of every page, and every text string and XMP packet
    outside the pages (reported under page :data:`DOCUMENT_LEVEL`, 0).
    Email addresses are checked too unless ``emails=False``. Matches inside a
    redaction ``label`` (e.g. a term like "act" inside "REDACTED") are
    ignored. Text inside images is not checked, but images with content in
    areas no page shows are reported as :data:`HIDDEN_IMAGE` on their pages.

    Returns:
        ``{page_number: [matched_text, ...]}``; empty if clean.
    """
    return _verify(Path(path), build_matchers(terms, patterns, emails=emails), label)


def _verify(path: Path, matchers: Sequence[Matcher], label: str | None) -> dict[int, list[str]]:
    found: dict[int, list[str]] = {}
    with _open(path) as doc:
        for page in doc:
            hits = PageText(page).find(matchers, protect=label)
            if hits:
                found[page.number + 1] = sorted({name for name, _, _ in hits})
        for page_no in hidden_image_pages(doc):
            found.setdefault(page_no, []).append(HIDDEN_IMAGE)

        outside = {
            name
            for _, text in _document_level_text(doc)
            for name, _, _ in find_in_text(text, matchers, protect=label or "***")
        }
        if outside:
            found[DOCUMENT_LEVEL] = sorted(outside)
    return found


def _document_level_text(doc: pymupdf.Document) -> Iterable[tuple[int, str]]:
    """Text strings in all objects, plus XMP metadata packets."""
    yield from iter_object_strings(doc)
    for xref in range(1, doc.xref_length()):
        if doc.xref_get_key(xref, "Type") == ("name", "/Metadata") and doc.xref_is_stream(xref):
            yield xref, (doc.xref_stream(xref) or b"").decode("utf-8", "replace")


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
