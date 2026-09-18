/**
 * AI-slop detection in the browser.
 *
 * Reads the same shared/slop_patterns.json the Python checker uses, so a draft judged clean
 * here is judged clean at the terminal too. Pure regex — no model call, no cost, no latency,
 * and no chance of the checker itself hallucinating a verdict.
 */
import patternsJson from "@shared/slop_patterns.json";

export interface SlopHit {
  id: string;
  label: string;
  severity: "high" | "medium" | "low";
  fix: string;
  excerpt: string;
  start: number;
}

export type SlopKind = "cv" | "linkedin_note" | "email";

interface PatternRule {
  id: string;
  label: string;
  regex: string;
  severity: string;
  fix: string;
}

interface StructuralRule {
  id: string;
  label: string;
  applies_to: string;
  fix: string;
  threshold_chars?: number;
  threshold_words?: number;
  threshold_count?: number;
}

const SEVERITY_COST: Record<string, number> = { high: 3, medium: 1.5, low: 0.5 };

const rules = patternsJson as unknown as {
  patterns: PatternRule[];
  structural_checks: StructuralRule[];
};

// Compiled once. The `g` flag is required for matchAll, and each pattern gets a fresh
// lastIndex per call because we rebuild the iterator rather than reusing state.
const compiled = rules.patterns.map((rule) => ({
  rule,
  re: new RegExp(rule.regex, "gim"),
}));

function bullets(text: string): { line: string; offset: number }[] {
  const out: { line: string; offset: number }[] = [];
  let offset = 0;
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (/^[•\-*–]\s/.test(line)) {
      out.push({ line: line.replace(/^[•\-*–]\s*/, ""), offset });
    }
    offset += raw.length + 1;
  }
  return out;
}

function structural(text: string, kind: SlopKind): SlopHit[] {
  const hits: SlopHit[] = [];
  for (const check of rules.structural_checks) {
    if (check.applies_to !== kind) continue;

    if (check.id === "note_too_long" && check.threshold_chars && text.length > check.threshold_chars) {
      hits.push({
        id: check.id,
        label: check.label,
        severity: "high",
        fix: check.fix,
        excerpt: `${text.length} characters (limit ${check.threshold_chars})`,
        start: 0,
      });
    } else if (check.id === "email_too_long" && check.threshold_words) {
      const words = text.split(/\s+/).filter(Boolean).length;
      if (words > check.threshold_words) {
        hits.push({
          id: check.id,
          label: check.label,
          severity: "medium",
          fix: check.fix,
          excerpt: `${words} words (aim for under ${check.threshold_words})`,
          start: 0,
        });
      }
    } else if (check.id === "bullet_too_long" && check.threshold_chars) {
      for (const b of bullets(text)) {
        if (b.line.length > check.threshold_chars) {
          hits.push({
            id: check.id,
            label: check.label,
            severity: "medium",
            fix: check.fix,
            excerpt: b.line.slice(0, 90),
            start: b.offset,
          });
        }
      }
    } else if (check.id === "repeated_opening_verb" && check.threshold_count) {
      const verbs = new Map<string, number>();
      for (const b of bullets(text)) {
        const first = b.line.match(/[A-Za-z']+/)?.[0]?.toLowerCase();
        if (first) verbs.set(first, (verbs.get(first) ?? 0) + 1);
      }
      for (const [verb, count] of verbs) {
        if (count >= check.threshold_count) {
          hits.push({
            id: check.id,
            label: check.label,
            severity: "low",
            fix: check.fix,
            excerpt: `"${verb}" opens ${count} bullets`,
            start: 0,
          });
        }
      }
    }
  }
  return hits;
}

export function findSlop(text: string, kind: SlopKind = "cv"): SlopHit[] {
  if (!text?.trim()) return [];

  const hits: SlopHit[] = [];
  for (const { rule, re } of compiled) {
    re.lastIndex = 0;
    for (const m of text.matchAll(re)) {
      hits.push({
        id: rule.id,
        label: rule.label,
        severity: (rule.severity as SlopHit["severity"]) ?? "medium",
        fix: rule.fix,
        excerpt: m[0].replace(/\s+/g, " ").slice(0, 90),
        start: m.index ?? 0,
      });
    }
  }
  hits.push(...structural(text, kind));
  hits.sort((a, b) => a.start - b.start);
  return hits;
}

export function slopPenalty(hits: SlopHit[], cap = 10): number {
  const total = hits.reduce((sum, h) => sum + (SEVERITY_COST[h.severity] ?? 1), 0);
  return Math.min(total, cap);
}

export function languageScore(text: string, kind: SlopKind = "cv", weight = 10) {
  const hits = findSlop(text, kind);
  return {
    score: Math.round(Math.max(0, weight - slopPenalty(hits, weight)) * 10) / 10,
    hits,
  };
}
