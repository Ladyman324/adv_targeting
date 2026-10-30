"use strict";

// Approved email display names from the user's full CSV reviewed 2026-10-30.
// This is NOT a firm-identity, source, domain, territory or attachment-routing
// table. Keep those original values untouched. The approved static JSON is
// deployed with this module: never read an editable CSV at runtime. Unfamiliar
// names have no inferred parent/brand mapping; legacy affiliations stay held.
const approved = require("./email-company-names.json");
if (approved.schemaVersion !== 1 || !approved.names
    || Object.keys(approved.names).length !== approved.approvedLabelCount) {
  throw new Error("Invalid approved email company display configuration");
}
const COMPANY_DISPLAY_NAMES = Object.freeze({ ...approved.names });

const key = (value) => value.trim().replace(/\s+/g, " ").toLowerCase();
const byLabel = new Map();
for (const [label, display] of Object.entries(COMPANY_DISPLAY_NAMES)) {
  if (!key(label) || typeof display !== "string" || !display.trim()
      || (byLabel.has(key(label)) && byLabel.get(key(label)) !== display)) {
    throw new Error("Conflicting or empty approved email company display name");
  }
  byLabel.set(key(label), display);
}

function companyDisplayName(value) {
  const original = String(value == null ? "" : value);
  // Formatting tolerance only for recognized labels. Unknown text is returned
  // byte-for-byte as a string: no title casing, suffix stripping or fuzzy match.
  return byLabel.get(key(original)) || original;
}

module.exports = { COMPANY_DISPLAY_NAMES, companyDisplayName };
