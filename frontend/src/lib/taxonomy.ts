/**
 * Query understanding in the browser.
 *
 * Mirrors backend/jobradar/taxonomy.py and reads the same shared/role_taxonomy.json, so a
 * search typed into the box resolves to the same role family the pipeline used when it
 * built the corpus. tests/test_relevance.py asserts the two stay in step.
 */
import taxonomyJson from "@shared/role_taxonomy.json";

interface Family {
  id: string;
  label: string;
  canonical: string[];
  aliases: string[];
  skills: string[];
  confused_with: string[];
}

const taxonomy = taxonomyJson as unknown as {
  families: Family[];
  noise_titles: string[];
};

const FAMILIES = new Map(taxonomy.families.map((f) => [f.id, f]));
const NOISE = new Set(taxonomy.noise_titles.map((t) => t.toLowerCase()));

// Abbreviation expansions. The word boundary must come before the optional dot, or "Sr."
// normalises to "senior." and stops matching anything.
const EXPANSIONS: [RegExp, string][] = [
  [/\bsr\b\.?/g, "senior"],
  [/\bjr\b\.?/g, "junior"],
  [/\bmgr\b\.?/g, "manager"],
  [/\beng\b\.?/g, "engineer"],
  [/\bdev\b\.?/g, "developer"],
  [/\bml\b/g, "machine learning"],
  [/\bai\b/g, "artificial intelligence"],
  [/\bbi\b/g, "business intelligence"],
  [/\bds\b/g, "data science"],
  [/\bswe\b/g, "software engineer"],
  [/\bsde\b/g, "software engineer"],
  [/\bpm\b/g, "product manager"],
];

export function normalise(text: string): string {
  let t = ` ${text.toLowerCase()} `;
  t = t.replace(/[^a-z0-9+#/&.\- ]+/g, " ");
  for (const [re, repl] of EXPANSIONS) t = t.replace(re, repl);
  return t.replace(/\s+/g, " ").trim();
}

export interface Intent {
  raw: string;
  normalised: string;
  terms: string[];
  familyId: string;
  aliases: string[];
  skills: string[];
  rivals: string[];
}

function allPhrases(f: Family): string[] {
  return [...f.canonical, ...f.aliases].map(normalise);
}

export function detectFamily(queryNorm: string): string {
  // Longest phrase wins, so "machine learning engineer" beats "machine learning".
  let bestId = "";
  let bestLen = 0;
  for (const f of taxonomy.families) {
    for (const phrase of allPhrases(f)) {
      if (phrase && queryNorm.includes(phrase) && phrase.length > bestLen) {
        bestId = f.id;
        bestLen = phrase.length;
      }
    }
  }
  return bestId;
}

export function understand(query: string): Intent {
  const norm = normalise(query);
  const intent: Intent = {
    raw: query,
    normalised: norm,
    terms: norm.split(" ").filter((t) => t.length > 1),
    familyId: "",
    aliases: [],
    skills: [],
    rivals: [],
  };

  const fid = detectFamily(norm);
  if (!fid) return intent;

  const fam = FAMILIES.get(fid)!;
  intent.familyId = fid;
  intent.aliases = allPhrases(fam);
  intent.skills = fam.skills.map(normalise);

  for (const siblingId of fam.confused_with) {
    const sibling = FAMILIES.get(siblingId);
    if (!sibling) continue;
    for (const phrase of sibling.canonical.map(normalise)) {
      // A phrase that is also one of our own aliases is not evidence against us.
      if (phrase && !intent.aliases.includes(phrase)) intent.rivals.push(phrase);
    }
  }
  return intent;
}

export function isNoiseTitle(title: string): boolean {
  const t = normalise(title);
  return !t || t.length < 3 || NOISE.has(t);
}
