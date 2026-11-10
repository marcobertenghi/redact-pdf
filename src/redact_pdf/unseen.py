"""Text that a reader cannot see: invisible, or outside the visible page.

Both kinds stay in the file and come out with a text extraction tool:

* Invisible text is drawn without ink (text render mode 3, as in the OCR
  layer of a scan) or fully transparent.
* Text outside the visible page lies beyond the crop box. Cropping a page
  in a PDF editor only narrows that box and leaves the content where it
  was; presentation and layout programs export what was parked next to
  the page.

Nobody can review such text, so :func:`remove_unseen_text` deletes it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from typing import Any

import pymupdf

from redact_pdf.matching import EVERYWHERE, TEXT_FLAGS

__all__ = ["INVISIBLE_TEXT", "OUTSIDE_TEXT", "remove_unseen_text", "visible_area"]

INVISIBLE_TEXT = "pages with invisible text"
OUTSIDE_TEXT = "pages with text outside the visible page"

# Set on text that is filled or stroked; neither means render mode 3 (invisible).
_INKED: int = pymupdf.mupdf.FZ_STEXT_FILLED | pymupdf.mupdf.FZ_STEXT_STROKED
_OCR_FONT = "GlyphLessFont"  # Tesseract's font without glyphs


def visible_area(page: pymupdf.Page) -> pymupdf.Rect:
    """The area a viewer shows, in the unrotated coordinates of extracted text."""
    area = pymupdf.Rect(page.rect * page.derotation_matrix)
    area.normalize()
    return area


def remove_unseen_text(doc: pymupdf.Document) -> Counter[str]:
    """Delete invisible text and text outside the visible page.

    Drawings and images are left alone. A line that runs off the page keeps
    the characters that are at least partly visible. Returns the number of
    pages each kind was found on, keyed by :data:`INVISIBLE_TEXT` and
    :data:`OUTSIDE_TEXT`.
    """
    found: Counter[str] = Counter()
    for page in doc.pages():
        invisible, outside = _unseen_text(page)
        if not invisible and not outside:
            continue
        if invisible:
            found[INVISIBLE_TEXT] += 1
        if outside:
            found[OUTSIDE_TEXT] += 1
        for rect in invisible + outside:
            # fill=False: no box is drawn (PyMuPDF's stub only lists colours).
            page.add_redact_annot(rect, fill=False, cross_out=False)  # type: ignore[arg-type]
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )
    return found


def _spans(page: pymupdf.Page, detail: str) -> Iterator[dict[str, Any]]:
    """Text spans of the whole page, including what lies outside it."""
    for block in page.get_text(detail, clip=EVERYWHERE, flags=TEXT_FLAGS)["blocks"]:
        for line in block.get("lines", ()):
            yield from line["spans"]


def _is_invisible(span: dict[str, Any]) -> bool:
    return bool(
        span["alpha"] == 0
        or not span["char_flags"] & _INKED
        or span["font"].rpartition("+")[2] == _OCR_FONT
    )


def _unseen_text(page: pymupdf.Page) -> tuple[list[pymupdf.Rect], list[pymupdf.Rect]]:
    """Areas of invisible text, and of text outside the visible page."""
    visible = visible_area(page)
    left, top, right, bottom = visible.x0, visible.y0, visible.x1, visible.y1
    invisible: list[pymupdf.Rect] = []
    outside: list[pymupdf.Rect] = []
    runs_off_page = False
    # Plain numbers: a page has hundreds of spans, and Rect objects are slow.
    for span in _spans(page, "dict"):
        x0, y0, x1, y1 = span["bbox"]
        if x1 <= x0 or y1 <= y0:
            continue
        if x1 <= left or x0 >= right or y1 <= top or y0 >= bottom:
            outside.append(pymupdf.Rect(x0, y0, x1, y1))
        elif _is_invisible(span):
            invisible.append(pymupdf.Rect(x0, y0, x1, y1))
        elif x0 < left or y0 < top or x1 > right or y1 > bottom:
            runs_off_page = True
    if runs_off_page:
        # Rare, so characters are only looked at one by one here.
        for span in _spans(page, "rawdict"):
            rect = pymupdf.Rect(span["bbox"])
            if rect.intersects(visible) and not visible.contains(rect):
                outside += _runs_outside(span["chars"], visible)
    return invisible, outside


def _runs_outside(chars: list[dict[str, Any]], visible: pymupdf.Rect) -> list[pymupdf.Rect]:
    """One rectangle per run of consecutive characters entirely outside ``visible``."""
    runs: list[pymupdf.Rect] = []
    in_run = False
    for char in chars:
        box = pymupdf.Rect(char["bbox"])
        if box.is_empty or char["c"].isspace():
            continue
        if box.intersects(visible):
            in_run = False
        elif in_run:
            runs[-1] |= box
        else:
            runs.append(box)
            in_run = True
    return runs
