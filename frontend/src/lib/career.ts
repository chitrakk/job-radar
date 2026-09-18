/**
 * CV scoring, outreach drafting and interview prep, in the browser.
 *
 * Mirrors backend/jobradar/career/. Prompts and the rubric come from the same shared JSON
 * the Python side reads, so the score you get here matches the one you get from the CLI.
 *
 * Note what is missing compared to the CLI: company-site scanning for contact discovery.
 * A browser cannot fetch an arbitrary company's /team page — CORS blocks it, and no amount
 * of client-side code gets around that. So the web app infers contacts from the posting
 * text it already has and hands you a LinkedIn people-search link; run `jobradar career
 * outreach` locally if you want the full domain scan and email-pattern inference.
 */
import rubricJson from "@shared/cv_rubric.json";
import promptsJson from "@shared/prompts.json";
import { completeJSON, type LLMKeys } from "./llm";
import { findSlop, languageScore, type SlopHit } from "./slop";
import type { JobEntry } from "../types";

// --------------------------------------------------------------------------- rubric

interface Criterion {
  id: string;
  label: string;
  weight: number;
  what_good_looks_like: string;
  common_failures: string[];
  scoring_anchors: Record<string, string>;
}

interface Band {
  min: number;
  label: string;
  meaning: string;
}

export const rubric = rubricJson as unknown as {
  total: number;
  criteria: Criterion[];
  bands: Band[];
};

const prompts = promptsJson as unknown as Record<string, Record<string, string>> & {
  shared_rules: { grounding: string; voice: string };
};

/** The one criterion graded by regex rather than by the model. */
const DETERMINISTIC = "language_quality";

export function bandFor(total: number): Band {
  return rubric.bands.find((b) => total >= b.min) ?? rubric.bands[rubric.bands.length - 1];
}

/** Fill {braced} placeholders, always supplying the shared grounding and voice rules. */
function build(section: string, fields: Record<string, string>, key = "template"): string {
  const template = prompts[section][key];
  const all: Record<string, string> = {
    grounding: prompts.shared_rules.grounding,
    voice: prompts.shared_rules.voice,
    ...fields,
  };
  return template.replace(/\{(\w+)\}/g, (match, name: string) =>
    name in all ? all[name] : match,
  );
}

function criteriaBrief(): string {
  return rubric.criteria
    .filter((c) => c.id !== DETERMINISTIC)
    .map((c) => {
      const anchors = Object.entries(c.scoring_anchors)
        .map(([k, v]) => `      ${k}: ${v}`)
        .join("\n");
      const failures = c.common_failures.map((f) => `      - ${f}`).join("\n");
      return `  ${c.id} (max ${c.weight}) — ${c.label}\n    Good: ${c.what_good_looks_like}\n    Common failures:\n${failures}\n    Score anchors:\n${anchors}`;
    })
    .join("\n\n");
}

// --------------------------------------------------------------------------- CV scoring

export interface CriterionScore {
  score: number;
  max: number;
  reason: string;
}

export interface CVReport {
  total: number;
  band: Band;
  scores: Record<string, CriterionScore>;
  top_fixes: { criterion?: string; problem: string; action: string; points_available?: number }[];
  bullet_rewrites: { original: string; rewritten: string; why: string }[];
  missing_keywords: string[];
  slop: SlopHit[];
  summary: string;
}

export async function scoreCV(
  cvText: string,
  keys: LLMKeys,
  jobDescription = "",
): Promise<CVReport> {
  const text = cvText.trim();
  if (!text) throw new Error("Paste your CV first.");

  const langWeight = rubric.criteria.find((c) => c.id === DETERMINISTIC)!.weight;
  const { score: lang, hits } = languageScore(text, "cv", langWeight);

  const jdBlock = jobDescription
    ? build("cv_score", { jd: jobDescription.slice(0, 6000) }, "jd_block")
    : "";

  const prompt = build("cv_score", {
    system: prompts.cv_score.system,
    criteria: criteriaBrief(),
    jd_block: jdBlock,
    cv: text.slice(0, 18000),
  });

  const payload = await completeJSON<{
    scores: Record<string, { score: number; reason: string }>;
    top_fixes?: CVReport["top_fixes"];
    bullet_rewrites?: CVReport["bullet_rewrites"];
    missing_keywords?: string[];
    summary?: string;
  }>(prompt, keys);

  // Clamp every model score to its criterion's declared maximum. Without this, a model
  // returning 20 for a criterion worth 8 silently produces a total over 100.
  const scores: Record<string, CriterionScore> = {};
  let total = 0;
  for (const c of rubric.criteria) {
    if (c.id === DETERMINISTIC) {
      scores[c.id] = {
        score: lang,
        max: c.weight,
        reason: `${hits.length} language issues found by pattern check.`,
      };
      total += lang;
      continue;
    }
    const raw = Number(payload.scores?.[c.id]?.score ?? 0);
    const clamped = Math.max(0, Math.min(Number.isFinite(raw) ? raw : 0, c.weight));
    scores[c.id] = {
      score: Math.round(clamped * 10) / 10,
      max: c.weight,
      reason: String(payload.scores?.[c.id]?.reason ?? "").slice(0, 300),
    };
    total += clamped;
  }

  total = Math.round(total * 10) / 10;
  return {
    total,
    band: bandFor(total),
    scores,
    top_fixes: (payload.top_fixes ?? []).slice(0, 5),
    bullet_rewrites: (payload.bullet_rewrites ?? []).slice(0, 5),
    missing_keywords: (payload.missing_keywords ?? []).slice(0, 12),
    slop: hits,
    summary: String(payload.summary ?? "").slice(0, 600),
  };
}

