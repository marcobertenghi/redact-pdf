"""Image areas that no page shows are blanked (cropped images, images off the page)."""

from __future__ import annotations

from pathlib import Path

import pymupdf

from redact_pdf import HIDDEN_IMAGE, apply_boxes, hidden_image_pages, redact_pdf, verify_pdf
from redact_pdf.cli import EXIT_LEFTOVERS, EXIT_OK, main

GREY, RED, BLUE, GREEN = (230, 230, 240), (200, 0, 0), (0, 0, 200), (0, 160, 0)


def tall_image(alpha: bool = False) -> pymupdf.Pixmap:
    """200x800 grey image with a red mark at the top and a blue one at the bottom."""
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 800), alpha)
    extra = (255,) if alpha else ()
    pix.set_rect(pix.irect, GREY + extra)
    pix.set_rect(pymupdf.IRect(20, 100, 60, 140), RED + extra)
    pix.set_rect(pymupdf.IRect(20, 500, 60, 540), BLUE + extra)
    return pix


def colours(doc: pymupdf.Document, xref: int) -> set[tuple[int, ...]]:
    pix = pymupdf.Pixmap(doc, xref)
    return {pix.pixel(x, y)[:3] for x in range(0, pix.width, 4) for y in range(0, pix.height, 4)}


def images(path: Path) -> list[tuple[int, int]]:
    """``[(page number, xref), ...]`` of the images drawn in ``path``."""
    with pymupdf.open(path) as doc:
        return [(p.number + 1, img[0]) for p in doc for img in p.get_images(full=True)]


def split_screenshot(path: Path) -> Path:
    """One tall image split over two pages, each page holding a full copy
    and a box over the mark it shows: the layout of a long screenshot."""
    with pymupdf.open() as doc:
        for shift in (0, -400):
            page = doc.new_page(width=200, height=400)
            page.insert_image(pymupdf.Rect(0, shift, 200, shift + 800), pixmap=tall_image())
            page.draw_rect(pymupdf.Rect(15, 95, 65, 145), color=None, fill=(0, 0, 0))
        doc.save(path)
    return path


def cropped_image(path: Path, pix: pymupdf.Pixmap | None = None, jpeg: bytes = b"") -> Path:
    """The tall image placed on a page but clipped to its top half, as Word
    and browsers crop images; the blue mark is cut off, yet stored."""
    with pymupdf.open() as doc:
        page = doc.new_page(width=400, height=600)
        page.insert_text((50, 40), "Contact: alice@example.com", fontsize=11)
        where = pymupdf.Rect(100, 100, 200, 500)
        if jpeg:
            page.insert_image(where, stream=jpeg)
        else:
            page.insert_image(where, pixmap=pix or tall_image())
        xref = page.get_contents()[-1]
        stream = doc.xref_stream(xref)
        clip = f"q 100 {600 - 300} 100 200 re W n ".encode()  # page y 100..300
        doc.update_stream(xref, clip + stream + b" Q")
        doc.save(path)
    return path


def test_split_screenshot_both_copies_are_blanked(tmp_path: Path) -> None:
    report = apply_boxes(split_screenshot(tmp_path / "in.pdf"), tmp_path / "out.pdf")
    assert report.total == 2
    assert report.hidden_images == 2
    assert report.is_clean
    with pymupdf.open(report.output) as doc:
        for _, xref in images(report.output):
            seen = colours(doc, xref)
            assert RED not in seen and BLUE not in seen  # neither copy keeps either mark
            assert GREY in seen


def test_cropped_image_is_blanked_by_redact(tmp_path: Path) -> None:
    report = redact_pdf(cropped_image(tmp_path / "in.pdf"), tmp_path / "out.pdf")
    assert report.hidden_images == 1
    assert report.is_clean
    [(_, xref)] = images(report.output)
    with pymupdf.open(report.output) as doc:
        seen = colours(doc, xref)
    assert RED in seen  # shown on the page: kept
    assert BLUE not in seen  # cropped off: gone


def test_visible_part_is_unchanged(tmp_path: Path) -> None:
    src = cropped_image(tmp_path / "in.pdf")
    redact_pdf(src, tmp_path / "out.pdf")
    with pymupdf.open(src) as before, pymupdf.open(tmp_path / "out.pdf") as after:
        old = pymupdf.Pixmap(before, images(src)[0][1])
        new = pymupdf.Pixmap(after, images(tmp_path / "out.pdf")[0][1])
        shown = pymupdf.IRect(0, 0, 200, 400)  # the top half of the image is on the page
        for x in range(0, 200, 3):
            for y in range(shown.y0, shown.y1, 3):
                assert old.pixel(x, y) == new.pixel(x, y)


def test_verify_reports_hidden_image_content(tmp_path: Path) -> None:
    src = cropped_image(tmp_path / "in.pdf")
    assert verify_pdf(src, ["nothing-to-find"], emails=False) == {1: [HIDDEN_IMAGE]}
    report = redact_pdf(src, tmp_path / "out.pdf")
    assert verify_pdf(report.output, ["nothing-to-find"], emails=False) == {}


def test_verify_cli(tmp_path: Path) -> None:
    src = cropped_image(tmp_path / "in.pdf")
    assert main(["verify", str(src), "-t", "nothing-to-find", "--no-email"]) == EXIT_LEFTOVERS
    redact_pdf(src, tmp_path / "out.pdf")
    assert main(["verify", str(tmp_path / "out.pdf"), "-t", "x-y-z", "--no-email"]) == EXIT_OK


