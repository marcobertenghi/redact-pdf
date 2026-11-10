"""Image areas that no page shows.

A page often shows only part of a stored image: the image is cropped by a
clip path (Word, browsers and Confluence crop this way), or it runs off the
page, as when a tall screenshot is split across two pages and each page
holds a full copy. The hidden part is still in the file and can be
extracted with any PDF tool. Redaction makes this worse: it blanks the
pixels under a box only in the copy on that page, so the same spot in the
other page's copy stays intact.

:func:`blank_hidden_image_areas` replaces every image area that no page
shows with white; :func:`hidden_image_pages` finds images that still have
content there.
"""

from __future__ import annotations

import itertools
import logging
import math
import re
from collections import defaultdict

import pymupdf

__all__ = ["HIDDEN_IMAGE", "blank_hidden_image_areas", "hidden_image_pages"]

logger = logging.getLogger(__name__)

HIDDEN_IMAGE = "image content outside the visible page"
"""Reported by verification for images with content no page shows."""

_KEEP = 2  # px kept around the visible area, so resampling at its edge is unchanged
_JPEG_EDGE = 16  # px ignored around the visible area when checking: JPEG ringing
_UNIFORM = 0.999  # share of the hidden area in one colour that counts as blank
_JPEG_QUALITY = 95
# Colour spaces whose decoded samples are the stored samples: an image
# rewritten in them looks the same. Others (Indexed, Separation...) are
# expanded on decoding and stored in the matching device space.
_SAME_SAMPLES = frozenset(
    {"/DeviceGray", "/DeviceRGB", "/DeviceCMYK", "/ICCBased", "/CalGray", "/CalRGB"}
)

_Placements = dict[int, list[tuple[int, pymupdf.Rect]]]
"""``{image xref: [(page number, visible part in image space 0..1), ...]}``"""


def blank_hidden_image_areas(doc: pymupdf.Document) -> int:
    """Blank the parts of images that no page shows. Returns the number of images changed.

    Visible areas are kept, plus a small margin: lossless images pixel for
    pixel, JPEG images re-encoded at high quality. Colour profiles are kept.
    Images whose hidden area is one plain colour are not rewritten. The image's soft
    mask (transparency) is blanked the same way, so a logo's outline does not
    survive there either. Inline images and stencil masks are left unchanged.
    """
    changed = 0
    for xref, placements in _placements(doc).items():
        visible = [rect for _, rect in placements]
        if any(_covers_all(rect) for rect in visible):
            continue
        try:
            done = _blank(doc, xref, visible)
            if (smask := _smask(doc, xref)) is not None:
                done = _blank(doc, smask, visible) or done
        except (RuntimeError, ValueError) as exc:  # undecodable image
            logger.warning("Could not check image %d for hidden areas: %s", xref, exc)
            continue
        changed += done
    return changed


def hidden_image_pages(doc: pymupdf.Document) -> dict[int, int]:
    """``{page number: images}`` for images with content in areas no page shows.

    Uniform areas (blank, or a plain margin) don't count, nor do the few
    pixels next to the visible area, where JPEG compression smears the edge.
    """
    pages: dict[int, int] = defaultdict(int)
    for xref, placements in _placements(doc).items():
        visible = [rect for _, rect in placements]
        if any(_covers_all(rect) for rect in visible):
            continue
        xrefs = [xref] + [s for s in [_smask(doc, xref)] if s is not None]
        try:
            hidden = any(_has_content(_load(doc, x), visible, _JPEG_EDGE) for x in xrefs)
        except (RuntimeError, ValueError):
            continue
        if hidden:
            for page_no in sorted({page_no for page_no, _ in placements}):
                pages[page_no] += 1
    return dict(pages)


