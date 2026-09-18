/**
 * Google Sheets tracker client.
 *
 * Posts to the Apps Script web app the viewer deploys against their own Sheet (see
 * sheets/Code.gs). Each person's tracker is their own spreadsheet under their own Google
 * account — there is no shared database and no service-account key anywhere.
 *
 * Apps Script does not return CORS headers on its /exec endpoint, so a normal fetch is
 * blocked by the browser even when the write succeeds. We therefore post with
 * `mode: "no-cors"`, which sends the request but makes the response opaque: we can confirm
 * the request left, not what the script replied. That is an acceptable trade for a tracker
 * — the CLI path (`jobradar career track`) has no CORS restriction and does report errors.
 */
import type { JobEntry } from "../types";
import type { CVReport } from "./career";
import { formatSalary } from "./format";

export type TrackerKind = "applications" | "outreach" | "cv";

export interface TrackerConfig {
  url: string;
  secret: string;
}

export function configured(cfg: Partial<TrackerConfig> | undefined): cfg is TrackerConfig {
  return Boolean(cfg?.url && cfg?.secret);
}

export function applicationRow(job: JobEntry, status = "Saved", notes = ""): Record<string, string> {
  return {
    "Job ID": job.id,
    Title: job.title,
    Company: job.company,
    Location: job.location,
    Remote: job.remote,
    Salary: formatSalary(job) ?? "",
    Source: job.source,
    Posted: job.posted_at ? job.posted_at.slice(0, 10) : "",
    URL: job.url,
    Status: status,
    "Next Action": "",
    Notes: notes,
  };
}

export function outreachRow(
  job: JobEntry,
  draft: {
    contact_name: string;
    contact_role: string;
    linkedin_search_url: string;
    linkedin_note: string;
    email_subject: string;
    email_body: string;
  },
): Record<string, string> {
  return {
    "Job ID": job.id,
    Company: job.company,
    "Contact Name": draft.contact_name,
    "Contact Role": draft.contact_role,
    Email: "",
    "Email Confidence": "",
    LinkedIn: draft.linkedin_search_url,
    Channel: "",
    "Sent?": "No",
    "LinkedIn Note": draft.linkedin_note,
    "Email Subject": draft.email_subject,
    "Email Body": draft.email_body,
    "Follow Up On": "",
  };
}

export function cvRow(report: CVReport, targetRole = ""): Record<string, string> {
  const s = (id: string) => {
    const row = report.scores[id];
    return row ? `${row.score}/${row.max}` : "";
  };
  return {
    "Job ID": "",
    Score: String(report.total),
    Band: report.band.label,
    Impact: s("impact"),
    Keywords: s("keyword_alignment"),
    ATS: s("ats_parseability"),
    Clarity: s("clarity"),
    Structure: s("structure"),
    Evidence: s("evidence"),
    Progression: s("progression"),
    Language: s("language_quality"),
    "Target Role": targetRole,
    "Top Fixes": report.top_fixes
      .slice(0, 3)
      .map((f) => `${f.problem} → ${f.action}`)
      .join(" | "),
  };
}

export async function pushRows(
  kind: TrackerKind,
  rows: Record<string, string>[],
  cfg: TrackerConfig,
): Promise<void> {
  if (!rows.length) return;
  await fetch(cfg.url, {
    method: "POST",
    mode: "no-cors",
    // Apps Script reads e.postData.contents regardless of content type, and text/plain
    // avoids a CORS preflight that the /exec endpoint would not answer.
    headers: { "Content-Type": "text/plain;charset=utf-8" },
    body: JSON.stringify({ secret: cfg.secret, kind, rows }),
  });
}
