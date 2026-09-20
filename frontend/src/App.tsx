import { useEffect, useMemo, useState } from "react";
import type { Filters, JobEntry, Meta, SourceHealth } from "./types";
import { EMPTY_FILTERS } from "./types";
import { applyFilters } from "./lib/search";
import { parseCorpus } from "./lib/corpus";
import { FilterBar } from "./components/FilterBar";
import { JobCard } from "./components/JobCard";
import { SourceHealthBar } from "./components/SourceHealthBar";
import { CVPanel } from "./components/CVPanel";
import { SettingsPanel } from "./components/SettingsPanel";
import { loadSettings, saveSettings, type Settings } from "./lib/settings";

// The corpus sits next to the built site, so one relative base works for local preview and
// for https://<user>.github.io/<repo>/ without knowing the repo name at build time.
const DATA_BASE = new URL("data/", document.baseURI).href;

const PAGE_SIZE = 40;

/** Read filters from the URL so a search can be linked and shared. */
function filtersFromUrl(): Filters {
  const p = new URLSearchParams(location.search);
  return {
    ...EMPTY_FILTERS,
    q: p.get("q") ?? "",
    location: p.get("loc") ?? "",
    remote: p.get("remote") ?? "",
    seniority: p.get("level") ?? "",
    source: p.get("src") ?? "",
    maxAgeDays: Number(p.get("age") ?? 0),
    minSalary: Number(p.get("pay") ?? 0),
    sort: (p.get("sort") as Filters["sort"]) ?? "relevance",
  };
}

function urlFromFilters(f: Filters): string {
  const p = new URLSearchParams();
  if (f.q) p.set("q", f.q);
  if (f.location) p.set("loc", f.location);
  if (f.remote) p.set("remote", f.remote);
  if (f.seniority) p.set("level", f.seniority);
  if (f.source) p.set("src", f.source);
  if (f.maxAgeDays) p.set("age", String(f.maxAgeDays));
  if (f.minSalary) p.set("pay", String(f.minSalary));
  if (f.sort !== "relevance") p.set("sort", f.sort);
  const qs = p.toString();
  return qs ? `?${qs}` : location.pathname;
}

