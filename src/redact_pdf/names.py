"""Best-effort person names taken from the email addresses in a document.

Two sources, both tied to an address that was actually found:

* **Display names** written next to an address, as in email headers:
  ``Lukas Körnich <lukas.koernich@acme.ch>`` or ``"Körnich, Lukas" <lk@acme.ch>``.
  These keep the exact spelling from the document.
* **The local part** of the address: ``lukas.koernich@`` gives "lukas" and
  "koernich". As addresses are usually written without accents, these are
  matched loosely: "koernich" also finds "Körnich", "Kornich" and "KÖRNICH".

Names are redacted as whole words: "Ross" does not remove "Rossi". A single
word must also start with a capital letter, as names are written, so
``format_c@`` does not remove "format" in running text; full names such as
"john smith" match in any case. Role accounts (``info@``, ``support@``),
mailing lists, titles and common header words are skipped. This is a
heuristic: capitalised words that are also names (Will, Mark, a German noun
used as an address) are redacted too, and names not tied to an address are
missed.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, field

from redact_pdf.matching import EMAIL_PATTERN, Matcher, term_matcher, whole_word

__all__ = ["NAME_STOPWORDS", "NameTerms", "display_names", "email_name_terms", "loose_matcher"]

NAME_STOPWORDS = frozenset(
    {
        # Role accounts and departments
        "abuse", "accounting", "admin", "administrator", "alert", "alerts", "billing",
        "bookings", "booking", "bounce", "buchhaltung", "career", "careers", "compliance",
        "contact", "customer", "customers", "daemon", "datenschutz", "desk", "donotreply",
        "email", "empfang", "events", "feedback", "hello", "hallo", "help", "helpdesk",
        "hostmaster", "info", "information", "invoice", "invoices", "jobs", "kontakt",
        "kunden", "kundendienst", "legal", "mail", "mailer", "marketing", "media", "news",
        "newsletter", "noreply", "notification", "notifications", "office", "order",
        "orders", "postmaster", "presse", "press", "privacy", "reception", "rechnung",
        "rechnungen", "reply", "root", "sales", "secretariat", "security", "sekretariat",
        "service", "services", "shop", "store", "support", "system", "team", "test",
        "user", "users", "verkauf", "webmaster",
        # German, French and Italian role accounts
        "abo", "abonnement", "accueil", "amministrazione", "anmeldung", "auftrag",
        "bestellung", "bestellungen", "betrieb", "bewerbung", "bewerbungen", "commande",
        "contatti", "direction", "direktion", "einkauf", "facturation", "informatik",
        "kanzlei", "kommunikation", "lager", "logistik", "medien", "mitglieder",
        "ordini", "personal", "post", "poststelle", "praxis", "redaktion", "segreteria",
        "technik", "termin", "termine", "versand", "verwaltung", "vorstand", "zentrale",
        # Organisations
        "abteilung", "ag", "co", "company", "department", "gmbh", "group", "gruppe",
        "inc", "kg", "llc", "ltd", "sa", "sarl", "the", "und", "and", "for", "für", "of",
        # Email header words and prefixes
        "an", "aw", "bcc", "cc", "datum", "date", "from", "fw", "fwd", "gesendet",
        "mailto", "re", "sent", "subject", "betreff", "to", "wg",
        # Pronouns and salutations (German capitalises "Sie")
        "sie", "ihr", "ihre", "du", "dear", "liebe", "lieber", "sehr", "geehrte",
        "geehrter", "hi", "hey",
    }
)  # fmt: skip
"""Words never taken as names. A display name containing one is skipped."""

_TITLES = frozenset(
    {"dr", "prof", "herr", "herrn", "frau", "mr", "mrs", "ms", "miss", "sir", "dipl",
     "ing", "mag", "lic", "med", "phil", "jur", "msc", "bsc", "mba", "phd", "dres"}
)  # fmt: skip
_PARTICLES = frozenset(
    {"von", "van", "de", "der", "den", "del", "della", "di", "da", "du", "dos", "das",
     "le", "la", "zu", "ten", "ter", "y", "al", "bin"}
)  # fmt: skip
_NAME_WORD = re.compile(r"[^\W\d_]+(?:['\u2019-][^\W\d_]+)*")
_DISPLAY_NAME_STOP = ':;,<>()[]{}|/\\"\n'
_QUOTES = "\"'\u201c\u201d\u201e\u00ab\u00bb"
_MAX_NAME_WORDS = 4
_MIN_LOCAL_PART = 3  # shorter pieces of an address are usually initials
# Mailing lists: the address names a project or topic, not a person
_LIST_DOMAIN_LABELS = frozenset({"lists", "list", "groups", "googlegroups", "listserv"})
_LIST_SUFFIXES = frozenset(
    {"announce", "bounces", "commits", "dev", "devel", "discuss", "general", "list",
     "owner", "request", "users", "betatesting", "testing"}
)  # fmt: skip
_GERMAN = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_COMBINING = "[̀-ͯ]*"


def _accented_letters() -> dict[str, str]:
    """``{"o": "òóôõöøō...", ...}``: accented Latin letters by base letter."""
    table: dict[str, list[str]] = {}
    for cp in range(0xC0, 0x250):
        char = chr(cp)
        base = unicodedata.normalize("NFD", char)[0].lower()
        if char.isalpha() and char.islower() and base.isascii() and base != char:
            table.setdefault(base, []).append(char)
    table.setdefault("o", []).append("ø")
    return {k: "".join(v) for k, v in table.items()}


_ACCENTED = _accented_letters()


def _fold(text: str) -> str:
    """Lowercase ASCII form for comparing names with addresses: Körnich -> koernich."""
    text = unicodedata.normalize("NFC", text).lower().translate(_GERMAN)
    return "".join(c for c in unicodedata.normalize("NFKD", text) if c.isascii())


def _strip_accents(text: str) -> str:
    """Körnich -> kornich."""
    nfkd = unicodedata.normalize("NFKD", unicodedata.normalize("NFC", text).lower())
    return "".join(c for c in nfkd if c.isascii())


def loose_matcher(name: str) -> Matcher:
    """Whole-word, accent-insensitive match for a name taken from an address.

    Each letter also matches its accented forms (o: ö, ó, ø, o + U+0308), and
    the transliterations ae/oe/ue/ss also match ä/ö/ü/ß:
    "koernich" finds "Koernich", "Körnich", "Kornich" and "KÖRNICH".
    Words in ``name`` are separated by any whitespace in the text.
    """
    name = name.lower()
    body = r"\s+".join(_loose_word(word) for word in name.split())
    return Matcher(re.compile(whole_word(body), re.IGNORECASE), name, _single_word(name))


def _loose_word(word: str) -> str:
    parts: list[str] = []
    i = 0
    while i < len(word):
        pair = word[i : i + 2]
        if pair in ("ae", "oe", "ue"):
            parts.append(f"{_letter(pair[0])}e?")
            i += 2
        elif pair == "ss":
            parts.append("(?:ss|ß)")
            i += 2
        else:
            parts.append(_letter(word[i]))
            i += 1
    return "".join(parts)


def _letter(char: str) -> str:
    variants = _ACCENTED.get(char, "")
    if not variants:
        return re.escape(char)
    return f"[{re.escape(char)}{variants}]{_COMBINING}"


def display_names(text: str) -> Iterator[tuple[str, str]]:
    """``(display name, address)`` for each address written as ``Name <address>``.

    The name may be on the line before the address, with a line holding only
    a page number in between (a header split across a page break).
    """
    for m in EMAIL_PATTERN.finditer(text):
        j = m.start()
        while j > 0 and text[j - 1] in " \t\u00a0":
            j -= 1
        if j == 0 or text[j - 1] not in "<([":
            continue
        j -= 1
        for _ in range(3):  # skip whitespace and up to two lines without letters
            while j > 0 and text[j - 1].isspace():
                j -= 1
            line_start = text.rfind("\n", 0, j) + 1
            if line_start == 0 or any(c.isalpha() for c in text[line_start:j]):
                break
            j = line_start
        if j > 0 and text[j - 1] == ")":  # Name (nickname) <address>
            opening = text.rfind("(", 0, j - 1)
            if opening < 0 or "\n" in text[opening:j]:
                continue
            j = opening
            while j > 0 and text[j - 1] in " \t\u00a0":
                j -= 1
        end = j
        if j > 0 and text[j - 1] in _QUOTES:
            start = max(text.rfind(q, 0, j - 1) for q in _QUOTES) + 1
            if start == 0:
                continue  # unbalanced quote
            end = j - 1
        else:
            start = j
            while start > 0 and text[start - 1] not in _DISPLAY_NAME_STOP:
                start -= 1
        name = " ".join(text[start:end].replace(",", " ").split())
        if name:
            yield unicodedata.normalize("NFC", name), m.group(0)


def _name_words(name: str, address: str) -> list[str] | None:
    """The words of a display name worth redacting, or ``None`` if it does
    not look like a person's name."""
    words = [w.strip(".'\u2019") for w in name.split()]
    if not 1 <= len(words) <= _MAX_NAME_WORDS + 2:  # allow two titles
        return None
    kept: list[str] = []
    for word in words:
        low = word.lower()
        if low in _TITLES or low in _PARTICLES:
            continue
        if not _NAME_WORD.fullmatch(word) or not word[0].isupper() or low in NAME_STOPWORDS:
            return None
        if len(word) >= 2:
            kept.append(word)
    if not 1 <= len(kept) <= _MAX_NAME_WORDS:
        return None
    if len(kept) == 1 and not _in_address(kept[0], address):
        return None  # a single word ("Support", "Buchhaltung") needs confirmation
    return kept


