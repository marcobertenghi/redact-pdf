"""remove-pages: pages leave the file completely, the rest follow on."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from redact_pdf import parse_pages, remove_pages
from redact_pdf.cli import EXIT_ERROR, EXIT_OK, main


def raw(path: Path) -> bytes:
    """The whole file with all streams decompressed, to search for leftovers."""
    with pymupdf.open(path) as doc:
        return bytes(doc.tobytes(expand=255, deflate=False))


def page_texts(path: Path) -> list[str]:
    with pymupdf.open(path) as doc:
        return [page.get_text().strip() for page in doc]


def book(path: Path, pages: int = 10) -> Path:
    """Pages "Page 1".."Page N", each with a unique word, labels, bookmarks,
    a link, an image only on page 5 and an image shared by pages 1 and 5."""
    with pymupdf.open() as doc:
        shared = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8), False)
        shared.set_rect(shared.irect, (0, 128, 0))
        only5 = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 9, 9), False)
        only5.set_rect(only5.irect, (200, 0, 0))
        for n in range(1, pages + 1):
            page = doc.new_page()
            page.insert_text((72, 72), f"Page {n} word{n}x", fontsize=12)
            if n in (1, 5):
                page.insert_image(pymupdf.Rect(72, 100, 120, 148), pixmap=shared)
            if n == 5:
                page.insert_image(pymupdf.Rect(200, 100, 248, 148), pixmap=only5)
        doc.set_page_labels([{"startpage": 0, "prefix": "", "style": "D", "firstpagenum": 1}])
        doc.set_toc(
            [
                [1, "Intro", 1],
                [1, "Secret chapter", 4],
                [2, "Kept subsection", 7],
                [1, "Appendix", 9],
            ]
        )
        link = {"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(72, 200, 200, 220)}
        doc[0].insert_link({**link, "page": 8})  # to page 9: kept
        doc[1].insert_link({**link, "page": 4})  # to page 5: removed
        doc.set_metadata({"author": "Someone", "title": "Book"})
        doc.save(path)
    return path


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("92-100", list(range(92, 101))),
        ("1,3,5-6", [1, 3, 5, 6]),
        ("1, 3 , 5 - 6", [1, 3, 5, 6]),
        ("98-", [98, 99, 100]),
        ("4,4,2-4", [2, 3, 4]),
        ([7, 2], [2, 7]),
    ],
)
def test_parse_pages(spec: str | list[int], expected: list[int]) -> None:
    assert parse_pages(spec, 100) == expected


@pytest.mark.parametrize("spec", ["0", "101", "5-3", "abc", "", "1-2-3", "-5", [0], []])
def test_parse_pages_rejects(spec: str | list[int]) -> None:
    with pytest.raises(ValueError):
        parse_pages(spec, 100)


def test_range_is_removed_and_the_rest_follows(tmp_path: Path) -> None:
    report = remove_pages(book(tmp_path / "in.pdf"), "4-6")
    assert report.output == tmp_path / "in_TRIMMED.pdf"
    assert report.removed == [4, 5, 6]
    assert report.pages_left == 7
    assert report.is_clean
    texts = page_texts(report.output)
    assert [t.split()[1] for t in texts] == ["1", "2", "3", "7", "8", "9", "10"]
    data = raw(report.output)
    for n in (4, 5, 6):
        assert f"word{n}x".encode() not in data
    assert b"word7x" in data
    with pymupdf.open(report.output) as doc:
        assert not doc.metadata["author"]
        assert [p.get_label() for p in doc][:4] == ["1", "2", "3", "4"]  # labels kept


def test_bookmarks_to_removed_pages_are_dropped(tmp_path: Path) -> None:
    report = remove_pages(book(tmp_path / "in.pdf"), "4-6", tmp_path / "out.pdf")
    assert report.bookmarks == 1
    with pymupdf.open(report.output) as doc:
        assert doc.get_toc() == [[1, "Intro", 1], [1, "Kept subsection", 4], [1, "Appendix", 6]]
    assert b"Secret chapter" not in raw(report.output)


def test_links_follow_the_new_numbering(tmp_path: Path) -> None:
    report = remove_pages(book(tmp_path / "in.pdf"), "4-6", tmp_path / "out.pdf", scrub=False)
    with pymupdf.open(report.output) as doc:
        assert [link["page"] for link in doc[0].get_links()] == [5]  # old page 9
        assert not doc[1].get_links()  # pointed to a removed page


def test_images_only_on_removed_pages_are_gone(tmp_path: Path) -> None:
    src = book(tmp_path / "in.pdf")
    with pymupdf.open(src) as doc:
        assert sum(1 for x in range(1, doc.xref_length()) if doc.xref_is_image(x)) == 2
    report = remove_pages(src, "5", tmp_path / "out.pdf")
    with pymupdf.open(report.output) as doc:
        images = [x for x in range(1, doc.xref_length()) if doc.xref_is_image(x)]
        assert len(images) == 1  # the shared one, still on page 1
        assert pymupdf.Pixmap(doc, images[0]).pixel(0, 0) == (0, 128, 0)


def test_references_from_elsewhere_do_not_keep_the_page(tmp_path: Path) -> None:
    src = tmp_path / "in.pdf"
    with pymupdf.open() as doc:
        for n in range(1, 5):
            doc.new_page().insert_text((72, 72), f"Page {n}")
        gone = doc[2]
        cat = doc.pdf_catalog()
        dests = doc.get_new_xref()
        doc.update_object(
            dests, f"<< /chapter.secret [{gone.xref} 0 R /Fit] /keep [{doc[1].xref} 0 R /Fit] >>"
        )
        doc.xref_set_key(cat, "Dests", f"{dests} 0 R")
        doc.xref_set_key(cat, "OpenAction", f"[{gone.xref} 0 R /Fit]")
        field = pymupdf.Widget()
        field.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
        field.field_name = "SecretField"
        field.field_value = "secret value"
        field.rect = pymupdf.Rect(72, 100, 200, 120)
        gone.add_widget(field)
        gone.add_text_annot((300, 300), "secret comment")
        doc.save(src)

    report = remove_pages(src, "3", tmp_path / "out.pdf", scrub=False)
    assert report.is_clean and report.leftovers == 0
    data = raw(report.output)
    for leftover in (b"secret value", b"secret comment", b"SecretField", b"chapter.secret"):
        assert leftover not in data
    with pymupdf.open(report.output) as doc:
        assert doc.page_count == 3
        pages = [
            x for x in range(1, doc.xref_length()) if doc.xref_get_key(x, "Type")[1] == "/Page"
        ]
        assert len(pages) == 3  # no orphaned page object kept alive by a reference
        assert "keep" in doc.xref_object(int(doc.xref_get_key(doc.pdf_catalog(), "Dests")[1][:-4]))


def test_cannot_remove_every_page(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="every page"):
        remove_pages(book(tmp_path / "in.pdf"), "1-")


def test_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = book(tmp_path / "in.pdf")
    assert main(["remove-pages", str(src), "2,4-6,9-"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "removed 6 page(s) (2, 4-6, 9-10), 4 left" in out
    assert "bookmarks to removed pages dropped: 2" in out
    assert len(page_texts(tmp_path / "in_TRIMMED.pdf")) == 4

    assert main(["remove-pages", str(src), "11"]) == EXIT_ERROR
    assert "does not exist" in capsys.readouterr().err
