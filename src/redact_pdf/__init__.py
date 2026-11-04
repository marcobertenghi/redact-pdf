"""True redaction of known strings and patterns in PDFs."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("redact-pdf")
except PackageNotFoundError:  # running from a source checkout without install
    __version__ = "0.0.0"

from redact_pdf.boxes import APPLIED_SUFFIX, BoxReport, apply_boxes
from redact_pdf.core import (
    DEFAULT_LABEL,
    DOCUMENT_LEVEL,
    EMAIL_PATTERN,
    FLATTENED_SUFFIX,
    REDACTED_SUFFIX,
    RedactionError,
    RedactionReport,
    default_output_path,
    flatten_pdf,
    load_terms,
    redact_pdf,
    verify_pdf,
)
from redact_pdf.metadata import MetadataReport, strip_jpeg_metadata, strip_metadata

__all__ = [
    "APPLIED_SUFFIX",
    "DEFAULT_LABEL",
    "DOCUMENT_LEVEL",
    "EMAIL_PATTERN",
    "FLATTENED_SUFFIX",
    "REDACTED_SUFFIX",
    "BoxReport",
    "MetadataReport",
    "RedactionError",
    "RedactionReport",
    "__version__",
    "apply_boxes",
    "default_output_path",
    "flatten_pdf",
    "load_terms",
    "redact_pdf",
    "strip_jpeg_metadata",
    "strip_metadata",
    "verify_pdf",
]
