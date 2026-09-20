"""Location matching, India-aware.

Needed because job boards write the same place a dozen ways — "Bengaluru", "Bangalore",
"Bengaluru, Karnataka, India", "BLR", "Bangalore/Hyderabad" — and because a naive
substring test on "India" misses every posting that only names a city. Without this the
corpus fills with San Francisco roles that merely matched a keyword.

The vocabulary lives in shared/locations.json because the browser needs exactly the same
answers. It used to live here alone, and the frontend fell back to a substring test — so
typing "Gurgaon" on the website matched none of the 41 jobs whose location reads
"Gurugram", while every worldwide-remote job matched every city anyone typed.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


def _shared_dir() -> Path:
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "shared" / "locations.json").exists():
            return candidate / "shared"
    raise FileNotFoundError("could not locate shared/ directory")


@lru_cache(maxsize=1)
def _vocab() -> dict[str, Any]:
    return json.loads((_shared_dir() / "locations.json").read_text())


@lru_cache(maxsize=1)
def _cities() -> dict[str, set[str]]:
    return {city: set(aliases) for city, aliases in _vocab()["cities"].items()}


@lru_cache(maxsize=1)
def _alias_to_city() -> dict[str, str]:
    return {alias: city for city, aliases in _cities().items() for alias in aliases}


@lru_cache(maxsize=1)
def _metro_of() -> dict[str, str]:
    """Which metro area a canonical city belongs to, if any.

    Delhi NCR is one job market: somebody searching "Delhi" wants the Gurugram and Noida
    postings too, and on the live corpus those are 44 of the 58 NCR jobs. Treating them as
    different cities hides most of the market from the people who live in it.
    """
    return {
        city: metro
        for metro, members in _vocab().get("metro_areas", {}).items()
        for city in members
    }


@lru_cache(maxsize=1)
def _anywhere() -> re.Pattern[str]:
    return re.compile(_vocab()["anywhere_pattern"], re.I)


@lru_cache(maxsize=1)
def _region_lock() -> re.Pattern[str]:
    return re.compile(_vocab()["region_lock_pattern"], re.I)


@lru_cache(maxsize=1)
def _india_terms() -> frozenset[str]:
    return frozenset(_vocab()["india_terms"])


# Kept as a module attribute because callers and tests import it directly.
INDIA_CITIES: dict[str, set[str]] = _cities()
INDIA_TERMS: set[str] = set(_india_terms()) | {"in"}

_WORD = re.compile(r"[a-z]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def canonical_city(location: str) -> str | None:
    """Return the canonical Indian city named in `location`, if any."""
    low = location.lower()
    tokens = _tokens(low)
    best: str | None = None
    best_len = 0
    for alias, city in _alias_to_city().items():
        # Multi-word aliases need a substring test; single words use token equality so
        # that "in" inside "Indiana" or "Berlin" does not match. Longest alias wins, so
        # "greater noida" resolves before "noida" and "navi mumbai" before "mumbai".
        hit = alias in low if " " in alias else alias in tokens
        if hit and len(alias) > best_len:
            best, best_len = city, len(alias)
    return best


def cities_in(location: str) -> set[str]:
    """Every canonical Indian city a location names.

    Indian boards routinely list one opening in several cities — Shine's "Bangalore,
    Chennai, Noida, Hyderabad +4 more" is typical. canonical_city() picks one of them, so
    a posting that genuinely hires in Noida could fail a Delhi search because Bangalore
    happened to be the name it resolved to.
    """
    low = location.lower()
    tokens = _tokens(low)
    found = {
        city
        for alias, city in _alias_to_city().items()
        if (alias in low if " " in alias else alias in tokens)
    }
    # "greater noida" also contains "noida"; both resolve to the same city, so no clash.
    return found


def metro_area(city: str | None) -> str | None:
    """The wider job market a city sits in, e.g. Gurugram -> delhi."""
    return _metro_of().get(city) if city else None


def same_metro(a: str | None, b: str | None) -> bool:
    """Two cities a commuter would treat as one market."""
    if not a or not b:
        return False
    if a == b:
        return True
    ma, mb = metro_area(a), metro_area(b)
    return bool(ma and ma == mb)


def is_india(location: str) -> bool:
    if not location:
        return False
    if canonical_city(location):
        return True
    toks = _tokens(location)
    # "IN" alone is ambiguous ("Bloomington, IN" is Indiana), so require the full word.
    return bool(toks & _india_terms())


def is_anywhere_remote(location: str, description: str = "") -> bool:
    """True when a remote role is genuinely open worldwide rather than region-locked.

    The "open worldwide" test reads the location field only. Descriptions are full of
    boilerplate like "our global team" and "customers worldwide", and matching those was
    enough to let San Francisco and Foster City roles through an India-only filter. The
    description is still used for the *negative* test, because an explicit "US only" there
    is a genuine signal.
    """
    if _region_lock().search(f"{location} {description[:800]}"):
        return False
    if _anywhere().search(location):
        return True

    # A bare "Remote" with no region is genuinely open. An *empty* location is not — it
    # means we failed to parse one, which is a different thing entirely.
    #
    # Treating empty as "anywhere" inverted the whole India filter: records we parsed
    # correctly got a real location like "San Francisco" and were dropped, while records
    # we failed to parse had no location and sailed through. Measured on the live corpus,
    # that made 254 of 273 Hacker News entries — 93% — unparsed junk that outranked the
    # jobs that had actually been read properly. The filter was selecting for its own
    # parse failures.
    normalised = location.strip().lower()
    if not normalised:
        # Fall back to the description: "remote, worldwide" in the body is real evidence,
        # silence is not.
        return bool(_anywhere().search(description[:800]))
    return normalised in {"remote", "remote worldwide", "fully remote", "remote - global"}


def locality(
    job_location: str,
    query_location: str,
    *,
    is_remote: bool = False,
    description: str = "",
) -> str:
    """How a posting satisfies a location query: "exact", "metro", "region", "remote" or "".

    Callers that only need a yes/no use matches_location(). Ranking needs the distinction,
    because "a remote job you could do from Delhi" and "a job in Delhi" are both matches
    and only one of them is what somebody typing "Delhi" was looking for.
    """
    if not query_location:
        return "exact"

    q = query_location.strip().lower()
    remote_ok = "remote" if (is_remote and is_anywhere_remote(job_location, description)) else ""

    if q in INDIA_TERMS or q == "india":
        return "region" if is_india(job_location) else remote_ok

    city = canonical_city(q)
    if city:
        job_cities = cities_in(job_location)
        if city in job_cities:
            return "exact"
        if any(same_metro(c, city) for c in job_cities):
            return "metro"
        # "India" with no city named still plausibly serves a city query.
        if is_india(job_location) and not job_cities:
            return "region"
        return remote_ok

    # Non-Indian query location: plain token overlap.
    if _tokens(q) & _tokens(job_location):
        return "exact"
    return remote_ok


def matches_location(
    job_location: str,
    query_location: str,
    *,
    is_remote: bool = False,
    description: str = "",
) -> bool:
    """Does this posting satisfy the query's location intent?

    A remote role open worldwide satisfies an India query — that is usually the best
    outcome for an India-based candidate, not a near miss.
    """
    return bool(
        locality(job_location, query_location, is_remote=is_remote, description=description)
    )
