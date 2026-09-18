"""Query understanding backed by shared/role_taxonomy.json.

Plain term matching cannot tell "data analyst" from "data scientist" from "data engineer",
because all three share the strongest word in the query. Measured on the live corpus, a
search for "data analyst" returned 115 results of which only 17 contained both words — the
rest were Data Engineers, Financial Analysts and anything else with "data" or "analyst" in
it somewhere.

This module maps a free-text query onto a role family so that ranking can do two things
term matching cannot: recognise that "BI Analyst" is the same job under a different name,
and actively demote the sibling roles that merely share vocabulary.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any


def shared_dir() -> Path:
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "shared" / "role_taxonomy.json").exists():
            return candidate / "shared"
    raise FileNotFoundError("could not locate shared/ directory")


@lru_cache(maxsize=1)
def taxonomy() -> dict[str, Any]:
    return json.loads((shared_dir() / "role_taxonomy.json").read_text())


@lru_cache(maxsize=1)
def families() -> dict[str, dict[str, Any]]:
    return {f["id"]: f for f in taxonomy()["families"]}


@lru_cache(maxsize=1)
def noise_titles() -> frozenset[str]:
    return frozenset(t.lower() for t in taxonomy().get("noise_titles", []))


def normalise(text: str) -> str:
    """Lowercase, expand the abbreviations these titles are riddled with, collapse space."""
    t = f" {text.lower()} "
    t = re.sub(r"[^a-z0-9+#/&.\- ]+", " ", t)
    # Expand before collapsing, so "Sr." and "Sr" both become "senior".
    for pattern, repl in (
        (r"\bsr\b\.?", "senior"),
        (r"\bjr\b\.?", "junior"),
        (r"\bmgr\b\.?", "manager"),
        (r"\beng\b\.?", "engineer"),
        (r"\bdev\b\.?", "developer"),
        (r"\banalytics\b", "analytics"),
        (r"\bml\b", "machine learning"),
        (r"\bai\b", "artificial intelligence"),
        (r"\bbi\b", "business intelligence"),
        (r"\bds\b", "data science"),
        (r"\bswe\b", "software engineer"),
        (r"\bsde\b", "software engineer"),
        (r"\bpm\b", "product manager"),
    ):
        t = re.sub(pattern, repl, t)
    return re.sub(r"\s+", " ", t).strip()


@dataclass
class Intent:
    """What a query is actually asking for."""

    raw: str
    normalised: str
    terms: list[str] = field(default_factory=list)
    family_id: str = ""
    # Phrases that mean the same role, used as positive title evidence.
    aliases: list[str] = field(default_factory=list)
    # Skills that corroborate the family when the title is vague.
    skills: list[str] = field(default_factory=list)
    # Canonical phrases of sibling families — a title matching one of these is probably a
    # different job, however many query words it happens to contain.
    rivals: list[str] = field(default_factory=list)

    @property
    def has_family(self) -> bool:
        return bool(self.family_id)


def _all_phrases(fam: dict[str, Any]) -> list[str]:
    return [normalise(p) for p in (fam.get("canonical", []) + fam.get("aliases", []))]


def detect_family(query_norm: str) -> str:
    """Pick the family a query is asking for, or "" when it names none.

    Longest phrase wins: "machine learning engineer" must beat "machine learning" so an ML
    Engineer search does not resolve to Data Scientist.
    """
    best_id, best_len = "", 0
    for fid, fam in families().items():
        for phrase in _all_phrases(fam):
            if phrase and phrase in query_norm and len(phrase) > best_len:
                best_id, best_len = fid, len(phrase)
    return best_id


def understand(query: str) -> Intent:
    """Turn a free-text query into an Intent."""
    norm = normalise(query)
    intent = Intent(raw=query, normalised=norm, terms=[t for t in norm.split() if len(t) > 1])

    fid = detect_family(norm)
    if not fid:
        return intent

    fam = families()[fid]
    intent.family_id = fid
    intent.aliases = _all_phrases(fam)
    intent.skills = [normalise(s) for s in fam.get("skills", [])]

    rivals: list[str] = []
    for sibling_id in fam.get("confused_with", []):
        sibling = families().get(sibling_id)
        if not sibling:
            continue
        for phrase in (normalise(p) for p in sibling.get("canonical", [])):
            # A phrase that is also one of our own aliases is not evidence against us.
            # "data science" belongs to data_scientist but appears in data_analyst's
            # alias list too, and must not penalise either.
            if phrase and phrase not in intent.aliases:
                rivals.append(phrase)
    intent.rivals = rivals
    return intent


def is_noise_title(title: str) -> bool:
    """Titles that carry no information and should never rank."""
    t = normalise(title)
    return not t or len(t) < 3 or t in noise_titles()


@lru_cache(maxsize=1)
def _skill_vocabulary() -> list[tuple[str, str]]:
    """Every skill phrase in the taxonomy, as (normalised phrase, display form).

    Longest first, so "machine learning" is matched and consumed before "learning" would
    be, and "power bi" before "bi".
    """
    seen: dict[str, str] = {}
    for fam in taxonomy()["families"]:
        for skill in fam.get("skills", []):
            key = normalise(skill)
            if key and key not in seen:
                seen[key] = skill
    return sorted(seen.items(), key=lambda kv: -len(kv[0]))


def extract_skills(*texts: str, limit: int = 10) -> list[str]:
    """Pull known skills out of free text, without an LLM.

    This exists because the `skills` field was empty on 100% of the live corpus — it is
    populated by LLM enrichment, and with no API key configured that never ran. Since
    search matches against title, company, skills, tags and summary, and three of those
    were always blank, search was effectively title-only.

    Matching a fixed vocabulary is far narrower than what a model would extract, but it is
    free, instant, deterministic, and it cannot invent a skill the posting never mentions.
    """
    blob = normalise(" ".join(t for t in texts if t))
    if not blob:
        return []

    found: list[str] = []
    for key, display in _skill_vocabulary():
        if len(found) >= limit:
            break
        # Word-boundary match so "r" does not fire on every word containing the letter,
        # and "ai" does not match "email".
        if re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", blob):
            found.append(display)
    return found
