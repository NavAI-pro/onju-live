# Vendored from NavAI-pro/navai-agent src/pipeline/providers/text_norm.py
# (origin/HEAD c98e9b5, 2026-10-15). Keep in sync with upstream rather than
# editing here.
"""Shared TTS text normalization for Uzbek (NAV-15).

Every TTS provider funnels synthesis text through :func:`normalize_tts_text`
before it hits the wire. Two jobs:

1. Acronym / URL expansion ("mygov.uz" -> "may nuqta gov nuqta uz").
2. **Digits -> Uzbek words.** The system prompt asks the LLM to spell numbers
   out ("Raqamlarni so'z bilan ayting"), but that is advisory: the model
   regularly emits bare digits, and the TTS engines read Latin digits with
   English rules ("28" -> "twenty eight"). This module makes it deterministic.

Kept cheap because it runs on every synthesis chunk: all patterns are compiled
once at import, the numeric pass is skipped outright for digit-free text, and
results are memoized (agents repeat greetings, prompts and canned lines).
"""

from __future__ import annotations

import re
from functools import lru_cache

__all__ = ["normalize_tts_text", "int_to_uzbek", "ordinal_to_uzbek"]

# --------------------------------------------------------------------------
# Number -> words
# --------------------------------------------------------------------------

_UNITS = (
    "nol",
    "bir",
    "ikki",
    "uch",
    "to'rt",
    "besh",
    "olti",
    "yetti",
    "sakkiz",
    "to'qqiz",
)
_TENS = (
    "",
    "o'n",
    "yigirma",
    "o'ttiz",
    "qirq",
    "ellik",
    "oltmish",
    "yetmish",
    "sakson",
    "to'qson",
)
# Descending scales. `bare` marks the scales Uzbek says without a leading
# "bir": 1000 is "ming", not "bir ming"; 100 is "yuz", not "bir yuz".
_SCALES = (
    (10**12, "trillion", False),
    (10**9, "milliard", False),
    (10**6, "million", False),
    (10**3, "ming", True),
    (100, "yuz", True),
)

# Runs this long are identifiers (phone numbers, card numbers, appeal codes),
# not quantities — read them digit by digit instead of as one huge numeral.
_DIGIT_BY_DIGIT_LEN = 10

_VOWELS = frozenset("aeiou")


def int_to_uzbek(value: int) -> str:
    """Spell a non-negative integer in Uzbek. 28 -> "yigirma sakkiz"."""
    if value < 0:
        return "minus " + int_to_uzbek(-value)
    if value < 10:
        return _UNITS[value]
    if value < 100:
        tens, unit = divmod(value, 10)
        return _TENS[tens] if unit == 0 else f"{_TENS[tens]} {_UNITS[unit]}"

    for scale, name, bare in _SCALES:
        if value >= scale:
            count, rest = divmod(value, scale)
            head = name if (bare and count == 1) else f"{int_to_uzbek(count)} {name}"
            return head if rest == 0 else f"{head} {int_to_uzbek(rest)}"

    return _UNITS[value]  # unreachable; keeps the type checker honest


def ordinal_to_uzbek(value: int) -> str:
    """Ordinal form: 3 -> "uchinchi", 2025 -> "ikki ming yigirma beshinchi"."""
    words = int_to_uzbek(value)
    return words + ("nchi" if words[-1] in _VOWELS else "inchi")


def _digits_one_by_one(digits: str) -> str:
    return " ".join(_UNITS[int(d)] for d in digits)


def _spell(digits: str) -> str:
    """Spell a digit run: as a numeral, or digit-by-digit when it is an ID."""
    if len(digits) >= _DIGIT_BY_DIGIT_LEN:
        return _digits_one_by_one(digits)
    stripped = digits.lstrip("0")
    # All zeros: one word per zero, so "00" is not voiced as a single "nol".
    if not stripped:
        return _digits_one_by_one(digits)
    # Leading zeros mean a code, not a quantity ("007", "05").
    if stripped != digits:
        return _digits_one_by_one(digits)
    return int_to_uzbek(int(stripped))


# --------------------------------------------------------------------------
# Patterns (compiled once)
# --------------------------------------------------------------------------

