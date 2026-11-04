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
