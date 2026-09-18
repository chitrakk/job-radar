"""Finding the right person to contact, and drafting a message worth reading.

Scope, deliberately: this module *researches and drafts*. It does not send, and it does not
drive a logged-in LinkedIn session. Automating connection requests or InMail from a logged-in
account breaches LinkedIn's User Agreement and their automation detection gets accounts
restricted or permanently banned — the cost of that falls on the candidate, whose network is
the asset. So the agent produces a named target, a best-guess address with a confidence
level, and a draft; the human reviews and sends. One considered message sent by hand beats
fifty automated ones from a banned account.

What it uses:
  - the job posting itself, which frequently names the recruiter or hiring manager
  - public company pages (team/about/leadership), fetched without credentials
  - the company's observed email pattern, inferred from public addresses on its own site
  - a LinkedIn *people search URL* for the human to open — a link, not a scrape

What it never does: guess an individual's personal email and present it as verified, harvest
addresses at scale, or fabricate a name to make a draft look personalised.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote_plus, urlsplit

import httpx

from ..fetcher import fetch_text, make_client
from ..llm import available, complete_json
from ..models import Job
from .prompts import build as build_prompt
from .prompts import prompts as shared_prompts
from .slop import find as find_slop

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")

# Addresses that exist on every site and identify nobody.
GENERIC_LOCAL = {
    "info",
    "contact",
    "hello",
    "support",
    "sales",
    "admin",
    "help",
    "team",
    "press",
    "media",
    "legal",
    "privacy",
    "security",
    "billing",
    "noreply",
    "no-reply",
    "donotreply",
    "webmaster",
    "postmaster",
    "abuse",
    "marketing",
}

# Local-part shapes we can infer a pattern from.
PATTERNS = {
    "first.last": "{first}.{last}",
    "firstlast": "{first}{last}",
    "first": "{first}",
    "f.last": "{f}.{last}",
    "flast": "{f}{last}",
    "first_last": "{first}_{last}",
    "last.first": "{last}.{first}",
    "first.l": "{first}.{l}",
}

CAREERS_PATHS = [
    "/careers",
    "/jobs",
    "/about",
    "/about-us",
    "/team",
    "/our-team",
    "/leadership",
    "/people",
    "/company",
    "/contact",
]


@dataclass
class Contact:
    """A person worth contacting, with an honest confidence level."""

    name: str = ""
    role: str = ""
    source: str = ""  # where the name came from
    email: str = ""
    email_confidence: str = "none"  # verified | inferred | none
    email_basis: str = ""  # why we think the pattern is right
    linkedin_search_url: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "source": self.source,
            "email": self.email,
            "email_confidence": self.email_confidence,
            "email_basis": self.email_basis,
            "linkedin_search_url": self.linkedin_search_url,
        }


@dataclass
class OutreachDraft:
    linkedin_note: str = ""
    email_subject: str = ""
    email_body: str = ""
    contacts: list[Contact] = field(default_factory=list)
    company_domain: str = ""
    warnings: list[str] = field(default_factory=list)
    slop: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "linkedin_note": self.linkedin_note,
            "email_subject": self.email_subject,
            "email_body": self.email_body,
            "contacts": [c.as_dict() for c in self.contacts],
            "company_domain": self.company_domain,
            "warnings": self.warnings,
            "slop": self.slop,
        }


# --------------------------------------------------------------------------- discovery


def linkedin_people_search(company: str, titles: list[str] | None = None) -> str:
    """A LinkedIn people-search URL for the human to open.

    A link, not a scrape: the person clicks it, looks at real profiles, and decides who to
    contact. That keeps the account in their hands and out of automation-detection range.
    """
    titles = titles or ["recruiter", "talent acquisition", "hiring manager"]
    query = f"{company} ({' OR '.join(titles)})"
    return f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(query)}"


def domain_from_url(url: str) -> str:
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    # ATS and aggregator hosts are not the employer's domain.
    if any(
        h in host
        for h in (
            "greenhouse.io",
            "lever.co",
            "ashbyhq.com",
            "workable.com",
            "smartrecruiters.com",
            "recruitee.com",
            "linkedin.com",
            "adzuna",
            "remoteok",
            "remotive",
            "weworkremotely",
            "himalayas",
            "arbeitnow",
            "ycombinator.com",
            "indeed.",
            "naukri.com",
        )
    ):
        return ""
    return host


async def find_company_domain(client: httpx.AsyncClient, job: Job) -> str:
    """Best-effort company domain, preferring the posting's own apply link."""
    for candidate in (job.apply_url, job.url):
        if candidate and (host := domain_from_url(candidate)):
            return host

    # Fall back to the description, where companies often cite their own site.
    for m in re.finditer(r"https?://([A-Za-z0-9.-]+\.[A-Za-z]{2,})", job.description or ""):
        host = m.group(1).lower().removeprefix("www.")
        if host and not domain_from_url(f"https://{host}") == "":
            return host
    return ""


