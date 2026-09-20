/**
 * Location matching in the browser.
 *
 * Mirrors backend/jobradar/geo.py and reads the same shared/locations.json.
 *
 * This file exists because it didn't. The filter used to be a substring test:
 *
 *     if (job.remote !== "remote" && !locTerms.some((t) => loc.includes(t))) continue;
 *
 * which had two consequences, both measured on the live corpus. Typing "Gurgaon" matched
 * none of the 41 jobs whose location field reads "Gurugram", and "Bombay" matched none of
 * the 34 in Mumbai — the alias map existed, but only in Python. And because every remote
 * job short-circuited the test, searching "Delhi" returned 111 jobs of which 9 were
 * anywhere near Delhi; the rest were worldwide-remote roles that match whatever anyone
 * types.
 */
import vocab from "@shared/locations.json";

const CITIES = vocab.cities as Record<string, string[]>;
const METRO_AREAS = (vocab.metro_areas ?? {}) as Record<string, string[]>;
const INDIA_TERMS = new Set(vocab.india_terms as string[]);
const ANYWHERE = new RegExp(vocab.anywhere_pattern as string, "i");
const REGION_LOCK = new RegExp(vocab.region_lock_pattern as string, "i");

const ALIAS_TO_CITY = new Map<string, string>();
for (const [city, aliases] of Object.entries(CITIES)) {
  for (const alias of aliases) ALIAS_TO_CITY.set(alias, city);
}

const METRO_OF = new Map<string, string>();
for (const [metro, members] of Object.entries(METRO_AREAS)) {
  for (const city of members) METRO_OF.set(city, metro);
}

function tokens(text: string): Set<string> {
  return new Set(text.toLowerCase().match(/[a-z]+/g) ?? []);
}

export function canonicalCity(location: string): string | null {
  const low = location.toLowerCase();
  const toks = tokens(low);
  let best: string | null = null;
  let bestLen = 0;
  for (const [alias, city] of ALIAS_TO_CITY) {
    // Multi-word aliases need a substring test; single words use token equality so "in"
    // inside "Indiana" does not match. Longest alias wins: "greater noida" before "noida".
    const hit = alias.includes(" ") ? low.includes(alias) : toks.has(alias);
    if (hit && alias.length > bestLen) {
      best = city;
      bestLen = alias.length;
    }
  }
  return best;
}

/**
 * Every canonical city a location names. Indian boards list one opening in several cities
 * ("Bangalore, Chennai, Noida +5 more"), and picking just one of them could make a posting
 * that genuinely hires in Noida fail a Delhi search.
 */
export function citiesIn(location: string): Set<string> {
  const low = location.toLowerCase();
  const toks = tokens(low);
  const found = new Set<string>();
  for (const [alias, city] of ALIAS_TO_CITY) {
    if (alias.includes(" ") ? low.includes(alias) : toks.has(alias)) found.add(city);
  }
  return found;
}

/** Two cities a commuter would treat as one market — Gurugram and Delhi, say. */
export function sameMetro(a: string | null, b: string | null): boolean {
  if (!a || !b) return false;
  if (a === b) return true;
  const ma = METRO_OF.get(a);
  return Boolean(ma && ma === METRO_OF.get(b));
}

export function isIndia(location: string): boolean {
  if (!location) return false;
  if (canonicalCity(location)) return true;
  for (const t of tokens(location)) if (INDIA_TERMS.has(t)) return true;
  return false;
}

export function isAnywhereRemote(location: string, description = ""): boolean {
  if (REGION_LOCK.test(`${location} ${description.slice(0, 800)}`)) return false;
  if (ANYWHERE.test(location)) return true;
  const norm = location.trim().toLowerCase();
  if (!norm) return ANYWHERE.test(description.slice(0, 800));
  return ["remote", "remote worldwide", "fully remote", "remote - global"].includes(norm);
}

export type Locality = "exact" | "metro" | "region" | "remote" | "";

/**
 * How a posting satisfies a location query.
 *
 * Ranking needs the distinction rather than a boolean: "a remote job you could do from
 * Delhi" and "a job in Delhi" are both matches, and only one of them is what somebody
 * typing "Delhi" was looking for.
 */
export function locality(
  jobLocation: string,
  queryLocation: string,
  opts: { isRemote?: boolean; description?: string } = {},
): Locality {
  if (!queryLocation.trim()) return "exact";

  const { isRemote = false, description = "" } = opts;
  const q = queryLocation.trim().toLowerCase();
  const remoteOk: Locality = isRemote && isAnywhereRemote(jobLocation, description) ? "remote" : "";

  if (q === "india" || INDIA_TERMS.has(q)) {
    return isIndia(jobLocation) ? "region" : remoteOk;
  }

  const city = canonicalCity(q);
  if (city) {
    const jobCities = citiesIn(jobLocation);
    if (jobCities.has(city)) return "exact";
    for (const c of jobCities) if (sameMetro(c, city)) return "metro";
    if (isIndia(jobLocation) && jobCities.size === 0) return "region";
    return remoteOk;
  }

  // Somewhere we have no vocabulary for: plain token overlap.
  const jobToks = tokens(jobLocation);
  for (const t of tokens(q)) if (jobToks.has(t)) return "exact";
  return remoteOk;
}

export function matchesLocation(
  jobLocation: string,
  queryLocation: string,
  opts: { isRemote?: boolean; description?: string } = {},
): boolean {
  return locality(jobLocation, queryLocation, opts) !== "";
}
