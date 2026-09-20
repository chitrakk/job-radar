import { useState } from "react";
import type { JobDetail, JobEntry } from "../types";
import type { Settings } from "../lib/settings";
import type { Mark } from "../lib/tracker";
import { JobActions } from "./JobActions";
import {
  displayTags,
  fixCase,
  formatSalary,
  relativeDate,
  seniorityLabel,
  sourceLabel,
} from "../lib/format";
import { shardFor } from "../lib/search";

const REMOTE_STYLES: Record<string, string> = {
  remote: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400",
  hybrid: "bg-amber-500/10 text-amber-700 dark:text-amber-400",
  onsite: "bg-slate-500/10 text-muted",
};

/** Sources that publish a listing with no description, so "Details" would open an empty
 *  panel. The adapter marks these; the card links out instead of wasting a click. */
const NO_DESCRIPTION_TAG = "Details on Naukri";

/** Cache of loaded shards, so opening ten jobs from one shard costs one request. */
const shardCache = new Map<string, Promise<Record<string, JobDetail>>>();

function loadShard(base: string, id: string): Promise<Record<string, JobDetail>> {
  const shard = shardFor(id);
  let p = shardCache.get(shard);
  if (!p) {
    p = fetch(`${base}jobs/${shard}.json`)
      .then((r) => (r.ok ? r.json() : {}))
      .catch(() => ({}));
    shardCache.set(shard, p);
  }
  return p;
}

/** Small square toggle. Colour carries the state, the label carries it for screen readers. */
function MarkButton({
  on,
  onClick,
  title,
  activeClass,
  children,
}: {
  on: boolean;
  onClick: () => void;
  title: string;
  activeClass: string;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      aria-label={title}
      aria-pressed={on}
      className={`rounded-lg border px-2 py-1.5 text-sm leading-none transition ${
        on ? activeClass : "border-line text-muted hover:text-ink"
      }`}
    >
      {children}
    </button>
  );
}

