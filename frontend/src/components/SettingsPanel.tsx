import { useState } from "react";
import type { Settings } from "../lib/settings";
import { clearSettings } from "../lib/settings";

const INPUT =
  "w-full rounded-lg border border-line bg-surface px-3 py-2 text-sm text-ink outline-none placeholder:text-muted focus:border-accent";
const LABEL = "block text-sm font-medium text-ink";
const HELP = "mt-1 text-xs text-muted";

export function SettingsPanel({
  settings,
  onChange,
  onClose,
}: {
  settings: Settings;
  onChange: (patch: Partial<Settings>) => void;
  onClose: () => void;
}) {
  const [showKeys, setShowKeys] = useState(false);

  return (
    <div className="space-y-5">
      <div className="rounded-xl border border-line bg-surface p-4">
        <h3 className="font-semibold text-ink">Where your data goes</h3>
        <p className="mt-1 text-sm leading-relaxed text-muted">
          Everything on this page stays in your browser. Your CV, your API keys and your
          tracker credentials are saved to this browser's local storage and are never
          uploaded — this site is static files with no server behind it. API keys are sent
          only to Google or Groq when you click a button; your CV is included in those
          requests as prompt text, so it reaches your chosen model provider and nobody else.
        </p>
      </div>

      <section className="space-y-3">
        <div>
          <h3 className="font-semibold text-ink">AI key</h3>
          <p className={HELP}>
            Needed for CV scoring, outreach drafts and interview prep. Both providers have
            free tiers and neither needs a credit card. Add both if you want a fallback when
            one hits its daily quota.
          </p>
        </div>

        <div>
          <label className={LABEL} htmlFor="gemini">
            Gemini API key
          </label>
          <input
            id="gemini"
            type={showKeys ? "text" : "password"}
            value={settings.geminiKey ?? ""}
            onChange={(e) => onChange({ geminiKey: e.target.value.trim() })}
            placeholder="AIza…"
            className={INPUT}
            autoComplete="off"
            spellCheck={false}
          />
          <p className={HELP}>
            Free from{" "}
            <a
              href="https://aistudio.google.com/apikey"
              target="_blank"
              rel="noopener noreferrer"
              className="text-accent underline"
            >
              aistudio.google.com/apikey
            </a>
            . Tried first — it handles long CVs better.
          </p>
        </div>

        <div>
          <label className={LABEL} htmlFor="groq">
            Groq API key <span className="font-normal text-muted">(optional fallback)</span>
          </label>
          <input
            id="groq"
            type={showKeys ? "text" : "password"}
            value={settings.groqKey ?? ""}
            onChange={(e) => onChange({ groqKey: e.target.value.trim() })}
            placeholder="gsk_…"
            className={INPUT}
            autoComplete="off"
            spellCheck={false}
          />
          <p className={HELP}>
            Free from{" "}
            <a
              href="https://console.groq.com/keys"
              target="_blank"
              rel="noopener noreferrer"
              className="text-accent underline"
            >
              console.groq.com/keys
            </a>
            . Used automatically if Gemini returns a quota error.
          </p>
        </div>

        <label className="flex items-center gap-2 text-sm text-muted">
          <input type="checkbox" checked={showKeys} onChange={(e) => setShowKeys(e.target.checked)} />
          Show keys
        </label>
      </section>

      <section className="space-y-3">
        <div>
          <h3 className="font-semibold text-ink">You</h3>
          <p className={HELP}>Used to sign off drafted emails.</p>
        </div>
        <input
          type="text"
          value={settings.candidateName}
          onChange={(e) => onChange({ candidateName: e.target.value })}
          placeholder="Your name"
          className={INPUT}
        />
      </section>

      <section className="space-y-3">
        <div>
          <h3 className="font-semibold text-ink">Google Sheets tracker</h3>
          <p className={HELP}>
            Optional. Saves applications, outreach drafts and CV scores to a spreadsheet you
            own. Deploy <code className="rounded bg-slate-500/10 px-1">sheets/Code.gs</code>{" "}
            from the repo as an Apps Script web app, then paste its URL and your secret here.
          </p>
        </div>
        <input
          type="url"
          value={settings.sheetsUrl}
          onChange={(e) => onChange({ sheetsUrl: e.target.value.trim() })}
          placeholder="https://script.google.com/macros/s/…/exec"
          className={INPUT}
          spellCheck={false}
        />
        <input
          type={showKeys ? "text" : "password"}
          value={settings.sheetsSecret}
          onChange={(e) => onChange({ sheetsSecret: e.target.value })}
          placeholder="Shared secret from Code.gs"
          className={INPUT}
          autoComplete="off"
        />
      </section>

      <div className="flex flex-wrap gap-2 border-t border-line pt-4">
        <button
          onClick={onClose}
          className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white hover:opacity-90"
        >
          Done
        </button>
        <button
          onClick={() => {
            if (confirm("Erase your CV, keys and tracker settings from this browser?")) {
              clearSettings();
              location.reload();
            }
          }}
          className="rounded-lg border border-line px-4 py-2 text-sm text-muted hover:text-ink"
        >
          Erase everything
        </button>
      </div>
    </div>
  );
}
