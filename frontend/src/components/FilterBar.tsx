import type { Filters, JobEntry } from "../types";
import { sourceLabel } from "../lib/format";

interface Props {
  filters: Filters;
  onChange: (patch: Partial<Filters>) => void;
  jobs: JobEntry[];
  resultCount: number;
  onReset: () => void;
}

const SELECT =
  "rounded-lg border border-line bg-surface px-2.5 py-2 text-sm text-ink outline-none focus:border-accent";

export function FilterBar({ filters, onChange, jobs, resultCount, onReset }: Props) {
  // Facets are derived from the corpus rather than hardcoded, so a source or seniority
  // that stops appearing disappears from the UI instead of offering an empty filter.
  const sources = [...new Set(jobs.map((j) => j.source))].sort();
  const seniorities = [...new Set(jobs.map((j) => j.seniority))].filter((s) => s !== "unknown").sort();

  const active =
    filters.q ||
    filters.location ||
    filters.remote ||
    filters.seniority ||
    filters.source ||
    filters.maxAgeDays ||
    filters.minSalary;

  return (
    <div className="sticky top-0 z-10 border-b border-line bg-canvas/95 backdrop-blur">
      <div className="mx-auto max-w-5xl px-4 py-3">
        <div className="flex flex-col gap-2 sm:flex-row">
          <input
            type="search"
            value={filters.q}
            onChange={(e) => onChange({ q: e.target.value })}
            placeholder="Role, skill or company — e.g. data analyst, python, Razorpay"
            className="flex-1 rounded-lg border border-line bg-surface px-3 py-2 text-sm text-ink outline-none placeholder:text-muted focus:border-accent"
            aria-label="Search jobs"
          />
          <input
            type="search"
            value={filters.location}
            onChange={(e) => onChange({ location: e.target.value })}
            placeholder="Location"
            className="rounded-lg border border-line bg-surface px-3 py-2 text-sm text-ink outline-none placeholder:text-muted focus:border-accent sm:w-44"
            aria-label="Filter by location"
          />
        </div>

        <div className="mt-2 flex flex-wrap items-center gap-2">
          <select
            value={filters.remote}
            onChange={(e) => onChange({ remote: e.target.value })}
            className={SELECT}
            aria-label="Work mode"
          >
            <option value="">Any mode</option>
            <option value="remote">Remote</option>
            <option value="hybrid">Hybrid</option>
            <option value="onsite">On-site</option>
          </select>

          <select
            value={filters.seniority}
            onChange={(e) => onChange({ seniority: e.target.value })}
            className={SELECT}
            aria-label="Seniority"
          >
            <option value="">Any level</option>
            {seniorities.map((s) => (
              <option key={s} value={s}>
                {s.charAt(0).toUpperCase() + s.slice(1)}
              </option>
            ))}
          </select>

          <select
            value={filters.source}
            onChange={(e) => onChange({ source: e.target.value })}
            className={SELECT}
            aria-label="Source"
          >
            <option value="">All sources</option>
            {sources.map((s) => (
              <option key={s} value={s}>
                {sourceLabel(s)}
              </option>
            ))}
          </select>

          <select
            value={filters.maxAgeDays}
            onChange={(e) => onChange({ maxAgeDays: Number(e.target.value) })}
            className={SELECT}
            aria-label="Posted within"
          >
            <option value={0}>Any time</option>
            <option value={1}>Past 24 hours</option>
            <option value={7}>Past week</option>
            <option value={14}>Past 2 weeks</option>
            <option value={30}>Past month</option>
          </select>

          <select
            value={filters.minSalary}
            onChange={(e) => onChange({ minSalary: Number(e.target.value) })}
            className={SELECT}
            aria-label="Minimum salary"
          >
            <option value={0}>Any salary</option>
            <option value={500_000}>5L+</option>
            <option value={1_000_000}>10L+</option>
            <option value={2_000_000}>20L+</option>
            <option value={4_000_000}>40L+</option>
          </select>

          <select
            value={filters.sort}
            onChange={(e) => onChange({ sort: e.target.value as Filters["sort"] })}
            className={SELECT}
            aria-label="Sort by"
          >
            <option value="relevance">Most relevant</option>
            <option value="newest">Newest</option>
            <option value="salary">Highest salary</option>
          </select>

          <span className="ml-auto text-sm text-muted">
            {resultCount.toLocaleString("en-IN")} {resultCount === 1 ? "job" : "jobs"}
          </span>
          {active ? (
            <button
              onClick={onReset}
              className="rounded-lg border border-line px-2.5 py-2 text-sm text-muted hover:text-ink"
            >
              Clear
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}
