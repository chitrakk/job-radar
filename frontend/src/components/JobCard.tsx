import { useState } from "react";
import type { JobDetail, JobEntry } from "../types";
import type { Settings } from "../lib/settings";
import { JobActions } from "./JobActions";
import { formatSalary, relativeDate, seniorityLabel, sourceLabel } from "../lib/format";
import { shardFor } from "../lib/search";

const REMOTE_STYLES: Record<string, string> = {
  remote: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400",
  hybrid: "bg-amber-500/10 text-amber-700 dark:text-amber-400",
  onsite: "bg-slate-500/10 text-muted",
};

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

export function JobCard({
  job,
  dataBase,
  settings,
}: {
  job: JobEntry;
  dataBase: string;
  settings: Settings;
}) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<JobDetail | null>(null);
  const [loading, setLoading] = useState(false);

  const salary = formatSalary(job);
  const posted = relativeDate(job.posted_at ?? job.first_seen_at);
  const seniority = seniorityLabel(job.seniority);
  // Cross-posting markers added by dedupe.py, e.g. "also:linkedin".
  const alsoOn = job.tags.filter((t) => t.startsWith("also:")).map((t) => sourceLabel(t.slice(5)));
  const topics = job.tags.filter((t) => !t.startsWith("also:")).slice(0, 5);

  async function toggle() {
    const next = !open;
    setOpen(next);
    if (next && !detail) {
      setLoading(true);
      const shard = await loadShard(dataBase, job.id);
      setDetail(shard[job.id] ?? { description: "", apply_url: "", salary_period: "", enriched: false });
      setLoading(false);
    }
  }

  return (
    <article className="rounded-xl border border-line bg-surface p-4 transition hover:border-accent/40 sm:p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <h3 className="text-base font-semibold leading-snug text-ink">
            <a
              href={job.url}
              target="_blank"
              rel="noopener noreferrer"
              className="hover:text-accent hover:underline"
            >
              {job.title}
            </a>
          </h3>
          <p className="mt-1 text-sm text-muted">
            <span className="font-medium text-ink/80">{job.company}</span>
            {job.location && <> · {job.location}</>}
          </p>
        </div>
        {salary && (
          <span className="shrink-0 rounded-lg bg-accent/10 px-2.5 py-1 text-sm font-semibold text-accent">
            {salary}
          </span>
        )}
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-1.5 text-xs">
        {job.remote !== "unknown" && (
          <span className={`rounded-md px-2 py-0.5 font-medium capitalize ${REMOTE_STYLES[job.remote]}`}>
            {job.remote}
          </span>
        )}
        {seniority && (
          <span className="rounded-md bg-slate-500/10 px-2 py-0.5 font-medium text-muted">{seniority}</span>
        )}
        {topics.map((t) => (
          <span key={t} className="rounded-md bg-slate-500/10 px-2 py-0.5 text-muted">
            {t}
          </span>
        ))}
        <span className="ml-auto flex items-center gap-2 text-muted">
          {posted && <span>{posted}</span>}
          <span className="opacity-60">{sourceLabel(job.source)}</span>
        </span>
      </div>

      {job.summary && <p className="mt-3 text-sm leading-relaxed text-muted">{job.summary}</p>}

      {job.skills.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {job.skills.slice(0, 8).map((s) => (
            <span
              key={s}
              className="rounded border border-line px-1.5 py-0.5 text-xs text-muted"
            >
              {s}
            </span>
          ))}
        </div>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-3 border-t border-line pt-3 text-sm">
        <a
          href={detail?.apply_url || job.url}
          target="_blank"
          rel="noopener noreferrer"
          className="rounded-lg bg-accent px-3 py-1.5 font-medium text-white hover:opacity-90"
        >
          Apply
        </a>
        <button
          onClick={toggle}
          className="rounded-lg border border-line px-3 py-1.5 font-medium text-muted hover:text-ink"
          aria-expanded={open}
        >
          {open ? "Hide details" : "Details"}
        </button>
        {alsoOn.length > 0 && (
          <span className="text-xs text-muted">Also on {alsoOn.join(", ")}</span>
        )}
      </div>

      {open && (
        <div className="mt-3 border-t border-line pt-3">
          {loading ? (
            <p className="text-sm text-muted">Loading…</p>
          ) : (
            <>
              {detail?.description ? (
                <p className="max-h-96 overflow-y-auto whitespace-pre-line text-sm leading-relaxed text-muted">
                  {detail.description}
                </p>
              ) : (
                <p className="text-sm text-muted">
                  This source did not publish a description. Open the posting to read it.
                </p>
              )}
              <JobActions job={job} description={detail?.description ?? ""} settings={settings} />
            </>
          )}
        </div>
      )}
    </article>
  );
}
