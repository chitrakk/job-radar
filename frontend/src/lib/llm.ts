/**
 * Browser LLM client — Gemini first, Groq as fallback.
 *
 * The key is the viewer's own, kept in localStorage and sent only to Google or Groq. It
 * never reaches this site's server, because there isn't one: the whole app is static files
 * on GitHub Pages. That is what makes the personal features (CV, outreach, interview prep)
 * safe to offer publicly — your CV is never uploaded anywhere we control.
 *
 * Mirrors backend/jobradar/llm.py. Same providers, same fallback order, same JSON contract.
 */

const GEMINI_URL =
  "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent";
const GROQ_URL = "https://api.groq.com/openai/v1/chat/completions";

export const DEFAULT_GEMINI_MODEL = "gemini-2.5-flash";
export const DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile";

export interface LLMKeys {
  geminiKey?: string;
  groqKey?: string;
  geminiModel?: string;
  groqModel?: string;
}

export class QuotaError extends Error {}
export class NoKeyError extends Error {}

function stripFence(text: string): string {
  const t = text.trim();
  if (!t.startsWith("```")) return t;
  return t.replace(/^```(?:json)?\s*/, "").replace(/\s*```$/, "").trim();
}

export function hasKey(keys: LLMKeys): boolean {
  return Boolean(keys.geminiKey || keys.groqKey);
}

async function callGemini(prompt: string, schema: object | undefined, keys: LLMKeys): Promise<string> {
  const config: Record<string, unknown> = {
    temperature: 0.1,
    responseMimeType: "application/json",
  };
  if (schema) config.responseSchema = schema;

  const resp = await fetch(
    GEMINI_URL.replace("{model}", keys.geminiModel || DEFAULT_GEMINI_MODEL),
    {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-goog-api-key": keys.geminiKey! },
      body: JSON.stringify({
        contents: [{ parts: [{ text: prompt }] }],
        generationConfig: config,
      }),
    },
  );

  if (resp.status === 429 || resp.status === 503) throw new QuotaError("Gemini rate limit");
  if (resp.status === 400 || resp.status === 403) {
    throw new Error("Gemini rejected the key. Check it at aistudio.google.com/apikey");
  }
  if (!resp.ok) throw new Error(`Gemini returned ${resp.status}`);

  const data = await resp.json();
  const parts = data?.candidates?.[0]?.content?.parts ?? [];
  const text = parts.map((p: { text?: string }) => p.text ?? "").join("");
  if (!text) throw new Error("Gemini returned an empty response");
  return text;
}

async function callGroq(prompt: string, keys: LLMKeys): Promise<string> {
  const resp = await fetch(GROQ_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${keys.groqKey}`,
    },
    body: JSON.stringify({
      model: keys.groqModel || DEFAULT_GROQ_MODEL,
      messages: [{ role: "user", content: prompt }],
      temperature: 0.1,
      response_format: { type: "json_object" },
    }),
  });

  if (resp.status === 429 || resp.status === 503) throw new QuotaError("Groq rate limit");
  if (resp.status === 401) throw new Error("Groq rejected the key. Check it at console.groq.com");
  if (!resp.ok) throw new Error(`Groq returned ${resp.status}`);

  const data = await resp.json();
  const text = data?.choices?.[0]?.message?.content;
  if (!text) throw new Error("Groq returned an empty response");
  return text;
}

/** Ask for JSON, falling through providers. Throws with a message worth showing a user. */
export async function completeJSON<T = unknown>(
  prompt: string,
  keys: LLMKeys,
  schema?: object,
): Promise<T> {
  if (!hasKey(keys)) {
    throw new NoKeyError("Add a Gemini or Groq API key in Settings. Both have free tiers.");
  }

  const errors: string[] = [];

  for (const provider of ["gemini", "groq"] as const) {
    if (provider === "gemini" && !keys.geminiKey) continue;
    if (provider === "groq" && !keys.groqKey) continue;

    try {
      const raw =
        provider === "gemini"
          ? await callGemini(prompt, schema, keys)
          : await callGroq(prompt, keys);
      return JSON.parse(stripFence(raw)) as T;
    } catch (err) {
      if (err instanceof QuotaError) {
        // Falling through to the other provider is the whole point of holding two keys.
        errors.push(`${provider}: quota exhausted`);
        continue;
      }
      if (err instanceof SyntaxError) {
        errors.push(`${provider}: returned unparseable JSON`);
        continue;
      }
      errors.push(`${provider}: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  throw new Error(errors.join(" · ") || "No provider produced a usable response.");
}
