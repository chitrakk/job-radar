"""CV scoring, slop detection, outreach research and the tracker.

The LLM is always mocked here. These tests exist to pin down the parts that must hold
regardless of what a model returns — score clamping, grounding rules, and the promise that
nothing in the outreach path sends anything.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from jobradar.career import cv as cvmod
from jobradar.career import outreach as omod
from jobradar.career import sheets
from jobradar.career.slop import clean_report, find, language_score, penalty
from jobradar.models import Job

BAD_CV = """Dynamic, results-driven professional passionate about excellence.
- Responsible for managing the reporting pipeline
- Responsible for building dashboards
- Responsible for coordinating with teams
- Significantly improved a legacy process
"""

GOOD_CV = """Business Analyst, Acme — Bengaluru, 2023-present
- Cut month-end close from 9 days to 4 by rebuilding reconciliation in SQL
- Built a Power BI dashboard used daily by 40 people across 3 business units
- Migrated 2.1M customer records with zero data-loss incidents
"""


# --------------------------------------------------------------------------- slop


def test_slop_penalises_buzzwords_and_duty_statements() -> None:
    score, hits = language_score(BAD_CV)
    assert score == 0.0
    ids = {h.id for h in hits}
    assert "cv_buzzword_stack" in ids
    assert "responsible_for" in ids
    assert "vague_intensifier" in ids


def test_quantified_cv_scores_full_language_marks() -> None:
    score, hits = language_score(GOOD_CV)
    assert score == 10.0
    assert hits == []


def test_outreach_cliches_are_caught() -> None:
    note = (
        "I hope this message finds you well. I'm reaching out because I've long admired "
        "your work and I would be a great fit for this role."
    )
    ids = {h.id for h in find(note, kind="linkedin_note")}
    assert "outreach_hope_this" in ids
    assert "reaching_out" in ids
    assert "outreach_flattery" in ids
    assert "would_be_great_fit" in ids


def test_linkedin_note_over_limit_is_flagged() -> None:
    ids = {h.id for h in find("x" * 320, kind="linkedin_note")}
    assert "note_too_long" in ids
    # The same length is fine in a CV, where no 300-char limit exists.
    assert "note_too_long" not in {h.id for h in find("x" * 320, kind="cv")}


def test_penalty_is_capped_at_the_criterion_weight() -> None:
    hits = find(BAD_CV * 10)  # far more than 10 points of issues
    assert penalty(hits, cap=10.0) == 10.0


def test_binary_contrast_pattern() -> None:
    assert "binary_contrast" in {
        h.id for h in find("It's not just a job, it's a mission.", kind="email")
    }


def test_clean_text_reports_clean() -> None:
    assert clean_report("Cut reporting time 40% by rewriting the ETL in SQL.")["clean"] is True


# --------------------------------------------------------------------------- cv scoring


def _mock_gemini(payload: dict) -> None:
    respx.post(url__startswith="https://generativelanguage.googleapis.com").mock(
        return_value=httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]},
        )
    )


@respx.mock
async def test_score_cv_clamps_inflated_model_scores(monkeypatch) -> None:
    """A model returning 20 for a criterion worth 8 must not push the total past 100."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    _mock_gemini(
        {
            "scores": {
                cid: {"score": 999, "reason": "inflated"}
                for cid in [
                    c["id"] for c in cvmod.rubric()["criteria"] if c["id"] != cvmod.DETERMINISTIC
                ]
            },
            "top_fixes": [],
            "summary": "s",
        }
    )

    report = await cvmod.score_cv(GOOD_CV)

    assert report["total"] <= 100
    for cid, row in report["scores"].items():
        assert row["score"] <= row["max"], f"{cid} exceeded its max"
    # Every non-deterministic criterion was clamped to full marks, plus a clean language
    # score, so this particular CV maxes out.
    assert report["total"] == 100


