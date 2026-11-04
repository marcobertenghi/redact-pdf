"""Shared fixtures: small synthetic PDFs generated on the fly."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest


@pytest.fixture
def sample_pdf(tmp_path: Path) -> Path:
    """Two-page PDF with names, an email, an image and identifying metadata."""
    path = tmp_path / "sample.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 100), "Contract between John Smith and ACME GmbH.", fontsize=12)
        page.insert_text(
            (72, 130), "Contact: john.smith@acme.de, phone +49 30 1234567", fontsize=10
        )
        page.insert_text((72, 160), "Signed by john smith.", fontsize=8)

        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 60), False)
        pix.set_rect(pix.irect, (200, 220, 255))
        page.insert_image(pymupdf.Rect(72, 200, 272, 260), pixmap=pix)
        page.insert_text((80, 235), "John Smith on an image", fontsize=11)

        page2 = doc.new_page()
        page2.insert_text((72, 100), "Nothing to see here, except Smith.", fontsize=12)

        doc.set_metadata({"author": "John Smith", "title": "Secret contract"})
        doc.save(path)
    return path


def page_text(path: Path) -> str:
    with pymupdf.open(path) as doc:
        return "\n".join(page.get_text() for page in doc)


XMP_PACKET = (
    '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    '<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:xmp="http://ns.adobe.com/xap/1.0/">'
    "<dc:creator><rdf:Seq><rdf:li>Jane Secret</rdf:li></rdf:Seq></dc:creator>"
    "<xmp:CreatorTool>SecretTool 1.0</xmp:CreatorTool>"
    "</rdf:Description></rdf:RDF></x:xmpmeta>"
    '<?xpacket end="w"?>'
)
SECRETS = (b"Jane Secret", b"SecretTool", b"Secret Corp", b"Canon EOS")


def jpeg_segment(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


def plain_jpeg() -> bytes:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 32, 32), False)
    pix.set_rect(pix.irect, (200, 50, 50))
    data: bytes = pix.tobytes("jpeg")
    return data


def jpeg_with_metadata() -> bytes:
    """A valid JPEG carrying EXIF, a comment, IPTC and data appended after EOI."""
    base = plain_jpeg()
    extra = (
        jpeg_segment(0xE1, b"Exif\x00\x00MM\x00*\x00\x00\x00\x08Canon EOS Jane Secret")
        + jpeg_segment(0xFE, b"Comment by Jane Secret")
        + jpeg_segment(0xED, b"Photoshop 3.0\x00Jane Secret")
    )
    return base[:2] + extra + base[2:] + b"APPENDED Jane Secret"


@pytest.fixture
def metadata_pdf(tmp_path: Path) -> Path:
    """PDF with metadata in all the places exiftool does and doesn't show."""
    path = tmp_path / "meta.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 100), "Plain body text.", fontsize=12)
        page.insert_image(pymupdf.Rect(72, 150, 200, 278), stream=jpeg_with_metadata())
        annot = page.add_text_annot((300, 100), "a comment")
        annot.set_info(title="Jane Secret")
        annot.update()

        doc.set_metadata({"author": "Jane Secret", "title": "Secret Corp report"})
        info_xref = int(doc.xref_get_key(-1, "Info")[1].split()[0])
        doc.xref_set_key(info_xref, "Company", "(Secret Corp)")
        doc.set_xml_metadata(XMP_PACKET)

        xmp = doc.get_new_xref()
        doc.update_object(xmp, "<</Type/Metadata/Subtype/XML>>")
        doc.update_stream(xmp, XMP_PACKET.encode(), compress=False)
        image_xref = page.get_images()[0][0]
        for xref in (page.xref, image_xref):
            doc.xref_set_key(xref, "Metadata", f"{xmp} 0 R")
        doc.xref_set_key(page.xref, "PieceInfo", "<</Word<</Private(Secret Corp draft)>>>>")
        doc.xref_set_key(page.xref, "LastModified", "(D:20200101000000)")
        doc.save(path)
    return path


def all_pdf_bytes(path: Path) -> bytes:
    """Every object and every stream (decoded and raw) of a PDF, concatenated."""
    chunks: list[bytes] = []
    with pymupdf.open(path) as doc:
        chunks.append(doc.xref_object(-1).encode())  # trailer
        for xref in range(1, doc.xref_length()):
            chunks.append(doc.xref_object(xref).encode())
            if doc.xref_is_stream(xref):
                chunks.append(doc.xref_stream_raw(xref) or b"")
                if not doc.xref_is_image(xref):
                    chunks.append(doc.xref_stream(xref) or b"")
    return b"\n".join(chunks)
