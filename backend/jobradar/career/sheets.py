"""Write the tracker to a Google Sheet via an Apps Script web app.

Deliberately not the Sheets REST API. That needs OAuth or a service-account JSON key, which
would mean either an interactive browser flow (impossible in a cron job) or a long-lived
private key living in a repo secret with write access to a Google account. An Apps Script
web app sidesteps both: it executes as the sheet's own owner, so the only thing this side
holds is a URL and a shared secret, and each user deploys their own against their own sheet.

See sheets/Code.gs for the script and setup steps.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from ..models import Job

log = logging.getLogger(__name__)


class SheetsError(Exception):
    pass


def configured() -> bool:
    return bool(os.getenv("SHEETS_WEBAPP_URL") and os.getenv("SHEETS_SECRET"))


def _salary_text(job: Job) -> str:
    if job.salary_min is None:
        return ""
    cur = job.salary_currency or ""
    if job.salary_max and job.salary_max != job.salary_min:
        return f"{cur} {job.salary_min:,.0f}–{job.salary_max:,.0f}".strip()
    return f"{cur} {job.salary_min:,.0f}".strip()


def application_row(job: Job, *, status: str = "Found", notes: str = "") -> dict[str, Any]:
    return {
        "Job ID": job.id,
        "Title": job.title,
        "Company": job.company,
        "Location": job.location,
        "Remote": job.remote.value,
        "Salary": _salary_text(job),
        "Source": job.source,
        "Posted": job.posted_at.date().isoformat() if job.posted_at else "",
        "URL": job.apply_url or job.url,
        "Status": status,
        "Next Action": "",
        "Notes": notes,
    }


def outreach_row(job: Job, draft: Any, contact: Any) -> dict[str, Any]:
    return {
        "Job ID": job.id,
        "Company": job.company,
        "Contact Name": getattr(contact, "name", "") or "",
        "Contact Role": getattr(contact, "role", "") or "",
        "Email": getattr(contact, "email", "") or "",
        "Email Confidence": getattr(contact, "email_confidence", "") or "",
        "LinkedIn": getattr(contact, "linkedin_search_url", "") or "",
        "Channel": "",
        "Sent?": "No",
        "LinkedIn Note": getattr(draft, "linkedin_note", "") or "",
        "Email Subject": getattr(draft, "email_subject", "") or "",
        "Email Body": getattr(draft, "email_body", "") or "",
        "Follow Up On": "",
    }


def cv_row(report: dict[str, Any], *, target_role: str = "") -> dict[str, Any]:
    scores = report.get("scores", {})

    def s(key: str) -> str:
        row = scores.get(key)
        return f"{row['score']}/{row['max']}" if row else ""

    fixes = report.get("top_fixes") or []
    return {
        "Job ID": "",
        "Score": report.get("total") if report.get("total") is not None else "partial",
        "Band": (report.get("band") or {}).get("label", ""),
        "Impact": s("impact"),
        "Keywords": s("keyword_alignment"),
        "ATS": s("ats_parseability"),
        "Clarity": s("clarity"),
        "Structure": s("structure"),
        "Evidence": s("evidence"),
        "Progression": s("progression"),
        "Language": s("language_quality"),
        "Target Role": target_role,
        "Top Fixes": " | ".join(
            f"{f.get('problem', '')} → {f.get('action', '')}" for f in fixes[:3]
        ),
    }


async def push(
    kind: str,
    rows: list[dict[str, Any]],
    *,
    url: str | None = None,
    secret: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Append or update rows in the tracker. `kind` is applications | outreach | cv."""
    url = url or os.getenv("SHEETS_WEBAPP_URL", "")
    secret = secret or os.getenv("SHEETS_SECRET", "")
    if not (url and secret):
        raise SheetsError("Set SHEETS_WEBAPP_URL and SHEETS_SECRET (see sheets/Code.gs for setup).")
    if not rows:
        return {"ok": True, "added": 0, "updated": 0}

    owns_client = client is None
    # Apps Script answers /exec with a 302 to script.googleusercontent.com, so redirects
    # must be followed or every write looks like it failed.
    client = client or httpx.AsyncClient(follow_redirects=True, timeout=60.0)
    try:
        resp = await client.post(
            url, json={"secret": secret, "kind": kind, "rows": rows}, follow_redirects=True
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        raise SheetsError(f"could not reach the Apps Script web app: {exc}") from exc
    except ValueError as exc:
        # A login page instead of JSON means the deployment is not set to "Anyone".
        raise SheetsError(
            "the web app did not return JSON — check the deployment is set to "
            '"Who has access: Anyone"'
        ) from exc
    finally:
        if owns_client:
            await client.aclose()

    if not data.get("ok"):
        raise SheetsError(data.get("error", "unknown error from Apps Script"))
    return data


async def fetch(
    kind: str = "applications",
    *,
    url: str | None = None,
    secret: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """Read the tracker back, so the CLI and UI can show what is already logged."""
    url = url or os.getenv("SHEETS_WEBAPP_URL", "")
    secret = secret or os.getenv("SHEETS_SECRET", "")
    if not (url and secret):
        raise SheetsError("Set SHEETS_WEBAPP_URL and SHEETS_SECRET.")

    owns_client = client is None
    client = client or httpx.AsyncClient(follow_redirects=True, timeout=60.0)
    try:
        resp = await client.get(url, params={"secret": secret, "kind": kind})
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SheetsError(f"could not read the tracker: {exc}") from exc
    finally:
        if owns_client:
            await client.aclose()

    if not data.get("ok"):
        raise SheetsError(data.get("error", "unknown error"))
    return data.get("rows", [])