def _placements(doc: pymupdf.Document) -> _Placements:
    """Where each image is drawn, as the visible part of the image's unit square.

    MuPDF's text page lists every drawn image with its size, its transform
    and its visible box, clipped by clip paths and the page. The image's
    xref is found by its size, which is nearly always unique; only when two
    images share a size are their contents compared, which is slow.
    """
    by_size = _xrefs_by_size(doc)
    digest_of: dict[int, bytes] = {}
    found: _Placements = defaultdict(list)
    for page in doc.pages():
        # Most pages have no image, and asking MuPDF where images are drawn
        # means interpreting the whole page.
        if not page.get_images(full=True):
            continue
        # The clip flag makes the reported box the visible one. Box and
        # transform must come from the same list: block numbers differ
        # between PyMuPDF's outputs.
        textpage = page.get_textpage(
            flags=pymupdf.TEXT_PRESERVE_IMAGES | pymupdf.TEXT_MEDIABOX_CLIP
        )
        hashed: dict[int, bytes] | None = None
        for info in textpage.extractIMGINFO():
            candidates = by_size.get((info["width"], info["height"]), [])
            if len(candidates) > 1:  # same size: tell them apart by content
                if hashed is None:
                    hashed = {
                        i["number"]: i["digest"] for i in textpage.extractIMGINFO(hashes=True)
                    }
                for xref in candidates:
                    if xref not in digest_of:
                        digest_of[xref] = pymupdf.Pixmap(doc, xref).digest
                candidates = [x for x in candidates if digest_of[x] == hashed[info["number"]]][:1]
            xref = candidates[0] if candidates else 0  # 0: inline image
            if xref <= 0 or _is_stencil(doc, xref):
                continue
            matrix = pymupdf.Matrix(info["transform"])
            if abs(matrix.a * matrix.d - matrix.b * matrix.c) < 1e-9:
                continue  # degenerate: drawn with no area
            visible = pymupdf.Rect(info["bbox"])  # empty if nothing of it is shown
            unit = (visible * ~matrix).normalize() if not visible.is_empty else pymupdf.Rect()
            found[xref].append((page.number + 1, unit & pymupdf.Rect(0, 0, 1, 1)))
    return dict(found)


def _xrefs_by_size(doc: pymupdf.Document) -> dict[tuple[int, int], list[int]]:
    """``{(width, height): [xref, ...]}`` of images that can be drawn (not soft masks)."""
    images, masks = [], set()
    for xref in range(1, doc.xref_length()):
        if doc.xref_get_key(xref, "Subtype")[1] != "/Image":
            continue
        images.append(xref)
        if (smask := _smask(doc, xref)) is not None:
            masks.add(smask)
    sizes: dict[tuple[int, int], list[int]] = defaultdict(list)
    for xref in images:
        if xref not in masks:
            width, height = (doc.xref_get_key(xref, k)[1] for k in ("Width", "Height"))
            if width.isdigit() and height.isdigit():
                sizes[(int(width), int(height))].append(xref)
    return dict(sizes)


def _covers_all(unit: pymupdf.Rect, tolerance: float = 1e-3) -> bool:
    return bool(
        unit.x0 <= tolerance
        and unit.y0 <= tolerance
        and unit.x1 >= 1 - tolerance
        and unit.y1 >= 1 - tolerance
    )


def _is_stencil(doc: pymupdf.Document, xref: int) -> bool:
    return bool(doc.xref_get_key(xref, "ImageMask")[1] == "true")


def _smask(doc: pymupdf.Document, xref: int) -> int | None:
    kind, value = doc.xref_get_key(xref, "SMask")
    return int(value.split()[0]) if kind == "xref" else None


def _pixel_rects(
    pix: pymupdf.Pixmap, visible: list[pymupdf.Rect], margin: int
) -> list[pymupdf.IRect]:
    w, h = pix.width, pix.height
    rects = []
    for unit in visible:
        if unit.is_empty:
            continue
        rect = pymupdf.IRect(
            math.floor(unit.x0 * w) - margin,
            math.floor(unit.y0 * h) - margin,
            math.ceil(unit.x1 * w) + margin,
            math.ceil(unit.y1 * h) + margin,
        )
        rects.append(rect & pix.irect)
    return rects


def _load(doc: pymupdf.Document, xref: int) -> pymupdf.Pixmap:
    """The decoded image, without alpha (a soft mask is a separate image)."""
    pix = pymupdf.Pixmap(doc, xref)
    return pymupdf.Pixmap(pix, 0) if pix.alpha else pix