def infer_pattern(emails: list[str], domain: str) -> tuple[str, str]:
    """Infer the company's email pattern from real addresses found on its own site.

    Returns (pattern_key, evidence). Only non-generic addresses count — "info@" tells us
    nothing about how people are named.
    """
    named = []
    for addr in emails:
        local, _, host = addr.lower().partition("@")
        if host != domain or local in GENERIC_LOCAL:
            continue
        named.append(local)

    for local in named:
        if re.fullmatch(r"[a-z]+\.[a-z]+", local):
            return "first.last", f"found {local}@{domain}"
        if re.fullmatch(r"[a-z]\.[a-z]+", local):
            return "f.last", f"found {local}@{domain}"
        if re.fullmatch(r"[a-z]+_[a-z]+", local):
            return "first_last", f"found {local}@{domain}"
    if named:
        return "first", f"found {named[0]}@{domain} (single-name pattern)"
    return "", ""


def build_email(name: str, domain: str, pattern_key: str) -> str:
    """Render a candidate address. Returns "" when the name cannot be split reliably."""
    if not (name and domain and pattern_key in PATTERNS):
        return ""
    parts = [p for p in re.split(r"\s+", re.sub(r"[^A-Za-z\s]", "", name).strip()) if p]
    if len(parts) < 2:
        return ""
    first, last = parts[0].lower(), parts[-1].lower()
    return (
        PATTERNS[pattern_key].format(first=first, last=last, f=first[0], l=last[0]) + f"@{domain}"
    )


async def scan_company_site(client: httpx.AsyncClient, domain: str) -> tuple[list[str], str]:
    """Collect public email addresses and page text from a company's own pages.

    Only the company's own public website, only pages a visitor would browse, and rate
    limited by the shared fetcher. No credentials anywhere.
    """
    if not domain:
        return [], ""

    found: set[str] = set()
    text_parts: list[str] = []

    for path in CAREERS_PATHS:
        url = f"https://{domain}{path}"
        try:
            html = await fetch_text(client, url, min_interval=1.5, retries=0)
        except Exception:  # noqa: BLE001 - a missing /team page is the norm, not an error
            continue
        for m in EMAIL_RE.finditer(html):
            addr = m.group(0).lower()
            if m.group(1).lower().removeprefix("www.") == domain:
                found.add(addr)
        text_parts.append(re.sub(r"<[^>]+>", " ", html)[:4000])
        if len(found) >= 3:
            break

    return sorted(found), " ".join(text_parts)[:12000]


CONTACT_SCHEMA = {
    "type": "object",
    "properties": {
        "contacts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["name", "role", "evidence"],
            },
        }
    },
    "required": ["contacts"],
}


async def discover_contacts(
    client: httpx.AsyncClient, job: Job, domain: str, site_text: str
) -> list[Contact]:
    """Find named people from the posting and the company's public pages."""
    contacts: list[Contact] = []
    haystack = f"{job.description or ''}\n{site_text}"

    if available() and haystack.strip():
        payload = await complete_json(
            client,
            build_prompt(
                "contacts",
                company=job.company,
                title=job.title,
                text=haystack[:12000],
            ),
            schema=CONTACT_SCHEMA,
        )
        if isinstance(payload, dict):
            for row in payload.get("contacts") or []:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("name", "")).strip()
                # Reject anything that is a job title masquerading as a name.
                if not name or len(name.split()) < 2:
                    continue
                if re.search(r"\b(manager|recruiter|team|department|hr)\b", name, re.I):
                    continue
                contacts.append(
                    Contact(
                        name=name,
                        role=str(row.get("role", "")).strip()[:120],
                        source=str(row.get("evidence", "")).strip()[:200],
                    )
                )

    # Always give the human a search link, even when no name was found — that is the
    # realistic path for most postings.
    contacts.append(
        Contact(
            name="",
            role="Recruiter / hiring manager (search manually)",
            source="No name published in the posting — open this search and pick a person.",
            linkedin_search_url=linkedin_people_search(job.company),
        )
    )
    for c in contacts:
        if c.name and not c.linkedin_search_url:
            c.linkedin_search_url = (
                "https://www.linkedin.com/search/results/people/?keywords="
                + quote_plus(f"{c.name} {job.company}")
            )
    return contacts[:5]


