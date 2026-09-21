import { useEffect, useRef, useState } from "react";

export interface MultiOption {
  value: string;
  label: string;
  /** Optional heading this option sits under, e.g. "Delhi NCR". */
  group?: string;
}

interface Props {
  /** Shown on the button when nothing is picked, and as the panel heading. */
  label: string;
  options: MultiOption[];
  selected: string[];
  onChange: (next: string[]) => void;
  /** How many postings each option would match, shown beside it. */
  counts?: Record<string, number>;
  /** Adds a type-to-filter box. Worth it past roughly a dozen options. */
  searchable?: boolean;
  /** Placeholder for that box. */
  searchPlaceholder?: string;
}

/**
 * A checkbox dropdown.
 *
 * Replaces a free-text box for cities and roles. Typing was the wrong control for both:
 * you had to already know that the corpus spells it "Gurugram", you could only ask for one
 * place at a time, and there was no way to see that a search for "Delhi" would also pick
 * up Noida. A list you tick makes the vocabulary and the multi-select visible at once.
 */
export function MultiSelect({
  label,
  options,
  selected,
  onChange,
  counts,
  searchable = false,
  searchPlaceholder = "Type to filter…",
}: Props) {
  const [open, setOpen] = useState(false);
  const [needle, setNeedle] = useState("");
  const wrap = useRef<HTMLDivElement>(null);

  // Close on a click anywhere else, and on Escape. Without the first, two open dropdowns
  // overlap each other; without the second there is no keyboard way out.
  useEffect(() => {
    if (!open) return;
    function onDown(e: PointerEvent) {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const n = selected.length;
  const chosen = options.filter((o) => selected.includes(o.value));
  // One pick reads better as its own name than as "Cities (1)".
  const buttonText = n === 0 ? label : n === 1 ? chosen[0]?.label ?? label : `${label} (${n})`;

  const q = needle.trim().toLowerCase();
  const visible = q ? options.filter((o) => o.label.toLowerCase().includes(q)) : options;

  function toggle(value: string) {
    onChange(
      selected.includes(value) ? selected.filter((v) => v !== value) : [...selected, value],
    );
  }

  // Group headings, in the order the options arrive.
  const groups: { name: string; items: MultiOption[] }[] = [];
  for (const o of visible) {
    const name = o.group ?? "";
    const last = groups[groups.length - 1];
    if (last && last.name === name) last.items.push(o);
    else groups.push({ name, items: [o] });
  }

  return (
    <div ref={wrap} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="true"
        data-testid={`ms-${label.toLowerCase()}`}
        className={`flex max-w-[15rem] items-center gap-1 truncate rounded-lg border px-2.5 py-1.5 text-[13px] transition ${
          n
            ? "border-accent bg-accent/10 font-medium text-accent"
            : "border-line bg-surface text-ink hover:border-accent/50"
        }`}
      >
        <span className="truncate">{buttonText}</span>
        <span aria-hidden className="shrink-0 text-[10px] opacity-60">
          ▼
        </span>
      </button>

      {open && (
        // A group, not a listbox: a listbox's children have to be options, and these are
        // real checkboxes, which screen readers already announce as multi-select.
        <div
          role="group"
          aria-label={label}
          className="absolute left-0 z-30 mt-1 max-h-[22rem] w-64 overflow-y-auto rounded-xl border border-line bg-surface p-1.5 shadow-xl"
        >
          <div className="flex items-center justify-between px-1.5 pb-1.5">
            <span className="text-[11px] font-semibold uppercase tracking-wide text-muted">
              {label}
            </span>
            {n > 0 && (
              <button
                type="button"
                onClick={() => onChange([])}
                className="text-[11px] font-medium text-accent hover:underline"
              >
                Clear {n}
              </button>
            )}
          </div>

          {searchable && (
            <input
              type="search"
              autoFocus
              value={needle}
              onChange={(e) => setNeedle(e.target.value)}
              placeholder={searchPlaceholder}
              aria-label={`Filter ${label}`}
              className="mb-1 w-full rounded-lg border border-line bg-canvas px-2 py-1.5 text-[13px] text-ink outline-none placeholder:text-muted focus:border-accent"
            />
          )}

          {visible.length === 0 && (
            <p className="px-2 py-3 text-center text-[13px] text-muted">No match.</p>
          )}

          {groups.map((g) => (
            <div key={g.name || "_"}>
              {g.name && (
                <p className="px-1.5 pb-0.5 pt-2 text-[10px] font-semibold uppercase tracking-wide text-muted/70">
                  {g.name}
                </p>
              )}
              {g.items.map((o) => {
                const on = selected.includes(o.value);
                const count = counts?.[o.value];
                return (
                  <label
                    key={o.value}
                    className={`flex cursor-pointer items-center gap-2 rounded-lg px-1.5 py-1.5 text-[13px] hover:bg-accent/5 ${
                      on ? "text-ink" : "text-muted"
                    }`}
                  >
                    <input
                      type="checkbox"
                      checked={on}
                      onChange={() => toggle(o.value)}
                      className="size-3.5 shrink-0 accent-[var(--color-accent)]"
                    />
                    <span className={`min-w-0 flex-1 truncate ${on ? "font-medium" : ""}`}>
                      {o.label}
                    </span>
                    {count !== undefined && (
                      // A zero here is information: it says the corpus has nothing there
                      // right now, rather than leaving you to tick it and find out.
                      <span className="shrink-0 text-[11px] tabular-nums text-muted/70">
                        {count}
                      </span>
                    )}
                  </label>
                );
              })}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
