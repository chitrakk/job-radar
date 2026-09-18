# 📡 Job Radar

Finds relevant jobs across the internet, scores your CV against them, drafts outreach to the
right person, and preps you for the interview. Refreshed every four hours by GitHub Actions
and served as a web app anyone can use — once deployed, nothing depends on your laptop.

**Live site:** `https://<your-username>.github.io/<repo>/` (after the first deploy)

---

## How it works

GitHub Pages can only serve static files, so the scrapers do not run there. Instead:

```
GitHub Actions (cron, every 4h)
  config/queries.yml → 14 sources → dedupe → rank → LLM enrich → corpus JSON
                                                                    │
                          ┌─────────────────────────────────────────┘
                          ▼
GitHub Pages (static React app, free, always on)
  loads index.json → instant client-side search, filter and sort
```

The corpus is published to an orphan `data` branch (force-pushed each run, so four-hourly
commits never bloat `main`) and copied into the Pages deploy.

## What it does

| | |
| --- | --- |
| **Find** | 14 sources, deduplicated across boards, ranked by relevance and recency. India-aware location matching. |
| **Score your CV** | Out of 100 across 8 weighted criteria, with grounded rewrite suggestions and the keywords a target role expects. |
| **Draft outreach** | Finds who to contact, infers their email pattern, writes a LinkedIn note and a cold email — then checks its own output for AI slop. |
| **Prep for interview** | Likely questions for *that* posting, STAR outlines from your real CV, and the gaps they will probe. |
| **Track** | Applications, outreach and CV scores written to a Google Sheet you own. |

### On outreach: it drafts, you send

