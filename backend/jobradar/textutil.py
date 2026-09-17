"""HTML-to-text and salary normalisation.

Indian boards quote pay in ways nothing off-the-shelf parses: "12-18 LPA", "₹8,00,000 -
₹12,00,000 P.A.", "6 Lakh per annum", "1.2 Cr". Normalising these to an annual number is
what makes a salary filter possible across Indian and international sources at once.
"""

from __future__ import annotations

import html
import re

from selectolax.parser import HTMLParser

from .models import RemoteKind, Seniority

# --------------------------------------------------------------------------- text


def html_to_text(raw: str) -> str:
    """Flatten an HTML job description to readable plain text."""
    if not raw:
        return ""
    if "<" not in raw:
        return re.sub(r"\s+", " ", html.unescape(raw)).strip()

    tree = HTMLParser(raw)
    for tag in tree.css("script, style, noscript"):
        tag.decompose()
    # Keep list and paragraph boundaries as newlines so the LLM and the UI both see structure.
    for tag in tree.css("li"):
        tag.insert_before("\n• ")
    for tag in tree.css("p, div, br, h1, h2, h3, h4, tr"):
        tag.insert_before("\n")

    text = tree.text(separator="")
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def truncate(text: str, limit: int = 8000) -> str:
    """Cap description length. Job pages carry long boilerplate footers that add no signal
    and would otherwise dominate LLM token spend."""
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " …"


# --------------------------------------------------------------------------- salary

_CURRENCY = [
    (re.compile(r"₹|\brs\.?\b|\binr\b", re.I), "INR"),
    (re.compile(r"\$|\busd\b", re.I), "USD"),
    (re.compile(r"£|\bgbp\b", re.I), "GBP"),
    (re.compile(r"€|\beur\b", re.I), "EUR"),
]

# Multipliers that turn a quoted figure into rupees/dollars.
# The suffix forms must use a lookbehind rather than \b: in "170k" and "12L" there is no
# word boundary between the digit and the letter, so \bk\b never matches. Getting this
# wrong silently turned "$170k" into "$170/hour" and invented a $353,600 salary.
_SCALE = [
    (re.compile(r"\b(lpa|lakhs?\s*p\.?a\.?|lacs?|lakhs?)\b", re.I), 100_000),
    (re.compile(r"(?<=\d)\s*l\b", re.I), 100_000),
    (re.compile(r"\b(crores?)\b|(?<=\d)\s*cr\b", re.I), 10_000_000),
    (re.compile(r"(?<=\d)\s*k\b", re.I), 1_000),
]

_PERIOD = [
    (re.compile(r"\b(per\s*annum|p\.?a\.?|/\s*year|per\s*year|annual|yearly|lpa)\b", re.I), "year"),
    (re.compile(r"\b(per\s*month|p\.?m\.?|/\s*month|monthly)\b", re.I), "month"),
    (re.compile(r"\b(per\s*hour|/\s*hour|hourly|/hr)\b", re.I), "hour"),
]

_NUM = re.compile(r"\d[\d,.]*")

_PERIOD_TO_YEAR = {"year": 1, "month": 12, "hour": 2080}


