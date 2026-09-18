import { useState } from "react";
import { rubric, scoreCV, type CVReport } from "../lib/career";
import { languageScore } from "../lib/slop";
import { configured, cvRow, pushRows } from "../lib/sheets";
import type { Settings } from "../lib/settings";

const SEVERITY_STYLE: Record<string, string> = {
  high: "text-rose-600 dark:text-rose-400",
  medium: "text-amber-600 dark:text-amber-400",
  low: "text-muted",
};

function ScoreRing({ total }: { total: number }) {
  const pct = Math.max(0, Math.min(100, total));
  // Colour by band rather than a gradient, so the number and the colour agree.
  const stroke = pct >= 85 ? "#10b981" : pct >= 70 ? "#3b82f6" : pct >= 55 ? "#f59e0b" : "#ef4444";
  const r = 52;
  const circumference = 2 * Math.PI * r;

  return (
    <svg viewBox="0 0 120 120" className="h-28 w-28 shrink-0" role="img" aria-label={`Score ${total} out of 100`}>
      <circle cx="60" cy="60" r={r} fill="none" stroke="currentColor" strokeWidth="10" className="text-line" />
      <circle
        cx="60"
        cy="60"
        r={r}
        fill="none"
        stroke={stroke}
        strokeWidth="10"
        strokeLinecap="round"
        strokeDasharray={circumference}
        strokeDashoffset={circumference * (1 - pct / 100)}
        transform="rotate(-90 60 60)"
      />
      <text x="60" y="58" textAnchor="middle" className="fill-ink" fontSize="28" fontWeight="700">
        {Math.round(total)}
      </text>
      <text x="60" y="76" textAnchor="middle" className="fill-muted" fontSize="12">
        / 100
      </text>
    </svg>
  );
}

