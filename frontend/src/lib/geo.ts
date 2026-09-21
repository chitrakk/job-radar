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

const STATE_ALIASES = new Map<string, string>();
for (const [state, aliases] of Object.entries((vocab.states ?? {}) as Record<string, string[]>)) {
  for (const alias of aliases) STATE_ALIASES.set(alias, state);
}
const CITY_STATE = (vocab.city_state ?? {}) as Record<string, string>;

/** Every Indian state named in a location string. */
export function statesIn(location: string): Set<string> {
  const low = location.toLowerCase();
  const out = new Set<string>();
  for (const [alias, state] of STATE_ALIASES) if (low.includes(alias)) out.add(state);
  return out;
}

/**
 * The states a city query can legitimately be satisfied by — including the rest of its
 * metro area, since Delhi NCR spans three of them and a posting saying only "Haryana"
 * may well be in Gurugram.
 */
export function statesFor(city: string): Set<string> {
  const metro = METRO_OF.get(city);
  const members = metro ? (METRO_AREAS[metro] ?? [city]) : [city];
  const out = new Set<string>();
  for (const c of members) if (CITY_STATE[c]) out.add(CITY_STATE[c]);
  return out;
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
    if (jobCities.size === 0) {
      // A posting naming a *state* and no city is not "somewhere in India": Maharashtra
      // is not Delhi. 104 of the 125 state-only postings in the live corpus said
      // "Maharashtra", and answered searches for every city alike.
      const jobStates = statesIn(jobLocation);
      if (jobStates.size) {
        const allowed = statesFor(city);
        for (const s of jobStates) if (allowed.has(s)) return "region";
        return remoteOk;
      }
      if (isIndia(jobLocation)) return "region";
    }
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

/* ------------------------------------------------------------------ *
 * Picking cities from a list rather than typing one.
 * ------------------------------------------------------------------ */

/** Cities whose canonical name is not what most people call them. The dropdown has to
 *  show both, or somebody looking for Gurgaon scrolls past "Gurugram" without seeing it. */
const CITY_LABELS: Record<string, string> = {
  bengaluru: "Bengaluru (Bangalore)",
  delhi: "Delhi / New Delhi",
  gurugram: "Gurugram (Gurgaon)",
  noida: "Noida / Greater Noida",
  mumbai: "Mumbai (Bombay)",
  chennai: "Chennai (Madras)",
  kolkata: "Kolkata (Calcutta)",
  kochi: "Kochi (Cochin)",
  mysuru: "Mysuru (Mysore)",
  vadodara: "Vadodara (Baroda)",
  visakhapatnam: "Visakhapatnam (Vizag)",
  thiruvananthapuram: "Thiruvananthapuram",
  chandigarh: "Chandigarh / Mohali",
  hyderabad: "Hyderabad / Secunderabad",
  ahmedabad: "Ahmedabad / Gandhinagar",
};

/** The seven cities that carry most of the corpus, after NCR. */
const METRO_ORDER = [
  "bengaluru",
  "mumbai",
  "hyderabad",
  "pune",
  "chennai",
  "kolkata",
  "ahmedabad",
];

export interface CityOption {
  value: string;
  label: string;
  group: string;
}

function titleCase(key: string): string {
  return key.charAt(0).toUpperCase() + key.slice(1);
}

/**
 * The city list for the picker, NCR first.
 *
 * Ordering is not cosmetic here: this is a Delhi-NCR job search, and an alphabetical list
 * puts Ahmedabad and Bhopal above the five cities that actually matter to it.
 */
export function cityOptions(): CityOption[] {
  const ncr = METRO_AREAS.delhi ?? [];
  const seen = new Set<string>();
  const out: CityOption[] = [];

  const push = (key: string, group: string) => {
    if (seen.has(key) || !CITIES[key]) return;
    seen.add(key);
    out.push({ value: key, label: CITY_LABELS[key] ?? titleCase(key), group });
  };

  for (const c of ncr) push(c, "Delhi NCR");
  for (const c of METRO_ORDER) push(c, "Other metros");
  for (const c of Object.keys(CITIES).sort()) push(c, "Elsewhere in India");
  return out;
}

/** Highest-to-lowest, so the best of several selected cities is the one that ranks. */
const LOCALITY_RANK: Record<Locality, number> = {
  exact: 4,
  metro: 3,
  region: 2,
  remote: 1,
  "": 0,
};

/**
 * How well a posting satisfies a *set* of chosen cities — the best match among them.
 *
 * Deliberately not `cities.some(c => locality(job, c))`, for two reasons.
 *
 * The cheap one: that recomputes `citiesIn` for every selected city on every job, and it
 * throws away the distinction between an exact hit and a remote pass-through, which is
 * what keeps real Noida jobs above "Anywhere in the World" ones.
 *
 * The load-bearing one: `locality` treats a posting that names no city we know, and no
 * state, as a "region" match for *whatever city you asked for*, on the grounds that it is
 * at least in India. That is tolerable for a typed box and wrong for a picker. Measured on
 * the live corpus, picking Noida returned 1,027 postings and picking Gurugram returned the
 * same 1,027 — because 268 of them matched on nothing more than the word "India", among
 * them "Nanakramguda, India", which is in Hyderabad. So here a bare country-level match
 * does not satisfy a named city; only a city, its metro, or a state that actually contains
 * it does.
 *
 * `locality` itself is left alone on purpose: it is mirrored by geo.py and pinned by
 * test_shared_parity.py, and the backend ranks with it.
 */
export function bestLocality(
  jobLocation: string,
  cities: string[],
  opts: { isRemote?: boolean; description?: string; includeNearby?: boolean } = {},
): Locality {
  if (!cities.length) return "exact";

  const { isRemote = false, description = "", includeNearby = true } = opts;
  const remoteOk: Locality =
    isRemote && isAnywhereRemote(jobLocation, description) ? "remote" : "";

  // Computed once for the posting, then tested against each chosen city.
  const jobCities = citiesIn(jobLocation);
  const jobStates = jobCities.size ? null : statesIn(jobLocation);

  let best: Locality = "";
  for (const city of cities) {
    let here: Locality = "";

    if (jobCities.has(city)) {
      here = "exact";
    } else if (includeNearby) {
      for (const c of jobCities) {
        if (sameMetro(c, city)) {
          here = "metro";
          break;
        }
      }
    }

    if (!here && jobCities.size === 0 && jobStates && jobStates.size) {
      // A posting naming only a state answers a city in that state, nothing wider.
      const allowed = statesFor(city);
      for (const s of jobStates) {
        if (allowed.has(s)) {
          here = "region";
          break;
        }
      }
    }

    if (!here) here = remoteOk;
    if (LOCALITY_RANK[here] > LOCALITY_RANK[best]) best = here;
    if (best === "exact") break;
  }
  return best;
}
