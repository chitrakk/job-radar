"""The shared JSON must behave the same in Python and in the browser.

shared/slop_patterns.json is consumed by both backend/jobradar/career/slop.py and
frontend/src/lib/slop.ts. Python and JavaScript regex dialects are not identical — named
groups, possessive quantifiers, inline flags and some escapes differ — so a pattern that
works in one can silently fail or match differently in the other. That would mean a draft
judged clean on the website and dirty at the terminal.

These tests run the real patterns through Node and compare against Python.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap

import pytest

from jobradar.career.slop import _compiled, shared_dir

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is needed to check JavaScript regex parity"
)

SAMPLES = {
    "bad_cv": (
        "Dynamic, results-driven professional passionate about excellence.\n"
        "- Responsible for managing the reporting pipeline\n"
        "- Responsible for building dashboards\n"
        "- Significantly improved a legacy process\n"
    ),
    "note": (
        "I hope this message finds you well. I'm reaching out because I've long admired "
        "your work and I would be a great fit for this role."
    ),
    "clean": (
        "- Cut month-end close from 9 days to 4 by rebuilding reconciliation in SQL\n"
        "- Built a Power BI dashboard used daily by 40 people across 3 business units\n"
    ),
    "binary": "It's not just a job, it's a mission.",
    "puffery": "This marks a pivotal moment and is a testament to the team.",
}


def _node(script: str, payload: dict) -> dict:
    result = subprocess.run(
        ["node", "-e", script],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        pytest.fail(f"node failed: {result.stderr[:800]}")
    return json.loads(result.stdout)


def test_every_pattern_compiles_in_javascript() -> None:
    patterns = json.loads((shared_dir() / "slop_patterns.json").read_text())["patterns"]
    script = textwrap.dedent(
        """
        let raw = ''; process.stdin.on('data', c => raw += c);
        process.stdin.on('end', () => {
          const { patterns } = JSON.parse(raw);
          const failures = [];
          for (const p of patterns) {
            try { new RegExp(p.regex, 'gim'); }
            catch (e) { failures.push({ id: p.id, error: e.message }); }
          }
          process.stdout.write(JSON.stringify({ failures }));
        });
        """
    )
    out = _node(script, {"patterns": patterns})
    assert out["failures"] == [], f"patterns invalid in JS: {out['failures']}"


def test_patterns_match_identically_in_both_languages() -> None:
    patterns = json.loads((shared_dir() / "slop_patterns.json").read_text())["patterns"]
    script = textwrap.dedent(
        """
        let raw = ''; process.stdin.on('data', c => raw += c);
        process.stdin.on('end', () => {
          const { patterns, samples } = JSON.parse(raw);
          const compiled = patterns.map(p => ({ id: p.id, re: new RegExp(p.regex, 'gim') }));
          const out = {};
          for (const [key, text] of Object.entries(samples)) {
            const ids = [];
            for (const { id, re } of compiled) {
              re.lastIndex = 0;
              for (const _ of text.matchAll(re)) ids.push(id);
            }
            out[key] = ids.sort();
          }
          process.stdout.write(JSON.stringify(out));
        });
        """
    )
    js = _node(script, {"patterns": patterns, "samples": SAMPLES})

    for key, text in SAMPLES.items():
        py = sorted(rule["id"] for rule, pat in _compiled() for _ in pat.finditer(text))
        assert py == js[key], (
            f"sample {key!r} diverged\n"
            f"  python only: {sorted(set(py) - set(js[key]))}\n"
            f"  js only:     {sorted(set(js[key]) - set(py))}"
        )


def test_shared_json_files_are_valid_and_complete() -> None:
    """Catches a malformed edit before it reaches either runtime."""
    slop = json.loads((shared_dir() / "slop_patterns.json").read_text())
    rubric = json.loads((shared_dir() / "cv_rubric.json").read_text())
    prompts = json.loads((shared_dir() / "prompts.json").read_text())

    for p in slop["patterns"]:
        assert {"id", "label", "regex", "severity", "fix"} <= p.keys()
        assert p["severity"] in {"high", "medium", "low"}

    assert sum(c["weight"] for c in rubric["criteria"]) == rubric["total"] == 100
    for c in rubric["criteria"]:
        assert c["scoring_anchors"], f"{c['id']} has no scoring anchors"
        assert c["common_failures"], f"{c['id']} has no common failures"

    # A band must exist for every possible score, including 0.
    assert min(b["min"] for b in rubric["bands"]) == 0

    for section in ("cv_score", "outreach", "interview", "contacts"):
        assert "template" in prompts[section], f"{section} has no template"
    assert {"grounding", "voice"} <= prompts["shared_rules"].keys()


def test_prompt_templates_only_use_placeholders_the_callers_supply() -> None:
    """A stray {brace} in a prompt raises KeyError at run time, in the middle of a user's
    CV scoring. Better to catch it here."""
    import re

    from jobradar.career.prompts import build

    supplied = {
        "cv_score": {"system", "criteria", "jd_block", "cv", "grounding", "voice"},
        "outreach": {
            "contact_block",
            "title",
            "company",
            "location",
            "jd",
            "candidate",
            "candidate_name",
            "grounding",
            "voice",
        },
        "interview": {"title", "company", "location", "jd", "cv", "grounding", "voice"},
        "contacts": {"company", "title", "text", "grounding", "voice"},
    }
    prompts = json.loads((shared_dir() / "prompts.json").read_text())

    for section, allowed in supplied.items():
        template = prompts[section]["template"]
        # Single braces only; {{...}} is a literal brace in the JSON-example blocks.
        used = set(re.findall(r"(?<!\{)\{(\w+)\}(?!\})", template))
        assert used <= allowed, f"{section} uses unsupplied placeholders: {used - allowed}"

    # And the real builders must not raise.
    build("cv_score", system="s", criteria="c", jd_block="", cv="cv")
    build("contacts", company="c", title="t", text="x")
