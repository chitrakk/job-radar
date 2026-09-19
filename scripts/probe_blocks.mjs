/**
 * Is the wall IP reputation or browser fingerprinting?
 *
 * Same URL twice from the same (residential) IP: once as a plain HTTP request with a
 * browser User-Agent, once through a real Chromium. If plain HTTP fails and the browser
 * succeeds, Playwright is the answer. If both succeed here but both fail from Actions,
 * the wall is the IP and no amount of browser automation moves it.
 */
import { chromium } from "playwright";

const TARGETS = [
  ["naukri-search", "https://www.naukri.com/data-scientist-jobs-in-delhi-ncr"],
  ["naukri-api", "https://www.naukri.com/jobapi/v3/search?noOfResults=20&urlType=search_by_keyword&searchType=adv&keyword=data%20scientist&location=delhi"],
  ["indeed-in", "https://in.indeed.com/jobs?q=data+scientist&l=Delhi"],
  ["foundit", "https://www.foundit.in/srp/results?query=data%20scientist&locations=Delhi"],
  ["timesjobs", "https://www.timesjobs.com/candidate/job-search.html?searchType=personalizedSearch&from=submit&txtKeywords=data+scientist&txtLocation=delhi"],
  ["instahyre", "https://www.instahyre.com/search-jobs/?job_functions=Data%20Science"],
  ["shine", "https://www.shine.com/job-search/data-scientist-jobs-in-delhi"],
  ["iimjobs", "https://www.iimjobs.com/c/analytics-jobs"],
  ["wellfound", "https://wellfound.com/role/r/data-scientist"],
  ["internshala", "https://internshala.com/jobs/data-science-jobs/"],
];

const UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

console.log("=== A. PLAIN HTTP (what the pipeline does today) ===");
const httpResults = {};
for (const [name, url] of TARGETS) {
  try {
    const r = await fetch(url, {
      headers: {
        "User-Agent": UA,
        Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
      },
      redirect: "follow",
    });
    const body = await r.text();
    httpResults[name] = r.status;
    console.log(
      `  ${name.padEnd(16)} HTTP ${r.status}  ${String(body.length).padStart(7)} bytes` +
        (/captcha|are you a human|access denied|unusual traffic/i.test(body) ? "  [challenge page]" : ""),
    );
  } catch (e) {
    httpResults[name] = "ERR";
    console.log(`  ${name.padEnd(16)} ERR   ${e.message.slice(0, 60)}`);
  }
}

console.log("\n=== B. REAL CHROMIUM (Playwright, same IP) ===");
const browser = await chromium.launch({
  args: ["--disable-blink-features=AutomationControlled"],
});
const ctx = await browser.newContext({
  userAgent: UA,
  locale: "en-IN",
  timezoneId: "Asia/Kolkata",
  viewport: { width: 1440, height: 900 },
});
// The single highest-value stealth tweak: navigator.webdriver is the first thing
// anti-bot scripts read.
await ctx.addInitScript(() => {
  Object.defineProperty(navigator, "webdriver", { get: () => undefined });
});

for (const [name, url] of TARGETS) {
  const page = await ctx.newPage();
  try {
    const resp = await page.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 });
    await page.waitForTimeout(3500);
    const info = await page.evaluate(() => ({
      title: document.title.slice(0, 70),
      text: document.body?.innerText?.length ?? 0,
      // Rough "did we get a job list" test: count anchors that look like job links.
      jobish: [...document.querySelectorAll("a")].filter((a) =>
        /job|position|opening|vacanc/i.test(a.getAttribute("href") || ""),
      ).length,
      challenge: /captcha|are you a human|access denied|unusual traffic|verify you are/i.test(
        document.body?.innerText || "",
      ),
    }));
    console.log(
      `  ${name.padEnd(16)} HTTP ${String(resp?.status() ?? "?").padEnd(4)} ` +
        `text=${String(info.text).padStart(6)} joblinks=${String(info.jobish).padStart(4)}` +
        `${info.challenge ? "  [CHALLENGE]" : ""}  "${info.title}"`,
    );
  } catch (e) {
    console.log(`  ${name.padEnd(16)} FAIL  ${e.message.split("\n")[0].slice(0, 70)}`);
  }
  await page.close();
}

await browser.close();
