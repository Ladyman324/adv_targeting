"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const addresses = require("./act-address-review");
const core = require("./email-core");

const crdPattern = /^\d{3,12}$/;
const text = (value) => String(value || "").trim();
const norm = (value) => text(value).toLowerCase();
const hash = (value) => crypto.createHash("sha256").update(value).digest("hex");
const failure = (message, statusCode = 409) =>
  Object.assign(new Error(message), { statusCode, addressReviewSafe: true });

function createService({ lookup, addressService, resolve, act, journal }) {
  const linked = lookup.contacts || {};
  const directory = lookup.crm_directory;
  const claimed = new Set(directory?.claimedCrds || []);
  const emails = new Set(directory?.emailHashes || []);

  function search(query) {
    if (!directory) throw failure("ACT! contacts are unavailable until the next data refresh.", 503);
    const terms = norm(query).slice(0, 120).split(/\s+/).filter(Boolean);
    if (terms.join("").length < 2) throw failure("Enter at least two letters or numbers.", 400);
    const matches = directory.contacts.filter((row) => {
      const searchable = norm([row.name, row.email, row.company, row.primary.city, row.primary.state].join(" "));
      return terms.every((term) => searchable.includes(term));
    });
    return { contacts: matches.slice(0, 50), count: matches.length };
  }

  async function draftFor(crd) {
    let approved;
    try { approved = await resolve(crd); }
    catch (error) { if (error.code !== "recipient_not_approved") throw error; return null; }
    const emailAddress = norm(approved.email);
    const lastName = text(approved.lastName);
    const fullName = text(approved.name);
    const words = fullName.split(/\s+/);
    const lastAt = norm(fullName).lastIndexOf(" " + norm(lastName));
    // The registry's surname can include multiple words. Require it to be
    // visible in the name before choosing a first name for the CRM.
    const suffix = fullName.slice(lastAt + lastName.length + 1);
    if (!lastName || lastAt < 1 || !/^(?:$|[\s,])/.test(suffix)
        || !core.validEmail(emailAddress) || !core.isExternal(emailAddress) || !words[0]) return null;
    const firstName = words[0];
    return { firstName, lastName, fullName, emailAddress, company: text(approved.firm),
      customFields: { crd }, isPrivate: false };
  }

  async function preview(crd) {
    if (!crdPattern.test(crd)) throw failure("A valid advisor CRD is required.", 400);
    if (linked[crd]) {
      const evidence = lookup.address_review?.[crd];
      if (!evidence) return { status: "linked" };
      try { return { ...await addressService.preview(crd), status: "linked" }; }
      catch {
        return { status: "linked", primary: evidence.primary,
          notice: "Showing the last synced ACT! address. Live confirmation is unavailable." };
      }
    }
    if (!directory) return { status: "unknown" };
    if (claimed.has(crd) || lookup.activity_contacts?.[crd]) return { status: "review" };
    const draft = await draftFor(crd);
    if (draft && emails.has(hash(draft.emailAddress))) return { status: "review" };
    if (!draft || !act.configured()) return { status: "unlinked" };
    const created = await journal.creationStatus(crd);
    if (created) return { status: created.state === "verified" ? "added" : "pending" };
    return { status: "unlinked", draft, revision: hash(JSON.stringify([lookup.identity_manifest_hash, draft])) };
  }

  async function change(who, body) {
    const crd = text(body.crd);
    if (!who?.id || who.dev) throw failure("Sign in to update ACT!.", 401);
    if (!crdPattern.test(crd)) throw failure("A valid advisor CRD is required.", 400);
    if (body.confirmed !== true) throw failure("Review and confirm this change first.", 400);
    if (body.action === "replace_primary") return addressService.update(who, { ...body, crd });
    if (body.action !== "add_contact") throw failure("Choose an ACT! contact action.", 400);
    return journal.lock("create-" + crd, async () => {
      const state = await preview(crd);
      if (!state.draft || state.revision !== body.revision)
        throw failure("This contact or its ACT! status changed. Reopen the profile before continuing.");
      if (await act.findContactDuplicates(crd, state.draft))
        throw failure("ACT! found a contact with this name or email. Check the existing record before adding another.");
      await journal.reserveCreation(crd, who, state.draft);
      try {
        const record = await act.addContact(crd, state.draft);
        if (!record?.id || norm(record.emailAddress) !== norm(state.draft.emailAddress)
            || norm(record.firstName) !== norm(state.draft.firstName)
            || norm(record.lastName) !== norm(state.draft.lastName)
            || text(record.customFields?.crd) !== crd)
          throw failure("Check the new contact in ACT! before trying again. The record could not be verified.", 502);
        await journal.finishCreation(crd, record.id);
        return { status: "added", verified: true };
      } catch {
        throw failure("ACT! did not confirm the result. Check the contact there before trying again.", 502);
      }
    });
  }
  return { search, preview, change };
}

let installed;
function service() {
  if (!installed) {
    const lookup = JSON.parse(fs.readFileSync(path.join(__dirname, "act_contacts.json"), "utf8"));
    installed = createService({ lookup, addressService: addresses.currentService(),
      resolve: require("./recipient-registry").resolve, act: require("./act"),
      journal: require("./address-change-store") });
  }
  return installed;
}
module.exports = { service, createService };
