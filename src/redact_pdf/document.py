"""Shared PDF file handling and cleanup for all document operations.

Keep opening, output-path checks and final sanitization here so redaction,
manual boxes and page removal use the same lifecycle without depending on
one another. Cleanup order matters: orphaned objects are dropped before
the remaining strings are rewritten and reported.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pymupdf

from redact_pdf.metadata import MetadataReport, strip_metadata
from redact_pdf.pdfstrings import redact_object_strings, strip_marked_content_text

REDACTED_SUFFIX = "_REDACTED"

# Blank only the image pixels under a redaction box. Defined dynamically by PyMuPDF,
# so it is invisible to type checkers; 2 is its documented value.
REDACT_IMAGE_PIXELS: int = getattr(pymupdf, "PDF_REDACT_IMAGE_PIXELS", 2)


class RedactionError(Exception):
    """Raised when a document cannot be processed safely."""


def default_output_path(input_path: str | Path, suffix: str = REDACTED_SUFFIX) -> Path:
    """``contract.pdf`` -> ``contract_REDACTED.pdf`` in the same directory."""
    src = Path(input_path)
    return src.with_name(f"{src.stem}{suffix}.pdf")


def open_pdf(path: Path) -> pymupdf.Document:
    """Open an existing, unencrypted PDF; the caller must close it."""
    if not path.is_file():
        raise FileNotFoundError(path)
    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # pymupdf raises several exception types
        raise RedactionError(f"Could not open {path}: {exc}") from exc
    if doc.needs_pass:
        doc.close()
        raise RedactionError(f"{path} is password protected; decrypt it first.")
    return doc


def check_output_path(input_path: Path, output_path: Path) -> None:
    """Protect the input from being overwritten and create the output directory."""
    if input_path.resolve() == output_path.resolve():
        raise RedactionError("Output must differ from input; the original is never overwritten.")
    output_path.parent.mkdir(parents=True, exist_ok=True)


def clean_and_save(
    data: bytes,
    dst: Path,
    *,
    scrub: bool,
    strings: Callable[[str], tuple[str, Counter[str]]] | None = None,
) -> tuple[MetadataReport, Counter[str]]:
    """Remove hidden content and metadata from a serialised PDF and save it.

    Runs ``scrub`` (links, attachments, scripts...), :func:`strip_metadata`
    and removes alternate text in page content. ``strings``, if given,
    rewrites text strings outside the pages. The file is fully rewritten, so
    no earlier revision survives. Returns what was removed and the string
    matches.
    """
    with pymupdf.open("pdf", data) as doc:
        if scrub:
            # Metadata is handled more thoroughly by strip_metadata().
            doc.scrub(metadata=False, xml_metadata=False)
        metadata = strip_metadata(doc)
        if n := strip_marked_content_text(doc):
            metadata.removed["alternate text in page content"] += n
        # Drop what scrubbing and the tag removal orphaned, so only strings
        # that end up in the output are rewritten and reported.
        data = doc.tobytes(garbage=4, deflate=True)

    hidden: Counter[str] = Counter()
    with pymupdf.open("pdf", data) as doc:
        if strings is not None:
            hidden = redact_object_strings(doc, strings)
        doc.save(dst, garbage=4, deflate=True, clean=True)
    return metadata, hidden
