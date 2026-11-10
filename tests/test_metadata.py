from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from redact_pdf import redact_pdf, strip_jpeg_metadata
from tests.conftest import (
    SECRETS,
    all_pdf_bytes,
    jpeg_segment,
    jpeg_with_metadata,
    plain_jpeg,
)


def test_secrets_are_present_in_fixture(metadata_pdf: Path) -> None:
    # Sanity check: the fixture really contains what the other tests expect removed.
    data = all_pdf_bytes(metadata_pdf)
    assert all(secret in data for secret in SECRETS)


def test_no_trace_of_metadata_anywhere(metadata_pdf: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.pdf"
    redact_pdf(metadata_pdf, out)
    data = all_pdf_bytes(out)
    for secret in SECRETS:
        assert secret not in data, secret
    assert b"APPENDED" not in data


def test_info_and_xmp_removed(metadata_pdf: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.pdf"
    report = redact_pdf(metadata_pdf, out)
    with pymupdf.open(out) as doc:
        assert doc.xref_get_key(-1, "Info")[0] == "null"
        assert doc.get_xml_metadata() == ""
        assert not any(doc.metadata[k] for k in ("author", "title", "creator", "producer"))
    assert report.metadata.removed["document info"] == 1
    assert report.metadata.removed["XMP packets"] == 3  # catalog, page, image


def test_document_id_is_new(metadata_pdf: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.pdf"
    redact_pdf(metadata_pdf, out)
    with pymupdf.open(metadata_pdf) as a, pymupdf.open(out) as b:
        old_ids = a.xref_get_key(-1, "ID")[1].strip("[]<>").split("><")
        new_ids = b.xref_get_key(-1, "ID")[1].strip("[]<>").split("><")
    assert not set(old_ids) & set(new_ids)


def test_annotations_are_baked_by_default(metadata_pdf: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.pdf"
    redact_pdf(metadata_pdf, out)
    with pymupdf.open(out) as doc:
        assert list(doc[0].annots()) == []


def test_kept_annotations_lose_their_author(metadata_pdf: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.pdf"
    redact_pdf(metadata_pdf, out, keep_annotations=True)
    with pymupdf.open(out) as doc:
        annots = list(doc[0].annots())
        assert len(annots) == 1
        assert annots[0].info["title"] == ""


def test_visible_annotation_text_is_redacted(tmp_path: Path) -> None:
    src = tmp_path / "ft.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        annot = page.add_freetext_annot(pymupdf.Rect(72, 72, 300, 100), "Reviewed by Jane Doe")
        annot.update()
        doc.save(src)
    out = tmp_path / "out.pdf"
    redact_pdf(src, out, ["Jane Doe"])
    assert b"Jane Doe" not in all_pdf_bytes(out)


def test_bookmark_titles_are_redacted(tmp_path: Path) -> None:
    src = tmp_path / "toc.pdf"
    with pymupdf.open() as doc:
        doc.new_page().insert_text((72, 72), "Hello")
        doc.set_toc([[1, "Letter to Jane Doe", 1], [1, "Contact: jd@example.com", 1]])
        doc.save(src)
    out = tmp_path / "out.pdf"
    report = redact_pdf(src, out, ["Jane Doe"])
    with pymupdf.open(out) as doc:
        titles = [t[1] for t in doc.get_toc()]
    assert titles == ["Letter to REDACTED", "Contact: EMAIL"]
    assert report.bookmarks == 2


def test_image_still_renders(metadata_pdf: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.pdf"
    redact_pdf(metadata_pdf, out)
    with pymupdf.open(out) as doc:
        xref = doc[0].get_images()[0][0]
        assert doc.xref_get_key(xref, "Filter")[1] == "/DCTDecode"
        pix = pymupdf.Pixmap(doc, xref)
        assert (pix.width, pix.height) == (32, 32)


# --- strip_jpeg_metadata -------------------------------------------------------


def test_jpeg_metadata_removed_and_image_decodes() -> None:
    cleaned = strip_jpeg_metadata(jpeg_with_metadata())
    assert b"Jane Secret" not in cleaned
    assert b"Canon" not in cleaned
    assert cleaned.endswith(b"\xff\xd9")
    assert pymupdf.Pixmap(cleaned).width == 32


def test_jpeg_image_data_is_unchanged() -> None:
    base = plain_jpeg()
    cleaned = strip_jpeg_metadata(jpeg_with_metadata())
    sos = base.index(b"\xff\xda")
    assert cleaned.endswith(base[sos:])


def test_jpeg_keeps_icc_and_adobe_segments() -> None:
    base = plain_jpeg()
    icc = jpeg_segment(0xE2, b"ICC_PROFILE\x00\x01\x01profile")
    adobe = jpeg_segment(0xEE, b"Adobe\x00\x64\x00\x00\x00\x00\x01")
    cleaned = strip_jpeg_metadata(base[:2] + icc + adobe + base[2:])
    assert icc in cleaned
    assert adobe in cleaned


def test_jfif_thumbnail_removed() -> None:
    base = plain_jpeg()
    thumb = b"\x02\x01" + bytes(2 * 1 * 3)  # 2x1 RGB thumbnail
    jfif = jpeg_segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01" + thumb)
    soi_removed = base[2:]
    if soi_removed.startswith(b"\xff\xe0"):  # drop the encoder's own JFIF segment
        length = int.from_bytes(soi_removed[2:4], "big")
        soi_removed = soi_removed[2 + length :]
    cleaned = strip_jpeg_metadata(base[:2] + jfif + soi_removed)
    assert b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" in cleaned
    assert pymupdf.Pixmap(cleaned).width == 32


def test_jpeg_is_idempotent() -> None:
    once = strip_jpeg_metadata(jpeg_with_metadata())
    assert strip_jpeg_metadata(once) == once


@pytest.mark.parametrize(
    "data", [b"", b"not a jpeg", b"\xff\xd8\x00garbage", b"\xff\xd8\xff\xe1\x00"]
)
def test_non_jpeg_input_is_returned_unchanged(data: bytes) -> None:
    assert strip_jpeg_metadata(data) == data
