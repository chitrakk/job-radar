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


# --------------------------------------------------------------------------- taxonomy


TAXONOMY_SAMPLES = [
    "Sr. Data Analyst",
    "Jr Data Scientist",
    "ML Engineer",
    "BI Analyst",
    "Senior SWE",
    "Engineering Mgr.",
    "Data Analyst/ Senior Data Analyst",
    "Financial Data Analyst (SQL, Power BI-DAX)",
    "data scientist",
    "machine learning engineer",
]


def test_normalise_is_identical_in_both_languages() -> None:
    """taxonomy.py and taxonomy.ts both normalise titles before matching. If they disagree,
    a job ranks differently on the website than in the pipeline that built the corpus —
    the exact drift the shared JSON is meant to prevent."""
    from jobradar.taxonomy import normalise

    script = textwrap.dedent(
        """
        let raw = ''; process.stdin.on('data', c => raw += c);
        process.stdin.on('end', () => {
          const { samples } = JSON.parse(raw);
          const EXPANSIONS = [
            [/\\bsr\\b\\.?/g, 'senior'], [/\\bjr\\b\\.?/g, 'junior'],
            [/\\bmgr\\b\\.?/g, 'manager'], [/\\beng\\b\\.?/g, 'engineer'],
            [/\\bdev\\b\\.?/g, 'developer'], [/\\bml\\b/g, 'machine learning'],
            [/\\bai\\b/g, 'artificial intelligence'], [/\\bbi\\b/g, 'business intelligence'],
            [/\\bds\\b/g, 'data science'], [/\\bswe\\b/g, 'software engineer'],
            [/\\bsde\\b/g, 'software engineer'], [/\\bpm\\b/g, 'product manager'],
          ];
          const out = {};
          for (const s of samples) {
            let t = ' ' + s.toLowerCase() + ' ';
            t = t.replace(/[^a-z0-9+#/&.\\- ]+/g, ' ');
            for (const [re, repl] of EXPANSIONS) t = t.replace(re, repl);
            out[s] = t.replace(/\\s+/g, ' ').trim();
          }
          process.stdout.write(JSON.stringify(out));
        });
        """
    )
    js = _node(script, {"samples": TAXONOMY_SAMPLES})
    for sample in TAXONOMY_SAMPLES:
        assert normalise(sample) == js[sample], (
            f"{sample!r}: python={normalise(sample)!r} js={js[sample]!r}"
        )


def test_role_taxonomy_is_well_formed() -> None:
    """A malformed family would silently stop demoting sibling roles, which is the whole
    reason the taxonomy exists."""
    from jobradar.taxonomy import families, taxonomy

    ids = set(families())
    assert ids, "taxonomy has no families"
    for fid, fam in families().items():
        assert fam["canonical"], f"{fid} has no canonical titles"
        for sibling in fam.get("confused_with", []):
            assert sibling in ids, f"{fid} points at unknown family {sibling!r}"
            assert sibling != fid, f"{fid} lists itself as confused_with"
    assert taxonomy().get("noise_titles"), "no noise titles configured"


def test_data_roles_do_not_claim_each_others_canonical_names() -> None:
    """data_analyst / data_scientist / data_engineer are the pair this whole design is
    about. If one lists another's canonical title as its own alias, the demotion silently
    stops working."""
    from jobradar.taxonomy import families, normalise

    fams = families()
    trio = ["data_analyst", "data_scientist", "data_engineer"]
    for fid in trio:
        mine = {normalise(p) for p in fams[fid]["canonical"]}
        aliases = {normalise(p) for p in fams[fid]["aliases"]}
        for other in trio:
            if other == fid:
                continue
            clash = {normalise(p) for p in fams[other]["canonical"]} & (mine | aliases)
            assert not clash, f"{fid} claims {other}'s canonical title(s): {clash}"


# --------------------------------------------------------------------------- geo


ESBUILD = shared_dir().parent / "frontend" / "node_modules" / ".bin" / "esbuild"

