"""Finding terms and patterns in text, located by character position.

Page text is extracted with one bounding box per character. Terms and
regular expressions run on that text, and each match is mapped back to the
boxes of exactly the characters it covers. Compared to searching the page for
the matched string a second time, this keeps regex semantics such as ``\\b``
intact and makes case-insensitive matching work for all letters, not just ASCII.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import pymupdf

__all__ = [
    "EMAIL_PATTERN",
    "FREEMAIL_DOMAINS",
    "Matcher",
    "PageText",
    "build_matchers",
    "email_domain_term",
    "find_in_text",
    "normalise_terms",
    "redact_text",
    "term_matcher",
    "whole_word",
]

EMAIL_PATTERN = re.compile(
    r"""
    (?<![\w%+-])                     # not in the middle of a longer token
    [\w%+-]{1,64}(?:\.[\w%+-]{1,64}){0,15}   # local part: dot-separated, no double dots
    @
    (?:[\w-]{1,63}\.){1,10}          # domain labels: mail. example.
    [^\W\d_]{2,63}                   # top-level domain: letters only (excludes 1.2@3.4)
    (?![\w-])                        # TLD must end here
    """,
    re.VERBOSE,
)
"""Pragmatic email matcher: covers real-world addresses, not every RFC 5322 edge case.

Repetitions are bounded (using RFC length limits) so matching stays linear-time,
even on adversarial text such as long dot-separated runs without an ``@``.
"""

FREEMAIL_DOMAINS = frozenset(
    {
        # International
        "aol", "fastmail", "gmail", "googlemail", "hey", "hotmail", "icloud", "inbox",
        "live", "mac", "mail", "mailbox", "me", "msn", "outlook", "pm", "proton",
        "protonmail", "tuta", "tutanota", "yahoo", "yandex", "ymail", "zoho",
        # Switzerland, Germany, Austria
        "bluewin", "freenet", "gmx", "hispeed", "posteo", "sunrise", "t-online", "web",
        "1und1", "arcor", "ewetel", "green", "ionos", "kabelmail", "netcologne", "online",
        "quickline", "swissonline", "vodafone",
        # Other European
        "free", "laposte", "libero", "onet", "orange", "seznam", "wanadoo", "wp",
        # Asia
        "126", "163", "naver", "qq", "rediffmail",
        # Reserved for documentation (RFC 2606), common in templates
        "example", "invalid", "localhost", "test",
    }
)  # fmt: skip
"""Domain names that identify a mail provider rather than an organisation.