@respx.mock
async def test_score_cv_handles_negative_and_missing_scores(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    _mock_gemini(
        {
            "scores": {"impact": {"score": -50, "reason": "bad"}},
            "top_fixes": [],
            "summary": "s",
        }
    )

    report = await cvmod.score_cv(GOOD_CV)

    assert report["scores"]["impact"]["score"] == 0.0
    # Criteria the model omitted default to zero rather than vanishing from the total.
    assert report["scores"]["structure"]["score"] == 0.0
    assert report["total"] == 10.0  # only the deterministic language score survives


@respx.mock
async def test_score_cv_falls_back_when_the_model_returns_junk(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    respx.post(url__startswith="https://generativelanguage.googleapis.com").mock(
        return_value=httpx.Response(200, json={"candidates": []})
    )

    report = await cvmod.score_cv(BAD_CV)

    # A partial report that says so beats a fabricated number.
    assert report["partial"] is True
    assert report["total"] is None


async def test_score_cv_without_a_key_is_partial_not_invented(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    report = await cvmod.score_cv(BAD_CV)

    assert report["partial"] is True
    assert report["total"] is None
    assert "no LLM key" in report["summary"]


async def test_score_cv_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="empty"):
        await cvmod.score_cv("   ")


def test_bands_cover_the_whole_range() -> None:
    for total, expected in [
        (95, "Strong"),
        (75, "Solid"),
        (60, "Needs work"),
        (40, "Weak"),
        (5, "Rebuild"),
    ]:
        assert cvmod.band_for(total)["label"] == expected


def test_rubric_weights_sum_to_one_hundred() -> None:
    assert sum(c["weight"] for c in cvmod.rubric()["criteria"]) == cvmod.rubric()["total"] == 100


# --------------------------------------------------------------------------- outreach


def test_email_pattern_inference_ignores_generic_addresses() -> None:
    # info@ and support@ exist everywhere and say nothing about how people are named.
    assert omod.infer_pattern(["info@acme.com", "support@acme.com"], "acme.com") == ("", "")

    key, evidence = omod.infer_pattern(["priya.sharma@acme.com", "info@acme.com"], "acme.com")
    assert key == "first.last"
    assert "priya.sharma" in evidence


def test_email_pattern_ignores_other_domains() -> None:
    assert omod.infer_pattern(["a.b@other.com"], "acme.com") == ("", "")


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("first.last", "priya.sharma@acme.com"),
        ("firstlast", "priyasharma@acme.com"),
        ("f.last", "p.sharma@acme.com"),
        ("flast", "psharma@acme.com"),
        ("first", "priya@acme.com"),
    ],
)
def test_build_email_renders_each_pattern(pattern: str, expected: str) -> None:
    assert omod.build_email("Priya Sharma", "acme.com", pattern) == expected


def test_build_email_refuses_a_single_name() -> None:
    # Guessing a surname would produce a confident wrong address.
    assert omod.build_email("Priya", "acme.com", "first.last") == ""


def test_domain_extraction_skips_ats_and_aggregator_hosts() -> None:
    assert omod.domain_from_url("https://boards.greenhouse.io/acme/jobs/1") == ""
    assert omod.domain_from_url("https://in.linkedin.com/jobs/view/1") == ""
    assert omod.domain_from_url("https://careers.acme.com/jobs/1") == "careers.acme.com"


def test_linkedin_helper_returns_a_search_url_not_a_scrape() -> None:
    url = omod.linkedin_people_search("Acme")
    assert url.startswith("https://www.linkedin.com/search/results/people/")
    assert "Acme" in url.replace("+", " ").replace("%20", " ")


