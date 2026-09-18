/**
 * Job Radar — Google Sheets tracker backend.
 *
 * This is the one place Apps Script is the right tool. It cannot scrape (no headless
 * browser, 6-minute limit, blocked by Cloudflare), but it CAN be a zero-infrastructure
 * write endpoint for a private tracker: it runs as you, owns your Sheet, needs no OAuth
 * server, no service-account key, and no secret stored in a public repo. Each user deploys
 * their own copy against their own Sheet, so nobody shares a database.
 *
 * ── Setup ──────────────────────────────────────────────────────────────────────
 * 1. Create a Google Sheet. Extensions → Apps Script. Delete the stub, paste this file.
 * 2. Edit SHARED_SECRET below to any long random string of your own.
 * 3. Deploy → New deployment → type "Web app".
 *      Execute as:        Me
 *      Who has access:    Anyone
 *    "Anyone" is required because a static page calls it — the SHARED_SECRET is what
 *    actually gates writes. Treat that secret like a password.
 * 4. Copy the /exec URL. Paste it, with the secret, into Job Radar's Tracker settings.
 *
 * Tabs are created on first use: Applications, Outreach, CV Scores.
 */

const SHARED_SECRET = "CHANGE-ME-to-a-long-random-string";

const TABS = {
  applications: {
    name: "Applications",
    headers: [
      "Logged At", "Job ID", "Title", "Company", "Location", "Remote",
      "Salary", "Source", "Posted", "URL", "Status", "Next Action", "Notes",
    ],
  },
  outreach: {
    name: "Outreach",
    headers: [
      "Logged At", "Job ID", "Company", "Contact Name", "Contact Role",
      "Email", "Email Confidence", "LinkedIn", "Channel", "Sent?",
      "LinkedIn Note", "Email Subject", "Email Body", "Follow Up On",
    ],
  },
  cv: {
    name: "CV Scores",
    headers: [
      "Logged At", "Score", "Band", "Impact", "Keywords", "ATS", "Clarity",
      "Structure", "Evidence", "Progression", "Language", "Target Role", "Top Fixes",
    ],
  },
};

function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) {
      return json({ ok: false, error: "empty request" });
    }
    const body = JSON.parse(e.postData.contents);

    if (body.secret !== SHARED_SECRET) {
      return json({ ok: false, error: "bad secret" });
    }

    const spec = TABS[body.kind];
    if (!spec) {
      return json({ ok: false, error: "unknown kind: " + body.kind });
    }

    const rows = Array.isArray(body.rows) ? body.rows : [body.row];
    if (!rows.length || !rows[0]) {
      return json({ ok: false, error: "no rows" });
    }

    const sheet = getSheet(spec);
    const stamp = new Date();

    // Deduplicate on Job ID so re-logging the same application updates rather than
    // appending a second row — otherwise the tracker fills with repeats every refresh.
    const existing = jobIdIndex(sheet, spec);
    let added = 0;
    let updated = 0;

    rows.forEach(function (row) {
      const values = [stamp].concat(spec.headers.slice(1).map(function (h) {
        const v = row[h];
        return v === undefined || v === null ? "" : String(v);
      }));

      const key = String(row["Job ID"] || "");
      const dedupe = body.kind !== "cv" && key && existing[key];
      if (dedupe) {
        sheet.getRange(existing[key], 1, 1, values.length).setValues([values]);
        updated++;
      } else {
        sheet.appendRow(values);
        added++;
      }
    });

    return json({ ok: true, added: added, updated: updated, tab: spec.name });
  } catch (err) {
    return json({ ok: false, error: String(err) });
  }
}

/** Read back the tracker so the app can show what is already logged. */
function doGet(e) {
  try {
    const params = (e && e.parameter) || {};
    if (params.secret !== SHARED_SECRET) {
      return json({ ok: false, error: "bad secret" });
    }

    const spec = TABS[params.kind || "applications"];
    if (!spec) {
      return json({ ok: false, error: "unknown kind" });
    }

    const sheet = getSheet(spec);
    const values = sheet.getDataRange().getValues();
    if (values.length < 2) {
      return json({ ok: true, rows: [] });
    }

    const headers = values[0];
    const rows = values.slice(1).map(function (row) {
      const obj = {};
      headers.forEach(function (h, i) {
        obj[h] = row[i] instanceof Date ? row[i].toISOString() : row[i];
      });
      return obj;
    });
    return json({ ok: true, rows: rows });
  } catch (err) {
    return json({ ok: false, error: String(err) });
  }
}

function getSheet(spec) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(spec.name);
  if (!sheet) {
    sheet = ss.insertSheet(spec.name);
  }
  if (sheet.getLastRow() === 0) {
    sheet.appendRow(spec.headers);
    const header = sheet.getRange(1, 1, 1, spec.headers.length);
    header.setFontWeight("bold").setBackground("#f1f3f4");
    sheet.setFrozenRows(1);
  }
  return sheet;
}

/** Map Job ID → row number, for in-place updates. */
function jobIdIndex(sheet, spec) {
  const col = spec.headers.indexOf("Job ID") + 1;
  const index = {};
  if (col === 0 || sheet.getLastRow() < 2) {
    return index;
  }
  const ids = sheet.getRange(2, col, sheet.getLastRow() - 1, 1).getValues();
  ids.forEach(function (cell, i) {
    const id = String(cell[0] || "");
    if (id) {
      index[id] = i + 2;
    }
  });
  return index;
}

function json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(
    ContentService.MimeType.JSON
  );
}
