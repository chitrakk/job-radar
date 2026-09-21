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
 *   4. every query term present in the job's own text (skills, tags, description)
 *   5. some query terms in the title
 *   6. partial body evidence only
 *
 * A title naming a sibling role ("Data Engineer" for a "data analyst" query) is then
 * demoted, which is the thing plain term matching cannot do, as is a title at the wrong
 * experience level for what the query asked for.
 */
import type { Filters, JobEntry } from "../types";
import { bestLocality, type Locality } from "./geo";
import {
  intentForFamily,
  isNoiseTitle,
  normalise,
  seniorityDistance,
  understand,
  type Intent,
} from "./taxonomy";

const T_EXACT_PHRASE = 1.0;
const T_ALIAS = 0.82;
const T_ALL_TERMS = 0.7;
/**
 * Every query term is in the job's own text, just not in its title.
 *
 * Without a tier here, a one-word skill search could not match anything: "tableau" has no
 * role family, so `intent.skills` is empty, and a single body hit scored exactly
 * `W_BODY_TERM`, which the `corr <= W_BODY_TERM` guard below then rejected as noise.
 * Measured on the live corpus, "tableau", "statistics", "excel" and "pandas" each returned
 * zero results and "sql" returned one — for a corpus where those are the most common
 * skills in the index.
 */
const T_BODY_ALL_TERMS = 0.44;
const T_PARTIAL = 0.34;
const T_BODY_ONLY = 0.16;

const W_SKILL_HIT = 0.03;
const W_BODY_TERM = 0.02;
const W_COMPANY = 0.015;
const MAX_CORROBORATION = 0.18;
const W_TITLE_FOCUS = 0.12;

const RIVAL_PENALTY = 0.35;
const NOISE_PENALTY = 0.15;

/** Per rung of the seniority ladder between what was asked for and what is offered. */
const SENIORITY_STEP_PENALTY = 0.2;
const MAX_SENIORITY_PENALTY = 0.62;

/**
 * How much a job's location matters once it has passed the location filter.
 *
 * A worldwide-remote job technically satisfies "Delhi", but somebody who typed Delhi wants
 * Delhi. Ranking remote pass-throughs below real local matches is the difference between
 * the first NCR result being "Data Scientist at EXL, Gurugram" and "Video Editor,
 * Anywhere in the World".
 */
const LOCALITY_WEIGHT: Record<Locality, number> = {
  exact: 1.25,
  metro: 1.18,
  region: 1.0,
  remote: 0.78,
  "": 0,
};

/**
 * Minimum score to show for a keyword search.
 *
 * Set just above T_PARTIAL, so a result must have real evidence in its *title* or match
 * every term in its body, rather than a passing mention.
 */
const RELEVANCE_FLOOR = 0.35;

/**
 * If the floor leaves too few results, relax it rather than showing an empty page — a
 * narrow or unusual query should degrade to "here are the near misses", not to nothing.
 *
 * Both numbers below are load-bearing and were wrong. RELAXED_FLOOR used to be 0.15, which
 * is *below* T_BODY_ONLY (0.16) — so relaxing admitted every job with any body evidence
 * whatsoever, and there was no cap. The effect was that adding a filter made results worse
 * and more numerous: "data scientist" gave 27 results, and "data scientist" + Delhi gave
 * 111, led by a Video Editor role. A fallback has to stay a fallback.
 */
const MIN_RESULTS_BEFORE_RELAXING = 5;
const RELAXED_FLOOR = 0.22;
const MAX_RELAXED_RESULTS = 12;

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

/** Multiplier for offering a different experience level than the query asked for. */
function seniorityFit(job: JobEntry, intent: Intent): number {
  if (!intent.seniority) return 1;
  const distance = seniorityDistance(intent.seniority, job.seniority);
  if (!distance) return 1;
  return 1 - Math.min(MAX_SENIORITY_PENALTY, distance * SENIORITY_STEP_PENALTY);
}

export function scoreJob(job: JobEntry, intent: Intent): number {
  const titleNorm = normalise(job.title);
  const haystack = haystackFor(job);

  const tier = titleTier(titleNorm, intent);
  const corr = corroboration(job, intent, haystack);

  let base: number;
  if (tier === 0) {
    const hayTokens = new Set(haystack.split(" "));
    const allInBody =
      intent.terms.length > 0 && intent.terms.every((t) => hayTokens.has(t));
    if (allInBody) {
      base = T_BODY_ALL_TERMS + corr;
    } else if (corr > W_BODY_TERM) {
      base = T_BODY_ONLY + corr;
    } else {
      return 0;
    }
  } else {
    base = tier + corr + W_TITLE_FOCUS * titleFocus(titleNorm, intent);
  }

  if (intent.familyId && intent.rivals.some((r) => r && titleNorm.includes(r))) {
    // Unless it also matches us by name, e.g. "Data Analyst / Data Engineer".
    if (!intent.aliases.some((a) => a && titleNorm.includes(a))) base *= RIVAL_PENALTY;
  }

  if (isNoiseTitle(job.title)) base *= NOISE_PENALTY;

  base *= seniorityFit(job, intent);

  return base * (0.7 + 0.3 * recencyBoost(job));
}