# --------------------------------------------------------------------------- drafting

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "linkedin_note": {"type": "string"},
        "email_subject": {"type": "string"},
        "email_body": {"type": "string"},
    },
    "required": ["linkedin_note", "email_subject", "email_body"],
}


async def draft_outreach(
    job: Job,
    *,
    candidate_summary: str,
    candidate_name: str = "",
    contact: Contact | None = None,
    client: httpx.AsyncClient | None = None,
    research: bool = True,
) -> OutreachDraft:
    """Research the company and draft a LinkedIn note plus an email.

    `candidate_summary` is the candidate's own CV text or a précis of it — the only source
    the draft may draw claims from.
    """
    draft = OutreachDraft()
    owns_client = client is None
    client = client or make_client()

    try:
        domain = await find_company_domain(client, job) if research else ""
        draft.company_domain = domain

        emails: list[str] = []
        site_text = ""
        if research and domain:
            emails, site_text = await scan_company_site(client, domain)

        contacts = [contact] if contact else await discover_contacts(client, job, domain, site_text)

        # Infer an address pattern from real published addresses, and be explicit that a
        # generated address is a guess.
        pattern_key, evidence = infer_pattern(emails, domain) if domain else ("", "")
        for c in contacts:
            if not c.name or not domain:
                continue
            if pattern_key:
                guess = build_email(c.name, domain, pattern_key)
                if guess:
                    c.email = guess
                    c.email_confidence = "inferred"
                    c.email_basis = f"{pattern_key} pattern — {evidence}"
            else:
                c.email_basis = (
                    f"No named address published on {domain}, so no pattern could be "
                    "inferred. Verify before sending."
                )
        draft.contacts = contacts

        if emails:
            generic = [e for e in emails if e.split("@")[0] in GENERIC_LOCAL]
            if generic:
                draft.warnings.append(
                    f"Published company addresses: {', '.join(generic[:3])}. "
                    "These reach a shared inbox — use only if no named contact is found."
                )
        if any(c.email_confidence == "inferred" for c in contacts):
            draft.warnings.append(
                "Email addresses are inferred from the company's observed naming pattern, "
                "not verified. A bounce costs nothing, but check before a bulk send."
            )

        if not available():
            draft.warnings.append(
                "No LLM key configured, so no drafts were written. Contact research above "
                "still applies."
            )
            return draft

        named = next((c for c in contacts if c.name), None)
        spec = shared_prompts()["outreach"]
        contact_block = (
            spec["contact_named"].format(name=named.name, role=named.role)
            if named
            else spec["contact_unknown"]
        )

        payload = await complete_json(
            client,
            build_prompt(
                "outreach",
                contact_block=contact_block,
                title=job.title,
                company=job.company,
                location=job.location or "",
                jd=(job.description or "(no description published)")[:3500],
                candidate=candidate_summary[:6000],
                candidate_name=candidate_name or "[your name]",
            ),
            schema=DRAFT_SCHEMA,
        )
        if not isinstance(payload, dict):
            draft.warnings.append("Draft generation failed; contact research still applies.")
            return draft

        draft.linkedin_note = str(payload.get("linkedin_note", "")).strip()
        draft.email_subject = str(payload.get("email_subject", "")).strip()
        draft.email_body = str(payload.get("email_body", "")).strip()

        # Gate the output through the same slop check the CV uses. A draft that trips these
        # is worse than no draft, so it is surfaced rather than quietly returned.
        note_hits = find_slop(draft.linkedin_note, kind="linkedin_note")
        email_hits = find_slop(draft.email_body, kind="email")
        draft.slop = {
            "linkedin_note": [h.as_dict() for h in note_hits],
            "email_body": [h.as_dict() for h in email_hits],
            "clean": not note_hits and not email_hits,
        }
        if len(draft.linkedin_note) > 300:
            draft.warnings.append(
                f"LinkedIn note is {len(draft.linkedin_note)} characters; LinkedIn truncates "
                "connection notes at 300. Trim before sending."
            )
        return draft
    finally:
        if owns_client:
            await client.aclose()
