"""Salary parsing and classification.

Salary is the field most likely to be wrong in a way nobody notices, because a plausible
looking number renders fine. These cases lock in the two rules that matter: Indian units
parse correctly, and ambiguous input yields nothing rather than a guess.
"""

from __future__ import annotations

import pytest

from jobradar.models import RemoteKind, Seniority
from jobradar.textutil import (
    detect_remote,
    detect_seniority,
    html_to_text,
    parse_salary,
    truncate,
)


@pytest.mark.parametrize(
    ("text", "lo", "hi", "currency"),
    [
        # Indian conventions
        ("12-18 LPA", 1_200_000, 1_800_000, "INR"),
        ("₹8,00,000 - ₹12,00,000 P.A.", 800_000, 1_200_000, "INR"),
        ("6 Lakh per annum", 600_000, 600_000, "INR"),
        ("Rs 50,000 per month", 600_000, 600_000, "INR"),
        ("1.2 Cr", 12_000_000, 12_000_000, "INR"),
        # Western conventions
        ("$170k - $200k", 170_000, 200_000, "USD"),
        ("$170,000 - $200,000", 170_000, 200_000, "USD"),
        ("$45 per hour", 93_600, 93_600, "USD"),
        ("£60,000 - £75,000", 60_000, 75_000, "GBP"),
    ],
)
def test_parse_salary_known_forms(text: str, lo: float, hi: float, currency: str) -> None:
    result = parse_salary(text)
    assert result, f"expected a salary from {text!r}"
    assert result["salary_min"] == pytest.approx(lo)
    assert result["salary_max"] == pytest.approx(hi)
    assert result["salary_currency"] == currency
    # Everything is normalised to an annual figure so the UI can compare across sources.
    assert result["salary_period"] == "year"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Competitive salary",
        "team of 5-10 engineers",
        "170 - 200",  # no currency, no period: could be anything
        "founded in 2019",
        "5 years of experience required",
    ],
)
def test_parse_salary_refuses_ambiguous(text: str) -> None:
    # A wrong salary is worse than a missing one, because the UI filters on it.
    assert parse_salary(text) == {}


def test_k_suffix_is_not_read_as_hourly() -> None:
    """Regression: `\\bk\\b` never matched "170k", so the scale was missed and the figure
    fell through to the hourly branch, inventing a $353,600 salary."""
    result = parse_salary("$170k - $200k")
    assert result["salary_max"] == pytest.approx(200_000)
    assert result["salary_max"] < 250_000


def test_lakh_implies_rupees_without_a_symbol() -> None:
    assert parse_salary("15 LPA")["salary_currency"] == "INR"


def test_html_to_text_keeps_structure_and_drops_scripts() -> None:
    html = "<div><script>evil()</script><p>Role</p><ul><li>Python</li><li>SQL</li></ul></div>"
    text = html_to_text(html)
    assert "evil" not in text
    assert "Role" in text
    assert "Python" in text and "SQL" in text


def test_truncate_does_not_split_a_word() -> None:
    out = truncate("alpha beta gamma delta", limit=12)
    assert out.endswith("…")
    assert "gam…" not in out


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (("Remote",), RemoteKind.REMOTE),
        (("Work from home",), RemoteKind.REMOTE),
        (("Hybrid - Bengaluru",), RemoteKind.HYBRID),
        (("Bengaluru, on-site",), RemoteKind.ONSITE),
        (("Bengaluru",), RemoteKind.UNKNOWN),
    ],
)
def test_detect_remote(fields: tuple[str, ...], expected: RemoteKind) -> None:
    assert detect_remote(*fields) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Data Science Intern", Seniority.INTERN),
        ("VP of Engineering", Seniority.EXEC),
        ("Senior Engineering Manager", Seniority.LEAD),  # lead outranks senior
        ("Senior Data Analyst", Seniority.SENIOR),
        ("Junior Developer", Seniority.ENTRY),
    ],
)
def test_detect_seniority_precedence(title: str, expected: Seniority) -> None:
    assert detect_seniority(title) == expected


def test_detect_seniority_falls_back_to_years_of_experience() -> None:
    assert detect_seniority("Data Analyst", "We want 6+ years of experience") == Seniority.SENIOR
