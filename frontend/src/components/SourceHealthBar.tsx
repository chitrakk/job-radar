import { useState } from "react";
import type { Meta, SourceHealth } from "../types";
import { relativeDate } from "../lib/format";
import { sourceLabel } from "../lib/format";

/**
 * Shows which sources worked on the last run.
 *
 * This exists so a degraded corpus is visible rather than silent. If Naukri is blocked or
 * the Adzuna key expired, the list quietly gets shorter and nothing else would tell you —
 * you would just assume there were fewer jobs this week.
 */
export function SourceHealthBar({ health, meta }: { health: SourceHealth[]; meta: Meta | null }) {
  const [open, setOpen] = useState(false);
  if (!health.length) return null;

  const failed = health.filter((h) => !h.ok);
  const ok = health.filter((h) => h.ok);

  return (
    <div className="border-b border-line bg-surface/50 text-sm">
      <div className="mx-auto max-w-5xl px-4 py-2">
        <button
          onClick={() => setOpen(!open)}
          className="flex w-full items-center gap-2 text-left text-muted hover:text-ink"
          aria-expanded={open}
        >
          <span
            className={`inline-block h-2 w-2 shrink-0 rounded-full ${
              failed.length === 0 ? "bg-emerald-500" : failed.length < 3 ? "bg-amber-500" : "bg-rose-500"
            }`}
            aria-hidden
          />
          <span>
            {ok.length}/{health.length} sources healthy
            {failed.length > 0 && ` · ${failed.length} degraded`}
          </span>
          {meta && (
            <span className="ml-auto hidden sm:inline">
              updated {relativeDate(meta.updated_at)} · {meta.total_jobs.toLocaleString("en-IN")} jobs
            </span>
          )}
          <span className="ml-2 text-xs opacity-60">{open ? "▲" : "▼"}</span>
        </button>

        {open && (
          <div className="mt-2 grid gap-1 pb-2 sm:grid-cols-2">
            {[...health]
              .sort((a, b) => Number(a.ok) - Number(b.ok) || b.jobs_found - a.jobs_found)
              .map((h) => (
                <div key={h.source} className="flex items-center gap-2 text-xs">
                  <span
                    className={`inline-block h-1.5 w-1.5 shrink-0 rounded-full ${
                      h.ok ? "bg-emerald-500" : "bg-rose-500"
                    }`}
                    aria-hidden
                  />
                  <span className="text-ink/80">{sourceLabel(h.source)}</span>
                  {h.tier === "tier2" && (
                    <span className="rounded bg-slate-500/10 px-1 text-[10px] text-muted">scraped</span>
                  )}
                  <span className="ml-auto text-muted">
                    {h.ok ? `${h.jobs_found} found` : h.error.slice(0, 48) || "failed"}
                  </span>
                </div>
              ))}
          </div>
        )}
      </div>
    </div>
  );
}
