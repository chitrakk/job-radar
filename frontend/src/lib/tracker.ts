/**
 * What you have already done about a job, kept in this browser.
 *
 * A job search runs for weeks. Without this the site forgets everything the moment you
 * close the tab, so every visit means re-reading the same postings and trying to remember
 * which ones you already applied to. That is the difference between a job board and a
 * job search, and it was the missing half.
 *
 * Stored in localStorage only, like settings: there is no server, and each viewer's marks
 * are their own. Every read and write is wrapped, because localStorage throws in private
 * windows and when site data is blocked — losing your marks is bad, white-screening the
 * site because of them is worse.
 */

const KEY = "jobradar.tracker.v1";

/** Marks are exclusive: applying to a job supersedes having saved it. */
export type Mark = "saved" | "applied" | "hidden";

export interface Tracked {
  mark: Mark;
  /** ISO date the mark was set, so "applied 3 weeks ago, no reply" is answerable later. */
  at: string;
}

export type Tracker = Record<string, Tracked>;

export function loadTracker(): Tracker {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    // Trust nothing on the way in: a half-written or hand-edited entry costs us that row,
    // not the whole history.
    const out: Tracker = {};
    for (const [id, v] of Object.entries(parsed as Record<string, unknown>)) {
      if (!v || typeof v !== "object") continue;
      const mark = (v as Tracked).mark;
      if (mark !== "saved" && mark !== "applied" && mark !== "hidden") continue;
      const at = typeof (v as Tracked).at === "string" ? (v as Tracked).at : "";
      out[id] = { mark, at };
    }
    return out;
  } catch {
    return {};
  }
}

export function saveTracker(t: Tracker): boolean {
  try {
    localStorage.setItem(KEY, JSON.stringify(t));
    return true;
  } catch {
    return false;
  }
}

/** Set a mark, or clear it when the same mark is applied twice (the button is a toggle). */
export function toggleMark(t: Tracker, id: string, mark: Mark): Tracker {
  const next = { ...t };
  if (next[id]?.mark === mark) delete next[id];
  else next[id] = { mark, at: new Date().toISOString() };
  return next;
}

export function countsByMark(t: Tracker): Record<Mark, number> {
  const c: Record<Mark, number> = { saved: 0, applied: 0, hidden: 0 };
  for (const v of Object.values(t)) c[v.mark] += 1;
  return c;
}
