from __future__ import annotations

import time
from pathlib import Path

import pymupdf
import pytest

from redact_pdf import (
    EMAIL_PATTERN,
    RedactionError,
    default_output_path,
    flatten_pdf,
    load_terms,
    redact_pdf,
    verify_pdf,
)
from tests.conftest import page_text

PHONE = r"\+\d{2}(?: \d+)+"


def test_terms_are_removed_from_text_layer(sample_pdf: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.pdf"
    report = redact_pdf(sample_pdf, out, ["John Smith", "ACME GmbH"])

    text = page_text(out).lower()
    assert "john smith" not in text
    assert "acme gmbh" not in text
    assert "contract between" in text  # surrounding text is kept
    assert report.is_clean
    assert report.hits[1]["John Smith"] == 3  # case-insensitive, incl. text on image


def test_label_is_inserted(sample_pdf: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.pdf"
    redact_pdf(sample_pdf, out, ["ACME GmbH"], label="XXX")
    assert "XXX" in page_text(out)


def test_no_label_draws_plain_boxes(sample_pdf: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.pdf"
    redact_pdf(sample_pdf, out, ["ACME GmbH"], label=None)
    assert "REDACTED" not in page_text(out)


def test_regex_patterns(sample_pdf: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.pdf"
    report = redact_pdf(sample_pdf, out, patterns=[PHONE], emails=False)
    assert "1234567" not in page_text(out)
    assert "+49 30 1234567" in report.hits[1]


def test_emails_are_redacted_by_default(sample_pdf: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.pdf"
    report = redact_pdf(sample_pdf, out)  # no terms needed
    assert "@" not in page_text(out)
    assert report.hits[1] == {"john.smith@acme.de": 1}
    assert report.is_clean


def test_emails_can_be_kept(sample_pdf: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.pdf"
    redact_pdf(sample_pdf, out, ["ACME GmbH"], emails=False)
    assert "john.smith@acme.de" in page_text(out)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("mail john.smith@acme.de today", "john.smith@acme.de"),
        ("first.last+tag@mail.example.co.uk", "first.last+tag@mail.example.co.uk"),
        ("j_doe-1@my-company.io", "j_doe-1@my-company.io"),
        ("jürgen.müller@beispiel.de", "jürgen.müller@beispiel.de"),
        ("UPPER@EXAMPLE.COM", "UPPER@EXAMPLE.COM"),
        ("(contact: info@example.org).", "info@example.org"),
        ("<info@example.org>", "info@example.org"),
        ("mailto:info@example.org", "info@example.org"),
        ("end of sentence a@b.de.", "a@b.de"),
        ("ellipsis...name@example.com", "name@example.com"),
    ],
)
def test_email_pattern_matches(text: str, expected: str) -> None:
    assert [m.group(0) for m in EMAIL_PATTERN.finditer(text)] == [expected]


@pytest.mark.parametrize(
    "text",
    [
        "@handle",
        "user@localhost",
        "1.2@3.4",
        "no-at-sign.example.com",
        "a@b",
        "a@.com",
        "a@b..com",
    ],
)
def test_email_pattern_rejects(text: str) -> None:
    assert EMAIL_PATTERN.search(text) is None


def test_multiple_emails_on_one_line() -> None:
    text = "cc: a@x.de, b@y.de; c@z.de"
    assert [m.group(0) for m in EMAIL_PATTERN.finditer(text)] == ["a@x.de", "b@y.de", "c@z.de"]


def test_metadata_is_removed(sample_pdf: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.pdf"
    redact_pdf(sample_pdf, out, ["ACME"])
    with pymupdf.open(out) as doc:
        assert not doc.metadata["author"]
        assert not doc.metadata["title"]


def test_image_pages_are_reported(sample_pdf: Path, tmp_path: Path) -> None:
    report = redact_pdf(sample_pdf, tmp_path / "out.pdf", ["ACME"])
    assert report.image_pages == {1: 1}


def test_longer_terms_win(sample_pdf: Path, tmp_path: Path) -> None:
    report = redact_pdf(sample_pdf, tmp_path / "out.pdf", ["Smith", "John Smith"])
    assert "John Smith" in report.hits[1]
    assert report.hits[2] == {"Smith": 1}


def test_term_inside_label_is_not_a_leftover(sample_pdf: Path, tmp_path: Path) -> None:
    # "act" occurs in "REDACTED"; it must not be reported as remaining.
    report = redact_pdf(sample_pdf, tmp_path / "out.pdf", ["act"])
    assert report.is_clean


def test_input_is_not_modified(sample_pdf: Path, tmp_path: Path) -> None:
    before = sample_pdf.read_bytes()
    redact_pdf(sample_pdf, tmp_path / "out.pdf", ["John Smith"])
    assert sample_pdf.read_bytes() == before


def test_refuses_to_overwrite_input(sample_pdf: Path) -> None:
    with pytest.raises(RedactionError):
        redact_pdf(sample_pdf, sample_pdf, ["John"])


def test_requires_terms_or_patterns(sample_pdf: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        redact_pdf(sample_pdf, tmp_path / "out.pdf", emails=False)


def test_missing_input(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        redact_pdf(tmp_path / "nope.pdf", tmp_path / "out.pdf", ["x"])


def test_encrypted_input_is_rejected(sample_pdf: Path, tmp_path: Path) -> None:
    locked = tmp_path / "locked.pdf"
    with pymupdf.open(sample_pdf) as doc:
        doc.save(locked, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
    with pytest.raises(RedactionError, match="password"):
        redact_pdf(locked, tmp_path / "out.pdf", ["John"])


def test_verify_finds_terms(sample_pdf: Path) -> None:
    found = verify_pdf(sample_pdf, ["John Smith"], emails=False)
    assert found == {1: ["John Smith"]}


def test_verify_checks_emails_by_default(sample_pdf: Path) -> None:
    assert verify_pdf(sample_pdf) == {1: ["john.smith@acme.de"]}


def test_flatten_removes_text_layer(sample_pdf: Path, tmp_path: Path) -> None:
    out = tmp_path / "flat.pdf"
    flatten_pdf(sample_pdf, out, dpi=72)
    assert page_text(out).strip() == ""
    with pymupdf.open(out) as doc:
        assert doc.page_count == 2


def test_flatten_rejects_bad_dpi(sample_pdf: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        flatten_pdf(sample_pdf, tmp_path / "flat.pdf", dpi=5)


def test_load_terms(tmp_path: Path) -> None:
    f = tmp_path / "terms.txt"
    f.write_text("# comment\nJohn Smith\n\n  ACME  \n", encoding="utf-8")
    assert load_terms(f) == ["John Smith", "ACME"]


@pytest.mark.parametrize("text", ["a." * 100_000, "a@" + "b." * 100_000, "-" * 200_000])
def test_email_pattern_is_linear_time(text: str) -> None:
    # Regression test: an unbounded pattern took minutes on such input.
    start = time.perf_counter()
    list(EMAIL_PATTERN.finditer(text))
    assert time.perf_counter() - start < 2


def test_default_output_path(sample_pdf: Path) -> None:
    report = redact_pdf(sample_pdf, terms=["John Smith"])
    assert report.output == sample_pdf.with_name("sample_REDACTED.pdf")
    assert report.output.is_file()


def test_default_output_path_helper() -> None:
    assert default_output_path("a/b/c.pdf") == Path("a/b/c_REDACTED.pdf")
    assert default_output_path("c.PDF", "_X") == Path("c_X.pdf")


def test_flatten_default_output(sample_pdf: Path) -> None:
    assert flatten_pdf(sample_pdf, dpi=72) == sample_pdf.with_name("sample_FLATTENED.pdf")