def _to_float(token: str) -> float | None:
    # Indian digit grouping ("8,00,000") and Western ("800,000") both just lose commas.
    cleaned = token.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_salary(text: str) -> dict:
    """Best-effort salary extraction. Returns empty dict when nothing is confidently found.

    Deliberately conservative: a wrong salary is worse than a missing one, because the
    frontend lets people filter on it.
    """
    if not text:
        return {}
    blob = " ".join(text.split())[:300]

    currency = ""
    for pattern, code in _CURRENCY:
        if pattern.search(blob):
            currency = code
            break

    scale = 1
    for pattern, mult in _SCALE:
        if pattern.search(blob):
            scale = mult
            break

    # Lakh and crore are rupee units by definition — "12-18 LPA" never needs a ₹ to be INR.
    if not currency and scale in (100_000, 10_000_000):
        currency = "INR"

    period = ""
    for pattern, name in _PERIOD:
        if pattern.search(blob):
            period = name
            break

    numbers = [n for tok in _NUM.findall(blob) if (n := _to_float(tok)) is not None]
    # Drop stray years ("2026") and percentages that slip into the same sentence.
    numbers = [n for n in numbers if not (1900 <= n <= 2100 and scale == 1)]
    if not numbers:
        return {}

    lo = min(numbers) * scale
    hi = max(numbers) * scale

    # "12-18 LPA" gives lo=1.2M hi=1.8M; a lone figure gives lo == hi, which we keep as a floor.
    if not period:
        # An INR figure in the lakhs is annual by convention; a smaller one is monthly.
        if currency == "INR":
            period = "year" if hi >= 100_000 else "month"
        elif hi >= 10_000:
            period = "year"
        else:
            # Could be hourly, could be a headcount, a team size or a version number that
            # wandered into the same sentence. Guessing "hourly" here is how a "$170-200"
            # fragment becomes a $416,000 salary, so refuse instead.
            return {}

    factor = _PERIOD_TO_YEAR.get(period, 1)
    lo_annual, hi_annual = lo * factor, hi * factor

    # Sanity floor/ceiling — parses that land outside a plausible band are discarded rather
    # than shown, since they would poison the salary filter.
    if hi_annual < 1_000 or hi_annual > 500_000_000:
        return {}

    return {
        "salary_min": round(lo_annual, 2),
        "salary_max": round(hi_annual, 2),
        "salary_currency": currency,
        "salary_period": "year",
        "salary_text": blob[:120],
    }


# --------------------------------------------------------------------------- classification

_REMOTE = re.compile(
    r"\b(fully[\s-]?remote|work from home|wfh|remote[\s-]?first|100% remote)\b", re.I
)
_HYBRID = re.compile(r"\bhybrid\b", re.I)
_ONSITE = re.compile(r"\b(on[\s-]?site|in[\s-]?office|work from office|wfo)\b", re.I)


def detect_remote(*fields: str) -> RemoteKind:
    blob = " ".join(f for f in fields if f)
    if _HYBRID.search(blob):
        return RemoteKind.HYBRID
    if _REMOTE.search(blob) or re.search(r"\bremote\b", blob, re.I):
        return RemoteKind.REMOTE
    if _ONSITE.search(blob):
        return RemoteKind.ONSITE
    return RemoteKind.UNKNOWN


_SENIORITY = [
    (Seniority.INTERN, re.compile(r"\b(intern|internship|trainee|apprentice)\b", re.I)),
    (
        Seniority.EXEC,
        re.compile(r"\b(chief|cto|ceo|cfo|coo|vp|vice president|head of|director)\b", re.I),
    ),
    (Seniority.LEAD, re.compile(r"\b(lead|principal|staff|architect|manager)\b", re.I)),
    (Seniority.SENIOR, re.compile(r"\b(senior|sr\.?|sde\s*[3-9]|iii|iv)\b", re.I)),
    (
        Seniority.ENTRY,
        re.compile(r"\b(junior|jr\.?|entry|fresher|graduate|associate|sde\s*1|i{1,2}\b)\b", re.I),
    ),
]


def detect_seniority(title: str, description: str = "") -> Seniority:
    """Heuristic seniority from the title. Order matters — 'Senior Engineering Manager'
    should read as lead, and 'Intern' beats everything."""
    for level, pattern in _SENIORITY:
        if pattern.search(title):
            return level
    # Fall back to years-of-experience in the body, common on Indian postings.
    m = re.search(r"(\d+)\s*(?:\+|-|to)?\s*(?:\d+)?\s*(?:years?|yrs?)", description, re.I)
    if m:
        years = int(m.group(1))
        if years >= 8:
            return Seniority.LEAD
        if years >= 5:
            return Seniority.SENIOR
        if years >= 2:
            return Seniority.MID
        return Seniority.ENTRY
    return Seniority.UNKNOWN
