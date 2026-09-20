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

# A phrase in a job description that is actually quoting money, rather than a sentence
# that merely contains digits. Either a labelled field ("CTC: 12-18 LPA", "Salary — ₹8L")
# or a bare figure carrying a currency symbol or an Indian scale word.
_SALARY_PHRASE = re.compile(
    r"(?:\b(?:ctc|salary|package|compensation|remuneration)\b\s*[:\-–]?\s*"
    r"(?P<labelled>[^\n.;|]{0,40}))"
    r"|(?P<bare>(?:₹|\brs\.?\s*|\binr\s*)\s*\d[\d,.]*\s*(?:lakhs?|lacs?|lpa|crores?|cr|l|k)?"
    r"(?:\s*(?:-|–|to)\s*(?:₹|\brs\.?\s*|\binr\s*)?\s*\d[\d,.]*\s*"
    r"(?:lakhs?|lacs?|lpa|crores?|cr|l|k)?)?"
    r"|\d[\d,.]*\s*(?:-|–|to)\s*\d[\d,.]*\s*(?:lakhs?|lacs?|lpa|crores?)\b)",
    re.I,
)

# The next field in a pasted advert. A labelled salary runs until one of these starts.
_NEXT_FIELD = re.compile(
    r"\b(experience|exp|location|notice|qualification|skills?|department|industry|company|"
    r"role|position|vacanc|openings?|joining|shift|timing|education|gender|age)\b",
    re.I,
)

# A phrase is only a salary if it carries a currency or an Indian scale word. "2-5 years"
# does not, and must not be read as ₹5 lakh.
_HAS_MONEY = re.compile(
    r"₹|\brs\b|\binr\b|\$|£|€|\blakhs?\b|\blacs?\b|\blpa\b|\bcrores?\b|\bcr\b|(?<=\d)\s*[lk]\b",
    re.I,
)


def salary_from_text(text: str) -> dict:
    """Find a salary *quoted in prose*, or nothing.

    parse_salary() is deliberately willing to read a short label like "12 - 14 Lakh/Yr".
    Handing it the opening 300 characters of a job description is a different thing
    entirely, and it produced exactly the failure you would expect: a live posting whose
    description began "Hiring: Associate Product Manager ... CTC: 2021 LPA Experience"
    was published at ₹20.2 crore a year.

    So we locate a phrase that is actually about money and parse only that.
    """
    if not text:
        return {}
    for match in _SALARY_PHRASE.finditer(text[:4000]):
        phrase = match.group("labelled") or match.group("bare") or ""
        # "CTC: 2021 LPA Experience 2-5 years" — stop before the next field, or the years
        # of experience get read as the package.
        if cut := _NEXT_FIELD.search(phrase):
            phrase = phrase[: cut.start()]
        if not phrase.strip() or not _HAS_MONEY.search(phrase):
            continue
        pay = parse_salary(phrase)
        if pay:
            pay["salary_text"] = " ".join(phrase.split())[:120]
            return pay
    return {}


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
    # Drop stray years ("2026") and percentages that slip into the same sentence. A year
    # is just as likely next to a scale word: a live posting read "CTC: 2021 LPA", which
    # is a mangled "20-21 LPA", and 2021 lakh is ₹20.21 crore for an Associate Product
    # Manager. Nobody quotes a package as a four-digit lakh figure.
    if scale >= 100_000:
        numbers = [n for n in numbers if not (1900 <= n <= 2100)]
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
    # than shown, since they would poison the salary filter. The rupee ceiling is much
    # tighter than the generic one: ₹10 crore a year is already far beyond any advertised
    # job, so a figure above it is a parse error, not a windfall.
    if hi_annual < 1_000 or hi_annual > 500_000_000:
        return {}
    if currency == "INR" and hi_annual > 100_000_000:
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