Their names are not added as terms: ``web.de`` would otherwise remove every
"web", and ``me.com`` every "me".
"""

# Second-level labels used under country-code TLDs: acme.co.uk -> acme
_SECOND_LEVEL = frozenset({"ac", "co", "com", "edu", "gov", "gv", "ltd", "net", "or", "org", "plc"})

# Text extraction flags: keep whitespace, ignore text outside the page, expand
# ligatures into separate letters (so "fi" in a term matches the "ﬁ" glyph).
TEXT_FLAGS: int = (
    pymupdf.TEXT_PRESERVE_WHITESPACE
    | pymupdf.TEXT_MEDIABOX_CLIP
    | pymupdf.TEXT_CID_FOR_UNKNOWN_UNICODE
)
_LINE_END_HYPHENS = "-\u00ad\u2010"  # hyphen-minus, soft hyphen, hyphen


@dataclass(frozen=True)
class Matcher:
    """A compiled pattern plus the name used for its matches in reports.

    ``key=None`` reports the matched text itself (used for regexes and emails).
    ``capitalised=True`` only accepts matches starting with an uppercase
    letter, as names are written (used for names taken from email addresses).
    """

    pattern: re.Pattern[str]
    key: str | None = None
    capitalised: bool = False

    def name(self, match: re.Match[str]) -> str:
        return self.key if self.key is not None else " ".join(match.group(0).split())


def normalise_terms(terms: Iterable[str]) -> list[str]:
    """Deduplicate and sort longest-first, for stable, readable reports."""
    unique = {t.strip() for t in terms if t and t.strip()}
    return sorted(unique, key=lambda t: (-len(t), t))


def _literal(word: str) -> str:
    """Regex for ``word`` that accepts accented letters in either Unicode form.

    "ö" can be stored as one character (U+00F6) or as "o" plus a combining
    diaeresis (U+0308). Both look the same, so both are matched, whichever
    form the term was typed in.
    """
    parts = []
    for char in unicodedata.normalize("NFC", word):
        decomposed = unicodedata.normalize("NFD", char)
        if decomposed == char:
            parts.append(re.escape(char))
        else:
            parts.append(f"(?:{re.escape(char)}|{re.escape(decomposed)})")
    return "".join(parts)


def whole_word(body: str) -> str:
    """No letter, digit or combining accent directly before or after the match."""
    return rf"(?<![^\W_])(?<![\u0300-\u036f]){body}(?![^\W_]|[\u0300-\u036f])"


def term_matcher(term: str, *, whole_word_only: bool = True) -> Matcher:
    """Case-insensitive literal match, bounded by whole words by default.

    Any whitespace in ``term`` also matches line breaks and repeated spaces,
    so "John Smith" is found across lines.
    """
    term = unicodedata.normalize("NFC", term)
    body = r"\s+".join(map(_literal, term.split()))
    if whole_word_only:
        body = whole_word(body)
    return Matcher(re.compile(body, re.IGNORECASE), term)


def build_matchers(
    terms: Iterable[str], patterns: Iterable[str | re.Pattern[str]], *, emails: bool
) -> list[Matcher]:
    matchers = [term_matcher(t) for t in normalise_terms(terms)]
    matchers += [Matcher(p if isinstance(p, re.Pattern) else re.compile(p)) for p in patterns]
    if emails:
        matchers.append(Matcher(EMAIL_PATTERN))
    return matchers


def email_domain_term(address: str) -> str | None:
    """The organisation name in an email address's domain, or ``None``.

    ``john@apple.com`` -> ``apple``, ``a@mail.acme.co.uk`` -> ``acme``.
    Mail providers (see :data:`FREEMAIL_DOMAINS`) and one-letter names give ``None``.
    """
    labels = address.rpartition("@")[2].lower().split(".")
    if len(labels) < 2:
        return None
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        name = labels[-3]
    else:
        name = labels[-2]
    if len(name) < 2 or name in FREEMAIL_DOMAINS:
        return None
    return name


Span = tuple[str, int, int]
"""``(name, start, end)`` of a match."""


def _protected(text: str, protect: str | None) -> list[tuple[int, int]]:
    if not protect:
        return []
    return [(m.start(), m.end()) for m in re.finditer(re.escape(protect), text)]


def _inside(start: int, end: int, ranges: Sequence[tuple[int, int]]) -> bool:
    return any(lo <= start and end <= hi for lo, hi in ranges)


def find_in_text(text: str, matchers: Sequence[Matcher], protect: str | None = None) -> list[Span]:
    """All non-blank matches in ``text``. Matches lying entirely inside an
    occurrence of ``protect`` (the redaction label) are ignored."""
    protected = _protected(text, protect)
    spans: list[Span] = []
    for matcher in matchers:
        for m in matcher.pattern.finditer(text):
            if matcher.capitalised and not m.group(0)[:1].isupper():
                continue
            if m.group(0).strip() and not _inside(m.start(), m.end(), protected):
                spans.append((matcher.name(m), m.start(), m.end()))
    return spans


def select(spans: Iterable[Span]) -> tuple[Counter[str], set[int]]:
    """Merge overlapping matches.

    Returns the per-name counts and the set of covered positions. Longer
    matches are taken first; a match entirely inside an earlier one (e.g.
    "Smith" inside "John Smith") is not counted separately.
    """
    counts: Counter[str] = Counter()
    covered: set[int] = set()
    for name, start, end in sorted(spans, key=lambda s: (s[1] - s[2], s[1])):
        positions = set(range(start, end))
        if not positions <= covered:
            counts[name] += 1
            covered |= positions
    return counts, covered


def _runs(positions: Iterable[int]) -> list[tuple[int, int]]:
    """Group sorted positions into ``[start, end)`` runs of consecutive values."""
    runs: list[tuple[int, int]] = []
    for p in sorted(positions):
        if runs and runs[-1][1] == p:
            runs[-1] = (runs[-1][0], p + 1)
        else:
            runs.append((p, p + 1))
    return runs


def redact_text(
    text: str, matchers: Sequence[Matcher], replacement: str
) -> tuple[str, Counter[str]]:
    """Replace every match (merged into runs) by ``replacement``.

    Existing occurrences of ``replacement`` are left alone, so running twice
    does not nest labels. Returns the new text and the per-name counts.
    """
    counts, covered = select(find_in_text(text, matchers, protect=replacement))
    if not covered:
        return text, counts
    out, pos = [], 0
    for start, end in _runs(covered):
        out += [text[pos:start], replacement]
        pos = end
    out.append(text[pos:])
    return "".join(out), counts


class PageText:
    """A page's text with one bounding box per character.

    Matching runs on two renderings of the text: as laid out (lines joined
    by newlines), and with words hyphenated at a line end joined back
    together, so "Mül-" + "ler" is found by the term "Müller".
    """

    def __init__(self, page: pymupdf.Page) -> None:
        self.chars: list[str] = []
        self.boxes: list[tuple[float, float, float, float]] = []
        self.line_of: list[int] = []
        lines: list[tuple[int, int]] = []  # (start, end) character indices

        raw = page.get_text("rawdict", flags=TEXT_FLAGS)
        for block in raw["blocks"]:
            for line in block.get("lines", ()):
                start = len(self.chars)
                for span in line["spans"]:
                    for char in span["chars"]:
                        self.chars.append(char["c"])
                        self.boxes.append(tuple(char["bbox"]))
                        self.line_of.append(len(lines))
                lines.append((start, len(self.chars)))

        self.variants = [self._render(lines, dehyphenate=False)]
        joined = self._render(lines, dehyphenate=True)
        if joined[0] != self.variants[0][0]:
            self.variants.append(joined)

    def _render(
        self, lines: Sequence[tuple[int, int]], *, dehyphenate: bool
    ) -> tuple[str, list[int]]:
        """Return the text and, per text position, its character index (-1 for
        the newlines added between lines).

        Lines are joined across text blocks too: MuPDF often puts lines with
        wider spacing in separate blocks, and a wrong join can only cause
        extra redaction, never less.
        """
        text: list[str] = []
        index: list[int] = []
        for n, (start, end) in enumerate(lines):
            stop = end
            join = False
            if dehyphenate and n + 1 < len(lines):
                last = end
                while last > start and self.chars[last - 1].isspace():
                    last -= 1
                nxt = lines[n + 1][0]
                if (
                    last > start + 1
                    and self.chars[last - 1] in _LINE_END_HYPHENS
                    and self.chars[last - 2].isalpha()
                    and nxt < len(self.chars)
                    and self.chars[nxt].isalpha()
                ):
                    stop, join = last - 1, True
            text += self.chars[start:stop]
            index += range(start, stop)
            if not join:
                text.append("\n")
                index.append(-1)
        return "".join(text), index

    def find(self, matchers: Sequence[Matcher], protect: str | None = None) -> list[Span]:
        """Matches as ``(name, first_char, end_char)`` in character indices."""
        found: dict[tuple[int, int], str] = {}
        for text, index in self.variants:
            for name, start, end in find_in_text(text, matchers, protect):
                chars = [i for i in index[start:end] if i >= 0]
                if chars:
                    found.setdefault((chars[0], chars[-1] + 1), name)
        return [(name, start, end) for (start, end), name in found.items()]

    def rects(self, covered: Iterable[int]) -> list[pymupdf.Rect]:
        """One rectangle per run of consecutive covered characters on a line.

        Whitespace at either end of a run does not widen the rectangle.
        """
        rects: list[pymupdf.Rect] = []
        for start, end in _runs(covered):
            run: list[int] = []
            for i in range(start, end):
                if run and self.line_of[i] != self.line_of[run[0]]:
                    rects += self._box(run)
                    run = []
                run.append(i)
            rects += self._box(run)
        return rects

    def _box(self, run: Sequence[int]) -> list[pymupdf.Rect]:
        boxes = [self.boxes[i] for i in run if not self.chars[i].isspace()]
        if not boxes:
            return []
        rect = pymupdf.Rect(
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )
        if rect.is_empty:  # degenerate glyph boxes: still cover the spot
            rect.x1 = max(rect.x1, rect.x0 + 1)
            rect.y1 = max(rect.y1, rect.y0 + 1)
        return [rect]
