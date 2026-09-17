# 📡 Job Radar

Aggregates job postings from across the internet into one searchable web app, refreshed
every four hours by GitHub Actions. Runs entirely on GitHub infrastructure — once deployed,
nothing depends on your laptop being on.

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

### Tier 2 — scraped, best-effort (disabled by default)

Naukri, Indeed India, Foundit, TimesJobs, Instahyre. **These are expected to fail from
GitHub Actions**, because Naukri answers datacenter IPs with HTTP 406 and Indeed India runs
Cloudflare Turnstile. They are built with proxy support wired in — set `JOBRADAR_PROXY_URL`
to a residential proxy and enable them in `config/sources.yml` to turn them on. Failures
degrade a run and show in the UI's source-health strip; they never fail the workflow.

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
| `GEMINI_API_KEY` | AI enrichment (phase 2), tried first | Yes |
| `GROQ_API_KEY` | AI enrichment fallback when Gemini hits quota | Yes |
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
uv run pytest                                    # 68 tests, no live network

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
frontend/src/    React + Tailwind static app
config/          queries, companies, source switches
```

## Conduct

Public listings only. No authenticated or cookie-based scraping, no candidate data
collected, conservative per-source rate limits, and whole-feed sources fetched once per run
rather than once per query. Listings link back to the original posting — applications
happen there, not here.
