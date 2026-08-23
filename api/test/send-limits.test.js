"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const core = require("../shared/email-core");

function cfgWith(env) {
  const saved = { ...process.env };
  Object.assign(process.env, env);
  const c = core.config();
  process.env = saved;
  return c;
}

test("a batch over the per-send limit is refused for sending but not for drafts", () => {
  // The exact case that reached a confirmation dialog before failing: 48
  // recipients with the limit at 25.
  const cfg = cfgWith({ EMAIL_DIRECT_BATCH_MAX: "25", EMAIL_EXTERNAL_24H_LIMIT: "25" });
  const drafts = core.guardrail(48, "drafts", cfg);
  const send = core.guardrail(48, "send", cfg);
  assert.equal(drafts.blocked, false, "drafts are not limited by the send cap");
  assert.equal(send.blocked, true);
  assert.match(send.message, /limited to 25 recipients per batch/);
});

test("the per-send limit is a hard edge, not a soft one", () => {
  // Nothing splits a batch across days. It is refused whole, so the boundary
  // has to be exact.
  const cfg = cfgWith({ EMAIL_DIRECT_BATCH_MAX: "25" });
  assert.equal(core.guardrail(25, "send", cfg).blocked, false, "25 is allowed");
  assert.equal(core.guardrail(26, "send", cfg).blocked, true, "26 is not");
});

test("the client's blocking reason matches what the server would do", () => {
  // The composer disables Approve & Send using its own copy of the rule. If the
  // two drift, a rep is either blocked from something legal or walked into a
  // failure again.
  const src = require("fs").readFileSync(
    require.resolve("../../webapp/email.js"), "utf8");
  const fn = src.split("function sendBlockedReason")[1].split(String.fromCharCode(10) + "  }")[0];
  assert.match(fn, /lim\.directBatchMax && n > lim\.directBatchMax/,
    "same comparison as guardrail: strictly greater than");
  assert.match(fn, /externalCount/,
    "the rolling window counts EXTERNAL recipients, not all of them");
  assert.match(fn, /directSendAvailable/, "and respects the kill switch");
});

test("internal recipients do not consume the rolling window", () => {
  const cfg = cfgWith({ EMAIL_INTERNAL_DOMAINS: "eicatlanta.com" });
  assert.equal(core.isExternal("colleague@eicatlanta.com", cfg), false);
  assert.equal(core.isExternal("advisor@morganstanley.com", cfg), true);
});
