"use strict";
const assert = require("assert");
const core = require("../shared/email-core");

const withEnv = (env, fn) => {
  const saved = {};
  for (const k of Object.keys(env)) { saved[k] = process.env[k];
    if (env[k] === undefined) delete process.env[k]; else process.env[k] = env[k]; }
  try { return fn(); } finally {
    for (const k of Object.keys(saved)) {
      if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; }
  }
};

const doc = [{ id: "d1", name: "Case for Value.pdf", size: 10 }];

withEnv({ EMAIL_MATERIAL_BCC: undefined, EMAIL_INTERNAL_DOMAINS: undefined }, () => {
  // The default is on: nobody has to set an app setting for a compliance rule.
  assert.deepStrictEqual(
    core.complianceBcc({ recipientEmail: "adv@lpl.com", attachments: doc }),
    ["mktgmaterial@eicatlanta.com"]);

  // No attachment: nothing left the firm that needs retaining.
  assert.deepStrictEqual(core.complianceBcc({ recipientEmail: "adv@lpl.com", attachments: [] }), []);
  assert.deepStrictEqual(core.complianceBcc({ recipientEmail: "adv@lpl.com" }), []);

  // Internal recipient -- this is what testing on yourself looks like.
  assert.deepStrictEqual(
    core.complianceBcc({ recipientEmail: "rep@eicatlanta.com", attachments: doc }), []);

  // Never blind-copy the desk on its own mail.
  assert.deepStrictEqual(
    core.complianceBcc({ recipientEmail: "MktgMaterial@eicatlanta.com", attachments: doc }), []);
});

// Explicitly switchable off, and a malformed value fails closed rather than
// putting garbage in bccRecipients and failing every draft.
withEnv({ EMAIL_MATERIAL_BCC: "" }, () =>
  assert.deepStrictEqual(core.complianceBcc({ recipientEmail: "a@lpl.com", attachments: doc }), []));
withEnv({ EMAIL_MATERIAL_BCC: "not an address" }, () =>
  assert.deepStrictEqual(core.complianceBcc({ recipientEmail: "a@lpl.com", attachments: doc }), []));
withEnv({ EMAIL_MATERIAL_BCC: "Archive@Example.COM" }, () =>
  assert.deepStrictEqual(core.complianceBcc({ recipientEmail: "a@lpl.com", attachments: doc }),
    ["archive@example.com"]));

// Signature: title sits between name and company, and exactly ONE number shows.
const sig = (p) => core.corporateSignature(p, "");
const desk = sig({ displayName: "Jane Rep", jobTitle: "Portfolio Specialist",
  mail: "jane@eicatlanta.com", businessPhones: ["404-555-0100"], mobilePhone: "404-555-0199" });
assert.ok(sig({ displayName: "Jane Rep", jobTitle: "Portfolio Specialist" })
  .indexOf("Portfolio Specialist") < sig({ displayName: "Jane Rep", jobTitle: "Portfolio Specialist" })
  .indexOf("Equity Investment Corporation"), "job title must precede the company");
assert.ok(desk.includes("T 404-555-0100"), "business phone wins");
assert.ok(!desk.includes("404-555-0199"), "the mobile must not appear alongside it");

const cell = sig({ displayName: "Jane Rep", businessPhones: [], mobilePhone: "404-555-0199" });
assert.ok(cell.includes("M 404-555-0199"), "mobile is the fallback, labelled as a mobile");

const none = sig({ displayName: "Jane Rep" });
assert.ok(!/[TM] \d/.test(none), "no phone line at all when neither is on record");

console.log("email-bcc.test.js ok");


// ---------------------------------------------------------------------------
// Copy me / copy a colleague.
// ---------------------------------------------------------------------------
withEnv({ EMAIL_INTERNAL_RECIPIENTS:
    "kate@eicatlanta.com; Will Smith <will@eicatlanta.com>; not-an-address" }, () => {
  const cfg = core.config();
  const sender = { mail: "bo@eicatlanta.com" };
  const msg = { recipientEmail: "dana@advisorfirm.com", attachments: [{ id: "x" }] };

  // The App Setting parses both forms and rejects anything that is not an address.
  assert.deepStrictEqual(cfg.internalRecipients.map((r) => r.address),
    ["kate@eicatlanta.com", "will@eicatlanta.com"]);
  assert.strictEqual(cfg.internalRecipients[0].name, "kate@eicatlanta.com",
    "a bare address labels itself, rather than a name being guessed from it");
  assert.strictEqual(cfg.internalRecipients[1].name, "Will Smith");

  // OFF by default. Only the compliance blind copy, which is not a preference.
  assert.deepStrictEqual(core.extraRecipients(msg, {}, sender, cfg),
    { cc: [], bcc: ["mktgmaterial@eicatlanta.com"] });

  // cc and bcc are separate choices and land in separate buckets.
  assert.deepStrictEqual(core.extraRecipients(msg, { copySelf: "cc" }, sender, cfg).cc,
    ["bo@eicatlanta.com"]);
  assert.ok(core.extraRecipients(msg, { copySelf: "bcc" }, sender, cfg).bcc
    .includes("bo@eicatlanta.com"));

  // AN ADDRESS OFF THE LIST IS REFUSED. A preference saved months ago cannot
  // grant a permission the App Setting no longer gives.
  assert.deepStrictEqual(
    core.extraRecipients(msg, { copyInternal: "cc", copyInternalTo: "stranger@elsewhere.com" },
      sender, cfg).cc, [],
    "an address not on the allowlist is dropped, not copied");
  assert.deepStrictEqual(
    core.extraRecipients(msg, { copyInternal: "cc", copyInternalTo: "will@eicatlanta.com" },
      sender, cfg).cc, ["will@eicatlanta.com"]);

  // Nobody is copied who is already the recipient.
  assert.deepStrictEqual(
    core.extraRecipients({ ...msg, recipientEmail: "will@eicatlanta.com" },
      { copyInternal: "cc", copyInternalTo: "will@eicatlanta.com" }, sender, cfg).cc, []);

  // The compliance copy is unaffected by any of it.
  const both = core.extraRecipients(msg,
    { copySelf: "bcc", copyInternal: "cc", copyInternalTo: "kate@eicatlanta.com" }, sender, cfg);
  assert.ok(both.bcc.includes("mktgmaterial@eicatlanta.com"),
    "a rep's preferences never displace the compliance blind copy");
});