// --------------------------------------------------------------------------- outreach

export interface OutreachDraft {
  linkedin_note: string;
  email_subject: string;
  email_body: string;
  linkedin_search_url: string;
  contact_name: string;
  contact_role: string;
  slop: { linkedin_note: SlopHit[]; email_body: SlopHit[]; clean: boolean };
  warnings: string[];
}

export function linkedInPeopleSearch(company: string): string {
  const q = `${company} (recruiter OR "talent acquisition" OR "hiring manager")`;
  return `https://www.linkedin.com/search/results/people/?keywords=${encodeURIComponent(q)}`;
}

export async function draftOutreach(
  job: JobEntry,
  description: string,
  cvText: string,
  keys: LLMKeys,
  candidateName = "",
): Promise<OutreachDraft> {
  if (!cvText.trim()) {
    throw new Error("Add your CV in the CV tab first — drafts are grounded in it.");
  }

  // Only a name the posting itself published; the browser cannot scan the company site.
  const named = extractContactName(description);
  const contactBlock = named
    ? build("outreach", { name: named, role: "hiring contact" }, "contact_named")
    : prompts.outreach.contact_unknown;

  const prompt = build("outreach", {
    contact_block: contactBlock,
    title: job.title,
    company: job.company,
    location: job.location ?? "",
    jd: (description || "(no description published)").slice(0, 3500),
    candidate: cvText.slice(0, 6000),
    candidate_name: candidateName || "[your name]",
  });

  const payload = await completeJSON<{
    linkedin_note?: string;
    email_subject?: string;
    email_body?: string;
  }>(prompt, keys);

  const note = String(payload.linkedin_note ?? "").trim();
  const body = String(payload.email_body ?? "").trim();
  const noteHits = findSlop(note, "linkedin_note");
  const bodyHits = findSlop(body, "email");

  const warnings: string[] = [];
  if (note.length > 300) {
    warnings.push(
      `The note is ${note.length} characters; LinkedIn truncates connection notes at 300.`,
    );
  }
  warnings.push(
    "Nothing is sent. Review, edit and send it yourself — automating LinkedIn outreach risks a permanent account restriction.",
  );

  return {
    linkedin_note: note,
    email_subject: String(payload.email_subject ?? "").trim(),
    email_body: body,
    linkedin_search_url: linkedInPeopleSearch(job.company),
    contact_name: named,
    contact_role: named ? "named in the posting" : "",
    slop: {
      linkedin_note: noteHits,
      email_body: bodyHits,
      clean: noteHits.length === 0 && bodyHits.length === 0,
    },
    warnings,
  };
}

/** Pull a contact name out of the posting when it publishes one. Conservative by design —
 *  a wrong name in a cold email is worse than no name. */
function extractContactName(description: string): string {
  if (!description) return "";
  const patterns = [
    /(?:contact|reach out to|email|write to|questions to)\s+([A-Z][a-z]+\s+[A-Z][a-z]+)/,
    /([A-Z][a-z]+\s+[A-Z][a-z]+)\s*(?:,|\s-\s)?\s*(?:our|the)?\s*(?:recruiter|talent|hiring manager)/i,
  ];
  for (const re of patterns) {
    const m = description.match(re);
    if (m?.[1] && !/\b(hiring|talent|team|manager|resources)\b/i.test(m[1])) return m[1];
  }
  return "";
}

// --------------------------------------------------------------------------- interview

export interface InterviewPack {
  role_read?: string;
  likely_questions?: {
    question: string;
    why_asked: string;
    kind: string;
    answer_skeleton: string;
  }[];
  gaps_to_defend?: { gap: string; how_it_will_be_probed: string; how_to_handle: string }[];
  technical_topics?: string[];
  questions_to_ask_them?: string[];
  first_90_days_pitch?: string;
}

export async function interviewPrep(
  job: JobEntry,
  description: string,
  cvText: string,
  keys: LLMKeys,
): Promise<InterviewPack> {
  const prompt = build("interview", {
    title: job.title,
    company: job.company,
    location: job.location ?? "",
    jd: (description || "(no description published — prepare from the title)").slice(0, 5000),
    cv: cvText.trim() ? cvText.slice(0, 10000) : prompts.interview.no_cv,
  });
  return completeJSON<InterviewPack>(prompt, keys);
}
