"""Text no reader can see (invisible, or outside the visible page) and links."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from redact_pdf import apply_boxes, redact_pdf, remove_pages, verify_pdf
from redact_pdf.cli import EXIT_LEFTOVERS, EXIT_OK, main
from redact_pdf.document import remove_links
from redact_pdf.matching import EVERYWHERE
from redact_pdf.unseen import INVISIBLE_TEXT, OUTSIDE_TEXT, remove_unseen_text
from tests.conftest import all_pdf_bytes, page_text

SECRETS = ("bob@acme-corp.com", "carol@acme-corp.com", "erin@acme-corp.com", "Falcon")


def all_text(path: Path) -> str:
    """The text of every page, including what lies outside the visible page."""
    with pymupdf.open(path) as doc:
        return "\n".join(page.get_text(clip=EVERYWHERE, flags=0) for page in doc)


@pytest.fixture
def cropped_pdf(tmp_path: Path) -> Path:
    """Text beyond the page edge, and a second page cropped to its top half."""
    path = tmp_path / "cropped.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=400, height=400)
        page.insert_text((50, 100), "Visible: alice@acme-corp.com on Project Falcon", fontsize=10)
        page.insert_text((50, 200), "Kept line.", fontsize=10)
        page.insert_text(
            (450, 100), "Beside the page: bob@acme-corp.com Project Falcon", fontsize=10
        )
        page.insert_text((50, -40), "Above the page: carol@acme-corp.com Falcon notes", fontsize=10)
        page = doc.new_page(width=400, height=400)
        page.insert_text((50, 100), "Inside the crop: dave@acme-corp.com", fontsize=10)
        page.insert_text((50, 350), "Cropped off: erin@acme-corp.com Project Falcon", fontsize=10)
        page.insert_text((50, 370), "Cropped off, no match: internal draft remark", fontsize=10)
        page.set_cropbox(pymupdf.Rect(0, 0, 400, 200))
        doc.save(path)
    return path


def test_text_outside_the_page_is_in_the_file_but_not_shown(cropped_pdf: Path) -> None:
    assert "erin@acme-corp.com" not in page_text(cropped_pdf)  # viewers and get_text() hide it
    assert all(secret in all_text(cropped_pdf) for secret in SECRETS)


def test_redact_removes_text_outside_the_page(cropped_pdf: Path, tmp_path: Path) -> None:
    report = redact_pdf(cropped_pdf, tmp_path / "out.pdf", ["Project Falcon"])
    assert report.is_clean
    assert report.metadata.removed[OUTSIDE_TEXT] == 2
    text = all_text(report.output)
    assert not any(secret in text for secret in SECRETS)
    assert "internal draft remark" not in text  # unmatched, but nobody could review it
    assert "Kept line." in text and "Inside the crop:" in text
    # Matches out there are counted like any other.
    assert report.hits[2]["erin@acme-corp.com"] == 1
    assert report.hits[1]["Project Falcon"] == 2


def test_matches_outside_the_page_are_redacted_without_scrub(
    cropped_pdf: Path, tmp_path: Path
) -> None:
    report = redact_pdf(cropped_pdf, tmp_path / "out.pdf", ["Project Falcon"], scrub=False)
    assert report.is_clean
    text = all_text(report.output)
    assert "@acme-corp.com" not in text and "Project Falcon" not in text
    # Hidden text that matches nothing is kept on request.
    assert "internal draft remark" in text and "Falcon notes" in text


def test_verify_sees_matches_outside_the_page(cropped_pdf: Path) -> None:
    found = verify_pdf(cropped_pdf, ["Project Falcon"])
    assert "bob@acme-corp.com" in found[1] and "carol@acme-corp.com" in found[1]
    assert "erin@acme-corp.com" in found[2] and "Project Falcon" in found[2]
    assert main(["verify", str(cropped_pdf), "-t", "internal draft"]) == EXIT_LEFTOVERS


def test_cli_reports_removed_outside_text(
    cropped_pdf: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["redact", str(cropped_pdf), str(tmp_path / "out.pdf")]) == EXIT_OK
    assert f"{OUTSIDE_TEXT} x2" in capsys.readouterr().out


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_cropped_and_rotated_page(tmp_path: Path, rotation: int) -> None:
    src = tmp_path / "in.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=400, height=300)
        page.insert_text((150, 100), "Shown: keep this", fontsize=10)
        page.insert_text((150, 250), "Cropped: secret remark", fontsize=10)
        page.insert_text((10, 100), "Left: other remark", fontsize=10)
        page.set_cropbox(pymupdf.Rect(100, 50, 400, 200))
        page.set_rotation(rotation)
        doc.save(src)
    report = redact_pdf(src, tmp_path / "out.pdf")
    assert report.metadata.removed[OUTSIDE_TEXT] == 1
    text = all_text(report.output)
    assert "keep this" in text
    assert "secret remark" not in text and "other remark" not in text


def test_line_running_off_the_page_loses_only_the_hidden_part(tmp_path: Path) -> None:
    src = tmp_path / "in.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=120, height=200)
        page.insert_text((20, 100), "shown words then the hidden tail far outside", fontsize=10)
        doc.save(src)
    report = redact_pdf(src, tmp_path / "out.pdf")
    assert report.metadata.removed[OUTSIDE_TEXT] == 1
    text = all_text(report.output)
    assert text.strip().startswith("shown words")
    assert "hidden" not in text and "tail" not in text and "outside" not in text
    assert page_text(report.output) == page_text(src)  # what the page shows is unchanged


def unseen_pdf(path: Path) -> Path:
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 100), "normal text", fontsize=11)
        page.insert_text((72, 130), "invisible OCR layer", fontsize=11, render_mode=3)
        page.insert_text((72, 160), "outlined text", fontsize=11, render_mode=1)
        page.insert_text((72, 190), "transparent text", fontsize=11, fill_opacity=0)
        page.insert_text((72, 220), "faint text", fontsize=11, fill_opacity=0.5)
        # Invisible text between two visible parts of the same line.
        x = 72.0
        for part, mode in (("before ", 0), ("unseen", 3), (" after", 0)):
            page.insert_text((x, 260), part, fontsize=11, render_mode=mode)
            x += pymupdf.get_text_length(part, fontsize=11)
        page.draw_line((72, 133), (200, 133), color=(1, 0, 0), width=0.5)  # under the OCR line
        doc.save(path)
    return path


def test_invisible_text_is_removed_and_the_rest_kept(tmp_path: Path) -> None:
    with pymupdf.open(unseen_pdf(tmp_path / "in.pdf")) as doc:
        found = remove_unseen_text(doc)
        text = doc[0].get_text()
        drawings = doc[0].get_drawings()
    assert found == {INVISIBLE_TEXT: 1}
    for gone in ("invisible OCR layer", "transparent text", "unseen"):
        assert gone not in text
    for kept in ("normal text", "outlined text", "faint text", "before", "after"):
        assert kept in text
    assert len(drawings) == 1  # only text is removed


def test_invisible_text_is_kept_without_scrub(tmp_path: Path) -> None:
    src = unseen_pdf(tmp_path / "in.pdf")
    assert "invisible OCR layer" not in page_text(redact_pdf(src, tmp_path / "a.pdf").output)
    kept = redact_pdf(src, tmp_path / "b.pdf", scrub=False)
    assert "invisible OCR layer" in page_text(kept.output)
    assert not kept.metadata.removed[INVISIBLE_TEXT]


def test_other_commands_remove_text_outside_the_page_too(cropped_pdf: Path, tmp_path: Path) -> None:
    boxes = apply_boxes(cropped_pdf, tmp_path / "boxes.pdf")
    pages = remove_pages(cropped_pdf, "1", tmp_path / "pages.pdf")
    for report in (boxes, pages):
        assert report.metadata.removed[OUTSIDE_TEXT]
        assert "internal draft remark" not in all_text(report.output)


def links_pdf(path: Path) -> Path:
    """Links of the kinds PyMuPDF's own link removal stops at, then a web link."""
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 100), "Links: back, thread, website", fontsize=11)
        page_xref = page.xref
        doc.new_page()  # invalidates ``page``
        actions = (
            "<</S/Named/N/GoBack>>",
            "<</S/Thread/D 1>>",
            "<</S/URI/URI(https://intranet.example.org/people/jane-secret)>>",
            "<</S/GoTo/D[1 /Fit]>>",
            "<</S/Launch/F(C:/Users/jane.secret/draft.docx)>>",
        )
        refs = []
        for n, action in enumerate(actions):
            xref = doc.get_new_xref()
            rect = f"[72 {700 - 20 * n} 200 {715 - 20 * n}]"
            doc.update_object(xref, f"<</Type/Annot/Subtype/Link/Rect{rect}/A{action}>>")
            refs.append(f"{xref} 0 R")
        doc.xref_set_key(page_xref, "Annots", f"[{' '.join(refs)}]")
        doc.save(path)
    return path


def link_count(doc: pymupdf.Document) -> int:
    return sum(kind == pymupdf.PDF_ANNOT_LINK for page in doc for _, kind, _ in page.annot_xrefs())


def test_every_kind_of_link_is_removed(tmp_path: Path) -> None:
    src = links_pdf(tmp_path / "in.pdf")
    with pymupdf.open(src) as doc:
        assert link_count(doc) == 5
        assert remove_links(doc) == 5
        assert link_count(doc) == 0
    report = redact_pdf(src, tmp_path / "out.pdf")
    with pymupdf.open(report.output) as doc:
        assert link_count(doc) == 0
    everything = all_pdf_bytes(report.output)
    assert b"jane-secret" not in everything and b"jane.secret" not in everything


def test_links_are_kept_without_scrub(tmp_path: Path) -> None:
    report = redact_pdf(links_pdf(tmp_path / "in.pdf"), tmp_path / "out.pdf", scrub=False)
    with pymupdf.open(report.output) as doc:
        assert link_count(doc) == 5