def _is_mailing_list(address: str) -> bool:
    local, _, domain = address.lower().rpartition("@")
    return bool(_LIST_DOMAIN_LABELS & set(domain.split("."))) or any(
        local.endswith(f"-{suffix}") for suffix in _LIST_SUFFIXES
    )


def _single_word(name: str) -> bool:
    """Single words must be capitalised to match; full names match in any case."""
    return len(name.split()) == 1


def _name_matcher(name: str) -> Matcher:
    m = term_matcher(name, whole_word_only=True)
    return Matcher(m.pattern, m.key, _single_word(name))


def _local_words(address: str) -> list[str]:
    local = address.rpartition("@")[0]
    return [w for w in re.split(r"[\W\d_]+", local) if w]


def _in_address(word: str, address: str) -> bool:
    local = "".join(_local_words(address)).lower()
    return any(
        len(f) >= _MIN_LOCAL_PART and f in local for f in (_fold(word), _strip_accents(word))
    )


@dataclass
class NameTerms:
    """Names found for each address, and the matchers that redact them."""

    by_address: dict[str, list[str]] = field(default_factory=dict)
    matchers: list[Matcher] = field(default_factory=list)

    def add(self, address: str, name: str, matcher: Matcher) -> None:
        names = self.by_address.setdefault(address, [])
        if name.lower() not in (n.lower() for n in names):
            names.append(name)
            self.matchers.append(matcher)


