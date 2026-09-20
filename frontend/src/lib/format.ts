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
