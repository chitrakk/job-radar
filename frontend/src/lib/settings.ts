/**
 * Per-viewer settings, stored in localStorage only.
 *
 * API keys, your CV and your tracker credentials live in your browser and nowhere else.
 * There is no server to send them to — the site is static files on GitHub Pages — and each
 * artifact origin keeps its own storage, so nothing here reaches other viewers or us.
 *
 * Every read and write is wrapped: localStorage throws in private windows and when site
 * data is blocked, and the app has to keep working in that case rather than white-screening.
 */
import type { LLMKeys } from "./llm";

const KEY = "jobradar.settings.v1";

export interface Settings extends LLMKeys {
  cvText: string;
  candidateName: string;
  sheetsUrl: string;
  sheetsSecret: string;
}

export const EMPTY_SETTINGS: Settings = {
  geminiKey: "",
  groqKey: "",
  geminiModel: "",
  groqModel: "",
  cvText: "",
  candidateName: "",
  sheetsUrl: "",
  sheetsSecret: "",
};

export function loadSettings(): Settings {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return { ...EMPTY_SETTINGS };
    return { ...EMPTY_SETTINGS, ...(JSON.parse(raw) as Partial<Settings>) };
  } catch {
    // Private window, blocked site data, or corrupt JSON. Defaults keep the app usable.
    return { ...EMPTY_SETTINGS };
  }
}

export function saveSettings(settings: Settings): boolean {
  try {
    localStorage.setItem(KEY, JSON.stringify(settings));
    return true;
  } catch {
    return false;
  }
}

export function clearSettings(): void {
  try {
    localStorage.removeItem(KEY);
  } catch {
    /* nothing to do — the data was never stored */
  }
}
