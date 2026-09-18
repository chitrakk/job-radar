/**
 * Client-side ranking.
 *
 * Mirrors backend/jobradar/search.py — same tiers, same weights, same penalties — so a job
 * sits in the same position whether it was ranked by the scheduled pipeline or here in the
 * browser. If you change one, change the other; tests/test_relevance.py checks the shared
 * taxonomy stays in step.
 *
 * Tiers, highest first:
 *   1. exact query phrase in the title
 *   2. a known alias of the same role in the title
 *   3. every query term present in the title
 *   4. some query terms in the title
 *   5. body / skills evidence only
 *
 * A title naming a sibling role ("Data Engineer" for a "data analyst" query) is then
 * demoted, which is the thing plain term matching cannot do.
 */
import type { Filters, JobEntry } from "../types";
import { isNoiseTitle, normalise, understand, type Intent } from "./taxonomy";

const T_EXACT_PHRASE = 1.0;
const T_ALIAS = 0.82;
const T_ALL_TERMS = 0.7;
const T_PARTIAL = 0.34;
const T_BODY_ONLY = 0.16;

const W_SKILL_HIT = 0.03;
const W_BODY_TERM = 0.02;
const W_COMPANY = 0.015;
const MAX_CORROBORATION = 0.18;
const W_TITLE_FOCUS = 0.12;

const RIVAL_PENALTY = 0.35;
const NOISE_PENALTY = 0.15;

/**
 * Minimum score to show for a keyword search.
 *
 * Set just above T_PARTIAL, so a result must have real evidence in its *title* rather than
 * a passing mention in the body. Measured on the live corpus this is where the distribution
 * falls off a cliff: "data scientist" goes 457 results at 0.02, to 76 at 0.20, to 12 at
 * 0.35 — and then stays at 12 however much higher you push it. Everything below the cliff
 * was a job whose description happened to say "data" somewhere.
 */
const RELEVANCE_FLOOR = 0.35;

/**
 * If the floor leaves too few results, relax it rather than showing an empty page — a
 * narrow or unusual query should degrade to "here are the near misses", not to nothing.
 */
const MIN_RESULTS_BEFORE_RELAXING = 5;
const RELAXED_FLOOR = 0.15;

function ageDays(job: JobEntry): number {
  const stamp = Date.parse(job.posted_at ?? job.first_seen_at);
  if (Number.isNaN(stamp)) return 999;
  return Math.max(0, (Date.now() - stamp) / 86_400_000);
}

export function recencyBoost(job: JobEntry, halfLifeDays = 14): number {
  return Math.exp(-ageDays(job) / halfLifeDays);
}

/** Everything the job says about itself, for corroboration only. */
function haystackFor(job: JobEntry): string {
  return normalise(
    `${job.title} ${job.skills.join(" ")} ${job.tags.join(" ")} ${job.summary}`,
  );
}

function titleTier(titleNorm: string, intent: Intent): number {
  if (intent.terms.length === 0) return T_ALL_TERMS;
  if (intent.normalised && titleNorm.includes(intent.normalised)) return T_EXACT_PHRASE;
  for (const alias of intent.aliases) {
    if (alias && titleNorm.includes(alias)) return T_ALIAS;
  }
  const titleTokens = new Set(titleNorm.split(" "));
  const present = intent.terms.filter((t) => titleTokens.has(t)).length;
  if (present === intent.terms.length) return T_ALL_TERMS;
  if (present > 0) return T_PARTIAL * (present / intent.terms.length);
  return 0;
}

/** What fraction of the title the query accounts for — "Data Analyst" is a cleaner match
 *  for "data analyst" than "Financial Data Analyst (SQL, Power BI-DAX)". */
function titleFocus(titleNorm: string, intent: Intent): number {
  if (!titleNorm) return 0;
  let best = titleNorm.includes(intent.normalised) ? intent.normalised.length : 0;
  for (const alias of intent.aliases) {
    if (alias && titleNorm.includes(alias)) best = Math.max(best, alias.length);
  }
  if (!best) {
    const words = titleNorm.split(" ");
    if (!words.length) return 0;
    return words.filter((w) => intent.terms.includes(w)).length / words.length;
  }
  return Math.min(1, best / titleNorm.length);
}

