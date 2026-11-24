"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const addresses = require("./act-address-review");
const core = require("./email-core");
const { indexDirectory } = require("./act-directory");

const crdPattern = /^\d{3,12}$/;
const mailCodes = require("./email-suppress");
const ACT_EMAIL_ONLY = "2";
const phoneDigits = (value) => String(value || "").replace(/\D/g, "").replace(/^1(?=\d{10}$)/, "");
const verifiedFields = {
  department: "Team", jobTitle: "Title", website: "Website", salutation: "Salutation",
  businessPhone: "Office phone", businessExtension: "Extension", mobilePhone: "Mobile phone",
};
async function defaultMailCode(crd, email) {
  const saved = mailCodes.floorCodeFor({ crd, email });
  if (saved === "BB") return "N";
  if (["U", "NC", "N"].includes(saved)) return saved;
  return (saved || await mailCodes.isSuppressed(email)) ? "U" : ACT_EMAIL_ONLY;
}
const text = (value) => String(value || "").trim();
const norm = (value) => text(value).toLowerCase();
const hash = (value) => crypto.createHash("sha256").update(value).digest("hex");
const failure = (message, statusCode = 409) =>
  Object.assign(new Error(message), { statusCode, addressReviewSafe: true });

function createService({ lookup, addressService, resolve, act, journal, mailCodeFor = defaultMailCode }) {
  const linked = lookup.contacts || {};
  const directory = lookup.crm_directory;
  const claimed = new Set(directory?.claimedCrds || []);
  const emails = new Set(directory?.emailHashes || []);
  const contacts = indexDirectory(directory);
  const lookupContacts = keys => keys.map(key => contacts.get(key)).filter(Boolean);

  function advisorStatuses(crds) {
    if (crds.length > 100 || crds.some(crd => !crdPattern.test(crd)))
      throw failure("Look up at most 100 valid advisor CRDs at a time.", 400);
    return [...new Set(crds)].map(crd => {
      if (linked[crd]) return [crd, "linked"];
      if (!directory) return [crd, "unknown"];
      const emailHash = lookup.contact_create_profiles?.[crd]?.emailHash;
      const review = claimed.has(crd) || lookup.activity_contacts?.[crd] || emails.has(emailHash);
      return [crd, review ? "review" : "unlinked"];
    });
  }

  function search(query, personOnly = false) {
    if (!directory) throw failure("ACT! contacts are unavailable until the next data refresh.", 503);
    const terms = norm(query).slice(0, 120).split(/\s+/).filter(Boolean);
    if (terms.join("").length < 2) throw failure("Enter at least two letters or numbers.", 400);
    const matches = directory.contacts.filter((row) => {
      const fields = personOnly ? [row.name, row.email] : [row.name, row.email, row.company, row.primary.city, row.primary.state];
      const searchable = norm(fields.join(" "));
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
    const candidate = lookup.contact_create_profiles?.[crd];
    const profile = candidate?.emailHash === hash(emailAddress) ? candidate : {};
    const draft = { firstName, lastName, fullName, emailAddress, company: text(approved.firm),
      customFields: { crd }, isPrivate: false };
    for (const key of ["department", "jobTitle", "businessExtension"]) {
      if (profile[key]) draft[key] = text(profile[key]);
    }
    for (const key of ["businessPhone", "mobilePhone"]) {
      const phone = phoneDigits(profile[key]);
      if (/^\d{10}$/.test(phone)) draft[key] = phone;
    }
    if (!draft.businessPhone) delete draft.businessExtension;
    if (/^https?:\/\/[^/\s]+/i.test(profile.website || "")) draft.website = text(profile.website);
    if (addresses.usable(profile.businessAddress)) draft.businessAddress = addresses.clean(profile.businessAddress);
    if (approved.greetingName) draft.salutation = text(approved.greetingName);
    return draft;
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
    draft.customFields.email__y_n = await mailCodeFor(crd, draft.emailAddress);
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
        const problems = [];
        for (const [key, label] of Object.entries(verifiedFields)) {
          if (!state.draft[key]) continue;
          const normalize = key.endsWith("Phone") ? phoneDigits : text;
          if (normalize(record[key]) !== normalize(state.draft[key])) problems.push(label);
        }
        if (state.draft.businessAddress && !addresses.sameAddress(record.businessAddress, state.draft.businessAddress))
          problems.push("Business address");
        if (text(record.customFields?.email__y_n) !== state.draft.customFields.email__y_n)
          problems.push("Mail Code");
        return { status: "added", verified: true,
          ...(record.businessAddress && addresses.usable(record.businessAddress)
            ? { primary: addresses.clean(record.businessAddress) } : {}),
          ...(problems.length ? { unverifiedFields: problems } : {}) };
      } catch {
        throw failure("ACT! did not confirm the result. Check the contact there before trying again.", 502);
      }
    });
  }
  return { search, lookupContacts, advisorStatuses, preview, change };
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
