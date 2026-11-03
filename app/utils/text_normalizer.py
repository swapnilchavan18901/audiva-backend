"""AI layer — clean up LLM text so the TTS voice reads it naturally.

The LLM sometimes writes things that look fine on screen but sound wrong:
  "2026-10-12T10:00"  → read as "two thousand twenty six dash ten dash..."
  "9175541890"        → read as "nine hundred seventeen crore..."
  "Dr. Mehta, ₹500"   → "D R dot Mehta, rupee symbol 500"
  "**Booked!** ✅"    → stars and emoji read out or glitch

normalize_for_speech() fixes these. It only changes what is SPOKEN — the chat
history (LLM context) keeps the original text.

English rules run for en-* languages. For other languages only the safe,
language-neutral rules run (markdown, emoji, phone digits, whitespace), because
the English words this file produces ("Monday", "rupees") would sound wrong there.
"""

import re
from datetime import date, datetime

from pipecat.utils.text.base_text_filter import BaseTextFilter

from app.constants.constants import _ONES, _TENS, ABBREVIATIONS, DAYS, EMOJI, MONTHS

# ---------- helpers ----------


def _speak_time(hour: int, minute: int) -> str:
    """13, 30 → '1:30 PM';  10, 0 → '10 AM';  12, 0 → '12 noon';  0, 0 → '12 midnight'."""
    if (hour, minute) == (12, 0):
        return "12 noon"
    if (hour, minute) == (0, 0):
        return "12 midnight"
    suffix = "AM" if hour < 12 else "PM"
    h = hour % 12 or 12
    return f"{h} {suffix}" if minute == 0 else f"{h}:{minute:02d} {suffix}"


def _speak_date(d: date) -> str:
    """date(2026, 10, 12) → 'Monday 12 October'."""
    return f"{d.strftime('%A')} {d.day} {MONTHS[d.month - 1]}"


def _spell_digits(digits: str) -> str:
    """'9175541890' → '9 1 7 5 5, 4 1 8 9 0' (groups of 5 after splitting, pause with a comma)."""
    d = re.sub(r"\D", "", digits)
    if len(d) == 10 or len(d) == 11 and d.startswith("0"):
        groups = [d[:5], d[5:]]
    else:
        groups = [d[i : i + 4] for i in range(0, len(d), 4)]
    return ", ".join(" ".join(g) for g in groups)


def _number_words(n: int) -> str:
    """0–99 → words: 10 → 'ten', 45 → 'forty five'."""
    if n < 20:
        return _ONES[n]
    tens, ones = divmod(n, 10)
    return _TENS[tens] + ("" if ones == 0 else f" {_ONES[ones]}")


# ---------- rules (order matters) ----------


def _strip_markdown(t: str) -> str:
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)  # [text](url) → text
    t = re.sub(r"`([^`]*)`", r"\1", t)  # `code`
    t = re.sub(r"(\*\*|__)(.+?)\1", r"\2", t)  # **bold**
    t = re.sub(r"(?<!\w)[*_](.+?)[*_](?!\w)", r"\1", t)  # *italic*
    t = re.sub(r"^\s*#{1,6}\s*", "", t, flags=re.MULTILINE)  # # heading
    t = re.sub(
        r"^\s*(?:[-*•]|\d+\.)\s+", "", t, flags=re.MULTILINE
    )  # - bullet / 1. item
    return t.replace("*", "").replace("~", "").replace("|", ", ")


def _iso_datetimes(t: str) -> str:
    def repl(m):
        try:
            dt = datetime(int(m[2]), int(m[3]), int(m[4]), int(m[5]), int(m[6]))
        except ValueError:
            return m[0]
        on = (
            "on " if m[1] else ""
        )  # "at 2026-10-12T10:00" → "on Monday 12 October at 10 AM"
        return f"{on}{_speak_date(dt.date())} at {_speak_time(dt.hour, dt.minute)}"

    return re.sub(
        r"(\bat\s+)?\b(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::\d{2})?\b", repl, t
    )


def _iso_dates(t: str) -> str:
    def repl(m):
        try:
            return _speak_date(date(int(m[1]), int(m[2]), int(m[3])))
        except ValueError:
            return m[0]

    return re.sub(r"\b(\d{4})-(\d{2})-(\d{2})\b", repl, t)