_STATIC_REPLACEMENTS = tuple(
    (re.compile(pattern, re.IGNORECASE), replacement)
    for pattern, replacement in (
        (r"\bmy\s*\.?\s*gov\s*\.?\s*uz\b", "may nuqta gov nuqta uz"),
        (r"\bmygov\.uz\b", "may nuqta gov nuqta uz"),
        (r"\bYIDXP\b", "Yagona interaktiv davlat xizmatlari portali"),
        (r"\bDXM\b", "Davlat xizmatlari markazi"),
        (r"\bBHM\b", "bazaviy hisoblash miqdori"),
    )
)

_DASHES = r"\-‐‑‒–—"

# Order matters — each pattern consumes the digits the later ones would
# otherwise mis-read.
# A leading "+" marks an E.164 phone number, voiced the way an Uzbek speaker
# dictates one: "plyus", country code digit by digit, then each remaining
# group as a numeral. See `_sub_phone`.
_RE_PHONE = re.compile(r"\+\s*(\d[\d\s\-()]*\d|\d)")
_RE_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_RE_PERCENT = re.compile(r"(\d[\d\s.,']*)\s*%")
_RE_RANGE = re.compile(rf"(\d[\d\s.,']*)\s*[{_DASHES}]\s*(\d[\d\s.,']*)(?=\s|$|[^\w{_DASHES}])")
_RE_ORDINAL = re.compile(rf"(\d[\d\s.,']*)[{_DASHES}](?=[^\W\d_])")
# 1 234 567 / 1'234'567 / 1.234.567 / 1,234,567 -> one number
_RE_GROUPED = re.compile(r"\b(\d{1,3}(?:([\s.,'])\d{3})+)\b")
_RE_DECIMAL = re.compile(r"\b(\d+)[.,](\d+)\b")
_RE_INT = re.compile(r"\d+")

# The grouping regexes above accept `\s`, which in Python is Unicode-aware and
# matches U+00A0, U+202F and U+2009 — all three occur in this KB. Stripping only
# the ASCII separators left those in place and `int()` then raised ValueError
# inside `NavaiWSTTS.synthesize()`, which does not catch it: the caller hears
# nothing at all. Every caller passes a string a digit regex already matched, so
# discarding every non-digit is both safe and total.
_RE_NON_DIGIT = re.compile(r"\D")
# Grouping and apostrophes are never a decimal point, so `_spell_amount` clears
# them before deciding; same Unicode-space problem, same fix.
_RE_GROUPING_SPACE = re.compile(r"[\s']")


def _only_digits(raw: str) -> str:
    return _RE_NON_DIGIT.sub("", raw)


def _phone_group(digits: str) -> str:
    """One dictated phone group. Hundreds keep the explicit "bir": 123 is
    "bir yuz yigirma uch", not the bare-quantity form "yuz yigirma uch"."""
    if digits.lstrip("0") != digits:  # 05, 007 — a code, not a quantity
        return _digits_one_by_one(digits)
    words = int_to_uzbek(int(digits))
    return f"bir {words}" if words.startswith("yuz") else words


def _sub_phone(match: re.Match[str]) -> str:
    raw = match.group(1)
    # Split on whatever separates the groups — space, hyphen or parens — so
    # "+998 90 123 45 67", "+998-90-123-45-67" and "+998 (90) 123-45-67" all
    # yield the same groups. Anything non-digit is a separator by definition.
    written_groups = _RE_INT.findall(raw)
    digits = "".join(written_groups)

    # Dictation groups are at most 3 digits; a longer one means the number was
    # only partly separated ("+998(90)1234567"), so the written grouping is not
    # trustworthy and we fall through to the canonical split below.
    if len(written_groups) > 1 and all(len(g) <= 3 for g in written_groups[1:]):
        # Respect the grouping as written: "+998 90 123 45 67".
        country, groups = written_groups[0], written_groups[1:]
    elif len(digits) == 12 and digits.startswith("998"):
        # Unspaced UZ number: split into the standard 998 XX XXX XX XX.
        country = "998"
        rest = digits[3:]
        groups = [rest[:2], rest[2:5], rest[5:7], rest[7:]]
    else:
        # Unknown shape — safest is one digit at a time.
        return f"plyus {_digits_one_by_one(digits)}"

    spoken = " ".join(_phone_group(g) for g in groups)
    return f"plyus {_digits_one_by_one(country)} {spoken}"


def _sub_time(match: re.Match[str]) -> str:
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:  # not a clock — leave it to the later passes
        return match.group(0)
    if minute == 0:
        return int_to_uzbek(hour)
    return f"{int_to_uzbek(hour)} {int_to_uzbek(minute)}"


