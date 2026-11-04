"""Deep metadata removal for PDF documents.

Tools like exiftool only look at the document information dictionary and the
document-level XMP packet. Identifying data also hides in places they don't
show, all of which are handled here:

* XMP packets attached to pages, images, fonts and other objects
* Application private data (``/PieceInfo``, e.g. from Illustrator or Word)
* Annotation authors and timestamps (when annotations are kept)
* EXIF, XMP, IPTC, comments and thumbnails inside embedded JPEG images
* The trailer ``/ID``, which otherwise links the output to the original file
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from dataclasses import dataclass, field

import pymupdf

__all__ = ["MetadataReport", "new_document_id", "strip_jpeg_metadata", "strip_metadata"]

logger = logging.getLogger(__name__)

# Keys that only carry metadata and can be removed from any PDF object,
# with the description used in reports.
OBJECT_METADATA_KEYS = {
    "Metadata": "XMP packets",
    "PieceInfo": "application private data",
    "LastModified": "modification dates",
}
# Annotation keys holding the author, timestamps and a generator-specific name.
ANNOTATION_METADATA_KEYS = ("T", "M", "CreationDate")

# JPEG markers
_SOI = b"\xff\xd8"
_EOI = 0xD9
_SOS = 0xDA
_COM = 0xFE
_APP0, _APP2, _APP14, _APP15 = 0xE0, 0xE2, 0xEE, 0xEF
_STANDALONE = {0x01, *range(0xD0, 0xD8)}  # TEM and RST0-7 have no length field
_JFIF_HEADER_LEN = 14  # "JFIF\0", version, units, densities, thumbnail size


@dataclass
class MetadataReport:
    """Counts of removed metadata items, keyed by a short description."""

    removed: Counter[str] = field(default_factory=Counter)

    def __bool__(self) -> bool:
        return bool(+self.removed)

    def summary(self) -> str:
        return ", ".join(f"{name} x{n}" for name, n in sorted(self.removed.items()) if n)


def new_document_id() -> str:
    """Random trailer ``/ID`` array, so the output cannot be linked to its source."""
    return f"[<{os.urandom(16).hex()}><{os.urandom(16).hex()}>]"


def strip_metadata(doc: pymupdf.Document) -> MetadataReport:
    """Remove metadata from an open document in place.

    The changes take full effect when the document is saved with
    ``garbage=4`` (which drops the now unreferenced objects) and without
    incremental saving (which would keep the old revision in the file).
    """
    report = MetadataReport()

    if doc.xref_get_key(-1, "Info")[0] != "null":
        report.removed["document info"] += 1
    doc.set_metadata({})
    doc.xref_set_key(-1, "Info", "null")

    for xref in range(1, doc.xref_length()):  # includes the catalog's XMP packet
        for key, description in OBJECT_METADATA_KEYS.items():
            if _drop_key(doc, xref, key):
                report.removed[description] += 1
        if _is_annotation(doc, xref):
            for key in ANNOTATION_METADATA_KEYS:
                if _drop_key(doc, xref, key):
                    report.removed["annotation authors/dates"] += 1
        if _is_plain_jpeg(doc, xref) and _strip_image_stream(doc, xref):
            report.removed["JPEG EXIF/XMP/comments"] += 1

    doc.del_xml_metadata()  # no-op after the loop; kept as a safeguard
    doc.xref_set_key(-1, "ID", new_document_id())
    if report:
        logger.info("Removed metadata: %s", report.summary())
    return report


def _drop_key(doc: pymupdf.Document, xref: int, key: str) -> bool:
    if doc.xref_get_key(xref, key)[0] == "null":
        return False
    doc.xref_set_key(xref, key, "null")
    return True


def _is_annotation(doc: pymupdf.Document, xref: int) -> bool:
    """Annotations have a /Rect and /Subtype. Form fields (/Widget) are excluded:
    their /T is the field name, not an author."""
    subtype = doc.xref_get_key(xref, "Subtype")
    return bool(
        subtype[0] == "name"
        and subtype[1] != "/Widget"
        and doc.xref_get_key(xref, "Rect")[0] == "array"
    )


def _is_plain_jpeg(doc: pymupdf.Document, xref: int) -> bool:
    """True for streams whose only filter is DCTDecode (a complete JPEG file)."""
    kind, value = doc.xref_get_key(xref, "Filter")
    return bool(
        (kind == "name" and value == "/DCTDecode")
        or (kind == "array" and value.replace(" ", "") == "[/DCTDecode]")
    )


def _strip_image_stream(doc: pymupdf.Document, xref: int) -> bool:
    raw = doc.xref_stream_raw(xref)
    cleaned = strip_jpeg_metadata(raw)
    if cleaned == raw:
        return False
    try:
        pymupdf.Pixmap(cleaned)  # make sure the image still decodes
    except Exception:  # pymupdf raises several exception types
        logger.warning("Kept image xref %d unchanged: cleaned JPEG did not decode", xref)
        return False

    decode_parms = doc.xref_get_key(xref, "DecodeParms")
    doc.update_stream(xref, cleaned, compress=False)
    # update_stream drops the filter; restore it so the bytes are read as JPEG again
    doc.xref_set_key(xref, "Filter", "/DCTDecode")
    if decode_parms[0] != "null":
        doc.xref_set_key(xref, "DecodeParms", decode_parms[1])
    return True


def strip_jpeg_metadata(data: bytes) -> bytes:
    """Losslessly remove metadata segments from a JPEG file.

    Removes EXIF/XMP (APP1), IPTC/Photoshop (APP13), comments, other
    application segments, JFIF thumbnails and anything appended after the
    end-of-image marker (e.g. MPF secondary images from phone cameras).
    Keeps what is needed to render correctly: JFIF (APP0), ICC colour
    profiles (APP2) and the Adobe colour-transform segment (APP14).
    The compressed image data is copied unchanged.

    Returns the input unchanged if it is not a well-formed JPEG.
    """
    if not data.startswith(_SOI):
        return data
    out = bytearray(_SOI)
    i, n = 2, len(data)
    while i + 1 < n:
        if data[i] != 0xFF:
            return data  # malformed
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in _STANDALONE:
            out += data[i : i + 2]
            i += 2
            continue
        if marker == _EOI:
            out += data[i : i + 2]
            return bytes(out)
        if marker == _SOS:
            end = _find_end_of_image(data, i)
            if end is None:
                return data
            out += data[i:end]
            return bytes(out)

        if i + 4 > n:
            return data
        length = int.from_bytes(data[i + 2 : i + 4], "big")
        segment = data[i : i + 2 + length]
        if length < 2 or len(segment) != 2 + length:
            return data
        out += _filter_segment(marker, segment)
        i += 2 + length
    return data


def _filter_segment(marker: int, segment: bytes) -> bytes:
    """Return the segment to keep (possibly shortened), or b'' to drop it."""
    payload = segment[4:]
    if marker == _COM:
        return b""
    if marker == _APP0:
        if payload.startswith(b"JFIF\x00") and len(payload) >= _JFIF_HEADER_LEN:
            # Keep the header, drop the embedded thumbnail (set its size to 0x0)
            header = bytearray(payload[:_JFIF_HEADER_LEN])
            header[12:14] = b"\x00\x00"
            return segment[:2] + (len(header) + 2).to_bytes(2, "big") + bytes(header)
        return b""  # JFXX extension = thumbnail
    if marker == _APP2:
        return segment if payload.startswith(b"ICC_PROFILE\x00") else b""
    if marker == _APP14:
        return segment if payload.startswith(b"Adobe") else b""
    if _APP0 < marker <= _APP15:
        return b""
    return segment  # tables, frame headers etc.


def _find_end_of_image(data: bytes, i: int) -> int | None:
    """Given the index of the first SOS marker, return the index just past EOI.

    Walks the scans of baseline and progressive JPEGs: segments are skipped by
    their length, entropy-coded data is scanned for the next real marker
    (``FF00`` is a stuffed byte and ``FFD0``-``FFD7`` are restart markers).
    """
    n = len(data)
    while i + 3 < n:
        length = int.from_bytes(data[i + 2 : i + 4], "big")
        i += 2 + length
        while True:
            j = data.find(b"\xff", i)
            if j == -1 or j + 1 >= n:
                return None
            nxt = data[j + 1]
            if nxt == 0x00 or nxt in _STANDALONE:
                i = j + 2
            elif nxt == 0xFF:
                i = j + 1
            else:
                break
        if nxt == _EOI:
            return j + 2
        i = j
    return None