@respx.mock
async def test_draft_outreach_without_a_key_still_researches_and_sends_nothing(
    monkeypatch,
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    job = Job(
        title="Data Analyst",
        company="Acme",
        location="Bengaluru",
        url="https://careers.acme.com/jobs/1",
    )
    # Every company page 404s; research should degrade quietly.
    respx.get(url__startswith="https://careers.acme.com").mock(return_value=httpx.Response(404))

    draft = await omod.draft_outreach(job, candidate_summary=GOOD_CV)

    assert draft.linkedin_note == ""
    assert any("No LLM key" in w for w in draft.warnings)
    # The human always gets a way to find the person themselves.
    assert any(c.linkedin_search_url for c in draft.contacts)


@respx.mock
async def test_draft_outreach_flags_slop_in_its_own_output(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    job = Job(title="Data Analyst", company="Acme", url="https://acme.com/jobs/1")
    respx.get(url__startswith="https://acme.com").mock(return_value=httpx.Response(404))
    _mock_gemini(
        {
            "linkedin_note": "I hope this message finds you well and I would be a great fit.",
            "email_subject": "Application",
            "email_body": "I'm reaching out because I've long admired your work.",
        }
    )

    draft = await omod.draft_outreach(job, candidate_summary=GOOD_CV)

    # The drafter is held to the same standard as the CV checker.
    assert draft.slop["clean"] is False
    assert draft.slop["linkedin_note"]
    assert draft.slop["email_body"]


@respx.mock
async def test_contact_discovery_rejects_job_titles_as_names(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    _mock_gemini(
        {
            "contacts": [
                {"name": "Hiring Manager", "role": "recruiter", "evidence": "x"},
                {"name": "Talent Team", "role": "ta", "evidence": "x"},
                {"name": "Priya", "role": "recruiter", "evidence": "x"},
                {"name": "Priya Sharma", "role": "Recruiter", "evidence": "Contact Priya Sharma"},
            ]
        }
    )

    async with httpx.AsyncClient() as client:
        contacts = await omod.discover_contacts(
            client, Job(title="X", company="Acme", url="https://acme.com"), "acme.com", "text"
        )

    names = [c.name for c in contacts if c.name]
    # Placeholders and single names are dropped; only the real full name survives.
    assert names == ["Priya Sharma"]


# --------------------------------------------------------------------------- sheets


def test_cv_row_survives_a_partial_report() -> None:
    row = sheets.cv_row({"total": None, "scores": {}, "top_fixes": []}, target_role="Analyst")
    assert row["Score"] == "partial"
    assert row["Target Role"] == "Analyst"


def test_application_row_has_every_column_the_sheet_expects() -> None:
    job = Job(title="Data Analyst", company="Acme", location="Bengaluru", url="https://x/1")
    row = sheets.application_row(job, status="Applied")
    for column in ("Job ID", "Title", "Company", "Location", "URL", "Status"):
        assert column in row
    assert row["Status"] == "Applied"


async def test_push_without_configuration_explains_the_fix(monkeypatch) -> None:
    monkeypatch.delenv("SHEETS_WEBAPP_URL", raising=False)
    monkeypatch.delenv("SHEETS_SECRET", raising=False)
    with pytest.raises(sheets.SheetsError, match="SHEETS_WEBAPP_URL"):
        await sheets.push("applications", [{"Job ID": "1"}])


@respx.mock
async def test_push_surfaces_an_apps_script_error(monkeypatch) -> None:
    monkeypatch.setenv("SHEETS_WEBAPP_URL", "https://script.google.com/macros/s/x/exec")
    monkeypatch.setenv("SHEETS_SECRET", "s")
    respx.post(url__startswith="https://script.google.com").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "bad secret"})
    )
    with pytest.raises(sheets.SheetsError, match="bad secret"):
        await sheets.push("applications", [{"Job ID": "1"}])


@respx.mock
async def test_push_detects_a_login_page_instead_of_json(monkeypatch) -> None:
    """The commonest setup mistake is leaving the deployment private, which returns HTML."""
    monkeypatch.setenv("SHEETS_WEBAPP_URL", "https://script.google.com/macros/s/x/exec")
    monkeypatch.setenv("SHEETS_SECRET", "s")
    respx.post(url__startswith="https://script.google.com").mock(
        return_value=httpx.Response(200, text="<html>Sign in</html>")
    )
    with pytest.raises(sheets.SheetsError, match="Anyone"):
        await sheets.push("applications", [{"Job ID": "1"}])
