import { useState } from "react";
import {
  draftOutreach,
  interviewPrep,
  type InterviewPack,
  type OutreachDraft,
} from "../lib/career";
import { applicationRow, configured, outreachRow, pushRows } from "../lib/sheets";
import type { Settings } from "../lib/settings";
import type { JobEntry } from "../types";
import type { SlopHit } from "../lib/slop";

const BTN =
  "rounded-lg border border-line px-3 py-1.5 text-sm font-medium text-muted hover:text-ink disabled:opacity-40";

function CopyBox({ label, text, limit }: { label: string; text: string; limit?: number }) {
  const [copied, setCopied] = useState(false);
  const over = limit != null && text.length > limit;

  return (
    <div>
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-muted">{label}</span>
        <span className="flex items-center gap-2 text-xs">
          {limit != null && (
            <span className={over ? "font-semibold text-rose-500" : "text-muted"}>
              {text.length}/{limit}
            </span>
          )}
          <button
            onClick={() => {
              navigator.clipboard?.writeText(text).then(() => {
                setCopied(true);
                setTimeout(() => setCopied(false), 1500);
              });
            }}
            className="text-accent hover:underline"
          >
            {copied ? "Copied" : "Copy"}
          </button>
        </span>
      </div>
      <p className="mt-1 whitespace-pre-line rounded-lg border border-line bg-canvas p-3 text-sm leading-relaxed text-ink">
        {text}
      </p>
    </div>
  );
}

function SlopList({ hits }: { hits: SlopHit[] }) {
  if (!hits.length) return null;
  return (
    <ul className="mt-2 space-y-1">
      {hits.map((h, i) => (
        <li key={`${h.id}-${i}`} className="text-xs text-muted">
          <span className="font-medium text-amber-600 dark:text-amber-400">{h.label}</span> —{" "}
          {h.fix}
        </li>
      ))}
    </ul>
  );
}