export function JobCard({
  job,
  dataBase,
  settings,
  mark,
  onMark,
  onScoreCV,
}: {
  job: JobEntry;
  dataBase: string;
  settings: Settings;
  mark?: Mark;
  onMark: (id: string, mark: Mark) => void;
  onScoreCV?: (role: string, description: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<JobDetail | null>(null);
  const [loading, setLoading] = useState(false);

  const salary = formatSalary(job);
  const posted = relativeDate(job.posted_at ?? job.first_seen_at);
  const seniority = seniorityLabel(job.seniority);
  const alsoOn = job.tags.filter((t) => t.startsWith("also:")).map((t) => sourceLabel(t.slice(5)));
  const linkOnly = job.tags.includes(NO_DESCRIPTION_TAG);
  // displayTags collapses contradictory "Experience:" pairs left by a dedupe merge.
  const topics = displayTags(job.tags)
    .filter((t) => t !== NO_DESCRIPTION_TAG)
    .slice(0, 3);

  async function toggle() {
    const next = !open;
    setOpen(next);
    if (next && !detail) {
      setLoading(true);
      const shard = await loadShard(dataBase, job.id);
      setDetail(
        shard[job.id] ?? { description: "", apply_url: "", salary_period: "", enriched: false },
      );
      setLoading(false);
    }
  }

  return (
    <article
      className={`rounded-lg border bg-surface px-3.5 py-3 transition ${
        mark === "applied"
          ? "border-emerald-500/40 bg-emerald-500/[0.03]"
          : mark === "saved"
            ? "border-amber-500/40"
            : "border-line hover:border-accent/40"
      }`}
    >
      {/* Title and pay share a line: pay is the field people scan for, and putting it
          anywhere else means reading two lines to reject a job. */}
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="min-w-0 text-[15px] font-semibold leading-snug text-ink">
          <a
            href={job.url}
            target="_blank"
            rel="noopener noreferrer"
            className="hover:text-accent hover:underline"
          >
            {job.title}
          </a>
        </h3>
        {salary && (
          <span className="shrink-0 text-sm font-semibold text-accent">{salary}</span>
        )}
      </div>

      {/* One line of provenance instead of three stacked rows. */}
      <p className="mt-0.5 truncate text-[13px] text-muted">
        <span className="font-medium text-ink/80">{fixCase(job.company)}</span>
        {job.location && <> · {fixCase(job.location)}</>}
        {posted && <> · {posted}</>}
        <> · {sourceLabel(job.source)}</>
        {alsoOn.length > 0 && <> · also on {alsoOn.join(", ")}</>}
      </p>

      {/* Chips are rendered only when the field exists. 81% of postings have no work mode
          and 44% no seniority, so a fixed row of slots would mostly be blank. */}
      {(job.remote !== "unknown" || seniority || topics.length > 0) && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px]">
          {job.remote !== "unknown" && (
            <span
              className={`rounded px-1.5 py-0.5 font-medium capitalize ${REMOTE_STYLES[job.remote]}`}
            >
              {job.remote}
            </span>
          )}
          {seniority && (
            <span className="rounded bg-slate-500/10 px-1.5 py-0.5 font-medium text-muted">
              {seniority}
            </span>
          )}
          {topics.map((t) => (
            <span key={t} className="rounded bg-slate-500/10 px-1.5 py-0.5 text-muted">
              {t}
            </span>
          ))}
        </div>
      )}

      {job.summary && <p className="mt-2 text-[13px] leading-relaxed text-muted">{job.summary}</p>}

      {job.skills.length > 0 && (
        <p className="mt-2 truncate text-[11px] text-muted/80">{job.skills.slice(0, 8).join(" · ")}</p>
      )}

      <div className="mt-2.5 flex items-center gap-1.5 text-[13px]">
        <a
          href={detail?.apply_url || job.url}
          target="_blank"
          rel="noopener noreferrer"
          onClick={() => {
            // Opening the posting is the moment you act on it. Marking it then means the
            // list remembers where you got to without a second deliberate click.
            if (!mark) onMark(job.id, "saved");
          }}
          className="rounded-lg bg-accent px-3 py-1.5 font-medium text-white hover:opacity-90"
        >
          Apply
        </a>
        {!linkOnly && (
          <button
            onClick={toggle}
            className="rounded-lg border border-line px-2.5 py-1.5 font-medium text-muted hover:text-ink"
            aria-expanded={open}
          >
            {open ? "Hide" : "Details"}
          </button>
        )}

        <span className="ml-auto flex items-center gap-1.5">
          <MarkButton
            on={mark === "saved"}
            onClick={() => onMark(job.id, "saved")}
            title={mark === "saved" ? "Remove from saved" : "Save for later"}
            activeClass="border-amber-500/50 bg-amber-500/10 text-amber-600 dark:text-amber-400"
          >
            {mark === "saved" ? "★" : "☆"} Save
          </MarkButton>
          <MarkButton
            on={mark === "applied"}
            onClick={() => onMark(job.id, "applied")}
            title={mark === "applied" ? "Mark as not applied" : "Mark as applied"}
            activeClass="border-emerald-500/50 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
          >
            ✓ Applied
          </MarkButton>
          <MarkButton
            on={mark === "hidden"}
            onClick={() => onMark(job.id, "hidden")}
            title="Hide this job"
            activeClass="border-line bg-slate-500/10 text-muted"
          >
            ✕
          </MarkButton>
        </span>
      </div>

      {open && (
        <div className="mt-3 border-t border-line pt-3">
          {loading ? (
            <p className="text-sm text-muted">Loading…</p>
          ) : (
            <>
              {detail?.description ? (
                <p className="max-h-80 overflow-y-auto whitespace-pre-line text-[13px] leading-relaxed text-muted">
                  {detail.description}
                </p>
              ) : (
                <p className="text-sm text-muted">
                  This source did not publish a description.{" "}
                  <a
                    href={job.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-accent hover:underline"
                  >
                    Open the posting
                  </a>{" "}
                  to read it.
                </p>
              )}
              <JobActions
                job={job}
                description={detail?.description ?? ""}
                settings={settings}
                onScoreCV={onScoreCV}
              />
            </>
          )}
        </div>
      )}
    </article>
  );
}
