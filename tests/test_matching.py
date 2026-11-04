"""Position-based matching, Unicode case, word boundaries and email domains."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from redact_pdf import redact_pdf, verify_pdf
from redact_pdf.matching import email_domain_term, redact_text, term_matcher
from tests.conftest import page_text


def make_pdf(path: Path, *pages: list[str], spacing: int = 15, unicode: bool = False) -> Path:
    """One page per argument, one text line per string.

    ``unicode=True`` uses a font that can write combining accents (U+0308);
    the default Helvetica replaces them with another character.
    """
    with pymupdf.open() as doc:
        for lines in pages:
            page = doc.new_page()
            font = "helv"
            if unicode:
                font = "uni"
                page.insert_font(fontname=font, fontbuffer=pymupdf.Font("cjk").buffer)
            for n, line in enumerate(lines):
                page.insert_text((72, 100 + spacing * n), line, fontsize=12, fontname=font)
        doc.save(path)
    return path


def test_case_insensitive_beyond_ascii(tmp_path: Path) -> None:
    src = make_pdf(tmp_path / "in.pdf", ["Signed: MÜLLER, müller and Müller."])
    out = tmp_path / "out.pdf"
    report = redact_pdf(src, out, ["Müller"], emails=False, label=None)
    assert report.hits[1] == {"Müller": 3}
    assert "ller" not in page_text(out).lower()
    assert report.is_clean
    assert verify_pdf(src, ["müller"], emails=False) == {1: ["müller"]}


def test_regex_word_boundaries_are_kept(tmp_path: Path) -> None:
    src = make_pdf(tmp_path / "in.pdf", ["Ann wrote the Annual report for Ann."])
    out = tmp_path / "out.pdf"
    report = redact_pdf(src, out, patterns=[r"\bAnn\b"], emails=False, label=None)
    assert report.hits[1] == {"Ann": 2}
    assert "wrote the Annual report for" in page_text(out)


def test_regex_is_case_sensitive_as_written(tmp_path: Path) -> None:
    src = make_pdf(tmp_path / "in.pdf", ["ABC-123 and abc-456"])
    out = tmp_path / "out.pdf"
    report = redact_pdf(src, out, patterns=[r"ABC-\d+"], emails=False, label=None)
    assert report.hits[1] == {"ABC-123": 1}
    assert "abc-456" in page_text(out)


def test_term_across_line_break(tmp_path: Path) -> None:
    src = make_pdf(tmp_path / "in.pdf", ["Letter from John", "Smith, Berlin"])
    out = tmp_path / "out.pdf"
    report = redact_pdf(src, out, ["John Smith"], emails=False, label=None)
    assert report.hits[1] == {"John Smith": 1}
    text = page_text(out)
    assert "John" not in text and "Smith" not in text
    assert "Letter from" in text and "Berlin" in text


@pytest.mark.parametrize("spacing", [15, 24])  # 24: MuPDF splits the lines into two blocks
def test_hyphenated_term_across_lines(tmp_path: Path, spacing: int) -> None:
    src = make_pdf(tmp_path / "in.pdf", ["Contract with Mül-", "ler GmbH"], spacing=spacing)
    out = tmp_path / "out.pdf"
    report = redact_pdf(src, out, ["Müller"], emails=False, label=None)
    assert report.hits[1] == {"Müller": 1}
    text = page_text(out)
    assert "Mül" not in text and "ler" not in text
    assert "GmbH" in text


def test_neighbouring_text_survives(tmp_path: Path) -> None:
    src = make_pdf(tmp_path / "in.pdf", ["xJohn Smithx and more"])
    out = tmp_path / "out.pdf"
    redact_pdf(src, out, ["John Smith"], emails=False, label=None)
    assert "and more" in page_text(out)


def test_overlapping_matches_are_counted_once(tmp_path: Path) -> None:
    src = make_pdf(tmp_path / "in.pdf", ["Mail john.smith@acme.de now"])
    out = tmp_path / "out.pdf"
    report = redact_pdf(src, out, ["Smith"], email_domains=False)
    # "smith" lies inside the email; one box, one count
    assert report.hits[1] == {"john.smith@acme.de": 1}
    assert "Mail" in page_text(out) and "now" in page_text(out)


def test_redact_text_does_not_nest_labels() -> None:
    matchers = [term_matcher("act"), term_matcher("Smith")]
    text, counts = redact_text("Smith: REDACTED act", matchers, "REDACTED")
    assert text == "REDACTED: REDACTED REDACTED"
    assert counts == {"Smith": 1, "act": 1}


def test_whole_word_term() -> None:
    m = term_matcher("acme", whole_word_only=True)
    found = [x.group(0) for x in m.pattern.finditer("www.acme.com ACME-Group acmeplex _acme_")]
    assert found == ["acme", "ACME", "acme"]


@pytest.mark.parametrize(
    ("address", "term"),
    [
        ("john@apple.com", "apple"),
        ("John@Apple.COM", "apple"),
        ("a@mail.acme.co.uk", "acme"),
        ("a@acme.com.au", "acme"),
        ("a@uni-zuerich.ch", "uni-zuerich"),
        ("a@sub.dept.example-corp.de", "example-corp"),
        ("a@co.uk", "co"),
        ("a@gmail.com", None),
        ("a@web.de", None),
        ("a@me.com", None),
        ("a@bluewin.ch", None),
        ("a@example.org", None),
        ("a@x.com", None),
    ],
)
def test_email_domain_term(address: str, term: str | None) -> None:
    assert email_domain_term(address) == term


def test_domain_from_later_page_redacts_earlier_page(tmp_path: Path) -> None:
    src = make_pdf(
        tmp_path / "in.pdf",
        ["ACME-Group, see www.acme.com", "Acmeplex is unrelated"],
        ["Contact: jane@acme.com", "Private: joe@gmail.com about Gmail"],
    )
    out = tmp_path / "out.pdf"
    report = redact_pdf(src, out, label=None)
    assert report.domain_terms == {"acme": "jane@acme.com"}
    assert report.hits[1] == {"acme": 2}
    assert report.hits[2] == {"jane@acme.com": 1, "joe@gmail.com": 1}
    text = page_text(out)
    assert "acme" not in text.replace("Acmeplex", "").lower()
    assert "Acmeplex is unrelated" in text
    assert "about Gmail" in text  # mail providers are not redacted
    assert report.is_clean


def test_email_domains_can_be_disabled(tmp_path: Path) -> None:
    src = make_pdf(tmp_path / "in.pdf", ["ACME AG, jane@acme.com"])
    out = tmp_path / "out.pdf"
    report = redact_pdf(src, out, email_domains=False)
    assert report.domain_terms == {}
    assert "ACME AG" in page_text(out)


def test_domain_from_bookmark(tmp_path: Path) -> None:
    src = tmp_path / "in.pdf"
    with pymupdf.open() as doc:
        doc.new_page().insert_text((72, 72), "Initech quarterly report")
        doc.set_toc([[1, "Questions: bill@initech.com", 1]])
        doc.save(src)
    report = redact_pdf(src, tmp_path / "out.pdf")
    assert report.domain_terms == {"initech": "bill@initech.com"}
    assert report.hits[1] == {"initech": 1}


DECOMPOSED = "Körnich"  # "o" + combining diaeresis, looks like "Körnich"


@pytest.mark.parametrize(
    ("in_pdf", "term"),
    [("Körnich", "Körnich"), (DECOMPOSED, "Körnich"), ("Körnich", DECOMPOSED)],
    ids=["both-composed", "pdf-decomposed", "term-decomposed"],
)
def test_umlaut_in_either_unicode_form(tmp_path: Path, in_pdf: str, term: str) -> None:
    src = make_pdf(tmp_path / "in.pdf", [f"An: Lukas {in_pdf}, Bern"], unicode=True)
    assert DECOMPOSED not in in_pdf or DECOMPOSED in page_text(src)  # fixture check
    out = tmp_path / "out.pdf"
    report = redact_pdf(src, out, [f"Lukas {term}"], emails=False, label=None)
    assert sum(report.hits[1].values()) == 1
    assert "rnich" not in page_text(out)
    assert "An:" in page_text(out) and "Bern" in page_text(out)
    assert report.is_clean