The agent researches and writes. It does **not** send, and it does not drive a logged-in
LinkedIn session. Automating connection requests breaches LinkedIn's User Agreement and their
automation detection restricts or permanently bans accounts — and it is *your* network at
risk. One considered message you send by hand beats fifty automated ones from a banned
account. Every draft is checked against the
[no-ai-slop](https://github.com/petergyang/no-ai-slop) patterns before you see it, because a
recruiter who can tell a message was generated is worse than no message.

Nothing in the CV or outreach path invents experience. Where a rewrite needs a metric you did
not supply, you get a `[X%]` placeholder to fill in — never a guessed number you would have to
defend in an interview.

---

## Sources

### Tier 1 — public APIs, no blocking

| Source | What it gives you |
| --- | --- |
| Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee | ~65 company boards, full req lists, unauthenticated |
| Adzuna | India-domestic listings — it licenses Naukri/Indeed data. **Needs a free API key.** |
| LinkedIn (guest) | Public job search, no cookies or account |
| RemoteOK, Remotive, Arbeitnow, Himalayas, WeWorkRemotely | Remote roles open to India |
| Hacker News "Who is Hiring" | Startup roles that never reach a job board |

### Tier 2 — scraped with Scrapling, best-effort (disabled by default)

Naukri, Indeed India, Foundit, TimesJobs and Instahyre, via
[Scrapling](https://github.com/d4vinci/Scrapling)'s `StealthyFetcher` — real browser TLS
fingerprints, Cloudflare Turnstile solving, and adaptive selectors that survive the layout
changes these sites ship constantly.

**Expect these to report as blocked in the cloud.** Naukri answers datacenter IPs with
HTTP 406 and Indeed India runs Turnstile; GitHub Actions runners and every free PaaS are
datacenter IPs. That is why they ship disabled. They are genuinely useful in two situations:

- **From your own machine** — a residential IP, so run `jobradar pipeline` locally.
- **With a residential proxy** — set `JOBRADAR_PROXY_URL` and they work in Actions too, with
  no code change.

To turn them on:

```bash
cd backend
uv pip install -e ".[scrape]"   # Scrapling is an optional extra
uv run scrapling install        # downloads the browser binaries
# then flip enabled: true for the sources you want in config/sources.yml
```

Without the extra installed, the module raises ImportError, the loader catches it, and
Tier 1 carries on untouched. A blocked source degrades a run and shows in the UI's
source-health strip — it never fails the workflow, and crucially it is never mistaken for
"this search had no results", because challenge pages are detected before parsing.

> **On India coverage.** Probing ~150 candidate tokens established that Indian-domestic
> employers mostly do *not* use Greenhouse/Lever/Ashby — they run Darwinbox, Keka, Workday
> or Naukri's own RMS, none of which expose a public API. Tier 1 therefore gives you global
> companies hiring into Bengaluru/Hyderabad/Pune/Gurugram plus India-founded product
> companies. **For India-domestic listings, Adzuna is the load-bearing source — set up its
> free key first.**

---

## Setup

### 1. Push to GitHub

```bash
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

### 2. Enable Pages

Repository **Settings → Pages → Source: GitHub Actions**.

### 3. Add secrets

**Settings → Secrets and variables → Actions → New repository secret.** All are optional —
the pipeline runs without them, just with fewer sources.

| Secret | Why | Free? |
| --- | --- | --- |
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | India-domestic jobs. Sign up at [developer.adzuna.com](https://developer.adzuna.com) | Yes, 1000 calls/mo |
| `GEMINI_API_KEY` | Job enrichment + career tools. Tried first | Yes |
| `GROQ_API_KEY` | Fallback when Gemini hits its daily quota | Yes |
| `SHEETS_WEBAPP_URL`, `SHEETS_SECRET` | Google Sheets tracker (CLI only) | Yes |
| `JOBRADAR_PROXY_URL` | Turns on Tier 2 scrapers | No (residential proxy) |

### 4. Run it

**Actions → Refresh jobs → Run workflow.** The first run takes ~3 minutes and publishes the
site. After that it runs itself every four hours.

---

## Configuring what it looks for

Everything lives in `config/`, so changing what gets collected needs no code changes —
edit, push, and the workflow picks it up.

- **`queries.yml`** — the saved searches. Keywords, locations, and `strict_location`
  (which is what keeps San Francisco roles out of an India search).
- **`companies.yml`** — ATS board tokens. Coverage scales directly with this list; adding a
  company is one line. Find a token in a careers page URL, e.g. `boards.greenhouse.io/<token>`.
- **`sources.yml`** — enable/disable sources and set rate limits.

Verify board tokens still work (they rot as companies switch ATS):

```bash
uv run python scripts/verify_boards.py
```

---

## Career tools

Available two ways: in the web app (**My CV** tab and the buttons on any expanded job), and
from the CLI. Both read the same rubric and prompts from `shared/`, so the score is the same.

```bash
uv run jobradar career find "data analyst"              # list job ids
uv run jobradar career score-cv ~/cv.pdf                # score out of 100
uv run jobradar career score-cv ~/cv.pdf --job b6edc6   # score against one posting
uv run jobradar career outreach b6edc6 --cv ~/cv.pdf    # contacts + drafts (sends nothing)
uv run jobradar career interview b6edc6 --cv ~/cv.pdf   # prep pack
uv run jobradar career track b6edc6 --status Applied    # log to Google Sheets
uv run jobradar career check-text draft.txt --kind email  # slop check, no AI needed
```

The slop check needs no API key at all — it is pure pattern matching, so it is free,
instant, reproducible, and cannot hallucinate a verdict.

### Privacy

In the web app your CV, API keys and tracker credentials live in **your browser's local
storage only**. There is no server to send them to. Your CV is included as prompt text in
requests to Gemini or Groq when you click a button, so it reaches your chosen model provider
and nobody else.

### Google Sheets tracker

Deploy `sheets/Code.gs` as an Apps Script web app bound to your own spreadsheet, then paste
the URL and secret into Settings. Each person gets their own sheet — no shared database, no
service-account key, no OAuth server. Setup steps are in the file's header comment.

---

## Local development

Needs Python 3.12 (`uv` provisions it; your system Python is untouched) and Node 22.

```bash
cd backend
uv venv --python 3.12
uv pip install -e ".[dev]"

uv run jobradar sources                          # what's registered and enabled
uv run jobradar scrape --source linkedin -q "data analyst" --location Bengaluru
uv run jobradar pipeline --dry-run --no-llm      # full run, writes nothing
uv run jobradar pipeline --no-llm                # writes ../data
uv run pytest                                    # 104 tests, no live network

cd ../frontend
npm install
cp -r ../data public/data
npm run dev
```

`--rebuild` discards the stored corpus first. Use it after tightening a filter, since
postings otherwise persist until they age out of the 60-day window.

---

## Layout

```
backend/jobradar/
  models.py      canonical Job schema — every source normalises into this
  sources/       one adapter per board; @register makes it live
  geo.py         India-aware location matching (Bengaluru/Bangalore/BLR, "IN" ≠ India)
  textutil.py    HTML flattening + salary parsing (LPA, lakh, crore, k)
  dedupe.py      collapses the same role across sources, merging complementary fields
  search.py      relevance ranking — mirrored in frontend/src/lib/search.ts
  store.py       corpus read/write, index + sharded descriptions
  pipeline.py    orchestration; no single source can fail a run
  llm.py         Gemini → Groq fallback; never blocks a run
  enrich.py      skills/seniority/summary for newly-seen postings only
  career/
    cv.py        CV scoring; model scores clamped to each criterion's max
    outreach.py  contact discovery + drafting. Researches and drafts; never sends
    interview.py prep pack grounded in the posting and your CV
    slop.py      deterministic AI-slop detection, no model call
    sheets.py    Apps Script tracker client
shared/          rubric, slop patterns and prompts — read by BOTH Python and the browser,
                 with a test that runs the patterns through Node to prove they behave
                 identically in each
frontend/src/    React + Tailwind static app
sheets/Code.gs   Apps Script web app for the tracker
config/          queries, companies, source switches
```

## Conduct

Public listings only. No authenticated or cookie-based scraping, no candidate data
collected, conservative per-source rate limits, and whole-feed sources fetched once per run
rather than once per query. Listings link back to the original posting — applications
happen there, not here.
