"""Redaction labels say what was removed: REDACTED, EMAIL, NAME or ORG."""

from __future__ import annotations

import re
from pathlib import Path

import pymupdf
import pytest

from redact_pdf import redact_pdf, verify_pdf
from redact_pdf.cli import EXIT_OK, main
from redact_pdf.core import KIND_LABELS
from redact_pdf.matching import (
    EMAIL,
    NAME,
    ORG,
    TERM,
    Matcher,
    build_matchers,
    find_in_text,
    redact_text,
    select,
    term_matcher,
)
from tests.conftest import page_text

LINES = [
    "From: Lukas Meier <lukas.meier@acme.ch>",
    "Lukas works for ACME AG on Project Falcon.",
    "Nothing else to hide here.",
]


@pytest.fixture
def letter(tmp_path: Path) -> Path:
    path = tmp_path / "letter.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        for n, line in enumerate(LINES):
            page.insert_text((72, 100 + 30 * n), line, fontsize=12)
        doc.set_toc([[1, "Mail from lukas.meier@acme.ch", 1], [1, "Falcon and Acme", 1]])
        doc.save(path)
    return path


def labels_on_page(path: Path) -> list[str]:
    """The label of each box, top to bottom and left to right."""
    known = {"REDACTED", "XXX", *KIND_LABELS.values()}
    with pymupdf.open(path) as doc:
        words = sorted(doc[0].get_text("words"), key=lambda w: (round(w[1] / 10), w[0]))
    return [w[4] for w in words if w[4] in known]


def test_each_kind_of_match_gets_its_label(letter: Path, tmp_path: Path) -> None:
    report = redact_pdf(letter, tmp_path / "out.pdf", ["Project Falcon"])
    assert report.is_clean
    assert labels_on_page(report.output) == ["NAME", "EMAIL", "NAME", "ORG", "REDACTED"]
    text = page_text(report.output)
    for gone in ("Lukas", "Meier", "acme", "ACME", "Falcon"):
        assert gone not in text
    assert "works for" in text and "Nothing else to hide here." in text


def test_labels_replace_matches_in_bookmarks_too(letter: Path, tmp_path: Path) -> None:
    report = redact_pdf(letter, tmp_path / "out.pdf", ["Falcon"])
    with pymupdf.open(report.output) as doc:
        assert [entry[1] for entry in doc.get_toc()] == ["Mail from EMAIL", "REDACTED and ORG"]


def test_single_label(letter: Path, tmp_path: Path) -> None:
    report = redact_pdf(letter, tmp_path / "out.pdf", ["Project Falcon"], kind_labels=False)
    assert labels_on_page(report.output) == ["REDACTED"] * 5
    assert report.is_clean


def test_custom_label_applies_to_own_terms(letter: Path, tmp_path: Path) -> None:
    report = redact_pdf(letter, tmp_path / "out.pdf", ["Project Falcon"], label="XXX")
    assert labels_on_page(report.output) == ["NAME", "EMAIL", "NAME", "ORG", "XXX"]
    single = redact_pdf(letter, tmp_path / "one.pdf", ["Falcon"], label="XXX", kind_labels=False)
    assert set(labels_on_page(single.output)) == {"XXX"}


def test_no_label_means_no_label_anywhere(letter: Path, tmp_path: Path) -> None:
    report = redact_pdf(letter, tmp_path / "out.pdf", ["Project Falcon"], label=None)
    assert labels_on_page(report.output) == []
    with pymupdf.open(report.output) as doc:
        assert doc.get_toc()[0][1] == "Mail from ***"


def test_own_term_decides_over_a_derived_one(letter: Path, tmp_path: Path) -> None:
    # "Acme" is also found from the email domain; listing it yourself wins.
    report = redact_pdf(letter, tmp_path / "out.pdf", ["Acme"])
    assert labels_on_page(report.output) == ["NAME", "EMAIL", "NAME", "REDACTED"]


def test_labels_are_not_reported_as_leftovers(tmp_path: Path) -> None:
    # The organisation is called "Org", the person "Name": both labels match a term.
    src = tmp_path / "in.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text(
            (72, 100), "Anna Name <anna.name@org.ch> of Org Ltd, says Name.", fontsize=12
        )
        doc.save(src)
    report = redact_pdf(src, tmp_path / "out.pdf")
    assert {"NAME", "EMAIL", "ORG"} <= set(labels_on_page(report.output))
    assert report.is_clean
    assert verify_pdf(report.output, ["org", "name"]) == {}


def test_short_label_fits_a_short_word(tmp_path: Path) -> None:
    src = tmp_path / "in.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 100), "Mail finn@acme.ch.", fontsize=10)
        page.insert_text((72, 130), "Ask Finn about it.", fontsize=10)
        doc.save(src)
    by_kind = redact_pdf(src, tmp_path / "kinds.pdf")
    assert labels_on_page(by_kind.output) == ["EMAIL", "NAME"]
    single = redact_pdf(src, tmp_path / "single.pdf", kind_labels=False)
    assert labels_on_page(single.output) == ["REDACTED"]  # too long for the box over "Finn"
    assert "Finn" not in page_text(single.output)


def test_cli_single_label(letter: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.pdf"
    assert (
        main(["redact", str(letter), str(out), "-t", "Project Falcon", "--single-label"]) == EXIT_OK
    )
    assert set(labels_on_page(out)) == {"REDACTED"}
    assert (
        main(["redact", str(letter), str(out), "-t", "Project Falcon", "--label", "XXX"]) == EXIT_OK
    )
    assert labels_on_page(out) == ["NAME", "EMAIL", "NAME", "ORG", "XXX"]
    assert main(["verify", str(out), "-t", "Project Falcon", "--label", "XXX"]) == EXIT_OK


def test_redact_text_uses_the_label_of_each_kind() -> None:
    matchers = [*build_matchers(["Falcon"], [], emails=True), term_matcher("acme", kind=ORG)]
    labels = {TERM: "REDACTED", EMAIL: "EMAIL", NAME: "NAME", ORG: "ORG"}
    text, counts = redact_text("Falcon: ask jo@acme.ch at Acme", matchers, labels)
    assert text == "REDACTED: ask EMAIL at ORG"
    assert counts == {"Falcon": 1, "jo@acme.ch": 1, "acme": 1}
    again, counts = redact_text(text, matchers, labels)
    assert again == text and not counts  # labels are not redacted again


def test_adjacent_matches_of_different_kinds_stay_apart() -> None:
    text = "JoSmith"
    matchers = [Matcher(re.compile("Jo"), kind=NAME), Matcher(re.compile("Smith"))]
    _, covered = select(find_in_text(text, matchers))
    assert [covered[i] for i in range(len(text))] == [NAME] * 2 + [TERM] * 5
    assert redact_text(text, matchers, {TERM: "[T]", NAME: "[N]"})[0] == "[N][T]"
