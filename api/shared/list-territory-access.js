"use strict";

// List/snapshot access is independent of email administration, mailbox
// connection, territory ownership, and sending limits.
function hasCrossTerritoryListAccess(who, env = process.env) {
  const email = String(who && who.name || "").trim().toLowerCase();
  if (!email || !email.endsWith("@eicatlanta.com")) return false;
  return new Set(String(env.EMAIL_CROSS_TERRITORY_LIST_EMAILS || "")
    .split(/[,;\s]+/).map(value => value.trim().toLowerCase()).filter(Boolean))
    .has(email);
}

module.exports = { hasCrossTerritoryListAccess };
