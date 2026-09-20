/**
 * Turning whatever index.json contains into records the UI can trust.
 *
 * This exists because stress testing broke the site four different ways with one bad
 * record: a null title, a number where a string belonged, a missing key, and a payload
 * that was an object rather than an array each produced a blank white page —
 * `Cannot read properties of null (reading 'toLowerCase')` and friends.
 *
 * That is not a hypothetical. 87% of the corpus now comes from HTML scrapers pointed at
 * sites that redesign without warning. A parser that starts returning nulls should cost
 * you those rows, not the whole site, so every field is coerced to its declared type and
 * a record too broken to show is dropped rather than rendered.
 */
import type { JobEntry } from "../types";

const REMOTE_KINDS = ["onsite", "hybrid", "remote", "unknown"] as const;
const SENIORITIES = ["intern", "entry", "mid", "senior", "lead", "exec", "unknown"] as const;

/** Longest string we will render in a card. A scraper that swallows a whole page
 *  shouldn't be able to freeze the browser laying out one line of text. */
const MAX_TEXT = 4000;

function str(value: unknown, max = MAX_TEXT): string {
  if (typeof value === "string") return value.slice(0, max);
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "";
}

function strArray(value: unknown, limit = 30): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((v) => str(v, 120)).filter(Boolean).slice(0, limit);
}

function num(value: unknown): number | null {
  const n = typeof value === "number" ? value : typeof value === "string" ? Number(value) : NaN;
  return Number.isFinite(n) && n > 0 ? n : null;
}

function oneOf<T extends readonly string[]>(value: unknown, allowed: T, fallback: T[number]) {
  const s = str(value, 20);
  return (allowed as readonly string[]).includes(s) ? (s as T[number]) : fallback;
}

function isoDate(value: unknown): string | null {
  const s = str(value, 40);
  return s && !Number.isNaN(Date.parse(s)) ? s : null;
}

/** A link we are willing to put behind an Apply button. `javascript:` is not one. */
function safeUrl(value: unknown): string {
  const s = str(value, 2000).trim();
  return /^https?:\/\//i.test(s) ? s : "";
}

export function normaliseJob(raw: unknown): JobEntry | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const r = raw as Record<string, unknown>;

  const title = str(r.title, 300).trim();
  const url = safeUrl(r.url);
  // Without a title or a working link there is nothing to show and nowhere to send
  // anyone, so the row is worthless rather than merely imperfect.
  if (!title || !url) return null;

  const firstSeen = isoDate(r.first_seen_at) ?? new Date(0).toISOString();
  return {
    id: str(r.id, 64) || url,
    title,
    company: str(r.company, 200).trim() || "Unknown",
    location: str(r.location, 200).trim(),
    remote: oneOf(r.remote, REMOTE_KINDS, "unknown"),
    url,
    source: str(r.source, 40) || "unknown",
    tier: oneOf(r.tier, ["tier1", "tier2"] as const, "tier1"),
    posted_at: isoDate(r.posted_at),
    first_seen_at: firstSeen,
    salary_min: num(r.salary_min),
    salary_max: num(r.salary_max),
    salary_currency: str(r.salary_currency, 8),
    salary_text: str(r.salary_text, 160),
    skills: strArray(r.skills),
    seniority: oneOf(r.seniority, SENIORITIES, "unknown"),
    summary: str(r.summary, 2000),
    tags: strArray(r.tags),
  };
}

export interface ParsedCorpus {
  jobs: JobEntry[];
  /** Records dropped as unusable, so the UI can admit the index is damaged. */
  dropped: number;
}

export function parseCorpus(raw: unknown): ParsedCorpus {
  if (!Array.isArray(raw)) return { jobs: [], dropped: 0 };
  const jobs: JobEntry[] = [];
  let dropped = 0;
  for (const row of raw) {
    const job = normaliseJob(row);
    if (job) jobs.push(job);
    else dropped++;
  }
  return { jobs, dropped };
}
