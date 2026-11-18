"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const FIELDS = ["line1", "line2", "line3", "city", "state", "postalCode"];
const text = value => String(value || "").trim().replace(/\s+/g, " ");
const clean = value => Object.fromEntries(FIELDS.map(key => [key, text((value || {})[key])]));
const idKey = value => text(value).toLowerCase();
const norm = value => text(value).toUpperCase().replace(/[^A-Z0-9]/g, "");
const hash = value => crypto.createHash("sha256").update(JSON.stringify(value)).digest("hex");
function failure(message, statusCode = 409) { return Object.assign(new Error(message), { statusCode, addressReviewSafe: true }); }
function usable(value) {
  const a = clean(value);
  return Boolean(a.line1 && a.city && /^[A-Z]{2}$/i.test(a.state) && /^\d{5}(?:-\d{4})?$/.test(a.postalCode));
}
function street(value) {
  const abbreviations = { STREET: "ST", AVENUE: "AVE", BOULEVARD: "BLVD", ROAD: "RD", DRIVE: "DR",
    LANE: "LN", COURT: "CT", SUITE: "STE", FLOOR: "FL", NORTH: "N", SOUTH: "S", EAST: "E", WEST: "W" };
  return norm(text(value).toUpperCase().replace(/[.,#]/g, " ").split(/\s+/).map(word => abbreviations[word] || word).join(" "));
}
function sameAddress(left, right) {
  if (!usable(left) || !usable(right)) return false;
  const a = clean(left), b = clean(right);
  return street([a.line1, a.line2, a.line3].join(" ")) === street([b.line1, b.line2, b.line3].join(" ")) && norm(a.city) === norm(b.city)
    && norm(a.state) === norm(b.state) && a.postalCode.slice(0, 5) === b.postalCode.slice(0, 5);
}
function revision(lookup, entry, record) {
  return hash([lookup.identity_manifest_hash, entry.contactId,
    entry.candidates, record.edited, record.firstName, record.middleName, record.lastName,
    record.emailAddress, record.businessAddress, record.homeAddress]);
}

function createService({ lookup, read, write, journal, secondaryField = "" }) {
  function approved(crd) {
    if (!/^\d{3,12}$/.test(crd)) throw failure("A valid advisor CRD is required.", 400);
    const id = (lookup.contacts || {})[crd], entry = (lookup.address_review || {})[crd];
    if (!id || !entry || idKey(id) !== idKey(entry.contactId)
        || Object.values(lookup.contacts).filter(value => idKey(value) === idKey(id)).length !== 1)
      throw failure("No unique approved ACT! link is available for this advisor.", 404);
    return entry;
  }
  function verify(entry, record) {
    if (!record || record.isPrivate || record.isActive === false || idKey(record.id) !== idKey(entry.contactId))
      throw failure("ACT! returned a different contact. No address was changed.");
    for (const [key, value] of Object.entries(entry.identity || {}))
      if (text(record[key]).toLowerCase() !== text(value).toLowerCase())
        throw failure("The ACT! contact identity has changed. Refresh the identity link before editing.");
    if (!entry.identity || !text(entry.identity.lastName))
      throw failure("The approved ACT! identity evidence is incomplete.");
  }
  function view(crd, entry, record) {
    const primary = clean(record.businessAddress);
    const secondary = secondaryField ? clean(record[secondaryField]) : null;
    const candidates = (entry.candidates || []).filter(c => usable(c.address)
      && !sameAddress(c.address, primary) && !sameAddress(c.address, secondary));
    return { crd, linked: true, contactId: entry.contactId, primary, secondary,
      primaryUsable: usable(primary), candidates,
      secondaryAvailable: secondaryField === "homeAddress" && !usable(secondary)
        && !FIELDS.some(key => text((record.homeAddress || {})[key])),
      revision: revision(lookup, entry, record) };
  }
  async function preview(crd) {
    const entry = approved(crd), record = await read(crd, entry.contactId);
    verify(entry, record);
    return view(crd, entry, record);
  }
  async function update(who, body) {
    const crd = String(body.crd || ""), entry = approved(crd);
    if (!who || !who.id || who.dev) throw failure("Sign in to update ACT!.", 401);
    if (!["replace_primary", "add_secondary"].includes(body.action)) throw failure("Choose an address action.", 400);
    const candidate = (entry.candidates || []).find(c => c.id === body.candidateId);
    if (!candidate || !usable(candidate.address)) throw failure("This source address is unavailable. Reload the contact.");
    // Client supplies only a source candidate id, never an ACT id or address.
    return journal.lock(entry.contactId, async () => {
      const current = await read(crd, entry.contactId);
      verify(entry, current);
      if (body.revision !== revision(lookup, entry, current))
        throw failure("The ACT! address or source evidence changed. Reload before choosing an action.");
      const state = view(crd, entry, current);
      if (!state.candidates.some(c => c.id === candidate.id)) return { ...state, changed: false, verified: true };
      const next = JSON.parse(JSON.stringify(current));
      const target = body.action === "replace_primary" ? "businessAddress" : secondaryField;
      if (body.action === "add_secondary" && !state.secondaryAvailable)
        throw failure("ACT! already has a secondary address, or secondary storage is unavailable. It was preserved.");
      next[target] = { ...(next[target] || {}), ...clean(candidate.address) };
      // Home country is Partners in this ACT database. Never copy a country
      // into it, and never erase it when saving a secondary office.
      next[target].latitude = null;
      next[target].longitude = null;
      const audit = await journal.begin({ crd, contactId: entry.contactId, actorId: who.id,
        actorName: who.name, action: body.action, candidate, before: clean(current[target]),
        after: clean(next[target]), identityManifest: lookup.identity_manifest_hash });
      let attempted = false;
      try {
        const latest = await read(crd, entry.contactId);
        verify(entry, latest);
        if (hash(latest) !== hash(current)) throw failure("ACT! changed while this action was being prepared. Reload before trying again.");
        attempted = true;
        await write(crd, entry.contactId, next);
        const saved = await read(crd, entry.contactId);
        verify(entry, saved);
        if (hash(clean(saved[target])) !== hash(clean(next[target]))
            || (body.action === "add_secondary" && hash(saved.businessAddress) !== hash(current.businessAddress))
            || (target === "homeAddress" && text((saved.homeAddress || {}).country) !== text((current.homeAddress || {}).country)))
          throw failure("ACT! did not confirm the requested address. Reopen the contact and check ACT! before retrying.", 502);
        let auditPending = false;
        try { await journal.finish(audit, "verified"); } catch { auditPending = true; }
        return { ...view(crd, entry, saved), changed: true, verified: true, auditPending };
      } catch (error) {
        try { await journal.finish(audit, attempted ? "verification_required" : "not_written"); } catch { /* durable intent remains */ }
        if (error.statusCode) throw error;
        throw failure(attempted ? "ACT! could not confirm the update. Reopen this contact and check ACT! before retrying."
          : "ACT! is unavailable. No address update was sent.", 502);
      }
    });
  }
  return { preview, update };
}

let service;
function currentService() {
  if (!service) {
    const act = require("./act");
    const lookup = JSON.parse(fs.readFileSync(path.join(__dirname, "act_contacts.json"), "utf8"));
    service = createService({ lookup, read: (crd, id) => act.addressRecord(crd, id),
      write: (crd, id, next) => act.addressRecord(crd, id, next),
      journal: require("./address-change-store"),
      secondaryField: process.env.ACT_SECONDARY_ADDRESS_FIELD || "" });
  }
  return service;
}
module.exports = { createService, currentService, clean, usable, sameAddress };