# Locations in the spellings Indian boards actually use, plus the traps: a remote role
# region-locked away from India, an American state abbreviation that reads like "India",
# and a city whose alias is a substring of another word.
GEO_SAMPLES: list[tuple[str, str, bool]] = [
    ("Gurugram, Haryana, India", "Delhi", False),
    ("Gurugram, Haryana, India", "Gurgaon", False),
    ("Noida, Uttar Pradesh, India", "Delhi NCR", False),
    ("Bengaluru East, Karnataka, India", "Bangalore", False),
    ("Mumbai, Maharashtra, India", "Bombay", False),
    ("Bengaluru, Karnataka, India", "Delhi", False),
    ("Bloomington, IN", "India", False),
    ("Anywhere in the World", "Delhi", True),
    ("REMOTE (US/Canada/UK)", "India", True),
    ("Remote", "India", True),
    ("", "India", True),
    ("San Francisco, CA", "India", False),
    ("Pune/Pimpri-Chinchwad Area", "Pune", False),
    ("Greater Noida", "Noida", False),
    ("Navi Mumbai", "Mumbai", False),
    ("Hyderabad, Telangana, India", "India", False),
    # Multi-city postings, as Shine and Internshala write them.
    ("Bangalore, Chennai, Noida, Hyderabad +4 more", "Delhi", False),
    ("Bangalore, Chennai, Noida, Hyderabad +4 more", "Noida", False),
    ("Bengaluru; Gurugram", "Gurgaon", False),
    ("Bangalore Urban, Chennai", "Delhi", False),
]


@pytest.mark.skipif(not ESBUILD.exists(), reason="frontend deps not installed")
def test_geo_gives_the_same_answers_in_both_languages(tmp_path) -> None:
    """The browser must agree with the pipeline about where a job is.

    This is checked against the real frontend/src/lib/geo.ts rather than a transcription
    of it, because the bug it guards against was precisely that the browser had no geo
    module: it did a substring test, so "Gurgaon" matched none of the 41 Gurugram jobs and
    every worldwide-remote posting matched every city anybody typed.
    """
    from jobradar.geo import locality

    bundle = tmp_path / "geo.mjs"
    build = subprocess.run(
        [
            str(ESBUILD),
            "--bundle",
            "--format=esm",
            "--platform=node",
            "--loader:.json=json",
            f"--outfile={bundle}",
            str(shared_dir().parent / "frontend" / "src" / "lib" / "geo.ts"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if build.returncode != 0:
        pytest.fail(f"esbuild failed: {build.stderr[-800:]}")

    script = textwrap.dedent(
        f"""
        import {{ locality }} from '{bundle}';
        let raw = ''; process.stdin.on('data', c => raw += c);
        process.stdin.on('end', () => {{
          const {{ samples }} = JSON.parse(raw);
          process.stdout.write(JSON.stringify(
            samples.map(([job, query, isRemote]) => locality(job, query, {{ isRemote }}))
          ));
        }});
        """
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        input=json.dumps({"samples": [list(s) for s in GEO_SAMPLES]}),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        pytest.fail(f"node failed: {result.stderr[-800:]}")
    js = json.loads(result.stdout)

    for (job_loc, query_loc, is_remote), got in zip(GEO_SAMPLES, js, strict=True):
        want = locality(job_loc, query_loc, is_remote=is_remote)
        assert want == got, (
            f"{job_loc!r} vs {query_loc!r} (remote={is_remote}): python={want!r} js={got!r}"
        )


def test_locations_json_is_well_formed() -> None:
    import re as _re

    vocab = json.loads((shared_dir() / "locations.json").read_text())
    for city, aliases in vocab["cities"].items():
        assert city in aliases, f"{city} is not among its own aliases"
        assert all(a == a.lower() for a in aliases), f"{city} has a non-lowercase alias"
    # A metro area may only name cities the vocabulary knows.
    for metro, members in vocab.get("metro_areas", {}).items():
        for member in members:
            assert member in vocab["cities"], f"metro {metro} names unknown city {member!r}"
    # Both regexes must compile in Python; the JS side is covered by the parity test above.
    _re.compile(vocab["anywhere_pattern"])
    _re.compile(vocab["region_lock_pattern"])
