"""CV scoring out of 100, and grounded rewrite suggestions.

The score is a hybrid on purpose. `language_quality` is graded by regex in slop.py, because
a model asked "is this AI slop?" will happily contradict itself between runs. The other
seven criteria need judgement and are graded by the LLM against explicit anchors from
shared/cv_rubric.json, so two runs on the same CV land close together instead of drifting.

The hard rule everywhere in this module: never invent experience. Suggestions may rephrase,
restructure, quantify-with-a-placeholder or surface something already in the CV. They may
not add a skill, employer, metric or claim the candidate did not state. An invented number
on a CV is something the candidate has to defend in an interview.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx

from ..llm import available, complete_json
from .prompts import build as build_prompt
from .prompts import prompts as shared_prompts
from .slop import language_score, shared_dir

log = logging.getLogger(__name__)

# The criterion graded deterministically rather than by the model.
DETERMINISTIC = "language_quality"


@lru_cache(maxsize=1)
def rubric() -> dict[str, Any]:
    return json.loads((shared_dir() / "cv_rubric.json").read_text())


def band_for(total: float) -> dict[str, Any]:
    for band in rubric()["bands"]:
        if total >= band["min"]:
            return band
    return rubric()["bands"][-1]


def _criteria_brief(exclude: str) -> str:
    lines = []
    for c in rubric()["criteria"]:
        if c["id"] == exclude:
            continue
        anchors = "\n".join(f"      {k}: {v}" for k, v in c["scoring_anchors"].items())
        failures = "\n".join(f"      - {f}" for f in c["common_failures"])
        lines.append(
            f"  {c['id']} (max {c['weight']}) — {c['label']}\n"
            f"    Good: {c['what_good_looks_like']}\n"
            f"    Common failures:\n{failures}\n"
            f"    Score anchors:\n{anchors}"
        )
    return "\n\n".join(lines)


def _schema(exclude: str) -> dict[str, Any]:
    ids = [c["id"] for c in rubric()["criteria"] if c["id"] != exclude]
    return {
        "type": "object",
        "properties": {
            "scores": {
                "type": "object",
                "properties": {
                    cid: {
                        "type": "object",
                        "properties": {
                            "score": {"type": "number"},
                            "reason": {"type": "string"},
                        },
                        "required": ["score", "reason"],
                    }
                    for cid in ids
                },
                "required": ids,
            },
            "top_fixes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "criterion": {"type": "string"},
                        "problem": {"type": "string"},
                        "action": {"type": "string"},
                        "points_available": {"type": "number"},
                    },
                    "required": ["criterion", "problem", "action"],
                },
            },
            "bullet_rewrites": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "original": {"type": "string"},
                        "rewritten": {"type": "string"},
                        "why": {"type": "string"},
                    },
                    "required": ["original", "rewritten", "why"],
                },
            },
            "missing_keywords": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": "string"},
        },
        "required": ["scores", "top_fixes", "summary"],
    }


def _heuristic_report(cv_text: str, lang_score: float, hits: list) -> dict[str, Any]:
    """Used when no LLM is configured. Honest about being partial rather than inventing
    scores for criteria that need judgement."""
    return {
        "total": None,
        "partial": True,
        "band": None,
        "scores": {
            DETERMINISTIC: {
                "score": lang_score,
                "max": 10,
                "reason": f"{len(hits)} language issues found by pattern check.",
            }
        },
        "top_fixes": [
            {
                "criterion": DETERMINISTIC,
                "problem": h.label,
                "action": h.fix,
                "excerpt": h.excerpt,
            }
            for h in hits[:5]
        ],
        "bullet_rewrites": [],
        "missing_keywords": [],
        "slop": [h.as_dict() for h in hits],
        "summary": (
            "Only the deterministic language check ran — no LLM key is configured, so the "
            "seven judgement-based criteria were not graded and there is no score out of 100."
        ),
    }


async def score_cv(
    cv_text: str,
    *,
    job_description: str = "",
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Score a CV out of 100. Returns a report dict safe to serialise to JSON."""
    cv_text = (cv_text or "").strip()
    if not cv_text:
        raise ValueError("cv_text is empty")

    lang_weight = next(c["weight"] for c in rubric()["criteria"] if c["id"] == DETERMINISTIC)
    lang, hits = language_score(cv_text, kind="cv", weight=lang_weight)

    if not available():
        return _heuristic_report(cv_text, lang, hits)

    spec = shared_prompts()["cv_score"]
    jd_block = spec["jd_block"].format(jd=job_description[:6000]) if job_description else ""
    prompt = build_prompt(
        "cv_score",
        system=spec["system"],
        criteria=_criteria_brief(DETERMINISTIC),
        jd_block=jd_block,
        cv=cv_text[:18000],
    )

    owns_client = client is None
    client = client or httpx.AsyncClient()
    try:
        payload = await complete_json(client, prompt, schema=_schema(DETERMINISTIC))
    finally:
        if owns_client:
            await client.aclose()

    if not isinstance(payload, dict) or not isinstance(payload.get("scores"), dict):
        log.warning("CV scoring returned no usable payload; falling back to partial report")
        return _heuristic_report(cv_text, lang, hits)

    # Clamp every model score to its criterion's declared maximum. Without this a model
    # that returns 20 for a criterion worth 8 silently inflates the total past 100.
    scores: dict[str, Any] = {}
    total = 0.0
    for c in rubric()["criteria"]:
        cid, weight = c["id"], c["weight"]
        if cid == DETERMINISTIC:
            scores[cid] = {
                "score": lang,
                "max": weight,
                "reason": f"{len(hits)} language issues found by pattern check.",
            }
            total += lang
            continue

        row = payload["scores"].get(cid) or {}
        try:
            raw = float(row.get("score", 0))
        except (TypeError, ValueError):
            raw = 0.0
        clamped = max(0.0, min(raw, float(weight)))
        scores[cid] = {
            "score": round(clamped, 1),
            "max": weight,
            "reason": str(row.get("reason", ""))[:300],
        }
        total += clamped

    total = round(total, 1)
    fixes = payload.get("top_fixes") or []
    rewrites = payload.get("bullet_rewrites") or []

    return {
        "total": total,
        "partial": False,
        "band": band_for(total),
        "scores": scores,
        "top_fixes": fixes[:5] if isinstance(fixes, list) else [],
        "bullet_rewrites": rewrites[:5] if isinstance(rewrites, list) else [],
        "missing_keywords": (payload.get("missing_keywords") or [])[:12],
        "slop": [h.as_dict() for h in hits],
        "summary": str(payload.get("summary", ""))[:600],
    }


