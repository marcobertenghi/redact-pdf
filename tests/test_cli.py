from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from redact_pdf.cli import EXIT_ERROR, EXIT_LEFTOVERS, EXIT_OK, main
from tests.conftest import page_text


def test_redact_and_verify(sample_pdf: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.pdf"
    assert main(["redact", str(sample_pdf), str(out), "-t", "John Smith"]) == EXIT_OK
    assert "John Smith" not in page_text(out)
    assert main(["verify", str(out), "-t", "John Smith"]) == EXIT_OK
    assert main(["verify", str(sample_pdf), "-t", "John Smith"]) == EXIT_LEFTOVERS


def test_emails_and_their_names_by_default(sample_pdf: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.pdf"
    assert main(["redact", str(sample_pdf), str(out)]) == EXIT_OK
    assert "@" not in page_text(out)
    assert "Smith" not in page_text(out)  # from john.smith@acme.de
    assert main(["redact", str(sample_pdf), str(out), "--no-email-names"]) == EXIT_OK
    assert "John Smith" in page_text(out)


def test_no_email_flag(sample_pdf: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.pdf"
    assert main(["redact", str(sample_pdf), str(out), "-t", "Contract", "--no-email"]) == EXIT_OK
    assert "john.smith@acme.de" in page_text(out)
    assert main(["redact", str(sample_pdf), str(out), "--no-email"]) == EXIT_ERROR


def test_terms_file(sample_pdf: Path, tmp_path: Path) -> None:
    terms = tmp_path / "terms.txt"
    terms.write_text("ACME GmbH\n", encoding="utf-8")
    out = tmp_path / "out.pdf"
    assert main(["redact", str(sample_pdf), str(out), "-f", str(terms)]) == EXIT_OK
    assert "ACME" not in page_text(out)


@pytest.mark.parametrize("from_file", [False, True], ids=["term-option", "terms-file"])
def test_literal_terms_match_whole_words(tmp_path: Path, from_file: bool) -> None:
    src = tmp_path / "in.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), "ETH methodology Whether")
        doc.save(src)
    args = ["-t", "ETH"]
    if from_file:
        terms = tmp_path / "terms.txt"
        terms.write_text("ETH\n", encoding="utf-8")
        args = ["-f", str(terms)]
    out = tmp_path / "out.pdf"
    assert main(["verify", str(src), *args, "--no-email"]) == EXIT_LEFTOVERS
    assert main(["redact", str(src), str(out), *args, "--no-email"]) == EXIT_OK
    assert "methodology Whether" in page_text(out)
    assert "ETH" not in page_text(out)
    assert main(["verify", str(out), *args, "--no-email"]) == EXIT_OK


def test_batch_directory(sample_pdf: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "redacted"
    code = main(["redact", str(sample_pdf.parent), str(out_dir), "-t", "John Smith"])
    assert code == EXIT_OK
    assert (out_dir / "sample_REDACTED.pdf").is_file()


def test_flatten(sample_pdf: Path, tmp_path: Path) -> None:
    out = tmp_path / "flat.pdf"
    assert main(["flatten", str(sample_pdf), str(out), "--dpi", "72"]) == EXIT_OK
    assert out.is_file()


def test_errors_return_exit_code(sample_pdf: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["redact", str(sample_pdf), str(sample_pdf), "-t", "x"]) == EXIT_ERROR
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["redact", "apply-boxes", "remove-pages", "flatten"])
@pytest.mark.parametrize("problem", ["missing", "invalid", "encrypted", "same-path"])
def test_document_errors_preserve_input_and_return_error(
    sample_pdf: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    problem: str,
) -> None:
    """All writing commands share input protection and document error handling."""
    src = tmp_path / "input.pdf"
    dst = tmp_path / "output.pdf"
    if problem == "invalid":
        src.write_bytes(b"Not a PDF")
    elif problem == "encrypted":
        with pymupdf.open(sample_pdf) as doc:
            doc.save(src, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
    elif problem == "same-path":
        src = sample_pdf
        dst = src
    before = src.read_bytes() if src.exists() else None
    args = [command, str(src)]
    if command == "remove-pages":
        args.append("1")
    args.append(str(dst))

    assert main(args) == EXIT_ERROR
    error = capsys.readouterr().err
    assert "error:" in error
    if problem == "invalid":
        assert "Could not open" in error
    elif problem == "encrypted":
        assert "password protected" in error
    elif problem == "same-path":
        assert "Output must differ from input" in error
    else:
        assert str(src) in error
    if before is not None:
        assert src.read_bytes() == before
    if dst != src:
        assert not dst.exists()


def test_invalid_regex_is_rejected(sample_pdf: Path, tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["redact", str(sample_pdf), str(tmp_path / "o.pdf"), "-r", "("])


def test_output_is_optional(sample_pdf: Path) -> None:
    assert main(["redact", str(sample_pdf)]) == EXIT_OK
    assert sample_pdf.with_name("sample_REDACTED.pdf").is_file()


def test_batch_in_place_skips_previous_outputs(sample_pdf: Path) -> None:
    folder = str(sample_pdf.parent)
    assert main(["redact", folder]) == EXIT_OK
    assert main(["redact", folder]) == EXIT_OK  # second run must not create *_REDACTED_REDACTED
    names = sorted(p.name for p in sample_pdf.parent.glob("*.pdf"))
    assert names == ["sample.pdf", "sample_REDACTED.pdf"]


def test_output_directory_for_single_file(sample_pdf: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    assert main(["redact", str(sample_pdf), str(out_dir)]) == EXIT_OK
    assert (out_dir / "sample_REDACTED.pdf").is_file()


def test_flatten_output_is_optional(sample_pdf: Path) -> None:
    assert main(["flatten", str(sample_pdf), "--dpi", "72"]) == EXIT_OK
    assert sample_pdf.with_name("sample_FLATTENED.pdf").is_file()


def test_email_domains_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = tmp_path / "in.pdf"
    with pymupdf.open() as doc:
        doc.new_page().insert_text((72, 72), "ACME AG, jane@acme.com")
        doc.save(src)
    out = tmp_path / "out.pdf"
    assert main(["redact", str(src), str(out)]) == EXIT_OK
    assert "email domains: 'acme' (from jane@acme.com)" in capsys.readouterr().out
    assert "ACME" not in page_text(out)
    assert main(["redact", str(src), str(out), "--no-email-domains"]) == EXIT_OK
    assert "ACME AG" in page_text(out)


def test_verify_reports_text_outside_pages(
    sample_pdf: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["verify", str(sample_pdf), "-t", "John Smith"]) == 1
    assert "outside page text: John Smith" in capsys.readouterr().out
