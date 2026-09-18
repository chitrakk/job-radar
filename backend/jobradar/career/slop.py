"""Deterministic AI-slop detection.

Patterns come from shared/slop_patterns.json (adapted from petergyang/no-ai-slop). This
runs as plain regex before and after any model call, which matters for three reasons: it
costs nothing, it is reproducible, and — unlike asking a model "is this slop?" — it cannot
itself hallucinate a verdict.

Used to grade the language_quality criterion of a CV, and as a gate on every LinkedIn note
and email the agent drafts, since a recruiter reading obvious AI output is worse for the
candidate than plain writing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

SEVERITY_COST = {"high": 3.0, "medium": 1.5, "low": 0.5}


@dataclass(frozen=True)
class Hit:
    """One slop pattern found in the text."""

    id: str
    label: str
    severity: str
    fix: str
    excerpt: str
    start: int

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "severity": self.severity,
            "fix": self.fix,
            "excerpt": self.excerpt,
            "start": self.start,
        }


def shared_dir() -> Path:
    """Locate shared/ by walking up from this file to the repo root."""
    here = Path(__file__).resolve()
    for candidate in here.parents:
        target = candidate / "shared" / "slop_patterns.json"
        if target.exists():
            return candidate / "shared"
    raise FileNotFoundError("could not locate shared/ directory")


@lru_cache(maxsize=1)
def _rules() -> tuple[list[dict], list[dict]]:
    data = json.loads((shared_dir() / "slop_patterns.json").read_text())
    return data["patterns"], data.get("structural_checks", [])


@lru_cache(maxsize=1)
def _compiled() -> list[tuple[dict, re.Pattern[str]]]:
    out = []
    for rule in _rules()[0]:
        try:
            out.append((rule, re.compile(rule["regex"], re.I | re.M)))
        except re.error as exc:  # a bad pattern must not break the whole checker
            raise ValueError(f"slop pattern {rule['id']} is invalid: {exc}") from exc
    return out


def find(text: str, *, kind: str = "cv") -> list[Hit]:
    """Return every slop pattern in `text`, plus structural problems for `kind`.

    `kind` is one of "cv", "linkedin_note" or "email" — it selects which structural
    checks apply, since a 300-character limit means nothing for a CV.
    """
    if not text or not text.strip():
        return []

    hits: list[Hit] = []
    for rule, pattern in _compiled():
        for m in pattern.finditer(text):
            excerpt = " ".join(m.group(0).split())[:90]
            hits.append(
                Hit(
                    id=rule["id"],
                    label=rule["label"],
                    severity=rule.get("severity", "medium"),
                    fix=rule.get("fix", ""),
                    excerpt=excerpt,
                    start=m.start(),
                )
            )

    hits.extend(_structural(text, kind))
    hits.sort(key=lambda h: h.start)
    return hits


def _structural(text: str, kind: str) -> list[Hit]:
    hits: list[Hit] = []
    for check in _rules()[1]:
        if check.get("applies_to") != kind:
            continue
        cid = check["id"]

        if cid == "note_too_long" and len(text) > check["threshold_chars"]:
            hits.append(
                Hit(
                    cid,
                    check["label"],
                    "high",
                    check["fix"],
                    f"{len(text)} characters (limit {check['threshold_chars']})",
                    0,
                )
            )

        elif cid == "email_too_long":
            words = len(text.split())
            if words > check["threshold_words"]:
                hits.append(
                    Hit(
                        cid,
                        check["label"],
                        "medium",
                        check["fix"],
                        f"{words} words (aim for under {check['threshold_words']})",
                        0,
                    )
                )

        elif cid == "bullet_too_long":
            for line, offset in _bullets(text):
                if len(line) > check["threshold_chars"]:
                    hits.append(Hit(cid, check["label"], "medium", check["fix"], line[:90], offset))

        elif cid == "repeated_opening_verb":
            verbs: dict[str, int] = {}
            for line, _ in _bullets(text):
                words = re.findall(r"[A-Za-z']+", line)
                if words:
                    verbs[words[0].lower()] = verbs.get(words[0].lower(), 0) + 1
            for verb, count in verbs.items():
                if count >= check["threshold_count"]:
                    hits.append(
                        Hit(
                            cid,
                            check["label"],
                            "low",
                            check["fix"],
                            f'"{verb}" opens {count} bullets',
                            0,
                        )
                    )
    return hits


def _bullets(text: str) -> list[tuple[str, int]]:
    """Lines that look like CV bullets, with their offset in the source."""
    out = []
    offset = 0
    for raw in text.splitlines():
        line = raw.strip()
        if line[:2] in {"• ", "- ", "* ", "– "} or re.match(r"^[•\-\*–]\s", line):
            out.append((re.sub(r"^[•\-\*–]\s*", "", line), offset))
        offset += len(raw) + 1
    return out


def penalty(hits: list[Hit], *, cap: float = 10.0) -> float:
    """Points to deduct from the language_quality criterion, capped at its full weight."""
    total = sum(SEVERITY_COST.get(h.severity, 1.0) for h in hits)
    return min(total, cap)


def language_score(text: str, *, kind: str = "cv", weight: float = 10.0) -> tuple[float, list[Hit]]:
    """Score language quality out of `weight`, deterministically."""
    hits = find(text, kind=kind)
    return round(max(0.0, weight - penalty(hits, cap=weight)), 1), hits


def clean_report(text: str, *, kind: str = "cv") -> dict:
    hits = find(text, kind=kind)
    by_severity: dict[str, int] = {}
    for h in hits:
        by_severity[h.severity] = by_severity.get(h.severity, 0) + 1
    return {
        "clean": not hits,
        "count": len(hits),
        "by_severity": by_severity,
        "hits": [h.as_dict() for h in hits],
    }