def render_report(report: dict[str, Any]) -> str:
    """Plain-text report for the CLI."""
    lines = []
    if report.get("partial"):
        lines.append("CV score: unavailable (no LLM key configured — partial check only)")
    else:
        band = report["band"]
        lines.append(f"CV score: {report['total']}/100 — {band['label']}")
        lines.append(f"  {band['meaning']}")
    lines.append("")

    for cid, row in report["scores"].items():
        label = next((c["label"] for c in rubric()["criteria"] if c["id"] == cid), cid)
        bar_width = 20
        filled = int(round(bar_width * row["score"] / row["max"])) if row["max"] else 0
        bar = "█" * filled + "·" * (bar_width - filled)
        lines.append(f"  {label:<30} {row['score']:>5}/{row['max']:<3} {bar}")
        if row.get("reason"):
            lines.append(f"      {row['reason']}")
    lines.append("")

    if report.get("summary"):
        lines.append(report["summary"])
        lines.append("")

    if report.get("top_fixes"):
        lines.append("Highest-leverage fixes:")
        for i, fix in enumerate(report["top_fixes"], 1):
            pts = fix.get("points_available")
            suffix = f"  (+{pts} pts)" if pts else ""
            lines.append(f"  {i}. {fix.get('problem', '')}{suffix}")
            lines.append(f"     → {fix.get('action', '')}")
        lines.append("")

    if report.get("bullet_rewrites"):
        lines.append("Suggested rewrites:")
        for rw in report["bullet_rewrites"]:
            lines.append(f"  before: {rw.get('original', '')}")
            lines.append(f"  after:  {rw.get('rewritten', '')}")
            lines.append(f"          {rw.get('why', '')}")
            lines.append("")

    if report.get("missing_keywords"):
        lines.append("Keywords the target role expects but the CV never names:")
        lines.append("  " + ", ".join(report["missing_keywords"]))
        lines.append("")

    return "\n".join(lines)


def read_cv(path: Path) -> str:
    """Read a CV from .txt, .md or .pdf.

    PDF support is optional: pypdf is only needed if you actually point this at a PDF, so
    the base install stays light.
    """
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError(
                "Reading a PDF CV needs pypdf. Install with: uv pip install pypdf"
            ) from exc
        reader = PdfReader(str(path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if not text.strip():
            raise RuntimeError(
                f"No text layer in {path.name} — it is probably a scan. "
                "An ATS cannot read it either, which is itself the most important finding."
            )
        return text
    return path.read_text(encoding="utf-8", errors="replace")