export default function App() {
  const [jobs, setJobs] = useState<JobEntry[] | null>(null);
  const [health, setHealth] = useState<SourceHealth[]>([]);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dropped, setDropped] = useState(0);
  const [filters, setFilters] = useState<Filters>(filtersFromUrl);
  const [shown, setShown] = useState(PAGE_SIZE);
  const [view, setView] = useState<"jobs" | "cv" | "settings">("jobs");
  const [settings, setSettings] = useState<Settings>(loadSettings);
  // Which posting the CV is being scored against. CVPanel always supported this, but
  // nothing passed it — so the keyword-alignment criterion, 18 of the 100 points, was
  // being judged against an empty string for every user.
  const [cvTarget, setCvTarget] = useState<{ role: string; description: string } | null>(null);

  function patchSettings(p: Partial<Settings>) {
    setSettings((s) => {
      const next = { ...s, ...p };
      saveSettings(next);
      return next;
    });
  }

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [idx, hl, mt] = await Promise.all([
          fetch(`${DATA_BASE}index.json`).then((r) => {
            if (!r.ok) throw new Error(`index.json: ${r.status}`);
            return r.json();
          }),
          // Health and meta are nice-to-have; a missing file must not blank the page.
          fetch(`${DATA_BASE}health.json`).then((r) => (r.ok ? r.json() : [])).catch(() => []),
          fetch(`${DATA_BASE}meta.json`).then((r) => (r.ok ? r.json() : null)).catch(() => null),
        ]);
        if (cancelled) return;
        // Never hand the UI a record straight off the wire: a scraper that starts
        // emitting nulls should cost us those rows, not blank the whole page.
        const parsed = parseCorpus(idx);
        if (!Array.isArray(idx)) throw new Error("index.json is not a list of jobs");
        if (parsed.dropped) {
          console.warn(`dropped ${parsed.dropped} unusable job records`);
        }
        setJobs(parsed.jobs);
        setDropped(parsed.dropped);
        setHealth(Array.isArray(hl) ? hl : []);
        setMeta(mt && typeof mt === "object" ? (mt as Meta) : null);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Keep the URL in step with the filters, replacing rather than pushing so the back
  // button leaves the site instead of walking back through every keystroke.
  useEffect(() => {
    history.replaceState(null, "", urlFromFilters(filters));
    setShown(PAGE_SIZE);
  }, [filters]);

  const search = useMemo(
    () => (jobs ? applyFilters(jobs, filters) : { jobs: [], relaxed: false, strongCount: 0 }),
    [jobs, filters],
  );
  const results = search.jobs;

  function patch(p: Partial<Filters>) {
    setFilters((f) => ({ ...f, ...p }));
  }

  return (
    <div className="min-h-screen">
      <header className="border-b border-line bg-surface">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-x-3 gap-y-2 px-4 py-4">
          <h1 className="text-lg font-semibold tracking-tight text-ink">📡 Job Radar</h1>
          <p className="hidden flex-1 text-sm text-muted lg:block">
            Openings pulled from company job boards, LinkedIn and remote feeds
          </p>
          <nav className="ml-auto flex gap-1" aria-label="Sections">
            {(
              [
                ["jobs", "Jobs"],
                ["cv", "My CV"],
                ["settings", "Settings"],
              ] as const
            ).map(([id, label]) => (
              <button
                key={id}
                onClick={() => setView(id)}
                aria-current={view === id ? "page" : undefined}
                className={
                  view === id
                    ? "rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-white"
                    : "rounded-lg border border-line px-3 py-1.5 text-sm font-medium text-muted hover:text-ink"
                }
              >
                {label}
              </button>
            ))}
          </nav>
        </div>
      </header>

      {view === "jobs" && <SourceHealthBar health={health} meta={meta} />}

      {view === "jobs" && jobs && (
        <FilterBar
          filters={filters}
          onChange={patch}
          jobs={jobs}
          resultCount={results.length}
          onReset={() => setFilters(EMPTY_FILTERS)}
        />
      )}

      <main className="mx-auto max-w-5xl px-4 py-5">
        {view === "settings" && (
          <SettingsPanel
            settings={settings}
            onChange={patchSettings}
            onClose={() => setView("jobs")}
          />
        )}

        {view === "cv" && (
          <CVPanel
            settings={settings}
            onChange={patchSettings}
            jobDescription={cvTarget?.description}
            targetRole={cvTarget?.role}
            onClearTarget={() => setCvTarget(null)}
          />
        )}

        {view === "jobs" && (
          <>
        {error && (
          <div className="rounded-xl border border-rose-500/30 bg-rose-500/5 p-4 text-sm">
            <p className="font-medium text-ink">Could not load the job index.</p>
            <p className="mt-1 text-muted">
              {error}. If this site was just deployed, the first scrape may not have finished yet.
            </p>
          </div>
        )}

        {!jobs && !error && (
          <div className="space-y-3" aria-busy>
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="h-28 animate-pulse rounded-xl border border-line bg-surface" />
            ))}
          </div>
        )}

        {/* A damaged index is worth admitting: it means a scraper is returning junk, and
            the counts on this page are lower than the source actually published. */}
        {dropped > 0 && (
          <div className="mb-3 rounded-xl border border-amber-500/30 bg-amber-500/5 p-3 text-sm text-muted">
            {dropped.toLocaleString("en-IN")} listing{dropped === 1 ? "" : "s"} in the last
            refresh were unreadable and have been left out.
          </div>
        )}

        {/* A quiet fallback that returns worse matches without saying so is how a search
            loses trust. If the bar had to come down, say it came down. */}
        {jobs && search.relaxed && (
          <div className="mb-3 rounded-xl border border-amber-500/30 bg-amber-500/5 p-3 text-sm">
            <p className="text-ink">
              {search.strongCount === 0
                ? "Nothing matches this closely."
                : `Only ${search.strongCount} close ${search.strongCount === 1 ? "match" : "matches"}.`}{" "}
              Showing near misses too — widen the location or drop a filter for better ones.
            </p>
          </div>
        )}

        {jobs && results.length === 0 && (
          <div className="rounded-xl border border-line bg-surface p-8 text-center">
            <p className="font-medium text-ink">No jobs match these filters.</p>
            <p className="mt-1 text-sm text-muted">
              Try a broader keyword, or clear the location and date filters.
            </p>
          </div>
        )}

        <div className="space-y-3">
          {results.slice(0, shown).map((job) => (
            <JobCard
              key={job.id}
              job={job}
              dataBase={DATA_BASE}
              settings={settings}
              onScoreCV={(role, description) => {
                setCvTarget({ role, description });
                setView("cv");
              }}
            />
          ))}
        </div>

        {results.length > shown && (
          <button
            onClick={() => setShown((n) => n + PAGE_SIZE)}
            className="mx-auto mt-5 block rounded-lg border border-line bg-surface px-4 py-2 text-sm font-medium text-ink hover:border-accent"
          >
            Show more ({(results.length - shown).toLocaleString("en-IN")} remaining)
          </button>
        )}
          </>
        )}
      </main>

      <footer className="border-t border-line px-4 py-6 text-center text-xs text-muted">
        <p>
          Aggregated from public job board APIs. Listings link to the original posting — apply there.
        </p>
        {meta && <p className="mt-1">Last refreshed {new Date(meta.updated_at).toLocaleString()}</p>}
      </footer>
    </div>
  );
}
