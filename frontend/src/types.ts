/** Mirrors Job.index_entry() in backend/jobradar/models.py. */
export interface JobEntry {
  id: string;
  title: string;
  company: string;
  location: string;
  remote: "onsite" | "hybrid" | "remote" | "unknown";
  url: string;
  source: string;
  tier: "tier1" | "tier2";
  posted_at: string | null;
  first_seen_at: string;
  salary_min: number | null;
  salary_max: number | null;
  salary_currency: string;
  salary_text: string;
  skills: string[];
  seniority: "intern" | "entry" | "mid" | "senior" | "lead" | "exec" | "unknown";
  summary: string;
  tags: string[];
}

/** Mirrors SourceHealth. */
export interface SourceHealth {
  source: string;
  tier: "tier1" | "tier2";
  ok: boolean;
  jobs_found: number;
  error: string;
  duration_ms: number;
  checked_at: string;
}

export interface Meta {
  updated_at: string;
  total_jobs: number;
  sources_ok: number;
  sources_failed: number;
  by_source: Record<string, number>;
  enriched: number;
  tiers: { tier1: number; tier2: number };
}

/** Full detail for one job, loaded lazily from jobs/<shard>.json. */
export interface JobDetail {
  description: string;
  apply_url: string;
  salary_period: string;
  enriched: boolean;
}

export interface Filters {
  q: string;
  /** Canonical city keys from shared/locations.json. Empty means anywhere. OR semantics:
   *  somebody who ticks Noida and Gurugram wants either, not both at once. */
  cities: string[];
  /** Role family ids from shared/role_taxonomy.json. Empty means any role. */
  roles: string[];
  /** Count a neighbouring city in the same metro as a match. On by default: Delhi, Noida
   *  and Gurugram are one commuter market. Off for somebody who will not cross the NCR. */
  includeNearby: boolean;
  remote: string;
  seniority: string;
  source: string;
  maxAgeDays: number;
  minSalary: number;
  sort: "relevance" | "newest" | "salary";
}

/** A fresh blank filter set. A function rather than a shared constant because two of the
 *  fields are arrays, and handing the same array to every reset invites a mutation bug. */
export function emptyFilters(): Filters {
  return {
    q: "",
    cities: [],
    roles: [],
    includeNearby: true,
    remote: "",
    seniority: "",
    source: "",
    maxAgeDays: 0,
    minSalary: 0,
    sort: "relevance",
  };
}
