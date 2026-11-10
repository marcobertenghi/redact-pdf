"""Turn boxes drawn during manual review into real redactions.

Drawing a box in an annotator or in Xournal++ only covers content: the text,
image pixels and drawings underneath stay in the file. :func:`apply_boxes`
finds those boxes and deletes what is under them, like the ``redact``
command does for matched text. Pages are not rasterised, so text elsewhere
stays text and the file stays small.

Two kinds of boxes are recognised:

* **Shapes drawn into the page** (Xournal++ exports boxes this way): opaque
  filled shapes of any colour (black boxes, white "whiteout" boxes) that
  cover text or images drawn *before* them. That content is invisible
  anyway, so deleting it does not change how the page looks; a shape that is
  not a rectangle is applied as its bounding rectangle. Filled design
  elements (a black header bar with white text on it) cover nothing and are
  left alone.
* **Annotations** added in a PDF viewer: rectangles, circles, polygons,
  freehand ink, highlights, strike-outs and redaction marks. Comments
  (sticky notes, text boxes), stamps and links are not boxes; they are
  merged into the page like in ``redact``.

Shapes that cover text but do not hide it (see-through fills, highlighter,
thick pen strokes, non-rectangular shapes) are reported, because they look
like boxes but would leave the text readable.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf

from redact_pdf.document import (
    REDACT_IMAGE_PIXELS,
    check_output_path,
    clean_and_save,
    default_output_path,
    open_pdf,
)
from redact_pdf.images import blank_hidden_image_areas, hidden_image_pages
from redact_pdf.metadata import MetadataReport

__all__ = ["APPLIED_SUFFIX", "BoxReport", "apply_boxes"]

logger = logging.getLogger(__name__)

APPLIED_SUFFIX = "_FINAL"

_OPAQUE = 0.99  # fill opacity from which a shape hides what is under it
_MIN_SIDE = 3.0  # pt; thinner rectangles are rules and borders, not boxes
_MIN_OVERLAP = 1.0  # pt²; less is just touching
_MARKER_WIDTH = 4.0  # pt; pen strokes at least this wide look like scribbles
_MOSTLY_INSIDE = 0.8  # share of a drawing's area under a box to delete it whole
_WHITE = 0.95  # colour components above this count as white (page background)
_INVISIBLE_TEXT = 3  # PDF text render mode "neither fill nor stroke"
_IMAGE_KINDS = ("fill-image", "fill-imgmask")

# Annotation types that mark an area to remove. Text markup marks exact
# text lines (its quads), pen marks the area around each stroke, the
# others their rectangle.
_TEXT_MARKUP = frozenset({"Highlight", "Underline", "StrikeOut", "Squiggly"})
_PEN_MARKS = frozenset({"Ink", "PolyLine", "Line", "Polygon"})
_AREA_MARKS = frozenset({"Square", "Circle", "Polygon", "PolyLine", "Line", "Ink", "Redact"})


@dataclass
class BoxReport:
    """Summary of an :func:`apply_boxes` run.

    Attributes:
        output: Path of the written PDF.
        drawn: ``{page_number: count}`` of boxes drawn into the page content.
        annotations: ``{page_number: count}`` of annotations applied as boxes.
        ignored: ``{page_number: count}`` of shapes that lie over text or
            images but do not hide them (see-through, highlighter, pen
            strokes, not rectangular). Not applied; check these pages.
        leftovers: ``{page_number: count}`` of boxes with text still under
            them after applying. Should be empty.
        metadata: What was removed from the file.
        hidden_images: Number of images whose areas that no page shows
            (cropped off, or off the page) were blanked.
        image_leftovers: ``{page_number: count}`` of images that still have
            content in such areas after applying. Should be empty.
    """

    output: Path
    drawn: dict[int, int] = field(default_factory=dict)
    annotations: dict[int, int] = field(default_factory=dict)
    ignored: dict[int, int] = field(default_factory=dict)
    leftovers: dict[int, int] = field(default_factory=dict)
    metadata: MetadataReport = field(default_factory=MetadataReport)
    hidden_images: int = 0
    image_leftovers: dict[int, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        """Number of boxes applied."""
        return sum(self.drawn.values()) + sum(self.annotations.values())

    @property
    def is_clean(self) -> bool:
        """True if boxes were applied, nothing remains under them, no
        box-like shape was ignored and no image keeps hidden content."""
        return (
            self.total > 0 and not self.leftovers and not self.ignored and not self.image_leftovers
        )


@dataclass(frozen=True)
class _Box:
    rect: pymupdf.Rect
    fill: tuple[float, ...]


@dataclass(frozen=True)
class _Item:
    """Something painted on the page, in painting order."""

    seqno: int
    rect: pymupdf.Rect
    kind: str  # "text", "image" or "path"
    visible: bool
    colors: tuple[tuple[float, ...], ...] = ()


def apply_boxes(
    input_path: str | Path,
    output_path: str | Path | None = None,
    *,
    scrub: bool = True,
    annotations_only: bool = False,
) -> BoxReport:
    """Delete the content under boxes drawn during manual review.

    Text, image pixels and drawings covered by a box are removed from the
    file and the box stays as a solid area. Image areas that no page shows
    are blanked too: a screenshot split across two pages is stored whole on
    both, and a box only blanks the copy on its own page. Remaining
    annotations and form fields are merged into the page, and hidden content
    and metadata are removed as in :func:`redact_pdf.redact_pdf`.

    Args:
        input_path: The reviewed PDF. Never modified.
        output_path: Destination PDF. Defaults to ``<input>_FINAL.pdf``.
        scrub: Also remove links, attachments, scripts and other hidden content.
        annotations_only: Only apply annotation marks, such as Firefox highlights
            and ink strokes. Skip detection of boxes drawn into page content,
            including boxes left by earlier automatic redactions. Each ink
            stroke still becomes its own box. Cleanup and verification still run.

    Returns:
        A :class:`BoxReport`. Check :attr:`BoxReport.is_clean`.
    """
    src = Path(input_path)
    dst = Path(output_path) if output_path is not None else default_output_path(src, APPLIED_SUFFIX)
    check_output_path(src, dst)

    report = BoxReport(output=dst)
    hidden: dict[int, list[set[_Char]]] = {}  # per page, per box: characters to remove
    with open_pdf(src) as doc:
        # Annotations first: their appearance would otherwise also be seen as
        # drawings. The rest (comments, form fields) become page content, so
        # an opaque text box over text is then found like a drawn box.
        marked = {page.number + 1: _annotation_boxes(page) for page in doc}
        doc.bake(annots=True, widgets=True)

        for page in doc:
            page_no = page.number + 1
            drawn, ignored = ([], 0) if annotations_only else _drawn_boxes(page)
            boxes = drawn + marked[page_no]
            if ignored:
                report.ignored[page_no] = ignored
            if not boxes:
                continue
            chars = _chars(page)
            hidden[page_no] = [_hidden_chars(chars, box.rect) for box in boxes]
            _redact(page, boxes)
            if drawn:
                report.drawn[page_no] = len(drawn)
            if marked[page_no]:
                report.annotations[page_no] = len(marked[page_no])

        report.hidden_images = blank_hidden_image_areas(doc)
        data = doc.tobytes(garbage=4, deflate=True)

    report.metadata, _ = clean_and_save(data, dst, scrub=scrub)
    report.leftovers = _check(dst, hidden)
    with pymupdf.open(dst) as out:
        report.image_leftovers = hidden_image_pages(out)
    logger.info("Wrote %s with %d box(es) applied", dst, report.total)
    return report


def _redact(page: pymupdf.Page, boxes: Sequence[_Box]) -> None:
    """Apply ``boxes`` as redactions, in up to two passes.

    MuPDF either deletes drawings a box covers completely, or every drawing
    it touches. "Covered" includes the corners of thick pen strokes, so a
    handwritten signature under a box would survive. Boxes whose touching
    drawings all lie mostly under them (a signature, the box's own outline)
    therefore delete what they touch; boxes crossed by a longer drawing (a
    table border) only delete what they cover, so the border stays.
    """
    # Plain numbers: every box meets every drawing, where Rect objects are slow.
    painted = [
        (r.x0, r.y0, r.x1, r.y1, r.get_area())
        for r in map(_painted_rect, page.get_drawings())
        if not r.is_empty
    ]
    passes: dict[int, list[_Box]] = {}
    for box in boxes:
        bx0, by0, bx1, by1 = box.rect.x0, box.rect.y0, box.rect.x1, box.rect.y1
        contained = True
        for x0, y0, x1, y1, area in painted:
            width = min(x1, bx1) - max(x0, bx0)
            height = min(y1, by1) - max(y0, by0)
            if width > 0 and height > 0 and width * height < _MOSTLY_INSIDE * area:
                contained = False
                break
        mode = (
            pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED
            if contained
            else pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED
        )
        passes.setdefault(mode, []).append(box)
    for mode, group in sorted(passes.items(), reverse=True):
        for box in group:
            page.add_redact_annot(box.rect, fill=box.fill, cross_out=False)
        page.apply_redactions(
            images=REDACT_IMAGE_PIXELS, graphics=mode, text=pymupdf.PDF_REDACT_TEXT_REMOVE
        )


def _painted_items(page: pymupdf.Page) -> tuple[list[_Item], list[dict[str, Any]]]:
    """Text, images and drawings of a page, and the raw drawings."""
    items: list[_Item] = []
    for span in page.get_texttrace():
        visible = span["type"] != _INVISIBLE_TEXT and span["opacity"] > 0
        items.append(
            _Item(span["seqno"], pymupdf.Rect(span["bbox"]), "text", visible, (span["color"],))
        )
    for seqno, (kind, rect) in enumerate(page.get_bboxlog()):
        if kind in _IMAGE_KINDS:
            items.append(_Item(seqno, pymupdf.Rect(rect), "image", True))
    drawings = page.get_drawings()
    for d in drawings:
        colors = tuple(c for c in (d.get("fill"), d.get("color")) if c is not None)
        # White shapes (page backgrounds) are not content worth protecting.
        visible = any(not _is_white(c) for c in colors)
        items.append(_Item(d["seqno"], _painted_rect(d), "path", visible, colors))
    return items, drawings


def _painted_rect(drawing: dict[str, Any]) -> pymupdf.Rect:
    """The area a path paints: a stroke is as wide as its pen, so a straight
    highlighter line covers text although its geometry has no height."""
    rect = pymupdf.Rect(drawing["rect"])
    if drawing.get("color") is not None and drawing.get("width"):
        half = drawing["width"] / 2
        rect = pymupdf.Rect(rect.x0 - half, rect.y0 - half, rect.x1 + half, rect.y1 + half)
    return rect


def _overlaps(a: pymupdf.Rect, b: pymupdf.Rect) -> bool:
    # Plain arithmetic: called for every pair of drawings, where Rect objects are slow.
    width = min(a.x1, b.x1) - max(a.x0, b.x0)
    height = min(a.y1, b.y1) - max(a.y0, b.y0)
    return width > 0 and height > 0 and width * height > _MIN_OVERLAP


def _is_white(color: Sequence[float]) -> bool:
    return all(c > _WHITE for c in color)


def _is_rectangle(drawing: dict[str, Any]) -> bool:
    """A path that is one axis-aligned rectangle."""
    ops = drawing["items"]
    if len(ops) == 1 and ops[0][0] == "re":
        return True
    if len(ops) == 1 and ops[0][0] == "qu":
        quad = ops[0][1]
        return bool(quad.is_rectangular) and quad.rect.get_area() > 0 and _axis_aligned(quad)
    if 3 <= len(ops) <= 4 and all(op[0] == "l" for op in ops):
        rect = pymupdf.Rect(drawing["rect"])
        corners = {(round(p.x, 1), round(p.y, 1)) for op in ops for p in op[1:]}
        expected = {
            (round(x, 1), round(y, 1)) for x in (rect.x0, rect.x1) for y in (rect.y0, rect.y1)
        }
        return corners == expected
    return False


def _axis_aligned(quad: pymupdf.Quad) -> bool:
    return bool(abs(quad.ul.y - quad.ur.y) < 0.1 and abs(quad.ul.x - quad.ll.x) < 0.1)


def _drawn_boxes(page: pymupdf.Page) -> tuple[list[_Box], int]:
    """Opaque shapes covering earlier content, and the number of box-like
    shapes that lie over text or images without hiding them."""
    items, drawings = _painted_items(page)
    boxes: list[_Box] = []
    ignored = 0
    for d in drawings:
        fill = d.get("fill")
        if fill is None and (d.get("width") or 0.0) < _MARKER_WIDTH:
            continue  # thin lines (rules, borders, underlines) are never boxes
        rect = pymupdf.Rect(d["rect"])  # the filled area, without the outline
        seqno = d["seqno"]
        painted = _painted_rect(d)
        below = [i for i in items if i.seqno < seqno and _overlaps(i.rect, painted)]
        hides_content = any(i.kind in ("text", "image") for i in below)
        if not hides_content and not any(i.kind == "path" and i.visible for i in below):
            continue

        opacity = d.get("fill_opacity")
        opaque = fill is not None and (opacity is None or opacity >= _OPAQUE)
        if fill is not None and opaque and min(rect.width, rect.height) >= _MIN_SIDE:
            if hides_content:
                # Any opaque shape hiding text or images: its bounding box.
                boxes.append(_Box(rect, tuple(fill)))
            elif _is_rectangle(d):
                # Hides only drawings: apply when that changes nothing visible.
                above = [i for i in items if i.seqno > seqno and _overlaps(i.rect, rect)]
                if not any(_shows_on(i, fill) for i in above):
                    boxes.append(_Box(rect, tuple(fill)))
        elif hides_content and _looks_like_a_box(d, rect):
            ignored += 1
    return boxes, ignored


def _shows_on(item: _Item, fill: Sequence[float]) -> bool:
    """Would ``item``, painted on top of a box with ``fill``, be visible?"""
    if not item.visible:
        return False
    if item.kind == "image":
        return True
    return any(tuple(c) != tuple(fill) for c in item.colors)


def _looks_like_a_box(drawing: dict[str, Any], rect: pymupdf.Rect) -> bool:
    """A shape drawn over text that someone may have meant to hide it:
    a filled area (see-through or not rectangular) or a thick pen stroke."""
    if drawing.get("fill") is not None:
        return min(rect.width, rect.height) >= _MIN_SIDE and not _is_white(drawing["fill"])
    color = drawing.get("color")
    width = drawing.get("width") or 0.0
    return color is not None and width >= _MARKER_WIDTH and not _is_white(color)


def _annotation_rects(annot: pymupdf.Annot) -> list[pymupdf.Rect]:
    """The areas a supported mark removes, without modifying the annotation.

    Text markup follows its line quads, ink follows individual pen strokes,
    and area marks use their rectangle. Other annotations are not marks.
    """
    kind = annot.type[1]
    if kind in _TEXT_MARKUP:
        vertices = annot.vertices or []
        quads = [pymupdf.Quad(*vertices[i : i + 4]) for i in range(0, len(vertices) - 3, 4)]
        return [q.rect for q in quads] or [annot.rect]
    if kind in _PEN_MARKS:
        return _stroke_rects(annot)
    if kind in _AREA_MARKS:
        return [annot.rect]
    return []


def _annotation_boxes(page: pymupdf.Page) -> list[_Box]:
    """Annotations that mark an area, removed from the page and returned."""
    boxes: list[_Box] = []
    for annot in list(page.annots()):
        rects = _annotation_rects(annot)
        if not rects:
            continue
        fill = (
            annot.colors.get("fill") if annot.type[1] in ("Square", "Circle", "Polygon") else None
        )
        color = tuple(fill) if fill and len(fill) == 3 else (0.0, 0.0, 0.0)
        boxes += [_Box(pymupdf.Rect(r), color) for r in rects if not pymupdf.Rect(r).is_empty]
        page.delete_annot(annot)
    return boxes


def _stroke_rects(annot: pymupdf.Annot) -> list[pymupdf.Rect]:
    """One rectangle per pen stroke, as wide as the pen.

    The annotation's own rectangle has generous margins and spans all its
    strokes: blots over a name and a signature drawn in one go would take
    every line in between. Gaps inside a blot stay covered.
    """
    vertices = annot.vertices or []
    strokes = vertices if annot.type[1] == "Ink" else [vertices]
    half = max(annot.border.get("width") or 1.0, 1.0) / 2
    rects = []
    for stroke in strokes:
        if not stroke:
            continue
        xs, ys = [p[0] for p in stroke], [p[1] for p in stroke]
        rect = pymupdf.Rect(min(xs) - half, min(ys) - half, max(xs) + half, max(ys) + half)
        rects.append(rect & annot.rect)
    return rects or [annot.rect]


_Char = tuple[str, tuple[float, float, float, float]]
"""A character and its rounded position."""


def _chars(page: pymupdf.Page) -> set[_Char]:
    raw = page.get_text("rawdict")
    return {
        (char["c"], tuple(round(v, 1) for v in char["bbox"]))
        for block in raw["blocks"]
        for line in block.get("lines", ())
        for span in line["spans"]
        for char in span["chars"]
        if not char["c"].isspace()
    }


def _hidden_chars(chars: set[_Char], rect: pymupdf.Rect) -> set[_Char]:
    """Those of a page's ``chars`` whose centre lies inside ``rect``, i.e.
    hidden by a box there."""
    x0, y0, x1, y1 = rect.x0, rect.y0, rect.x1, rect.y1
    return {
        (c, box)
        for c, box in chars
        if x0 <= (box[0] + box[2]) / 2 <= x1 and y0 <= (box[1] + box[3]) / 2 <= y1
    }


def _check(path: Path, hidden: dict[int, list[set[_Char]]]) -> dict[int, int]:
    """``{page_number: boxes whose hidden characters are still in place}``.

    Compares exact characters and positions: text next to a box that moved
    sideways when its neighbours were removed does not count.
    """
    leftovers: dict[int, int] = {}
    with open_pdf(path) as doc:
        for page_no, per_box in hidden.items():
            remaining = _chars(doc[page_no - 1])
            if count := sum(1 for chars in per_box if chars & remaining):
                leftovers[page_no] = count
    return leftovers