/** Annualised salary in a single currency, for sorting and the min-salary filter.
 *  Rough static rates — good enough to rank, not to quote. */
const TO_INR: Record<string, number> = { INR: 1, USD: 84, EUR: 91, GBP: 106, "": 1 };

export function salaryInINR(job: JobEntry): number | null {
  if (job.salary_max == null) return null;
  return job.salary_max * (TO_INR[job.salary_currency] ?? 1);
}

export interface SearchResult {
  jobs: JobEntry[];
  /** True when the relevance floor had to be lowered to avoid an empty page. */
  relaxed: boolean;
  /** How many results cleared the normal floor. */
  strongCount: number;
}

/**
 * Does every keyword appear somewhere in the posting?
 *
 * Used only when roles are picked from the list, where the keyword box narrows the chosen
 * roles instead of ranking against them. Substring rather than token equality on purpose:
 * this is a filter, not a score, so "python" should still find "Python3" and the role
 * selection has already done the precision work.
 */
function keywordHit(job: JobEntry, terms: string[], haystack: string): boolean {
  if (!terms.length) return true;
  const hay = `${haystack} ${normalise(job.company)} ${normalise(job.location)}`;
  return terms.every((t) => hay.includes(t));
}

export function applyFilters(jobs: JobEntry[], f: Filters): SearchResult {
  const roleIntents = f.roles
    .map(intentForFamily)
    .filter((i): i is Intent => i !== null);
  const usingRoles = roleIntents.length > 0;

  const qIntent = understand(f.q);
  const hasQuery = Boolean(f.q.trim());

  /**
   * When roles come from the dropdown they *are* the search, and a keyword typed next to
   * them narrows the result rather than competing to re-rank it. Letting both score would
   * mean two intents fighting: "Data Scientist" + "python" put a Python tutoring vacancy
   * above every data science role, because the tutor title matched the typed word exactly
   * and the picked family only by alias.
   */
  const narrowTerms = usingRoles && hasQuery ? qIntent.terms : [];
  const scoring = usingRoles || hasQuery;

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

    let localityWeight = 1;
    if (f.cities.length) {
      // `summary` stands in for the description, which the index does not carry; it is
      // only consulted for the negative "US only" test, so a missing one is harmless.
      const where = bestLocality(job.location, f.cities, {
        isRemote: job.remote === "remote",
        description: job.summary,
        includeNearby: f.includeNearby,
      });
      if (!where) continue;
      localityWeight = LOCALITY_WEIGHT[where];
    }

    let s: number;
    if (usingRoles) {
      if (narrowTerms.length && !keywordHit(job, narrowTerms, haystackFor(job))) continue;
      // Best of the chosen roles: ticking two families asks for either, so a job should
      // be ranked by the one it actually is, not penalised for not being the other.
      s = 0;
      for (const intent of roleIntents) s = Math.max(s, scoreJob(job, intent));
      if (s <= 0) continue;
    } else if (hasQuery) {
      s = scoreJob(job, qIntent);
      if (s <= 0) continue;
    } else {
      s = 1;
    }

    scored.push({ job, s: s * localityWeight });
  }

  scored.sort((a, b) => b.s - a.s);

  // Apply the relevance floor, relaxing it only if that would leave almost nothing — and
  // then only to a handful of near misses, clearly flagged, rather than to everything.
  let relaxed = false;
  let kept = scored;
  let strongCount = scored.length;

  if (scoring) {
    const strong = scored.filter((x) => x.s >= RELEVANCE_FLOOR);
    strongCount = strong.length;
    kept = strong;
    if (strong.length < MIN_RESULTS_BEFORE_RELAXING) {
      const near = scored.filter((x) => x.s >= RELAXED_FLOOR).slice(0, MAX_RELAXED_RESULTS);
      if (near.length > strong.length) {
        kept = near;
        relaxed = true;
      }
    }
  }

  const rows = [...kept];
  if (f.sort === "newest") {
    rows.sort(
      (a, b) =>
        Date.parse(b.job.posted_at ?? b.job.first_seen_at) -
        Date.parse(a.job.posted_at ?? a.job.first_seen_at),
    );
  } else if (f.sort === "salary") {
    rows.sort((a, b) => (salaryInINR(b.job) ?? -1) - (salaryInINR(a.job) ?? -1));
  } else {
    // Ties broken by recency, so equally relevant jobs surface newest-first.
    rows.sort(
      (a, b) =>
        b.s - a.s ||
        Date.parse(b.job.posted_at ?? b.job.first_seen_at) -
          Date.parse(a.job.posted_at ?? a.job.first_seen_at),
    );
  }

  return { jobs: rows.map((x) => x.job), relaxed, strongCount };
}

/** Which shard holds a job's full description. Mirrors SHARD_CHARS in store.py. */
export function shardFor(id: string): string {
  return id.slice(0, 2);
}