console.log("email-bcc.test.js copy-me/copy-a-colleague ok");

// ---------------------------------------------------------------------------
// Per-batch copies: the advisor's teammates, and an EIC colleague.
// ---------------------------------------------------------------------------
withEnv({ EMAIL_INTERNAL_RECIPIENTS: "kate@eicatlanta.com",
          EMAIL_MATERIAL_BCC: "" }, () => {
  const cfg = core.config();
  const msg = {
    recipientEmail: "dana@advisorfirm.com",
    attachments: [],
    // As stored on the message: already suppression-filtered at batch creation,
    // but still containing the recipient herself and a duplicate.
    teammateCc: ["mate@advisorfirm.com", "dana@advisorfirm.com", "mate@advisorfirm.com"],
  };

  const out = core.extraRecipients(msg, {}, {}, cfg);
  assert.deepStrictEqual(out.cc, ["mate@advisorfirm.com"],
    "the recipient is not copied on her own message, and no address appears twice");

  // Teammates are CC, never BCC: a hidden copy of a client-facing email is a
  // different thing from a visible one, and the advisor may reply to all.
  assert.deepStrictEqual(out.bcc, []);

  const withColleague = core.extraRecipients(msg, { ccColleague: "kate@eicatlanta.com" }, {}, cfg);
  assert.deepStrictEqual(withColleague.cc,
    ["mate@advisorfirm.com", "kate@eicatlanta.com"]);

  // A colleague address is allowlisted when the BATCH is built; extraRecipients
  // trusts that decision, which is why createBatch re-checks it rather than
  // storing whatever arrived.
  assert.ok(cfg.internalRecipients.some((r) => r.address === "kate@eicatlanta.com"));
});

console.log("email-bcc.test.js per-batch copies ok");


// ---------------------------------------------------------------------------
// A teammate copy replaces the direct send, and the cycle resolves.
// ---------------------------------------------------------------------------
{
  // Same walk createBatch does via dropCopiedRecipients(): first in the list
  // wins, and anyone a KEPT recipient copies is removed from the direct sends.
  const walk = (recipients) => {
    const kept = [], removed = [], copied = new Set();
    for (const r of recipients) {
      if (r.email && copied.has(r.email)) { removed.push(r); continue; }
      kept.push(r);
      for (const a of r.teammates || []) copied.add(a);
    }
    return { kept: kept.map((r) => r.email), removed: removed.map((r) => r.email) };
  };

  // Two advisors on one practice. One message, not two: she is written to and
  // he is copied, rather than each of them receiving the batch twice.
  assert.deepStrictEqual(
    walk([{ email: "a@f.com", teammates: ["b@f.com"] },
          { email: "b@f.com", teammates: ["a@f.com"] }]),
    { kept: ["a@f.com"], removed: ["b@f.com"] });

  // THE CYCLE IS THE POINT OF THE ORDERING. Three mutual teammates must not
  // each remove the others and leave the batch with nobody in it.
  assert.deepStrictEqual(
    walk([{ email: "a@f.com", teammates: ["b@f.com", "c@f.com"] },
          { email: "b@f.com", teammates: ["a@f.com", "c@f.com"] },
          { email: "c@f.com", teammates: ["a@f.com", "b@f.com"] }]),
    { kept: ["a@f.com"], removed: ["b@f.com", "c@f.com"] });

  // Somebody nobody copies is untouched.
  assert.deepStrictEqual(
    walk([{ email: "a@f.com", teammates: ["x@f.com"] },
          { email: "z@g.com", teammates: [] }]),
    { kept: ["a@f.com", "z@g.com"], removed: [] });
}

console.log("email-bcc.test.js teammate copy replaces direct send ok");
