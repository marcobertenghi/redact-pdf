"""Remove pages completely.

Deleting a page from the page list is not enough: a bookmark, a named jump
target, the "open at" action or a form field can still point to the page
object, and then it stays in the file with all its content. Here the
page's comments and form fields are deleted first, every remaining
reference to it is cleared, and the file is rewritten without it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from redact_pdf.core import _check_paths, _open, clean_and_save, default_output_path
from redact_pdf.metadata import MetadataReport

__all__ = ["TRIMMED_SUFFIX", "PageRemovalReport", "parse_pages", "remove_pages"]

logger = logging.getLogger(__name__)

TRIMMED_SUFFIX = "_TRIMMED"

# A destination array whose page was cleared, with the name, string or key
# it is stored under: "/chapter.3 [null /Fit]", "(intro) [null /XYZ 0 0 0]".
_DEAD_DESTINATION = re.compile(
    r"(?:/[^\s/\[\]<>()]+|\((?:[^()\\]|\\.)*\)|<[0-9A-Fa-f\s]*>)\s*\[\s*null\b[^\]]*\]"
)


@dataclass
class PageRemovalReport:
    """Summary of a :func:`remove_pages` run.

    Attributes:
        output: Path of the written PDF.
        removed: The removed pages, numbered as in the input (1-based).
        pages_left: Number of pages in the output.
        bookmarks: Number of bookmarks to removed pages that were dropped.
        metadata: What was removed from the file.
        leftovers: Page objects still in the file but not in its page list.
            Should be 0.
    """

    output: Path
    removed: list[int] = field(default_factory=list)
    pages_left: int = 0
    bookmarks: int = 0
    metadata: MetadataReport = field(default_factory=MetadataReport)
    leftovers: int = 0

    @property
    def is_clean(self) -> bool:
        """True if nothing of the removed pages is left in the file."""
        return self.leftovers == 0


def parse_pages(spec: str | Iterable[int], page_count: int) -> list[int]:
    """Turn ``"1,3,92-100"`` or ``"92-"`` (to the end) into sorted page numbers.

    Pages are numbered from 1. Raises ValueError for malformed input or
    pages outside the document.
    """
    if not isinstance(spec, str):
        numbers = set(spec)
    else:
        numbers = set()
        for part in spec.replace(" ", "").split(","):
            match = re.fullmatch(r"(\d+)(?:(-)(\d*))?", part)
            if not match:
                raise ValueError(f"Invalid page range {part!r}; use e.g. 5, 92-100 or 92-")
            first = int(match.group(1))
            last = (
                int(match.group(3)) if match.group(3) else (page_count if match.group(2) else first)
            )
            if last < first:
                raise ValueError(f"Invalid page range {part!r}: {last} comes before {first}")
            numbers.update(range(first, last + 1))
    if not numbers:
        raise ValueError("No pages given")
    outside = sorted(n for n in numbers if not 1 <= n <= page_count)
    if outside:
        raise ValueError(f"Page {outside[0]} does not exist; the document has {page_count} pages")
    return sorted(numbers)


def remove_pages(
    input_path: str | Path,
    pages: str | Iterable[int],
    output_path: str | Path | None = None,
    *,
    scrub: bool = True,
) -> PageRemovalReport:
    """Remove pages completely; the remaining pages follow on directly.

    Everything only the removed pages use (text, images, fonts, comments,
    form fields) leaves the file, as do bookmarks and jump targets pointing
    to them. Page labels and the numbers printed on the pages are left as
    they are. Metadata and hidden content are removed as in
    :func:`redact_pdf.redact_pdf`.

    Args:
        input_path: Source PDF. Never modified.
        pages: Pages to remove, numbered from 1: ``"92-100"``, ``"1,3,92-"``
            or an iterable of numbers.
        output_path: Destination PDF. Defaults to ``<input>_TRIMMED.pdf``.
        scrub: Also remove links, attachments, scripts and other hidden content.

    Returns:
        A :class:`PageRemovalReport`. Check :attr:`PageRemovalReport.is_clean`.

    Raises:
        ValueError: For invalid page numbers, or when every page would go.
        FileNotFoundError: If the input does not exist.
        RedactionError: If the input is unreadable, encrypted, or equals the output.
    """
    src = Path(input_path)
    dst = Path(output_path) if output_path is not None else default_output_path(src, TRIMMED_SUFFIX)
    _check_paths(src, dst)

    report = PageRemovalReport(output=dst)
    with _open(src) as doc:
        report.removed = parse_pages(pages, doc.page_count)
        if len(report.removed) == doc.page_count:
            raise ValueError("Cannot remove every page")
        indexes = [n - 1 for n in report.removed]
        gone = {doc[i].xref for i in indexes}

        for i in indexes:
            _delete_annotations(doc[i])
        toc, report.bookmarks = _toc_without(doc.get_toc(simple=False), report.removed)
        doc.delete_pages(indexes)
        doc.set_toc(toc)
        _clear_references(doc, gone)
        report.pages_left = doc.page_count
        data = doc.tobytes(garbage=4, deflate=True)

    report.metadata, _ = clean_and_save(data, dst, scrub=scrub)
    report.leftovers = _orphaned_pages(dst)
    logger.info("Wrote %s without %d page(s)", dst, len(report.removed))
    return report


def _delete_annotations(page: pymupdf.Page) -> None:
    """Comments and form fields, so their values don't outlive the page."""
    for widget in list(page.widgets()):
        page.delete_widget(widget)
    for annot in list(page.annots()):
        page.delete_annot(annot)


def _toc_without(toc: list[list[object]], removed: list[int]) -> tuple[list[list[object]], int]:
    """Bookmarks without those pointing to removed pages, renumbered.

    Their titles would otherwise stay in the file. Children of a dropped
    bookmark that point elsewhere move up a level.
    """
    gone = set(removed)
    kept: list[list[object]] = []
    ancestors: list[tuple[int, bool]] = []  # (level, dropped) of the open parents
    dropped = 0
    for level, title, page, *rest in toc:
        assert isinstance(level, int) and isinstance(page, int)
        while ancestors and ancestors[-1][0] >= level:
            ancestors.pop()
        is_gone = page in gone
        ancestors.append((level, is_gone))
        if is_gone:
            dropped += 1
            continue
        level -= sum(1 for _, gone_parent in ancestors[:-1] if gone_parent)
        if page > 0:
            page -= sum(1 for n in removed if n < page)
        previous = kept[-1][0] if kept else 0
        assert isinstance(previous, int)
        kept.append([max(1, min(level, previous + 1)), title, page, *rest])
    return kept, dropped


def _clear_references(doc: pymupdf.Document, gone: set[int]) -> None:
    """Point every remaining reference to a removed page at nothing, and
    drop jump targets that led there."""
    refs = re.compile(r"\b(?:" + "|".join(map(str, sorted(gone))) + r") 0 R\b")
    for xref in range(1, doc.xref_length()):
        if xref in gone:
            continue
        try:
            source = doc.xref_object(xref, compressed=True)
        except RuntimeError:  # free or broken entry
            continue
        if not refs.search(source):
            continue
        cleared = _DEAD_DESTINATION.sub("", refs.sub("null", source))
        doc.update_object(xref, cleared)


def _orphaned_pages(path: Path) -> int:
    """Page objects in the file that are not in its page list."""
    with pymupdf.open(path) as doc:
        listed = {page.xref for page in doc}
        return sum(
            1
            for xref in range(1, doc.xref_length())
            if doc.xref_get_key(xref, "Type")[1] == "/Page" and xref not in listed
        )