export function JobActions({
  job,
  description,
  settings,
}: {
  job: JobEntry;
  description: string;
  settings: Settings;
}) {
  const [tab, setTab] = useState<"" | "outreach" | "interview">("");
  const [draft, setDraft] = useState<OutreachDraft | null>(null);
  const [pack, setPack] = useState<InterviewPack | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [tracked, setTracked] = useState("");

  const tracker =
    settings.sheetsUrl && settings.sheetsSecret
      ? { url: settings.sheetsUrl, secret: settings.sheetsSecret }
      : undefined;

  async function run(kind: "outreach" | "interview") {
    setTab(kind);
    setError("");
    if (kind === "outreach" && draft) return;
    if (kind === "interview" && pack) return;

    setBusy(true);
    try {
      if (kind === "outreach") {
        const d = await draftOutreach(job, description, settings.cvText, settings, settings.candidateName);
        setDraft(d);
        if (configured(tracker)) {
          await pushRows("outreach", [outreachRow(job, d)], tracker);
          await pushRows("applications", [applicationRow(job, "Outreach drafted")], tracker);
          setTracked("Saved to your tracker.");
        }
      } else {
        setPack(await interviewPrep(job, description, settings.cvText, settings));
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function track() {
    if (!configured(tracker)) {
      setError("Add your Google Sheets tracker URL and secret in Settings first.");
      return;
    }
    setError("");
    try {
      await pushRows("applications", [applicationRow(job, "Saved")], tracker);
      setTracked("Saved to your tracker.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <div className="mt-3 border-t border-line pt-3">
      <div className="flex flex-wrap items-center gap-2">
        <button onClick={() => run("outreach")} className={BTN} disabled={busy}>
          {busy && tab === "outreach" ? "Drafting…" : "Draft outreach"}
        </button>
        <button onClick={() => run("interview")} className={BTN} disabled={busy}>
          {busy && tab === "interview" ? "Preparing…" : "Interview prep"}
        </button>
        <button onClick={track} className={BTN}>
          Save to tracker
        </button>
        {tracked && <span className="text-xs text-emerald-600 dark:text-emerald-400">{tracked}</span>}
      </div>

      {error && (
        <p className="mt-2 rounded-lg border border-rose-500/30 bg-rose-500/5 p-2 text-sm text-ink">
          {error}
        </p>
      )}

      {tab === "outreach" && draft && (
        <div className="mt-3 space-y-3">
          <div className="rounded-lg border border-cyan-500/30 bg-cyan-500/5 p-3 text-sm text-ink">
            <strong>Nothing has been sent.</strong> Review and send these yourself. Automating
            LinkedIn connection requests breaches their terms and gets accounts permanently
            restricted — your network is not worth that risk.
          </div>

          <div className="text-sm">
            <span className="text-muted">Who to contact: </span>
            {draft.contact_name ? (
              <span className="text-ink">{draft.contact_name} (named in the posting)</span>
            ) : (
              <span className="text-muted">
                Not named in the posting —{" "}
                <a
                  href={draft.linkedin_search_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-accent underline"
                >
                  search LinkedIn for a recruiter at {job.company}
                </a>
              </span>
            )}
          </div>

          {draft.linkedin_note && (
            <CopyBox label="LinkedIn connection note" text={draft.linkedin_note} limit={300} />
          )}
          {draft.email_subject && <CopyBox label="Email subject" text={draft.email_subject} />}
          {draft.email_body && <CopyBox label="Email" text={draft.email_body} />}

          {!draft.slop.clean && (
            <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3">
              <p className="text-sm font-medium text-ink">
                Slop check flagged this draft — edit before sending
              </p>
              <SlopList hits={[...draft.slop.linkedin_note, ...draft.slop.email_body]} />
            </div>
          )}
          {draft.slop.clean && (
            <p className="text-xs text-emerald-600 dark:text-emerald-400">
              Slop check clean — no AI-tell phrases found.
            </p>
          )}
        </div>
      )}

      {tab === "interview" && pack && (
        <div className="mt-3 space-y-4 text-sm">
          {pack.role_read && (
            <div>
              <h5 className="font-semibold text-ink">What they actually need</h5>
              <p className="mt-1 text-muted">{pack.role_read}</p>
            </div>
          )}

          {pack.likely_questions?.length ? (
            <div>
              <h5 className="font-semibold text-ink">Likely questions</h5>
              <ol className="mt-2 space-y-3">
                {pack.likely_questions.map((q, i) => (
                  <li key={i}>
                    <p className="text-ink">
                      <span className="mr-2 rounded bg-slate-500/10 px-1.5 py-0.5 text-xs text-muted">
                        {q.kind}
                      </span>
                      {q.question}
                    </p>
                    <p className="mt-0.5 text-xs text-muted">Testing: {q.why_asked}</p>
                    <p className="mt-1 whitespace-pre-line text-xs text-ink/80">
                      {q.answer_skeleton}
                    </p>
                  </li>
                ))}
              </ol>
            </div>
          ) : null}

          {pack.gaps_to_defend?.length ? (
            <div>
              <h5 className="font-semibold text-ink">Gaps they will probe</h5>
              <ul className="mt-2 space-y-2">
                {pack.gaps_to_defend.map((g, i) => (
                  <li key={i}>
                    <p className="text-ink">{g.gap}</p>
                    <p className="text-xs text-muted">Probed as: {g.how_it_will_be_probed}</p>
                    <p className="text-xs text-ink/80">Handle it: {g.how_to_handle}</p>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {pack.technical_topics?.length ? (
            <div>
              <h5 className="font-semibold text-ink">Revise</h5>
              <div className="mt-1 flex flex-wrap gap-1.5">
                {pack.technical_topics.map((t) => (
                  <span key={t} className="rounded border border-line px-2 py-0.5 text-xs text-ink">
                    {t}
                  </span>
                ))}
              </div>
            </div>
          ) : null}

          {pack.questions_to_ask_them?.length ? (
            <div>
              <h5 className="font-semibold text-ink">Ask them</h5>
              <ul className="mt-1 list-disc space-y-1 pl-5 text-muted">
                {pack.questions_to_ask_them.map((q) => (
                  <li key={q}>{q}</li>
                ))}
              </ul>
            </div>
          ) : null}

          {pack.first_90_days_pitch && (
            <div>
              <h5 className="font-semibold text-ink">First 90 days</h5>
              <p className="mt-1 text-muted">{pack.first_90_days_pitch}</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
