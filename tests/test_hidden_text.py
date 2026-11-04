"""Text outside the page content: tags, marked content, string objects, thumbnails."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from redact_pdf import DOCUMENT_LEVEL, redact_pdf, verify_pdf
from redact_pdf.pdfstrings import decode_string, tokens
from tests.conftest import all_pdf_bytes, page_text


def _page(doc: pymupdf.Document) -> pymupdf.Page:
    page = doc.new_page()
    page.insert_text((72, 100), "Hello John Smith", fontsize=12)
    return page


@pytest.fixture
def tagged_pdf(tmp_path: Path) -> Path:
    """Structure tree with alt text, a layer, page labels, a link."""
    path = tmp_path / "tagged.pdf"
    with pymupdf.open() as doc:
        page = _page(doc)
        elem = doc.get_new_xref()
        doc.update_object(
            elem, "<</Type/StructElem/S/Figure/Alt(Photo of John Smith)/ActualText(John Smith)>>"
        )
        root = doc.get_new_xref()
        doc.update_object(root, f"<</Type/StructTreeRoot/K {elem} 0 R>>")
        doc.xref_set_key(doc.pdf_catalog(), "StructTreeRoot", f"{root} 0 R")
        doc.xref_set_key(doc.pdf_catalog(), "MarkInfo", "<</Marked true>>")
        doc.add_ocg("Layer by John Smith")
        doc.set_page_labels([{"startpage": 0, "prefix": "JOHN SMITH-", "style": "D"}])
        page.insert_link(
            {"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 90, 200, 105),
             "uri": "https://example.org/john-smith?name=John%20Smith"}
        )  # fmt: skip
        doc.save(path)
    return path


def test_secrets_present_in_tagged_fixture(tagged_pdf: Path) -> None:
    found = verify_pdf(tagged_pdf, ["John Smith"], emails=False)
    assert found[DOCUMENT_LEVEL] == ["John Smith"]


def test_text_outside_pages_is_redacted(tagged_pdf: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.pdf"
    report = redact_pdf(tagged_pdf, out, ["John Smith"], scrub=False)
    assert report.is_clean
    assert report.metadata.removed["accessibility tags (alt text)"] == 1
    assert report.hidden_text == {"John Smith": 2}  # layer name, page label prefix
    data = all_pdf_bytes(out)
    assert b"John Smith" not in data and b"JOHN SMITH" not in data
    with pymupdf.open(out) as doc:
        assert doc.get_ocgs()  # the layer itself is kept, only renamed
        assert "REDACTED" in next(iter(doc.get_ocgs().values()))["name"]
        assert doc.xref_get_key(doc.pdf_catalog(), "StructTreeRoot")[0] == "null"


def test_actual_text_in_content_stream_is_removed(tmp_path: Path) -> None:
    src = tmp_path / "in.pdf"
    with pymupdf.open() as doc:
        page = _page(doc)
        xref = page.get_contents()[0]
        body = doc.xref_stream(xref)
        doc.update_stream(
            xref, b"/Span <</ActualText (John Smith) /Lang (en)>> BDC\n" + body + b"\nEMC"
        )
        doc.save(src)
    out = tmp_path / "out.pdf"
    report = redact_pdf(src, out, ["John Smith"])
    assert report.metadata.removed["alternate text in page content"] == 1
    assert report.is_clean
    assert "John Smith" not in page_text(out)
    assert b"John Smith" not in all_pdf_bytes(out)
    assert b"/Lang" in all_pdf_bytes(out)  # other properties are kept


def test_thumbnails_removed_even_without_scrub(tmp_path: Path) -> None:
    src = tmp_path / "in.pdf"
    with pymupdf.open() as doc:
        page = _page(doc)
        jpeg = page.get_pixmap(dpi=30).tobytes("jpeg")
        thumb = page.insert_image(pymupdf.Rect(0, 0, 1, 1), stream=jpeg)
        doc.xref_set_key(page.xref, "Thumb", f"{thumb} 0 R")
        doc.save(src)
    out = tmp_path / "out.pdf"
    report = redact_pdf(src, out, ["John Smith"], scrub=False)
    assert report.metadata.removed["page thumbnails"] == 1
    with pymupdf.open(out) as doc:
        assert doc.xref_get_key(doc[0].xref, "Thumb")[0] == "null"


def test_verify_reports_document_level(tmp_path: Path) -> None:
    src = tmp_path / "in.pdf"
    with pymupdf.open() as doc:
        doc.new_page().insert_text((72, 72), "Nothing here")
        doc.add_ocg("Draft by jane@corp.example.com")
        doc.save(src)
    assert verify_pdf(src) == {DOCUMENT_LEVEL: ["jane@corp.example.com"]}


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        (rb"(plain)", "plain"),
        (rb"(nested (parens) ok)", "nested (parens) ok"),
        (rb"(esc \(x\) \\ \n end)", "esc (x) \\ \n end"),
        (rb"(M\374ller)", "Müller"),
        (b"(line\\\ncontinued)", "linecontinued"),
        (b"<FEFF004D00FC006C006C00650072>", "Müller"),
        (b"<4A6F 686E>", "John"),
        (b"<4A6>", "J`"),
    ],
)
def test_decode_string(raw: bytes, text: str) -> None:
    assert decode_string(raw) == text


def test_tokenizer_skips_inline_images_and_comments() -> None:
    data = b"q BI /W 1 /H 1 ID \x00(\xff)<< EI Q % (comment)\n/Span <</Alt (x)>> BDC"
    strings = [data[t.start : t.end] for t in tokens(data) if t.kind == "string"]
    assert strings == [b"(x)"]
