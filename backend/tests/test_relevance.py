"""Search relevance, measured.

"Results are not good" is not a thing you can fix by feel, so these tests pin the ranking
to concrete outcomes on a fixture corpus built from real titles seen in the live index.

The golden queries are "data analyst" and "data scientist" — deliberately, because they are
the hardest pair in the corpus: they share their strongest word with each other and with
"data engineer", which is exactly the case bag-of-words ranking gets wrong.

Guarantees asserted here:
  - an exact title match outranks everything
  - a known alias (BI Analyst) outranks a sibling role (Data Engineer)
  - a sibling role never outranks a true match
  - junk titles never reach the top
  - precision@10 stays above a floor
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobradar.models import Job, Query
from jobradar.search import rank, score
from jobradar.taxonomy import detect_family, is_noise_title, normalise, understand

NOW = datetime.now(UTC)


def make(title: str, *, company: str = "Acme", desc: str = "", days: int = 3, **kw) -> Job:
    return Job(
        title=title,
        company=company,
        location=kw.pop("location", "Bengaluru, India"),
        url=f"https://x/{abs(hash(title + company)) % 10**8}",
        description=desc,
        posted_at=NOW - timedelta(days=days),
        **kw,
    )


# Titles taken from the live corpus, plus the near-misses that used to rank alongside them.
CORPUS = [
    # --- true data-analyst roles ---
    make("Data Analyst", company="Indium"),
    make("Senior Data Analyst", company="Druva"),
    make("Sr. Data Analyst", company="Prudent"),
    make("Data Analyst/ Senior Data Analyst", company="Indium2"),
    make("Financial Data Analyst (SQL, Power BI-DAX)", company="SITA"),
    # --- same job, different name ---
    make("BI Analyst", company="Zeta", desc="SQL, Tableau and dashboards"),
    make("Business Intelligence Analyst", company="Meesho"),
    make("Business Analyst", company="Infosys", desc="SQL and Excel reporting"),
    make("Analytics Specialist", company="Swiggy", desc="Power BI dashboards"),
    # --- true data-scientist roles ---
    make("Data Scientist", company="IBM"),
    make("Senior Data Scientist", company="Ecolab"),
    make("Data Scientist-Artificial Intelligence", company="IBM2"),
    make("Applied Scientist", company="Amazon", desc="machine learning, python, statistics"),
    make("Machine Learning Scientist", company="Sarvam"),
    # --- siblings that share vocabulary but are different jobs ---
    make("Data Engineer", company="GitLab", desc="spark airflow dbt pipelines"),
    make("Senior Data Engineer", company="Databricks"),
    make("Analytics Engineer", company="Atlan", desc="dbt and snowflake"),
    make("Machine Learning Engineer", company="Cursor", desc="pytorch mlops serving"),
    make("Software Engineer", company="Stripe", desc="java microservices"),
    make("Financial Analyst", company="EY", desc="valuation and financial modelling"),
    make("Product Manager", company="Razorpay"),
    # --- body mentions the query but the role is unrelated ---
    make(
        "Office Administrator",
        company="Globex",
        desc="You will support our data analyst team with scheduling and travel.",
    ),
    make(
        "Sales Executive",
        company="Initech",
        desc="Sell our analytics platform to data science teams across India.",
    ),
    # --- junk ---
    make("See posting", company="HNStartup", desc="we do data science and analytics"),
    make("Multiple roles", company="HNStartup2", desc="data analyst and engineer wanted"),
]

ANALYST_IDS = {
    "Data Analyst",
    "Senior Data Analyst",
    "Sr. Data Analyst",
    "Data Analyst/ Senior Data Analyst",
    "Financial Data Analyst (SQL, Power BI-DAX)",
    "BI Analyst",
    "Business Intelligence Analyst",
    "Business Analyst",
    "Analytics Specialist",
}
SCIENTIST_IDS = {
    "Data Scientist",
    "Senior Data Scientist",
    "Data Scientist-Artificial Intelligence",
    "Applied Scientist",
    "Machine Learning Scientist",
}


def q(text: str, **kw) -> Query:
    kw.setdefault("location", "")
    kw.setdefault("max_age_days", 0)
    kw.setdefault("limit", 0)
    return Query(keywords=[text], **kw)


def ranked_titles(query: str, limit: int = 10) -> list[str]:
    return [j.title for j in rank(list(CORPUS), q(query), min_score=0.0)[:limit]]


# --------------------------------------------------------------------------- intent


def test_query_resolves_to_the_right_family() -> None:
    assert understand("data analyst").family_id == "data_analyst"
    assert understand("data scientist").family_id == "data_scientist"
    assert understand("senior data scientist").family_id == "data_scientist"
    assert understand("data engineer").family_id == "data_engineer"


def test_longest_phrase_wins_so_ml_engineer_is_not_a_data_scientist() -> None:
    # "machine learning" alone belongs to data_scientist; the full phrase must win.
    assert detect_family(normalise("machine learning engineer")) == "ml_engineer"


def test_abbreviations_are_expanded() -> None:
    assert understand("BI analyst").family_id == "data_analyst"
    assert understand("ML engineer").family_id == "ml_engineer"
    assert normalise("Sr. Data Analyst") == "senior data analyst"


def test_unknown_query_has_no_family_but_still_has_terms() -> None:
    intent = understand("underwater basket weaver")
    assert intent.family_id == ""
    assert "basket" in intent.terms


# --------------------------------------------------------------------------- ordering


def test_exact_title_match_ranks_first() -> None:
    assert ranked_titles("data analyst")[0] in {
        "Data Analyst",
        "Senior Data Analyst",
        "Data Analyst/ Senior Data Analyst",
        "Sr. Data Analyst",
    }
    assert ranked_titles("data scientist")[0] in {
        "Data Scientist",
        "Senior Data Scientist",
        "Data Scientist-Artificial Intelligence",
    }


@pytest.mark.parametrize(
    ("query", "winner", "loser"),
    [
        # The whole point: a sibling role must not outrank a true match.
        ("data analyst", "Data Analyst", "Data Engineer"),
        ("data analyst", "BI Analyst", "Data Engineer"),
        ("data analyst", "Business Analyst", "Senior Data Engineer"),
        ("data scientist", "Data Scientist", "Data Engineer"),
        ("data scientist", "Applied Scientist", "Machine Learning Engineer"),
        ("data engineer", "Data Engineer", "Data Analyst"),
        # A body-only mention must never beat a title match.
        ("data analyst", "Data Analyst", "Office Administrator"),
        ("data scientist", "Data Scientist", "Sales Executive"),
        # Junk must never beat a real role.
        ("data analyst", "Data Analyst", "See posting"),
        ("data scientist", "Data Scientist", "Multiple roles"),
    ],
)
def test_pairwise_ordering(query: str, winner: str, loser: str) -> None:
    by_title = {j.title: j for j in CORPUS}
    query_obj = q(query)
    intent = understand(query)
    w = score(by_title[winner], query_obj, intent)
    lose = score(by_title[loser], query_obj, intent)
    assert w > lose, f"{winner!r} ({w}) should outrank {loser!r} ({lose}) for {query!r}"


def test_aliases_are_found_at_all() -> None:
    """Recall: 'data analyst' must surface BI and Business Analyst roles, which share no
    exact phrase with the query."""
    top = ranked_titles("data analyst", limit=10)
    assert "BI Analyst" in top
    assert "Business Intelligence Analyst" in top


def test_junk_titles_never_reach_the_top_three() -> None:
    for query in ("data analyst", "data scientist", "software engineer"):
        assert not any(is_noise_title(t) for t in ranked_titles(query, limit=3))


# --------------------------------------------------------------------------- precision


@pytest.mark.parametrize(
    ("query", "relevant", "floor"),
    [("data analyst", ANALYST_IDS, 0.8), ("data scientist", SCIENTIST_IDS, 0.8)],
)
def test_precision_at_five(query: str, relevant: set[str], floor: float) -> None:
    top = ranked_titles(query, limit=5)
    hits = sum(1 for t in top if t in relevant)
    precision = hits / len(top)
    assert precision >= floor, f"precision@5 for {query!r} was {precision:.0%}: {top}"


@pytest.mark.parametrize(
    ("query", "relevant"), [("data analyst", ANALYST_IDS), ("data scientist", SCIENTIST_IDS)]
)
def test_all_true_matches_are_recalled(query: str, relevant: set[str]) -> None:
    """Every genuinely relevant role must appear somewhere in the results, not just rank
    well — a precise but forgetful search is still a bad search."""
    found = set(ranked_titles(query, limit=len(CORPUS)))
    missing = relevant - found
    assert not missing, f"{query!r} failed to surface: {sorted(missing)}"


def test_unrelated_query_returns_nothing_rather_than_everything() -> None:
    results = rank(list(CORPUS), q("underwater welding"), min_score=0.02)
    assert len(results) <= 2, [j.title for j in results]


# --------------------------------------------------------------------------- behaviour


def test_freshness_breaks_ties_but_does_not_reorder_relevance() -> None:
    stale_exact = make("Data Analyst", company="Old", days=200)
    fresh_sibling = make("Data Engineer", company="New", days=0)
    query_obj, intent = q("data analyst"), understand("data analyst")
    # A 200-day-old exact match still beats a brand-new sibling role.
    assert score(stale_exact, query_obj, intent) > score(fresh_sibling, query_obj, intent)


def test_location_filter_still_applies_on_top_of_relevance() -> None:
    jobs = [
        make("Data Analyst", company="InIndia", location="Bengaluru, India"),
        make("Data Analyst", company="InUS", location="San Francisco, CA"),
    ]
    out = rank(jobs, q("data analyst", location="India", strict_location=True), min_score=0.0)
    assert [j.company for j in out] == ["InIndia"]


def test_empty_query_keeps_everything_scoreable() -> None:
    out = rank(list(CORPUS), q(""), min_score=0.0)
    assert len(out) == len(CORPUS)


def test_scores_stay_in_a_sane_range() -> None:
    intent = understand("data analyst")
    for job in CORPUS:
        s = score(job, q("data analyst"), intent)
        assert 0.0 <= s <= 2.0, f"{job.title} scored {s}"
