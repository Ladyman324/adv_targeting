/* Local CSV reader for list membership. The file stays in the browser; only
 * reviewed advisor snapshots and optional role labels reach the existing API.
 * Email addresses are exact identifiers here: no name or domain guesses. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ListImport = api;
})(typeof window === "undefined" ? globalThis : window, function () {
  "use strict";

  const EMAIL_HEADER = new Set([
    "email", "emailaddress", "emailaddresses", "primaryemail",
    "primaryemailaddress", "eicemail",
  ]);
  const EMAIL = /^[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+$/;
  const MAX_ROWS = 10000;
  const indexCache = new WeakMap();

  function byCrd(index) {
    if (!indexCache.has(index))
      indexCache.set(index, new Map((index.advisors || [])
        .map(row => [String(row[0]), row])));
    return indexCache.get(index);
  }

  function normalizeEmail(value) {
    return String(value || "").trim().toLowerCase();
  }

  function rowsOf(text) {
    const source = String(text || "").replace(/^\uFEFF/, "");
    const first = source.split(/\r?\n/, 1)[0] || "";
    const counts = { ",": 0, "\t": 0, ";": 0 };
    let quoted = false;
    for (let i = 0; i < first.length; i++) {
      if (first[i] === '"') {
        if (quoted && first[i + 1] === '"') i++;
        else quoted = !quoted;
      } else if (!quoted && Object.prototype.hasOwnProperty.call(counts, first[i])) {
        counts[first[i]]++;
      }
    }
    const delimiter = Object.keys(counts).sort((a, b) => counts[b] - counts[a])[0];
    const rows = [];
    let row = [], cell = "", inQuotes = false;
    for (let i = 0; i < source.length; i++) {
      const ch = source[i];
      if (ch === '"') {
        if (inQuotes && source[i + 1] === '"') { cell += '"'; i++; }
        else if (!cell.trim() || inQuotes) inQuotes = !inQuotes;
        else cell += ch;
      } else if (ch === delimiter && !inQuotes && counts[delimiter]) {
        row.push(cell); cell = "";
      } else if ((ch === "\n" || ch === "\r") && !inQuotes) {
        if (ch === "\r" && source[i + 1] === "\n") i++;
        row.push(cell);
        if (row.some(value => value.trim())) rows.push(row);
        if (rows.length > MAX_ROWS + 1) throw new Error("Use a file with at most 10,000 rows.");
        row = []; cell = "";
      } else cell += ch;
    }
    if (inQuotes) throw new Error("The CSV has an unclosed quoted value.");
    row.push(cell);
    if (row.some(value => value.trim())) rows.push(row);
    if (rows.length > MAX_ROWS + 1) throw new Error("Use a file with at most 10,000 rows.");
    return rows;
  }

  function parseEmails(text) {
    const rows = rowsOf(text);
    if (!rows.length) throw new Error("The file is empty.");
    const headers = rows[0].map(cell => cell.toLowerCase().replace(/[^a-z]/g, ""));
    const column = headers.findIndex(header => EMAIL_HEADER.has(header));
    const hasHeader = column >= 0;
    if (!hasHeader && rows[0].length !== 1)
      throw new Error("Include a column headed Email or Email Address.");
    const emailColumn = hasHeader ? column : 0;
    const values = hasHeader ? rows.slice(1) : rows;
    const seen = new Set(), emails = [];
    let duplicateRows = 0, invalidRows = 0;
    for (const row of values) {
      const email = normalizeEmail(row[emailColumn]);
      if (!EMAIL.test(email)) { invalidRows++; continue; }
      if (seen.has(email)) { duplicateRows++; continue; }
      seen.add(email); emails.push(email);
    }
    if (!emails.length) throw new Error("No valid email addresses were found.");
    return { emails, rows: values.length, duplicateRows, invalidRows };
  }

  function resolve(emails, advisors, index, territoryFor, canEmail) {
    const byEmail = new Map();
    for (const [crd, contact] of Object.entries(advisors || {})) {
      const email = normalizeEmail(contact && contact.e);
      if (!email) continue;
      if (!byEmail.has(email)) byEmail.set(email, []);
      byEmail.get(email).push({ crd: String(crd), contact });
    }
    const rowsByCrd = byCrd(index);
    const matched = [], unmatched = [], ambiguous = [], ineligible = [], noTerritory = [];
    for (const email of emails) {
      const found = byEmail.get(email) || [];
      if (!found.length) { unmatched.push(email); continue; }
      if (found.length !== 1) { ambiguous.push(email); continue; }
      const { crd, contact } = found[0];
      const row = rowsByCrd.get(crd);
      if (!row || !canEmail(contact)) { ineligible.push(email); continue; }
      const state = String(row[3] || "").toUpperCase();
      const territory = territoryFor(state);
      if (!territory) { noTerritory.push(email); continue; }
      matched.push({
        crd, email, name: String(contact.pn || row[1] || contact.n || ""),
        firm: String(contact.cn || ""), firmCrd: String(contact.fc || ""),
        state, city: String((index.cities || [])[row[4]] || ""),
        territory, phone: String(contact.w || ""),
        phoneKind: String(contact.wk || ""), contactTier: String(contact.t || ""),
        contactSource: String(contact.src || ""), source: "CSV",
      });
    }
    return { matched, unmatched, ambiguous, ineligible, noTerritory };
  }

  function unionRanked(matched, advisors, index, rankingIds, territoryFor, canEmail,
                       allFirms = false) {
    const firms = new Set(matched.map(row => row.firmCrd).filter(Boolean));
    const selected = new Map(matched.map(row => [row.crd, row]));
    const selectedEmails = new Set(matched.map(row => row.email));
    const rowsByCrd = byCrd(index);
    let added = 0, unavailable = 0;
    for (const crd of rankingIds) {
      const key = String(crd), contact = advisors && advisors[key];
      if (selected.has(key) || !contact
          || (!allFirms && !firms.has(String(contact.fc || "")))) continue;
      const row = rowsByCrd.get(key);
      const state = String(row && row[3] || "").toUpperCase();
      const territory = territoryFor(state);
      const email = normalizeEmail(contact.e);
      if (!row || !territory || !email || selectedEmails.has(email)
          || !canEmail(contact)) {
        unavailable++; continue;
      }
      selected.set(key, {
        crd:key, email,
        name:String(contact.pn || row[1] || contact.n || ""),
        firm:String(contact.cn || ""), firmCrd:String(contact.fc || ""),
        state, city:String((index.cities || [])[row[4]] || ""),
        territory, phone:String(contact.w || ""),
        phoneKind:String(contact.wk || ""), contactTier:String(contact.t || ""),
        contactSource:String(contact.src || ""), source:"Ranked",
      });
      selectedEmails.add(email);
      added++;
    }
    return { people:[...selected.values()], added, unavailable };
  }

  return { normalizeEmail, parseEmails, resolve, unionRanked };
});