def email_name_terms(text: str) -> NameTerms:
    """Names for all addresses in ``text`` (the whole document's text)."""
    result = NameTerms()
    seen: dict[str, str] = {}  # folded name -> address that added it
    shown: dict[str, list[str]] = {}  # address -> display-name words

    for name, address in display_names(text):
        words = _name_words(name, address)
        if not words:
            continue
        shown.setdefault(address.lower(), []).extend(words)
        if len(words) > 1:
            full = " ".join(words)
            result.add(address, full, _name_matcher(full))
        for word in words:
            folded = _fold(word)
            if folded not in seen:
                seen[folded] = address
                result.add(address, word, _name_matcher(word))

    for m in EMAIL_PATTERN.finditer(text):
        address = m.group(0)
        if _is_mailing_list(address):
            continue
        known = [_fold(w) for w in shown.get(address.lower(), [])]
        local_words = _local_words(address)
        words = [_fold(w) for w in local_words]
        if (
            len(words) > 1
            and not known
            and all(len(w) >= _MIN_LOCAL_PART and w not in NAME_STOPWORDS for w in words)
        ):
            full = " ".join(words)  # john.smith@ -> "john smith", one box
            result.add(address, full, loose_matcher(full))
        for word in local_words:
            folded = _fold(word)  # müller -> mueller, then matched loosely
            if (
                len(folded) < _MIN_LOCAL_PART
                or folded in NAME_STOPWORDS
                or folded in seen
                or any(len(k) >= _MIN_LOCAL_PART and k in folded for k in known)
            ):
                continue
            seen[folded] = address
            result.add(address, folded, loose_matcher(folded))
    return result
