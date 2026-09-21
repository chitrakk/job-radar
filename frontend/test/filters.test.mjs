/**
 * What the city and role pickers must do.
 *
 * There is no test runner in this project, and this logic is the part of the app that is
 * easiest to break silently: it is all scoring thresholds and vocabulary lookups, so a
 * wrong answer looks exactly like a right one until you count the results. The filter
 * layer is bundled with esbuild (already present, vite depends on it) and exercised
 * against a fixture corpus whose every row is here for a reason.
 *
 * Run with `npm test` from frontend/.
 */
import assert from "node:assert/strict";
import { readFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { build } from "esbuild";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");

const out = mkdtempSync(join(tmpdir(), "jobradar-filters-"));
await build({
  entryPoints: [join(here, "entry.ts")],
  bundle: true,
  format: "esm",
  platform: "node",
  outfile: join(out, "bundle.mjs"),
  alias: { "@shared": join(root, "..", "shared") },
  logLevel: "error",
});

const { applyFilters, emptyFilters, bestLocality, cityOptions, roleOptions, cityCounts, roleCounts } =
  await import(pathToFileURL(join(out, "bundle.mjs")).href);

const JOBS = JSON.parse(readFileSync(join(here, "fixture-corpus.json"), "utf8"));
const F = (patch) => ({ ...emptyFilters(), ...patch });
const idsOf = (r) => r.jobs.map((j) => j.id).sort();

let passed = 0;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

/* ------------------------------------------------------------------ */

test("no filters returns the whole corpus", () => {
  assert.equal(applyFilters(JOBS, F({})).jobs.length, JOBS.length);
});

test("the pickers offer a stable vocabulary", () => {
  const cities = cityOptions();
  assert.equal(new Set(cities.map((c) => c.value)).size, cities.length, "no duplicate cities");
  assert.deepEqual(
    cities.slice(0, 5).map((c) => c.value),
    ["delhi", "gurugram", "noida", "ghaziabad", "faridabad"],
    "NCR is offered first — this is a Delhi-NCR job search",
  );
  assert.ok(
    roleOptions().some((r) => r.value === "data_scientist"),
    "the role list comes from the shared taxonomy",
  );
});

test("picking several cities means either, not both", () => {
  const noida = idsOf(applyFilters(JOBS, F({ cities: ["noida"], includeNearby: false })));
  const blr = idsOf(applyFilters(JOBS, F({ cities: ["bengaluru"], includeNearby: false })));
  const both = idsOf(applyFilters(JOBS, F({ cities: ["noida", "bengaluru"], includeNearby: false })));
  for (const id of [...noida, ...blr]) assert.ok(both.includes(id), `${id} missing from the union`);
});

test("picking several roles means either, not both", () => {
  const ds = idsOf(applyFilters(JOBS, F({ roles: ["data_scientist"] })));
  const da = idsOf(applyFilters(JOBS, F({ roles: ["data_analyst"] })));
  const both = idsOf(applyFilters(JOBS, F({ roles: ["data_scientist", "data_analyst"] })));
  assert.ok(ds.length > 0 && da.length > 0, "each role finds something on its own");
  for (const id of [...ds, ...da]) assert.ok(both.includes(id), `${id} missing from the union`);
});

test("a posting that only says 'India' does not answer a named city", () => {
  // b4 is "India" and b3 is "Nanakramguda, India" — a Hyderabad suburb this vocabulary
  // does not know. Both used to satisfy every city in the list, which is what made
  // picking Noida and picking Gurugram return an identical 1,027 results.
  const got = idsOf(applyFilters(JOBS, F({ cities: ["noida"] })));
  assert.ok(!got.includes("b4"), "bare 'India' leaked into a Noida search");
  assert.ok(!got.includes("b3"), "an unknown Indian town leaked into a Noida search");
});

test("a posting naming only a state answers a city inside that state", () => {
  // b2 is "Haryana, India", which genuinely could be the Gurugram role it looks like.
  assert.ok(idsOf(applyFilters(JOBS, F({ cities: ["gurugram"] }))).includes("b2"));
  assert.ok(
    !idsOf(applyFilters(JOBS, F({ cities: ["bengaluru"] }))).includes("b2"),
    "Haryana is not Karnataka",
  );
});

test("nearby cities are included by default and can be switched off", () => {
  // a1 is in Noida; Gurugram is a different city in the same commuter market.
  const on = idsOf(applyFilters(JOBS, F({ cities: ["gurugram"] })));
  const off = idsOf(applyFilters(JOBS, F({ cities: ["gurugram"], includeNearby: false })));
  assert.ok(on.includes("a1"), "a Noida job should reach a Gurugram search by default");
  assert.ok(!off.includes("a1"), "switching nearby off should confine it to Gurugram");
  assert.ok(off.every((id) => on.includes(id)), "switching nearby off can only remove results");
});

test("a real local job outranks a worldwide-remote one", () => {
  const r = applyFilters(JOBS, F({ roles: ["data_analyst"], cities: ["noida"] }));
  const local = r.jobs.findIndex((j) => j.id === "a1");
  const remote = r.jobs.findIndex((j) => j.id === "c1");
  assert.ok(local >= 0, "the Noida data analyst should be in the results");
  if (remote >= 0) assert.ok(local < remote, "remote pass-through ranked above a local job");
});

test("a sibling role is not the role you picked", () => {
  // c2 is a Data Engineer. Picking Data Scientist must not serve it up as one.
  const ds = idsOf(applyFilters(JOBS, F({ roles: ["data_scientist"] })));
  const de = idsOf(applyFilters(JOBS, F({ roles: ["data_engineer"] })));
  assert.ok(de.includes("c2"), "Data Engineer should find the data engineer");
  assert.ok(!ds.includes("c2"), "Data Scientist should not return a Data Engineer");
});

test("MIS Executive counts as a data analyst", () => {
  // Deliberate: in Indian postings this is a reporting role, and the shared taxonomy
  // lists it as an alias. Worth pinning so nobody 'cleans it up' later.
  assert.ok(idsOf(applyFilters(JOBS, F({ roles: ["data_analyst"] }))).includes("a4"));
});

test("a keyword narrows a picked role instead of competing with it", () => {
  // c3 is a "Python Trainer" in Noida: an exact keyword match and not remotely a data
  // scientist. If the typed word were allowed to rank, it would lead the results.
  const role = idsOf(applyFilters(JOBS, F({ roles: ["data_scientist"] })));
  const narrowed = idsOf(applyFilters(JOBS, F({ roles: ["data_scientist"], q: "python" })));
  assert.ok(narrowed.every((id) => role.includes(id)), "narrowing must be a subset");
  assert.ok(!narrowed.includes("c3"), "a Python Trainer is not a Data Scientist");
  assert.ok(narrowed.includes("a2"), "the data scientist who uses python should survive");
});

test("the other filters still compose with the pickers", () => {
  const base = F({ roles: ["data_analyst", "data_scientist"], cities: ["gurugram"] });
  assert.ok(applyFilters(JOBS, base).jobs.length > 0);
  const senior = applyFilters(JOBS, { ...base, seniority: "senior" }).jobs;
  assert.ok(senior.every((j) => j.seniority === "senior"), "seniority filter ignored");
  const paid = applyFilters(JOBS, { ...base, minSalary: 2000000 }).jobs;
  assert.ok(paid.every((j) => j.salary_max >= 2000000 / 1), "salary filter ignored");
  const src = applyFilters(JOBS, { ...base, source: "linkedin" }).jobs;
  assert.ok(src.every((j) => j.source === "linkedin"), "source filter ignored");
});

test("facet counts describe the corpus", () => {
  const cc = cityCounts(JOBS);
  assert.equal(cc.noida, 5, "five fixture postings name Noida");
  assert.equal(cc.bengaluru, 1);
  const rc = roleCounts(JOBS);
  assert.ok(rc.data_analyst >= 3, "several fixture postings are analyst titles");
  assert.equal(rc.software_engineer, 1);
});

test("bestLocality grades a match rather than answering yes or no", () => {
  const opts = { includeNearby: true };
  assert.equal(bestLocality("Noida, India", ["noida"], opts), "exact");
  assert.equal(bestLocality("Gurugram, India", ["noida"], opts), "metro");
  assert.equal(bestLocality("Haryana, India", ["gurugram"], opts), "region");
  assert.equal(bestLocality("Bangalore, India", ["noida"], opts), "");
  assert.equal(
    bestLocality("Anywhere in the World", ["noida"], { ...opts, isRemote: true }),
    "remote",
  );
});

/* ------------------------------------------------------------------ */

let failed = 0;
for (const [name, fn] of tests) {
  try {
    fn();
    passed++;
    console.log(`  ok   ${name}`);
  } catch (e) {
    failed++;
    console.log(`  FAIL ${name}\n       ${e.message.split("\n")[0]}`);
  }
}
rmSync(out, { recursive: true, force: true });
console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
