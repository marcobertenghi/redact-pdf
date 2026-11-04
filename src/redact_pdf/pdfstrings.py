"""Text stored outside the page content: string objects and marked content.

Page redaction only removes glyphs drawn on a page. The same words can also
sit in PDF string objects (layer names, page labels, link targets, form
values, comment text) and in marked-content properties inside content
streams (``/ActualText``, ``/Alt``), which text extractors and screen readers
report in place of the glyphs. This module finds, decodes and rewrites them
with a small tokenizer for PDF syntax.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pymupdf

__all__ = [
    "decode_string",
    "iter_object_strings",
    "redact_object_strings",
    "strip_marked_content_text",
    "tokens",
]

logger = logging.getLogger(__name__)

_WHITESPACE = frozenset(b" \t\r\n\f\x00")
_DELIMITERS = frozenset(b"()<>[]{}/%")
_INLINE_IMAGE_END = re.compile(rb"\sEI(?=\s|$)")
_ESCAPES = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}
_OCTAL = frozenset(b"01234567")

# Strings under these keys are syntax or binary data, not text: default
# appearances of form fields, the document ID and encryption hashes.
_NON_TEXT_KEYS = frozenset({b"/DA", b"/DS", b"/ID", b"/O", b"/U", b"/OE", b"/UE", b"/Perms"})
# Marked-content properties that carry replacement or alternate text.
MARKED_CONTENT_TEXT_KEYS = frozenset({b"/ActualText", b"/Alt", b"/E"})


@dataclass(frozen=True)
class Token:
    kind: str  # "string", "name" or "other"
    start: int
    end: int


def tokens(data: bytes) -> Iterator[Token]:
    """Split PDF object or content-stream syntax into tokens.

    Only strings and names are told apart; everything else is "other".
    Comments are skipped, and inline image data (``ID ... EI``) is jumped
    over so binary bytes are never mistaken for strings.
    """
    i, n = 0, len(data)
    while i < n:
        c = data[i]
        if c in _WHITESPACE:
            i += 1
        elif c == 0x25:  # % comment
            while i < n and data[i] not in b"\r\n":
                i += 1
        elif c == 0x28:  # ( literal string
            end = _literal_end(data, i)
            yield Token("string", i, end)
            i = end
        elif c == 0x3C and data[i + 1 : i + 2] != b"<":  # <hex string>
            end = data.find(b">", i)
            end = n if end < 0 else end + 1
            yield Token("string", i, end)
            i = end
        elif c in b"<>":  # << or >> (a lone > is malformed; consume it)
            end = i + 2 if data[i + 1 : i + 2] == bytes([c]) else i + 1
            yield Token("other", i, end)
            i = end
        elif c == 0x2F:  # /Name
            end = _regular_end(data, i + 1)
            yield Token("name", i, end)
            i = end
        else:
            end = max(_regular_end(data, i), i + 1)
            yield Token("other", i, end)
            if data[i:end] == b"ID":  # inline image: skip its binary data
                m = _INLINE_IMAGE_END.search(data, end)
                end = m.end() if m else n
            i = end


def _regular_end(data: bytes, i: int) -> int:
    n = len(data)
    while i < n and data[i] not in _WHITESPACE and data[i] not in _DELIMITERS:
        i += 1
    return i


def _literal_end(data: bytes, i: int) -> int:
    depth, n = 0, len(data)
    while i < n:
        c = data[i]
        if c == 0x5C:  # backslash escapes the next byte
            i += 2
            continue
        if c == 0x28:
            depth += 1
        elif c == 0x29:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _string_bytes(raw: bytes) -> bytes:
    if raw.startswith(b"<"):
        digits = re.sub(rb"[^0-9A-Fa-f]", b"", raw)
        if len(digits) % 2:
            digits += b"0"
        return bytes.fromhex(digits.decode("ascii"))

    body = raw[1:-1] if raw.endswith(b")") else raw[1:]
    out = bytearray()
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        i += 1
        if c != 0x5C:
            out.append(c)
            continue
        if i >= n:
            break
        e = body[i]
        if e in _OCTAL:
            j = i
            while j < min(i + 3, n) and body[j] in _OCTAL:
                j += 1
            out.append(int(body[i:j], 8) & 0xFF)
            i = j
        elif e == 0x0D:  # line continuation
            i += 2 if body[i + 1 : i + 2] == b"\n" else 1
        elif e == 0x0A:
            i += 1
        else:
            out.append(_ESCAPES.get(e, e))
            i += 1
    return bytes(out)


def decode_string(raw: bytes) -> str:
    """Decode a literal ``(...)`` or hex ``<...>`` PDF string token to text."""
    data = _string_bytes(raw)
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16-be", "replace")
    if data.startswith(b"\xff\xfe"):  # not allowed by the spec, but seen in the wild
        return data[2:].decode("utf-16-le", "replace")
    if data.startswith(b"\xef\xbb\xbf"):  # PDF 2.0
        return data[3:].decode("utf-8", "replace")
    return data.decode("latin-1")  # PDFDocEncoding, close enough for matching


def _text_strings(data: bytes) -> Iterator[Token]:
    """String tokens that hold text (not under one of the binary/syntax keys)."""
    prev: Token | None = None
    for tok in tokens(data):
        if tok.kind == "string" and not (
            prev is not None
            and prev.kind == "name"
            and data[prev.start : prev.end] in _NON_TEXT_KEYS
        ):
            yield tok
        prev = tok


def iter_object_strings(doc: pymupdf.Document) -> Iterator[tuple[int, str]]:
    """Yield ``(xref, text)`` for every text string in every object."""
    for xref in range(1, doc.xref_length()):
        data = doc.xref_object(xref, compressed=True).encode()
        for tok in _text_strings(data):
            yield xref, decode_string(data[tok.start : tok.end])


def redact_object_strings(
    doc: pymupdf.Document, redact: Callable[[str], tuple[str, Counter[str]]]
) -> Counter[str]:
    """Rewrite string objects with ``redact`` (text -> new text, match counts).

    Returns the combined match counts.
    """
    total: Counter[str] = Counter()
    for xref in range(1, doc.xref_length()):
        data = doc.xref_object(xref, compressed=True).encode()
        out, pos = bytearray(), 0
        for tok in _text_strings(data):
            new, counts = redact(decode_string(data[tok.start : tok.end]))
            if counts:
                out += data[pos : tok.start] + str(pymupdf.get_pdf_str(new)).encode()
                pos = tok.end
                total += counts
        if pos:
            out += data[pos:]
            doc.update_object(xref, out.decode())
    return total


def _content_stream_xrefs(doc: pymupdf.Document) -> set[int]:
    xrefs = {xref for page in doc.pages() for xref in page.get_contents()}
    for xref in range(1, doc.xref_length()):
        if doc.xref_get_key(xref, "Subtype") == ("name", "/Form"):
            xrefs.add(xref)
    return xrefs


def strip_marked_content_text(doc: pymupdf.Document) -> int:
    """Remove ``/ActualText``, ``/Alt`` and ``/E`` from marked-content
    properties written inline in content streams.

    These strings replace the drawn glyphs in text extraction, so they would
    otherwise reveal redacted words. Returns the number of entries removed.
    """
    removed = 0
    for xref in sorted(_content_stream_xrefs(doc)):
        data = doc.xref_stream(xref)
        if not data or not any(key in data for key in MARKED_CONTENT_TEXT_KEYS):
            continue
        cuts: list[tuple[int, int]] = []
        prev: Token | None = None
        for tok in tokens(data):
            if (
                tok.kind == "string"
                and prev is not None
                and prev.kind == "name"
                and data[prev.start : prev.end] in MARKED_CONTENT_TEXT_KEYS
            ):
                cuts.append((prev.start, tok.end))
            prev = tok
        if cuts:
            out, pos = bytearray(), 0
            for start, end in cuts:
                out += data[pos:start]
                pos = end
            out += data[pos:]
            doc.update_stream(xref, bytes(out))
            removed += len(cuts)
    if removed:
        logger.info("Removed %d marked-content text entries", removed)
    return removed
