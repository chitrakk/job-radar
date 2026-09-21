/**
 * How many postings sit behind each option in the city and role pickers.
 *
 * A dropdown of thirty cities with no numbers makes you tick one, look, untick, tick the
 * next. The counts turn that into one glance — and they make an empty corner of the corpus
 * visible ("Raipur 0") instead of something you discover by trying it.
 *
 * These count what each option *names*, not everything ticking it would return: ticking
 * Gurugram also surfaces Delhi-metro and worldwide-remote postings, which is a bonus on
 * top of the number, never less than it. Counting the looser thing would be useless —
 * every posting whose location is just "India" would land in all thirty cities at once.
 */
import type { JobEntry } from "../types";
import { citiesIn } from "./geo";
import { intentForFamily, normalise, roleOptions } from "./taxonomy";

/** Postings whose location names each canonical city. */
export function cityCounts(jobs: JobEntry[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const job of jobs) {
    for (const city of citiesIn(job.location)) {
      out[city] = (out[city] ?? 0) + 1;
    }
  }
  return out;
}

/** Postings whose *title* names each role family. */
export function roleCounts(jobs: JobEntry[]): Record<string, number> {
  // Phrase lists are built once, not per job: nine families times ten-odd aliases across
  // three thousand postings is the difference between instant and visibly slow.
  const families = roleOptions()
    .map((o) => ({ id: o.value, phrases: intentForFamily(o.value)?.aliases ?? [] }))
    .filter((f) => f.phrases.length > 0);

  const out: Record<string, number> = {};
  for (const f of families) out[f.id] = 0;

  for (const job of jobs) {
    const title = normalise(job.title);
    if (!title) continue;
    for (const f of families) {
      if (f.phrases.some((p) => p && title.includes(p))) out[f.id] += 1;
    }
  }
  return out;
}