export function CVPanel({
  settings,
  onChange,
  jobDescription,
  targetRole,
}: {
  settings: Settings;
  onChange: (patch: Partial<Settings>) => void;
  jobDescription?: string;
  targetRole?: string;
}) {
  const [report, setReport] = useState<CVReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");

  // The regex check needs no key and no network, so it can run as you type.
  const live = settings.cvText.trim() ? languageScore(settings.cvText, "cv") : null;

  async function run() {
    setBusy(true);
    setError("");
    setSaved("");
    try {
      const result = await scoreCV(settings.cvText, settings, jobDescription ?? "");
      setReport(result);

      if (configured(settings.sheetsUrl && settings.sheetsSecret ? { url: settings.sheetsUrl, secret: settings.sheetsSecret } : undefined)) {
        await pushRows("cv", [cvRow(result, targetRole ?? "")], {
          url: settings.sheetsUrl,
          secret: settings.sheetsSecret,
        });
        setSaved("Logged to your tracker.");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-5">
      <section>
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h3 className="font-semibold text-ink">Your CV</h3>
          <span className="text-xs text-muted">
            Stays in this browser — never uploaded to this site
          </span>
        </div>
        <textarea
          value={settings.cvText}
          onChange={(e) => onChange({ cvText: e.target.value })}
          placeholder={
            "Paste your CV as plain text.\n\nFrom a PDF: open it, select all, copy. Keep the bullet characters — the checker uses them to find your bullets."
          }
          rows={12}
          className="mt-2 w-full rounded-lg border border-line bg-surface px-3 py-2 font-mono text-xs leading-relaxed text-ink outline-none placeholder:text-muted focus:border-accent"
          spellCheck={false}
        />
        {live && (
          <p className="mt-1 text-xs text-muted">
            Live language check (no AI needed): {live.score}/10 ·{" "}
            {live.hits.length === 0
              ? "no slop patterns found"
              : `${live.hits.length} issues to fix`}
          </p>
        )}
      </section>

      <div className="flex flex-wrap items-center gap-3">
        <button
          onClick={run}
          disabled={busy || !settings.cvText.trim()}
          className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white hover:opacity-90 disabled:opacity-40"
        >
          {busy ? "Scoring…" : jobDescription ? "Score against this job" : "Score my CV"}
        </button>
        {targetRole && <span className="text-sm text-muted">Target: {targetRole}</span>}
      </div>

      {error && (
        <div className="rounded-lg border border-rose-500/30 bg-rose-500/5 p-3 text-sm text-ink">
          {error}
        </div>
      )}
      {saved && <p className="text-sm text-emerald-600 dark:text-emerald-400">{saved}</p>}

      {live && live.hits.length > 0 && !report && (
        <section className="rounded-xl border border-line bg-surface p-4">
          <h4 className="font-semibold text-ink">Fix these first — no AI required</h4>
          <ul className="mt-2 space-y-2">
            {live.hits.slice(0, 8).map((h, i) => (
              <li key={`${h.id}-${i}`} className="text-sm">
                <span className={`font-medium ${SEVERITY_STYLE[h.severity]}`}>{h.label}</span>
                <span className="text-muted"> — “{h.excerpt}”</span>
                <p className="text-xs text-muted">→ {h.fix}</p>
              </li>
            ))}
          </ul>
        </section>
      )}

      {report && (
        <div className="space-y-5">
          <section className="flex flex-wrap items-center gap-5 rounded-xl border border-line bg-surface p-4">
            <ScoreRing total={report.total} />
            <div className="min-w-48 flex-1">
              <p className="text-lg font-semibold text-ink">{report.band.label}</p>
              <p className="mt-1 text-sm text-muted">{report.band.meaning}</p>
              {report.summary && <p className="mt-2 text-sm text-ink/80">{report.summary}</p>}
            </div>
          </section>

          <section className="rounded-xl border border-line bg-surface p-4">
            <h4 className="font-semibold text-ink">Breakdown</h4>
            <div className="mt-3 space-y-3">
              {rubric.criteria.map((c) => {
                const row = report.scores[c.id];
                if (!row) return null;
                const pct = row.max ? (row.score / row.max) * 100 : 0;
                return (
                  <div key={c.id}>
                    <div className="flex items-baseline justify-between gap-2 text-sm">
                      <span className="text-ink">{c.label}</span>
                      <span className="shrink-0 font-medium text-muted">
                        {row.score}/{row.max}
                      </span>
                    </div>
                    <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-line">
                      <div
                        className="h-full rounded-full bg-accent"
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                    {row.reason && <p className="mt-1 text-xs text-muted">{row.reason}</p>}
                  </div>
                );
              })}
            </div>
          </section>

          {report.top_fixes.length > 0 && (
            <section className="rounded-xl border border-line bg-surface p-4">
              <h4 className="font-semibold text-ink">Highest-leverage fixes</h4>
              <ol className="mt-2 space-y-3">
                {report.top_fixes.map((f, i) => (
                  <li key={i} className="text-sm">
                    <p className="font-medium text-ink">
                      {f.problem}
                      {f.points_available ? (
                        <span className="ml-2 rounded bg-emerald-500/10 px-1.5 py-0.5 text-xs font-semibold text-emerald-700 dark:text-emerald-400">
                          +{f.points_available} pts
                        </span>
                      ) : null}
                    </p>
                    <p className="mt-0.5 text-muted">{f.action}</p>
                  </li>
                ))}
              </ol>
            </section>
          )}

          {report.bullet_rewrites.length > 0 && (
            <section className="rounded-xl border border-line bg-surface p-4">
              <h4 className="font-semibold text-ink">Suggested rewrites</h4>
              <p className="mt-1 text-xs text-muted">
                Placeholders like [X%] are deliberate — fill in your real numbers rather than
                letting a model guess them.
              </p>
              <div className="mt-3 space-y-4">
                {report.bullet_rewrites.map((rw, i) => (
                  <div key={i} className="text-sm">
                    <p className="text-muted line-through decoration-rose-500/40">{rw.original}</p>
                    <p className="mt-1 text-ink">{rw.rewritten}</p>
                    <p className="mt-0.5 text-xs text-muted">{rw.why}</p>
                    <button
                      onClick={() => navigator.clipboard?.writeText(rw.rewritten)}
                      className="mt-1 text-xs text-accent hover:underline"
                    >
                      Copy
                    </button>
                  </div>
                ))}
              </div>
            </section>
          )}

          {report.missing_keywords.length > 0 && (
            <section className="rounded-xl border border-line bg-surface p-4">
              <h4 className="font-semibold text-ink">Keywords this role expects</h4>
              <p className="mt-1 text-xs text-muted">
                Add only the ones you genuinely have. The rest are real gaps, not wording fixes.
              </p>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {report.missing_keywords.map((k) => (
                  <span key={k} className="rounded border border-line px-2 py-0.5 text-xs text-ink">
                    {k}
                  </span>
                ))}
              </div>
            </section>
          )}
        </div>
      )}
    </div>
  );
}
