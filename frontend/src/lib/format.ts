import type { JobEntry } from "../types";

export function formatSalary(job: JobEntry): string | null {
  if (job.salary_min == null) return null;
  const cur = job.salary_currency || "";
  const fmt = (n: number) => {
    // Indian readers parse "18 LPA" far faster than "1,800,000".
    if (cur === "INR") {
      if (n >= 10_000_000) return `${(n / 10_000_000).toFixed(n % 10_000_000 ? 1 : 0)}Cr`;
      return `${(n / 100_000).toFixed(n % 100_000 ? 1 : 0)}L`;
    }
    if (n >= 1000) return `${Math.round(n / 1000)}k`;
    return `${Math.round(n)}`;
  };
  const symbol = cur === "INR" ? "₹" : cur === "USD" ? "$" : cur === "EUR" ? "€" : cur === "GBP" ? "£" : "";
  const lo = fmt(job.salary_min);
  const hi = job.salary_max != null ? fmt(job.salary_max) : null;
  const range = hi && hi !== lo ? `${lo}–${hi}` : lo;
  return `${symbol}${range}${cur === "INR" ? " PA" : "/yr"}`;
}

export function relativeDate(iso: string | null): string {
  if (!iso) return "";
  const ms = Date.now() - Date.parse(iso);
  if (Number.isNaN(ms)) return "";
  const days = Math.floor(ms / 86_400_000);
  if (days < 0) return "just now";
  if (days === 0) {
    const hours = Math.floor(ms / 3_600_000);
    return hours <= 1 ? "just now" : `${hours}h ago`;
  }
  if (days === 1) return "yesterday";
  if (days < 7) return `${days}d ago`;
  if (days < 30) return `${Math.floor(days / 7)}w ago`;
  return `${Math.floor(days / 30)}mo ago`;
}

const SOURCE_LABELS: Record<string, string> = {
  "ats:greenhouse": "Greenhouse",
  "ats:lever": "Lever",
  "ats:ashby": "Ashby",
  "ats:workable": "Workable",
  "ats:smartrecruiters": "SmartRecruiters",
  "ats:recruitee": "Recruitee",
  adzuna: "Adzuna",
  linkedin: "LinkedIn",
  remoteok: "RemoteOK",
  remotive: "Remotive",
  arbeitnow: "Arbeitnow",
  himalayas: "Himalayas",
  weworkremotely: "WeWorkRemotely",
  hn_hiring: "HN Who's Hiring",
  naukri: "Naukri",
  indeed_in: "Indeed",
  foundit: "Foundit",
  timesjobs: "TimesJobs",
  instahyre: "Instahyre",
  shine: "Shine",
  internshala: "Internshala",
  wellfound: "Wellfound",
  naukri_sitemap: "Naukri",
};

export function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? source;
}

export function seniorityLabel(s: string): string {
  return s === "unknown" ? "" : s.charAt(0).toUpperCase() + s.slice(1);
}

/**
 * Acronyms that Naukri's URL slugs lose the case of, since a slug is all lowercase and
 * word-capitalising it gives "Delhi Ncr" and "Evoke Hr". The backend now capitalises these
 * at parse time, but the published corpus is rebuilt only every four hours and holds
 * thousands of rows written before that — repairing on display fixes them now, and costs
 * nothing once the data is already right.
 *
 * Deliberately conservative: only tokens that are near-certainly acronyms in Indian job
 * data, and only applied to company and location, never to a job title where "It" or "Ai"
 * can be an ordinary word.
 */
const ACRONYMS = new Set([
  "ncr", "ey", "ibm", "tcs", "hcl", "kpmg", "pwc", "hdfc", "icici", "sbi", "hsbc",
  "hr", "it", "ites", "bpo", "kpo", "bfsi", "nbfc", "mnc", "sme", "msme",
  "ai", "ml", "sql", "aws", "gcp", "sap", "crm", "erp", "qa", "ui", "ux", "seo", "sem",
  "llp", "pvt", "ltd", "inc", "llc", "uae", "usa", "uk", "us", "gst", "kyc", "cfa",
  "mba", "bsc", "msc", "bca", "mca", "btech", "mtech", "ca", "cs", "cma",
]);

/** Repair slug-derived title case. Leaves already-correct text untouched. */
export function fixCase(s: string): string {
  if (!s) return s;
  return s.replace(/\b[A-Za-z]{1,6}\b/g, (w) => {
    const lower = w.toLowerCase();
    // Only rewrite words that look slug-derived — "Ncr", not "NCR" and not "ncr".
    if (!ACRONYMS.has(lower)) return w;
    if (w !== lower.charAt(0).toUpperCase() + lower.slice(1)) return w;
    // "Ltd"/"Pvt"/"Inc" read correctly in title case; they are here only so the
    // capitalisation check above does not treat them as unknown words.
    if (lower === "pvt" || lower === "ltd" || lower === "inc" || lower === "llc") return w;
    return lower.toUpperCase();
  });
}

/**
 * Tags fit for display. Two things go wrong upstream: dedupe unions the tag sets of two
 * merged postings, so a card can carry "Experience: 3-8 yrs" *and* "Experience: 5-10 yrs"
 * and state a contradiction; and "also:" markers are rendered separately.
 *
 * Keeping the first of each "Prefix:" family is the honest choice here — the surviving
 * record's own band — rather than showing both and making the reader pick.
 */
export function displayTags(tags: string[]): string[] {
  const seenPrefix = new Set<string>();
  const out: string[] = [];
  for (const t of tags) {
    if (t.startsWith("also:")) continue;
    const colon = t.indexOf(":");
    if (colon > 0) {
      const prefix = t.slice(0, colon).toLowerCase();
      if (seenPrefix.has(prefix)) continue;
      seenPrefix.add(prefix);
    }
    out.push(t);
  }
  return out;
}