function corroboration(job: JobEntry, intent: Intent, haystack: string): number {
  let s = 0;
  for (const skill of intent.skills) {
    if (skill && haystack.includes(skill)) s += W_SKILL_HIT;
  }
  const hayTokens = new Set(haystack.split(" "));
  for (const term of intent.terms) {
    if (hayTokens.has(term)) s += W_BODY_TERM;
  }
  const companyNorm = normalise(job.company);
  if (intent.terms.length && intent.terms.some((t) => companyNorm.includes(t))) {
    s += W_COMPANY;
  }
  return Math.min(s, MAX_CORROBORATION);
}

export function scoreJob(job: JobEntry, intent: Intent): number {
  const titleNorm = normalise(job.title);
  const haystack = haystackFor(job);

  const tier = titleTier(titleNorm, intent);
  const corr = corroboration(job, intent, haystack);

  let base: number;
  if (tier === 0) {
    if (corr <= W_BODY_TERM) return 0;
    base = T_BODY_ONLY + corr;
  } else {
    base = tier + corr + W_TITLE_FOCUS * titleFocus(titleNorm, intent);
  }

  if (intent.familyId && intent.rivals.some((r) => r && titleNorm.includes(r))) {
    // Unless it also matches us by name, e.g. "Data Analyst / Data Engineer".
    if (!intent.aliases.some((a) => a && titleNorm.includes(a))) base *= RIVAL_PENALTY;
  }

  if (isNoiseTitle(job.title)) base *= NOISE_PENALTY;

  return base * (0.7 + 0.3 * recencyBoost(job));
}

/** Annualised salary in a single currency, for sorting and the min-salary filter.
 *  Rough static rates — good enough to rank, not to quote. */
const TO_INR: Record<string, number> = { INR: 1, USD: 84, EUR: 91, GBP: 106, "": 1 };

export function salaryInINR(job: JobEntry): number | null {
  if (job.salary_max == null) return null;
  return job.salary_max * (TO_INR[job.salary_currency] ?? 1);
}

export function applyFilters(jobs: JobEntry[], f: Filters): JobEntry[] {
  const intent = understand(f.q);
  const locTerms = normalise(f.location).split(" ").filter(Boolean);

  const scored: { job: JobEntry; s: number }[] = [];

  for (const job of jobs) {
    if (f.remote && job.remote !== f.remote) continue;
    if (f.seniority && job.seniority !== f.seniority) continue;
    if (f.source && job.source !== f.source) continue;
    if (f.maxAgeDays && ageDays(job) > f.maxAgeDays) continue;
    if (f.minSalary) {
      const s = salaryInINR(job);
      if (s == null || s < f.minSalary) continue;
    }
    if (locTerms.length) {
      const loc = normalise(job.location);
      // A remote role satisfies any location the user typed.
      if (job.remote !== "remote" && !locTerms.some((t) => loc.includes(t))) continue;
    }

    const s = f.q ? scoreJob(job, intent) : 1;
    if (f.q && s <= 0) continue;
    scored.push({ job, s });
  }

  // Apply the relevance floor, relaxing it only if that would leave almost nothing.
  if (f.q) {
    const strong = scored.filter((x) => x.s >= RELEVANCE_FLOOR);
    const kept =
      strong.length >= MIN_RESULTS_BEFORE_RELAXING
        ? strong
        : scored.filter((x) => x.s >= RELAXED_FLOOR);
    scored.length = 0;
    scored.push(...kept);
  }

  if (f.sort === "newest") {
    scored.sort(
      (a, b) =>
        Date.parse(b.job.posted_at ?? b.job.first_seen_at) -
        Date.parse(a.job.posted_at ?? a.job.first_seen_at),
    );
  } else if (f.sort === "salary") {
    scored.sort((a, b) => (salaryInINR(b.job) ?? -1) - (salaryInINR(a.job) ?? -1));
  } else {
    // Ties broken by recency, so equally relevant jobs surface newest-first.
    scored.sort(
      (a, b) =>
        b.s - a.s ||
        Date.parse(b.job.posted_at ?? b.job.first_seen_at) -
          Date.parse(a.job.posted_at ?? a.job.first_seen_at),
    );
  }

  return scored.map((x) => x.job);
}

/** Which shard holds a job's full description. Mirrors SHARD_CHARS in store.py. */
export function shardFor(id: string): string {
  return id.slice(0, 2);
}
