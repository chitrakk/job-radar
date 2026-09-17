/**
 * Client-side ranking.
 *
 * Deliberately mirrors the weights in backend/jobradar/search.py so that a job's position
 * in the list is the same whether it was ranked in the scheduled run or here in the
 * browser. If you change one, change the other.
 *
 * Runs over the whole corpus on every keystroke, so it stays allocation-light: no regex
 * per job, no building intermediate arrays per field.
 */
import type { Filters, JobEntry } from "../types";

const W_TITLE = 10;
const W_SKILLS = 4;
const W_TAGS = 2.5;
const W_COMPANY = 2;
const W_BODY = 1; // summary stands in for the body, which is not in the index

const WORD = /[a-z0-9+#.]+/g;

export function tokenize(text: string): string[] {
  return text.toLowerCase().match(WORD) ?? [];
}

function fieldScore(terms: string[], text: string, weight: number): number {
  if (!text || terms.length === 0) return 0;
  const blob = text.toLowerCase();
  let hits = 0;
  for (const term of terms) {
    const at = blob.indexOf(term);
    if (at === -1) continue;
    // Whole-word match scores full, substring scores half — "analyst" inside "analytics"
    // is a weaker signal than the word itself.
    const before = at === 0 ? " " : blob[at - 1];
    const after = at + term.length >= blob.length ? " " : blob[at + term.length];
    const wholeWord = !/[a-z0-9]/.test(before) && !/[a-z0-9]/.test(after);
    hits += wholeWord ? 1 : 0.5;
  }
  return weight * hits;
}

function ageDays(job: JobEntry): number {
  const stamp = Date.parse(job.posted_at ?? job.first_seen_at);
  if (Number.isNaN(stamp)) return 999;
  return Math.max(0, (Date.now() - stamp) / 86_400_000);
}

export function recencyBoost(job: JobEntry, halfLifeDays = 14): number {
  return Math.exp(-ageDays(job) / halfLifeDays);
}

export function scoreJob(job: JobEntry, terms: string[]): number {
  let base = 1;
  if (terms.length) {
    const raw =
      fieldScore(terms, job.title, W_TITLE) +
      fieldScore(terms, job.skills.join(" "), W_SKILLS) +
      fieldScore(terms, job.tags.join(" "), W_TAGS) +
      fieldScore(terms, job.company, W_COMPANY) +
      fieldScore(terms, job.summary, W_BODY);
    base = raw / (terms.length * W_TITLE);
  }
  return base * (0.55 + 0.45 * recencyBoost(job));
}

/** Annualised salary in a single currency, for sorting and the min-salary filter.
 *  Rough static rates — good enough to rank, not to quote. */
const TO_INR: Record<string, number> = {
  INR: 1,
  USD: 84,
  EUR: 91,
  GBP: 106,
  "": 1,
};

export function salaryInINR(job: JobEntry): number | null {
  if (job.salary_max == null) return null;
  const rate = TO_INR[job.salary_currency] ?? 1;
  return job.salary_max * rate;
}

export function applyFilters(jobs: JobEntry[], f: Filters): JobEntry[] {
  const terms = tokenize(f.q);
  const locTerms = tokenize(f.location);

  let out = jobs.filter((job) => {
    if (f.remote && job.remote !== f.remote) return false;
    if (f.seniority && job.seniority !== f.seniority) return false;
    if (f.source && job.source !== f.source) return false;
    if (f.maxAgeDays && ageDays(job) > f.maxAgeDays) return false;
    if (f.minSalary) {
      const s = salaryInINR(job);
      if (s == null || s < f.minSalary) return false;
    }
    if (locTerms.length) {
      const loc = job.location.toLowerCase();
      // A remote role satisfies any location the user typed.
      if (job.remote !== "remote" && !locTerms.some((t) => loc.includes(t))) return false;
    }
    if (terms.length) {
      // Require at least one term to appear somewhere, so unrelated jobs drop out entirely
      // rather than merely ranking low.
      const haystack = `${job.title} ${job.company} ${job.skills.join(" ")} ${job.tags.join(
        " ",
      )} ${job.summary}`.toLowerCase();
      if (!terms.some((t) => haystack.includes(t))) return false;
    }
    return true;
  });

  if (f.sort === "newest") {
    out = out.sort(
      (a, b) =>
        Date.parse(b.posted_at ?? b.first_seen_at) - Date.parse(a.posted_at ?? a.first_seen_at),
    );
  } else if (f.sort === "salary") {
    out = out.sort((a, b) => (salaryInINR(b) ?? -1) - (salaryInINR(a) ?? -1));
  } else {
    out = out
      .map((job) => ({ job, s: scoreJob(job, terms) }))
      .sort((a, b) => b.s - a.s)
      .map((x) => x.job);
  }
  return out;
}

/** Which shard holds a job's full description. Mirrors SHARD_CHARS in store.py. */
export function shardFor(id: string): string {
  return id.slice(0, 2);
}