# A single "." or "," with a fraction that is NOT three digits is a decimal
# point ("3,5"); a three-digit fraction is a thousands group ("1,500" -> 1500).
_RE_DECIMAL_TOKEN = re.compile(r"^(\d+)[.,](\d{1,2}|\d{4,})$")


def _spell_amount(raw: str) -> str:
    """Spell a numeric token that may carry a decimal part.

    ``_only_digits`` treats "." and "," as thousands separators, so it must not
    be used on tokens that could be decimals — "3,5" would collapse to 35. This
    routes a real decimal through the "butun" form and everything else through
    the plain numeral path.
    """
    # Spaces and apostrophes are only ever grouping, never a decimal point.
    compact = _RE_GROUPING_SPACE.sub("", raw)
    m = _RE_DECIMAL_TOKEN.match(compact)
    if m:
        return f"{_spell(m.group(1))} butun {_spell(m.group(2))}"
    return _spell(_only_digits(raw))


def _sub_percent(match: re.Match[str]) -> str:
    return f"{_spell_amount(match.group(1))} foiz"


def _sub_range(match: re.Match[str]) -> str:
    start, end = _spell_amount(match.group(1)), _spell_amount(match.group(2))
    return f"{start} dan {end} gacha"


def _sub_ordinal(match: re.Match[str]) -> str:
    digits = _only_digits(match.group(1))
    if len(digits) >= _DIGIT_BY_DIGIT_LEN or digits.lstrip("0") != digits:
        return f"{_spell(digits)} "
    return ordinal_to_uzbek(int(digits)) + " "


def _sub_grouped(match: re.Match[str]) -> str:
    return _spell(_only_digits(match.group(1)))


def _sub_decimal(match: re.Match[str]) -> str:
    whole, frac = match.group(1), match.group(2)
    return f"{_spell(whole)} butun {_spell(frac)}"


def _sub_int(match: re.Match[str]) -> str:
    # Digits glued to letters belong to an identifier, code or hash
    # ("6973d1f88b45", "A4") — never a quantity. Spelling those as a numeral
    # produces garbage, so read them digit by digit.
    text, start, end = match.string, match.start(), match.end()
    before = text[start - 1] if start else ""
    after = text[end] if end < len(text) else ""
    if before.isalpha() or after.isalpha():
        # Pad so the words do not fuse with the surrounding letters; the final
        # whitespace collapse tidies up.
        return f" {_digits_one_by_one(match.group(0))} "
    return _spell(match.group(0))


def _spell_numbers(text: str) -> str:
    text = _RE_PHONE.sub(_sub_phone, text)
    text = _RE_TIME.sub(_sub_time, text)
    text = _RE_PERCENT.sub(_sub_percent, text)
    text = _RE_RANGE.sub(_sub_range, text)
    text = _RE_ORDINAL.sub(_sub_ordinal, text)
    text = _RE_GROUPED.sub(_sub_grouped, text)
    text = _RE_DECIMAL.sub(_sub_decimal, text)
    return _RE_INT.sub(_sub_int, text)


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

_RE_WHITESPACE = re.compile(r"\s+")


@lru_cache(maxsize=1024)
def _normalize_cached(text: str) -> str:
    normalized = text
    for pattern, replacement in _STATIC_REPLACEMENTS:
        normalized = pattern.sub(replacement, normalized)

    # Skip the whole numeric pipeline when there is nothing to spell — the
    # common case for most conversational turns.
    if any(ch.isdigit() for ch in normalized):
        normalized = _spell_numbers(normalized)

    return _RE_WHITESPACE.sub(" ", normalized).strip()


def _is_uzbek(language: str | None) -> bool:
    # Accept bare codes ("uz") and locales ("uz-UZ"); default to Uzbek when the
    # caller does not specify, preserving the original single-language behavior.
    return not language or language.lower().startswith("uz")


def normalize_tts_text(text: str, language: str | None = None) -> str:
    """Normalize text for TTS: expand acronyms, spell digits as Uzbek words.

    The number words and acronym expansions are Uzbek-specific, so they are
    applied ONLY for Uzbek. A Russian turn on a bilingual tenant (e.g. paynet,
    which uses the Yandex voice for both languages) must keep its digits intact
    so the Russian voice reads them natively — spelling "12500" as Uzbek
    "o'n ikki ming besh yuz" in a Russian sentence is a regression.
    """
    if not text or not _is_uzbek(language):
        return text
    return _normalize_cached(text)
