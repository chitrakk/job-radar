"""Location matching, India-aware.

Needed because job boards write the same place a dozen ways — "Bengaluru", "Bangalore",
"Bengaluru, Karnataka, India", "BLR", "Bangalore/Hyderabad" — and because a naive
substring test on "India" misses every posting that only names a city. Without this the
corpus fills with San Francisco roles that merely matched a keyword.
"""

from __future__ import annotations

import re

# Indian metros and tech hubs, with the spellings boards actually use. Keys are canonical;
# values are aliases that should resolve to the same city.
INDIA_CITIES: dict[str, set[str]] = {
    "bengaluru": {"bengaluru", "bangalore", "blr", "bangaluru", "bengalooru"},
    "mumbai": {"mumbai", "bombay", "navi mumbai", "thane"},
    "delhi": {"delhi", "new delhi", "ncr", "delhi ncr"},
    "gurugram": {"gurugram", "gurgaon"},
    "noida": {"noida", "greater noida"},
    "hyderabad": {"hyderabad", "secunderabad", "hyd"},
    "chennai": {"chennai", "madras"},
    "pune": {"pune", "poona", "pimpri"},
    "kolkata": {"kolkata", "calcutta"},
    "ahmedabad": {"ahmedabad", "gandhinagar"},
    "jaipur": {"jaipur"},
    "chandigarh": {"chandigarh", "mohali", "panchkula"},
    "kochi": {"kochi", "cochin", "ernakulam"},
    "coimbatore": {"coimbatore"},
    "indore": {"indore"},
    "bhubaneswar": {"bhubaneswar"},
    "thiruvananthapuram": {"thiruvananthapuram", "trivandrum"},
    "nagpur": {"nagpur"},
    "lucknow": {"lucknow"},
    "vadodara": {"vadodara", "baroda"},
    "surat": {"surat"},
    "mysuru": {"mysuru", "mysore"},
    "visakhapatnam": {"visakhapatnam", "vizag"},
    "patna": {"patna", "bodh gaya", "bodhgaya"},
    "bhopal": {"bhopal"},
    "goa": {"goa", "panaji"},
    "guwahati": {"guwahati"},
    "raipur": {"raipur"},
}

_ALIAS_TO_CITY = {alias: city for city, aliases in INDIA_CITIES.items() for alias in aliases}

# Words that mean "India" without naming a city.
INDIA_TERMS = {"india", "indian", "bharat", "in"}

# Phrases meaning the role is open to anyone anywhere — these satisfy an India query.
_ANYWHERE = re.compile(
    r"\b(anywhere in the world|worldwide|global|any location"
    r"|remote\s*[-–]?\s*(global|worldwide|anywhere))\b",
    re.I,
)

# Explicit region locks that exclude India even though the posting says "remote".
_REGION_LOCK = re.compile(
    r"\b(us only|usa only|united states only|u\.s\. only|remote\s*[-–(]*\s*(us|usa|united states|"
    r"canada|uk|emea|europe|latam|apac[- ]anz|australia)\b|eu only|europe only|uk only|"
    r"must be (located|based) in (the )?(us|usa|united states|uk|canada|europe))",
    re.I,
)

_WORD = re.compile(r"[a-z]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def canonical_city(location: str) -> str | None:
    """Return the canonical Indian city named in `location`, if any."""
    low = location.lower()
    for alias, city in _ALIAS_TO_CITY.items():
        # Multi-word aliases need a substring test; single words use token equality so
        # that "in" inside "Indiana" or "Berlin" does not match.
        if " " in alias:
            if alias in low:
                return city
        elif alias in _tokens(low):
            return city
    return None


def is_india(location: str) -> bool:
    if not location:
        return False
    if canonical_city(location):
        return True
    toks = _tokens(location)
    # "IN" alone is ambiguous ("Bloomington, IN" is Indiana), so require the full word.
    return bool(toks & (INDIA_TERMS - {"in"}))


def is_anywhere_remote(location: str, description: str = "") -> bool:
    """True when a remote role is genuinely open worldwide rather than region-locked.

    The "open worldwide" test reads the location field only. Descriptions are full of
    boilerplate like "our global team" and "customers worldwide", and matching those was
    enough to let San Francisco and Foster City roles through an India-only filter. The
    description is still used for the *negative* test, because an explicit "US only" there
    is a genuine signal.
    """
    if _REGION_LOCK.search(f"{location} {description[:800]}"):
        return False
    if _ANYWHERE.search(location):
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
        return bool(_ANYWHERE.search(description[:800]))
    return normalised in {"remote", "remote worldwide", "fully remote", "remote - global"}


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
    if not query_location:
        return True

    q = query_location.strip().lower()

    if q in INDIA_TERMS or q == "india":
        if is_india(job_location):
            return True
        return is_remote and is_anywhere_remote(job_location, description)

    # A specific city: match the city itself, or any India-wide/remote posting.
    city = canonical_city(q)
    if city:
        job_city = canonical_city(job_location)
        if job_city == city:
            return True
        # "India" with no city, or a worldwide-remote role, still plausibly serves the query.
        if is_india(job_location) and not job_city:
            return True
        return is_remote and is_anywhere_remote(job_location, description)

    # Non-Indian query location: plain token overlap.
    return bool(_tokens(q) & _tokens(job_location)) or (
        is_remote and is_anywhere_remote(job_location, description)
    )
