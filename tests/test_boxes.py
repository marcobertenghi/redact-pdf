"""apply-boxes: boxes drawn during review become real redactions."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pymupdf
import pytest

from redact_pdf import apply_boxes, redact_pdf
from redact_pdf.cli import EXIT_LEFTOVERS, EXIT_OK, main
from tests.conftest import page_text

BLACK, WHITE, RED = (0, 0, 0), (1, 1, 1), (1, 0, 0)
NAME_LINE = "Patient: Lukas Koernich, born 1980"
NAME = pymupdf.Rect(122, 88, 206, 104)  # covers "Lukas Koernich" on NAME_LINE


def reviewed_pdf(path: Path, draw: Callable[[pymupdf.Page], None]) -> Path:
    """A page with text, an image and a header bar, then ``draw`` on top:
    the painting order of a Xournal++ export."""
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.draw_rect(pymupdf.Rect(50, 40, 545, 70), color=None, fill=BLACK)
        page.insert_text((60, 60), "HEADER ON A BLACK BAR", fontsize=14, color=WHITE)
        page.insert_text((72, 100), NAME_LINE, fontsize=12)
        page.insert_text((72, 130), "Keep this sentence.", fontsize=12)
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 100), False)
        pix.set_rect(pix.irect, (40, 140, 220))
        page.insert_image(pymupdf.Rect(72, 200, 272, 300), pixmap=pix)
        draw(page)
        doc.set_metadata({"author": "Reviewer", "producer": "xournalpp"})
        doc.save(path)
    return path


def box(
    rect: pymupdf.Rect, fill: tuple[float, ...] = BLACK, **kw: object
) -> Callable[[pymupdf.Page], None]:
    kw.setdefault("color", BLACK)
    return lambda page: page.draw_rect(rect, fill=fill, **kw)


def test_drawn_box_removes_text_under_it(tmp_path: Path) -> None:
    src = reviewed_pdf(tmp_path / "in.pdf", box(NAME))
    report = apply_boxes(src)
    assert report.output == tmp_path / "in_FINAL.pdf"
    assert report.drawn == {1: 1}
    assert report.is_clean
    text = page_text(report.output)
    assert "Lukas" not in text and "Koernich" not in text
    assert "Patient:" in text and "born 1980" in text and "Keep this sentence." in text
    assert "HEADER ON A BLACK BAR" in text  # design element, not a box
    with pymupdf.open(report.output) as doc:
        assert not doc.metadata["author"] and not doc.metadata["producer"]


def test_whiteout_box(tmp_path: Path) -> None:
    src = reviewed_pdf(tmp_path / "in.pdf", box(NAME, fill=WHITE, color=None))
    report = apply_boxes(src, tmp_path / "out.pdf")
    assert report.drawn == {1: 1}
    assert "Lukas" not in page_text(report.output)
    with pymupdf.open(report.output) as doc:  # the area stays white
        pix = doc[0].get_pixmap(clip=NAME)
        assert pix.pixel(pix.width // 2, pix.height // 2) == (255, 255, 255)


@pytest.mark.parametrize("annotations_only", [False, True])
def test_image_pixels_under_box_are_removed(tmp_path: Path, annotations_only: bool) -> None:
    rect = pymupdf.Rect(72, 200, 172, 300)

    def mark(page: pymupdf.Page) -> None:
        if annotations_only:
            ink = page.add_ink_annot([[(80, 250), (164, 250)]])
            ink.set_border(width=16)
            ink.update()
        else:
            page.draw_rect(rect, fill=BLACK, color=BLACK)

    src = reviewed_pdf(tmp_path / "in.pdf", mark)
    report = apply_boxes(src, tmp_path / "out.pdf", annotations_only=annotations_only)
    assert report.total == 1
    assert report.is_clean
    with pymupdf.open(report.output) as doc:
        xref = doc[0].get_images()[0][0]
        image = pymupdf.Pixmap(doc, xref)  # the image data itself, not the rendering
        assert image.pixel(20, 50) != (40, 140, 220)  # under the box or ink stroke
        assert image.pixel(180, 50) == (40, 140, 220)  # outside it


def test_vector_drawing_under_box_is_removed(tmp_path: Path) -> None:
    def draw(page: pymupdf.Page) -> None:
        page.draw_polyline([(80, 400), (120, 380), (160, 410)], color=RED, width=2)  # signature
        page.draw_rect(pymupdf.Rect(70, 370, 170, 420), color=None, fill=BLACK)

    report = apply_boxes(reviewed_pdf(tmp_path / "in.pdf", draw), tmp_path / "out.pdf")
    assert report.drawn == {1: 1}
    with pymupdf.open(report.output) as doc:
        assert not [d for d in doc[0].get_drawings() if d.get("color") == RED]


def test_filled_ellipse_is_applied_as_its_bounding_box(tmp_path: Path) -> None:
    src = reviewed_pdf(tmp_path / "in.pdf", lambda p: p.draw_oval(NAME, color=None, fill=BLACK))
    report = apply_boxes(src, tmp_path / "out.pdf")
    assert report.drawn == {1: 1}
    assert "Koernich" not in page_text(report.output)


@pytest.mark.parametrize(
    "draw",
    [
        box(NAME, fill_opacity=0.5),  # see-through box
        lambda p: p.draw_line((116, 96), (212, 96), color=(1, 1, 0), width=12, stroke_opacity=0.5),
        lambda p: p.draw_polyline(
            [(116, 90), (140, 102), (164, 90), (188, 102), (212, 90)], color=BLACK, width=6
        ),  # pen scribble
    ],
    ids=["see-through", "highlighter", "pen"],
)
def test_shapes_that_do_not_hide_text_are_reported(
    tmp_path: Path, draw: Callable[[pymupdf.Page], None]
) -> None:
    report = apply_boxes(reviewed_pdf(tmp_path / "in.pdf", draw), tmp_path / "out.pdf")
    assert report.total == 0
    assert report.ignored == {1: 1}
    assert not report.is_clean
    assert "Lukas Koernich" in page_text(report.output)


def test_page_without_boxes_is_reported(tmp_path: Path) -> None:
    report = apply_boxes(reviewed_pdf(tmp_path / "in.pdf", lambda p: None), tmp_path / "out.pdf")
    assert report.total == 0 and not report.ignored
    assert not report.is_clean
    assert NAME_LINE in page_text(report.output)


def test_box_on_rotated_page(tmp_path: Path) -> None:
    def draw(page: pymupdf.Page) -> None:
        page.draw_rect(NAME, color=None, fill=BLACK)
        page.set_rotation(90)

    report = apply_boxes(reviewed_pdf(tmp_path / "in.pdf", draw), tmp_path / "out.pdf")
    assert report.drawn == {1: 1}
    assert report.is_clean
    assert "Koernich" not in page_text(report.output)


def annotated_pdf(path: Path) -> Path:
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 100), NAME_LINE, fontsize=12)
        page.insert_text((72, 130), "Highlighted: Fabio Bally", fontsize=12)
        page.insert_text((72, 160), "Inked: Finn Ross", fontsize=12)
        page.insert_text((72, 190), "Untouched line stays.", fontsize=12)
        square = page.add_rect_annot(NAME)
        square.set_colors(stroke=RED)
        square.update()
        page.add_highlight_annot(page.search_for("Fabio Bally")[0])
        page.add_ink_annot([[(118, 152), (160, 160), (170, 152)]])
        note = page.add_text_annot((400, 100), "Reviewer comment: check page 2")
        note.set_info(title="Marco")
        doc.save(path)
    return path


@pytest.mark.parametrize("annotations_only", [False, True])
def test_annotations_are_applied(tmp_path: Path, annotations_only: bool) -> None:
    report = apply_boxes(
        annotated_pdf(tmp_path / "in.pdf"), tmp_path / "out.pdf", annotations_only=annotations_only
    )
    assert report.annotations == {1: 3}  # square, highlight, ink; not the sticky note
    assert report.is_clean
    text = page_text(report.output)
    for gone in ("Koernich", "Fabio", "Finn"):
        assert gone not in text
    assert "Highlighted:" in text and "Untouched line stays." in text
    with pymupdf.open(report.output) as doc:
        assert not list(doc[0].annots())  # the sticky note is merged into the page
        assert b"Reviewer comment" not in doc.tobytes()


def test_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = reviewed_pdf(tmp_path / "in.pdf", box(NAME))
    assert main(["apply-boxes", str(src)]) == EXIT_OK
    assert "1 box(es) applied" in capsys.readouterr().out
    assert (tmp_path / "in_FINAL.pdf").exists()

    plain = reviewed_pdf(tmp_path / "plain.pdf", lambda p: None)
    assert main(["apply-boxes", str(plain)]) == EXIT_LEFTOVERS
    assert "no boxes found" in capsys.readouterr().out


def test_table_border_through_box_is_kept(tmp_path: Path) -> None:
    def draw(page: pymupdf.Page) -> None:
        page.draw_line((50, 96), (545, 96), color=RED, width=0.5)  # long rule through the name
        page.draw_rect(NAME, color=None, fill=BLACK)

    report = apply_boxes(reviewed_pdf(tmp_path / "in.pdf", draw), tmp_path / "out.pdf")
    assert report.drawn == {1: 1}
    assert "Koernich" not in page_text(report.output)
    with pymupdf.open(report.output) as doc:
        assert [d for d in doc[0].get_drawings() if d.get("color") == RED]


def test_opaque_text_box_over_text_is_applied(tmp_path: Path) -> None:
    src = tmp_path / "in.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 100), NAME_LINE, fontsize=12)
        note = page.add_freetext_annot(NAME, "XXXX", fill_color=WHITE, text_color=BLACK)
        note.update()
        doc.save(src)
    report = apply_boxes(src, tmp_path / "out.pdf")
    assert report.drawn == {1: 1}  # merged into the page, then found as a cover
    text = page_text(report.output)
    assert "Koernich" not in text and "Patient:" in text


@pytest.mark.parametrize("annotations_only", [False, True])
def test_pen_strokes_are_applied_one_by_one(tmp_path: Path, annotations_only: bool) -> None:
    """Two blots drawn in one go (one ink annotation, as Firefox saves them)
    only take what they paint, not every line between them."""
    src = tmp_path / "in.pdf"
    lines = [
        "Patient: Lukas Koernich",
        "Keep this line one.",
        "Keep this line two.",
        "Signed: Finn Ross",
    ]
    with pymupdf.open() as doc:
        page = doc.new_page()
        for i, line in enumerate(lines):
            page.insert_text((72, 100 + 20 * i), line, fontsize=12)

        def blot(x0: int, x1: int, y: int) -> list[tuple[int, int]]:
            return [(x, y + (4 if i % 2 else -4)) for i, x in enumerate(range(x0, x1, 5))]

        ink = page.add_ink_annot([blot(124, 205, 96), blot(124, 175, 156)])
        ink.set_border(width=8)
        ink.update()
        doc.save(src)
    report = apply_boxes(src, tmp_path / "out.pdf", annotations_only=annotations_only)
    assert report.annotations == {1: 2}  # one box per stroke
    assert report.is_clean
    text = page_text(report.output)
    assert "Koernich" not in text and "Finn" not in text
    assert "Keep this line one." in text and "Keep this line two." in text
    assert "Patient:" in text and "Signed:" in text


def test_annotations_only_after_automatic_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "in.pdf"
    with pymupdf.open() as doc:
        for _ in range(2):
            page = doc.new_page()
            page.insert_text((72, 70), "PrivateOrganisation public information", fontsize=12)
            page.insert_text((72, 100), NAME_LINE, fontsize=12)
        doc.save(src)
    automatic = redact_pdf(src, tmp_path / "auto.pdf", ["PrivateOrganisation"], emails=False)
    assert automatic.is_clean
    with pymupdf.open(automatic.output) as doc:
        ink = doc[0].add_ink_annot([[(126, 96), (202, 96)]])
        ink.set_border(width=8)
        ink.update()
        doc.save(tmp_path / "reviewed.pdf")

    def no_drawing_scan(page: pymupdf.Page) -> None:
        pytest.fail("Annotations-only processing must skip drawn-box detection")

    monkeypatch.setattr("redact_pdf.boxes._drawn_boxes", no_drawing_scan)
    report = apply_boxes(tmp_path / "reviewed.pdf", tmp_path / "out.pdf", annotations_only=True)
    assert report.is_clean
    assert report.drawn == {} and report.annotations == {1: 1}
    with pymupdf.open(report.output) as doc:
        assert "Koernich" not in doc[0].get_text()
        assert NAME_LINE in doc[1].get_text()  # the page without new marks is kept
        for page in doc:
            assert "REDACTED" in page.get_text()
            assert "public information" in page.get_text()
            assert "PrivateOrganisation" not in page.get_text()


def test_annotations_only_cli_does_not_apply_page_drawings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = reviewed_pdf(tmp_path / "in.pdf", box(NAME))
    assert main(["apply-boxes", str(src), "--annotations-only"]) == EXIT_LEFTOVERS
    assert "Koernich" in page_text(tmp_path / "in_FINAL.pdf")
    output = capsys.readouterr().out
    assert "no annotation boxes found" in output
    assert "rerun without --annotations-only" in output
    # The default still applies page drawings.
    assert main(["apply-boxes", str(src)]) == EXIT_OK
    assert "Koernich" not in page_text(tmp_path / "in_FINAL.pdf")


def test_annotations_only_cli(tmp_path: Path) -> None:
    src = annotated_pdf(tmp_path / "in.pdf")
    assert main(["apply-boxes", str(src), "--annotations-only"]) == EXIT_OK
    assert "Koernich" not in page_text(tmp_path / "in_FINAL.pdf")
