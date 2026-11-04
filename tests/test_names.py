"""Person names taken from email addresses."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from redact_pdf import redact_pdf
from redact_pdf.names import display_names, email_name_terms, loose_matcher
from tests.conftest import page_text

DECOMPOSED = "Ko\u0308rnich"  # "o" + combining diaeresis

HEADER = [
    "Gesendet: Mittwoch, 20. Dezember 2023 11:12",
    "An: Lukas Körnich <lukas.koernich@acme-sec.ch>",  # decomposed umlaut
    "Cc: Fabio Bally <fb@acme-sec.ch>; Finn Ross",
]
NEXT_PAGE = [
    "<finn.ross@acme-sec.ch>",  # address continues on the next page
    "Betreff: FIX RoE",
    "Hallo Lukas, danke. Herr Körnich und Fabio sind informiert.",
    "Gruss, Finn. PS: Rossi und Fabiola bleiben, ebenso Support.",
]


def write_pages(path: Path, *pages: list[str]) -> Path:
    with pymupdf.open() as doc:
        for n, lines in enumerate(pages):
            page = doc.new_page()
            # A font that can write the combining accent in HEADER
            page.insert_font(fontname="uni", fontbuffer=pymupdf.Font("cjk").buffer)
            for i, line in enumerate(lines):
                page.insert_text((50, 60 + 18 * i), line, fontsize=10, fontname="uni")
            page.insert_text((300, 800), str(n + 15), fontsize=9)  # page number
        doc.save(path)
    return path


def test_names_from_email_header_are_redacted_everywhere(tmp_path: Path) -> None:
    src = write_pages(tmp_path / "mail.pdf", HEADER, NEXT_PAGE)
    out = tmp_path / "out.pdf"
    assert DECOMPOSED in page_text(src)  # fixture check
    report = redact_pdf(src, out, label=None)
    text = page_text(out)
    for name in ("Lukas", "rnich", "Fabio Bally", "Finn", "Ross <"):
        assert name not in text, name
    assert "Rossi und Fabiola bleiben, ebenso Support." in text
    assert "Betreff: FIX RoE" in text
    assert report.name_terms["finn.ross@acme-sec.ch"] == ["Finn Ross", "Finn", "Ross"]
    assert report.is_clean


def test_names_can_be_disabled(tmp_path: Path) -> None:
    src = write_pages(tmp_path / "mail.pdf", HEADER, NEXT_PAGE)
    out = tmp_path / "out.pdf"
    report = redact_pdf(src, out, email_names=False)
    assert report.name_terms == {}
    assert "Hallo Lukas" in page_text(out)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("An: Lukas Körnich <lk@x.ch>", ("Lukas Körnich", "lk@x.ch")),
        ("Cc: a@x.ch; Ales Hartmann (ah@x.ch)", ("Ales Hartmann", "ah@x.ch")),
        ('"Meier, Anna" <am@x.ch>', ("Meier Anna", "am@x.ch")),
        ("Finn Ross\n15\n<fr@x.ch>", ("Finn Ross", "fr@x.ch")),
        ("Alberto Ornaghi (ALoR) <alor@x.net>", ("Alberto Ornaghi", "alor@x.net")),
    ],
)
def test_display_names(text: str, expected: tuple[str, str]) -> None:
    assert list(display_names(text)) == [expected]


def test_no_display_name_without_brackets() -> None:
    assert list(display_names("Write to Anna at am@x.ch today")) == []


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("Dr. Hans Muster <hans@x.ch>", ["Hans Muster", "Hans", "Muster"]),
        ("Support <support@x.ch>", []),  # role account
        ("Buchhaltung <rechnung@x.ch>", []),  # single word not in the address
        ("Anna <anna@x.ch>", ["Anna"]),  # single word confirmed by the address
        ("Kontaktieren Sie Herrn Müller <mueller@x.ch>", ["mueller"]),  # sentence: address only
        ("info@x.ch, noreply@x.ch", []),
        ("jürgen.weiß@x.de", ["juergen weiss", "juergen", "weiss"]),
        ("j.doe@x.com", ["doe"]),
        ("ettercap-betatesting@lists.sourceforge.net", []),  # mailing list
        ("project-users@x.org", []),
    ],
)
def test_email_name_terms(text: str, names: list[str]) -> None:
    found = [n for per_address in email_name_terms(text).by_address.values() for n in per_address]
    assert found == names


def test_loose_matcher() -> None:
    m = loose_matcher("koernich").pattern
    found = m.findall(f"Körnich, {DECOMPOSED}, KOERNICH, Kornich; Koernichs xKoernich")
    assert len(found) == 4
    assert loose_matcher("weiss").pattern.findall("Weiß WEISS weiss Weissach") == [
        "Weiß", "WEISS", "weiss",
    ]  # fmt: skip


DECOMPOSED = "Körnich"


def test_address_names_need_a_capital_letter(tmp_path: Path) -> None:
    src = write_pages(tmp_path / "in.pdf", ["Mail format_c@x.de. Format C:", "The format is old."])
    out = tmp_path / "out.pdf"
    report = redact_pdf(src, out, label=None)
    assert report.hits[1]["format"] == 1  # "Format" at the line start
    assert "The format is old." in page_text(out)