def _slash_dates(t: str) -> str:
    """Indian DD/MM/YYYY → '12 October 2026'."""

    def repl(m):
        day, month, year = int(m[1]), int(m[2]), int(m[3])
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return m[0]
        return f"{day} {MONTHS[month - 1]} {year}"

    return re.sub(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", repl, t)


def _phones(t: str) -> str:
    # +91 98765 43210 / +91-9876543210
    t = re.sub(
        r"\+91[\s-]?((?:\d[\s-]?){9}\d)\b",
        lambda m: "plus 9 1, " + _spell_digits(m[1]),
        t,
    )
    # 98765 43210 / 98765-43210 / 9876543210 / 02194 123456 / 022-12345678
    t = re.sub(
        r"(?<!\d)(?<!\d,)(\d{3,5})[\s-](\d{5,8})(?!\d|,\d)|(?<!\d)(?<!\d,)(\d{10,11})(?!\d|,\d)",
        lambda m: _spell_digits(m[0]),
        t,
    )
    return t


def _pincodes(t: str) -> str:
    """Six digits that are not amounts are spelled digit by digit."""
    return re.sub(
        r"(?<![\d,₹])\b(\d{6})\b(?!\s*(?:rupees|rs\b|/-))",
        lambda m: " ".join(m[1]),
        t,
        flags=re.IGNORECASE,
    )


def _currency(t: str) -> str:
    def amount(s: str) -> str:
        return s.replace(",", "")

    t = re.sub(
        r"(?:₹|\bRs\.?|\bINR)\s?(\d[\d,]*(?:\.\d+)?)(?:/-)?",
        lambda m: f"{amount(m[1])} rupees",
        t,
    )
    t = re.sub(r"\b(\d[\d,]*)\s?/-", lambda m: f"{amount(m[1])} rupees", t)
    return t


def _times(t: str) -> str:
    def with_suffix(m):
        h, mi, suffix = int(m[1]), int(m[2]), m[3].upper().replace(".", "")
        return f"{h} {suffix}" if mi == 0 else f"{h}:{mi:02d} {suffix}"

    # 10:00 am / 10:30 P.M. → normalise, drop ":00"
    t = re.sub(
        r"\b(\d{1,2}):(\d{2})\s*([AaPp]\.[Mm]\.|[AaPp][Mm])(?![a-z])", with_suffix, t
    )
    # 10am / 5 pm → 10 AM / 5 PM
    t = re.sub(
        r"\b(\d{1,2})\s*([AaPp])(?:\.[Mm]\.|[Mm])(?![a-z])",
        lambda m: f"{m[1]} {m[2].upper()}M",
        t,
    )
    # bare 24-hour 13:30 / 09:00 → 1:30 PM / 9 AM
    t = re.sub(
        r"\b([01]?\d|2[0-3]):([0-5]\d)\b(?!\s*[AaPp]\.?[Mm])",
        lambda m: _speak_time(int(m[1]), int(m[2])),
        t,
    )
    return t


def _day_ranges(t: str) -> str:
    def day(abbr: str) -> str:
        return DAYS[abbr[:3].capitalize()]

    pattern = r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\s*[-–—]\s*(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\b"
    t = re.sub(pattern, lambda m: f"{day(m[1])} to {day(m[2])}", t)
    return re.sub(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b", lambda m: DAYS[m[1]], t)


def _ranges(t: str) -> str:
    """9–5, 10-12, 9 AM–5 PM → '9 to 5' …  (phones and dates are already handled)."""
    t = re.sub(r"(\d(?:\s?[AP]M)?)\s*[–—]\s*(\d)", r"\1 to \2", t)
    t = re.sub(
        r"(?<![\d-])(\d{1,2}(?:\s?[AP]M)?)\s*-\s*(\d{1,2})(?![\d-])", r"\1 to \2", t
    )
    return t


def _number_compounds(t: str) -> str:
    """'10-digit', '30-minute', '2-day' → 'ten digit', 'thirty minute', 'two day'.

    Bulbul reads a hyphenated number+word as a code and spells it out ("one zero D I G I T").
    """
    return re.sub(
        r"\b(\d{1,2})-([A-Za-z]{2,})\b",
        lambda m: f"{_number_words(int(m[1]))} {m[2]}",
        t,
    )


def _abbreviations(t: str) -> str:
    for pattern, replacement in ABBREVIATIONS:
        t = re.sub(pattern, replacement, t)
    return t


def _emails(t: str) -> str:
    def repl(m):
        user, domain = m[1], m[2]
        return f"{user} at {domain.replace('.', ' dot ')}"

    return re.sub(r"\b([\w.+-]+)@([\w-]+(?:\.[\w-]+)+)\b", repl, t)


def _symbols(t: str) -> str:
    t = t.replace("&", " and ").replace("%", " percent")
    t = re.sub(r"\s\+\s", " plus ", t)
    t = re.sub(
        r"(?<=\w)/(?=[A-Za-z])", " or ", t
    )  # morning/evening → morning or evening
    return t


def _tidy(t: str) -> str:
    t = re.sub(r"([!?.]){2,}", r"\1", t)  # !!! → !
    t = re.sub(r"\s+([,.!?])", r"\1", t)  # space before punctuation
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip()


# ---------- public API ----------


def normalize_for_speech(text: str, language: str = "en-IN") -> str:
    t = EMOJI.sub("", text)
    t = _strip_markdown(t)
    t = _phones(t)
    if language.lower().startswith("en"):
        t = _emails(t)
        t = _iso_datetimes(t)
        t = _iso_dates(t)
        t = _slash_dates(t)
        t = _currency(t)
        t = _pincodes(t)
        t = _times(t)
        t = _day_ranges(t)
        t = _abbreviations(t)
        t = _number_compounds(t)
        t = _ranges(t)
        t = _symbols(t)
    return _tidy(t)


class SpeechNormalizer(BaseTextFilter):
    """Pipecat text filter: runs on each sentence right before it's sent to TTS."""

    def __init__(self, language: str = "en-IN"):
        self._language = language

    async def filter(self, text: str) -> str:
        return normalize_for_speech(text, self._language)

    async def update_settings(self, settings):
        self._language = settings.get("language", self._language)

    async def handle_interruption(self):
        pass

    async def reset_interruption(self):
        pass