def _copy(pix: pymupdf.Pixmap) -> pymupdf.Pixmap:
    return pymupdf.Pixmap(pix, 0)  # Pixmap(pix) alone would add an alpha channel


def _white(pix: pymupdf.Pixmap) -> tuple[int, ...]:
    """White in the pixmap's colour space (no ink in CMYK)."""
    channels: int = pix.n
    return (0 if channels == 4 else 255,) * channels


def _hidden_cells(
    pix: pymupdf.Pixmap, visible: list[pymupdf.Rect], margin: int
) -> list[pymupdf.IRect]:
    """The area outside ``visible`` (plus ``margin``), as rectangles.

    The edges of all visible rectangles cut the image into a grid; each cell
    lies either inside a visible rectangle or entirely outside all of them.
    """
    shown = [r for r in _pixel_rects(pix, visible, margin) if not r.is_empty]
    xs = sorted({0, pix.width, *(r.x0 for r in shown), *(r.x1 for r in shown)})
    ys = sorted({0, pix.height, *(r.y0 for r in shown), *(r.y1 for r in shown)})
    cells = []
    for y0, y1 in itertools.pairwise(ys):
        for x0, x1 in itertools.pairwise(xs):
            cell = pymupdf.IRect(x0, y0, x1, y1)
            if not any(r.contains(cell) for r in shown):
                cells.append(cell)
    return cells


def _has_content(pix: pymupdf.Pixmap, visible: list[pymupdf.Rect], margin: int) -> bool:
    """True if the area outside ``visible`` (plus ``margin``) is not one plain colour."""
    colors = set()
    for cell in _hidden_cells(pix, visible, margin):
        ratio, color = pix.color_topusage(clip=cell)
        if ratio < _UNIFORM:
            return True
        colors.add(bytes(color))
    return len(colors) > 1


def _blank(doc: pymupdf.Document, xref: int, visible: list[pymupdf.Rect]) -> bool:
    """Replace everything outside ``visible`` in image ``xref`` with white.

    Images whose hidden area is one plain colour already are left alone, so
    a 1-pixel border or a blank margin doesn't cause a rewrite.
    """
    pix = _load(doc, xref)
    if not _has_content(pix, visible, _KEEP):
        return False
    blank = _copy(pix)
    blank.clear_with(_white(pix)[0])
    for rect in _pixel_rects(pix, visible, _KEEP):
        blank.copy(pix, rect)
    _write(doc, xref, blank)
    return True


def _write(doc: pymupdf.Document, xref: int, pix: pymupdf.Pixmap) -> None:
    """Store ``pix`` as image ``xref``, keeping its other keys (mask, intent...)."""
    was_jpeg = "DCTDecode" in doc.xref_get_key(xref, "Filter")[1]
    data = None
    if was_jpeg and pix.n in (1, 3):
        data = pix.tobytes("jpeg", jpg_quality=_JPEG_QUALITY)
    if data is not None:
        doc.update_stream(xref, data, compress=False)
        doc.xref_set_key(xref, "Filter", "/DCTDecode")
    else:
        doc.update_stream(xref, pix.samples, compress=True)
        doc.xref_set_key(xref, "Filter", "/FlateDecode")
    doc.xref_set_key(xref, "DecodeParms", "null")
    doc.xref_set_key(xref, "Decode", "null")  # already applied when decoding
    if not _keeps_samples(doc, xref):
        space = {1: "/DeviceGray", 3: "/DeviceRGB", 4: "/DeviceCMYK"}[pix.n]
        doc.xref_set_key(xref, "ColorSpace", space)
    doc.xref_set_key(xref, "BitsPerComponent", "8")
    doc.xref_set_key(xref, "Width", str(pix.width))
    doc.xref_set_key(xref, "Height", str(pix.height))


def _keeps_samples(doc: pymupdf.Document, xref: int) -> bool:
    """True if the image's colour space can stay: its decoded samples are in it."""
    kind, value = doc.xref_get_key(xref, "ColorSpace")
    if kind == "xref":
        value = doc.xref_object(int(value.split()[0]), compressed=True)
    family = re.match(r"\s*\[?\s*(/[A-Za-z]+)", value)
    return family is not None and family.group(1) in _SAME_SAMPLES