def test_fully_visible_image_is_not_rewritten(tmp_path: Path) -> None:
    src = tmp_path / "in.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((50, 40), "Contact: alice@example.com", fontsize=11)
        page.insert_image(pymupdf.Rect(100, 100, 200, 500), pixmap=tall_image())
        doc.save(src)
    report = redact_pdf(src, tmp_path / "out.pdf")
    assert report.hidden_images == 0
    with pymupdf.open(report.output) as doc:
        assert {BLUE, RED} <= colours(doc, images(report.output)[0][1])


def test_jpeg_stays_jpeg(tmp_path: Path) -> None:
    jpeg = tall_image().tobytes("jpeg", jpg_quality=95)
    src = cropped_image(tmp_path / "in.pdf", jpeg=jpeg)
    with pymupdf.open(src) as doc:
        assert doc.xref_get_key(images(src)[0][1], "Filter")[1] == "/DCTDecode"
        assert hidden_image_pages(doc) == {1: 1}
    report = redact_pdf(src, tmp_path / "out.pdf")
    assert report.hidden_images == 1
    with pymupdf.open(report.output) as doc:
        xref = images(report.output)[0][1]
        assert doc.xref_get_key(xref, "Filter")[1] == "/DCTDecode"
        assert hidden_image_pages(doc) == {}  # JPEG noise at the edge is not content
        pix = pymupdf.Pixmap(doc, xref)
        assert pix.pixel(40, 520) == (255, 255, 255)  # the cropped-off blue mark


def test_transparency_mask_is_blanked_too(tmp_path: Path) -> None:
    pix = tall_image(alpha=True)
    pix.set_rect(pymupdf.IRect(100, 600, 180, 700), (*GREEN, 40))  # a shape only in alpha
    report = redact_pdf(cropped_image(tmp_path / "in.pdf", pix), tmp_path / "out.pdf")
    assert report.hidden_images == 1
    with pymupdf.open(report.output) as doc:
        xref = images(report.output)[0][1]
        smask = int(doc.xref_get_key(xref, "SMask")[1].split()[0])
        alpha = pymupdf.Pixmap(doc, smask)
        hidden = {alpha.pixel(x, y) for x in range(0, 200, 4) for y in range(420, 800, 4)}
        assert len(hidden) == 1  # the shape is gone from the mask


def test_rotated_placement_keeps_what_the_page_shows(tmp_path: Path) -> None:
    src = tmp_path / "in.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=300, height=300)
        page.insert_text((20, 20), "Contact: alice@example.com", fontsize=11)
        # Rotated a quarter turn and running off the right edge of the page.
        page.insert_image(pymupdf.Rect(0, 100, 800, 300), pixmap=tall_image(), rotate=90)
        doc.save(src)
        shown = {
            px
            for pix in [doc[0].get_pixmap()]
            for px in (pix.pixel(x, y) for x in range(0, 300, 2) for y in range(100, 300, 2))
        }
    assert RED in shown or BLUE in shown  # one mark is on the page...
    report = redact_pdf(src, tmp_path / "out.pdf")
    assert report.hidden_images == 1
    with pymupdf.open(report.output) as doc:
        kept = colours(doc, images(report.output)[0][1])
    for mark in (RED, BLUE):
        assert (mark in kept) == (mark in shown)  # ...and only that one is kept


def test_colour_profile_is_kept(tmp_path: Path) -> None:
    src = cropped_image(tmp_path / "in.pdf")
    with pymupdf.open(src) as doc:
        before = doc.xref_get_key(images(src)[0][1], "ColorSpace")
    report = redact_pdf(src, tmp_path / "out.pdf")
    assert report.hidden_images == 1
    with pymupdf.open(report.output) as doc:
        space = doc.xref_get_key(images(report.output)[0][1], "ColorSpace")
        assert space[0] == before[0]  # still the ICC profile, not plain RGB
        assert "ICCBased" in (
            doc.xref_object(int(space[1].split()[0])) if space[0] == "xref" else space[1]
        )


def test_plain_hidden_margin_is_not_rewritten(tmp_path: Path) -> None:
    pix = tall_image()
    pix.set_rect(pymupdf.IRect(0, 400, 200, 800), (255, 255, 255))  # nothing below the crop
    report = redact_pdf(cropped_image(tmp_path / "in.pdf", pix), tmp_path / "out.pdf")
    assert report.hidden_images == 0


def test_text_outside_the_page_does_not_confuse_image_areas(tmp_path: Path) -> None:
    """Off-page text shifts PyMuPDF's block numbers; each image must still be
    judged by its own visible area. Both images here are fully shown."""
    src = tmp_path / "in.pdf"
    red = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 40), False)
    red.set_rect(red.irect, RED)
    red.set_rect(pymupdf.IRect(8, 8, 28, 28), GREEN)  # not one plain colour
    blue = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 50, 80), False)
    blue.set_rect(blue.irect, BLUE)
    blue.set_rect(pymupdf.IRect(12, 40, 36, 72), GREEN)
    with pymupdf.open() as doc:
        page = doc.new_page(width=400, height=400)
        page.insert_text((-300, 100), "parked beside the page", fontsize=10)
        page.insert_text((50, 40), "Contact: alice@example.com", fontsize=11)
        page.insert_image(pymupdf.Rect(50, 100, 170, 180), pixmap=red)
        page.insert_image(pymupdf.Rect(220, 200, 320, 360), pixmap=blue)
        doc.save(src)
    report = redact_pdf(src, tmp_path / "out.pdf")
    assert report.hidden_images == 0
    assert report.is_clean
    with pymupdf.open(report.output) as doc:
        seen = [colours(doc, xref) for _, xref in images(report.output)]
    assert sorted(seen, key=sorted) == sorted([{RED, GREEN}, {BLUE, GREEN}], key=sorted)
