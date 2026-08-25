"use strict";

/* The bulk follow-up: who is left after a campaign, and who must not be.
 *
 * The rep's rule is "everyone who did not reply". These pin the three
 * exclusions that rule does not cover, each of which is a different failure:
 *
 *   an OUT-OF-OFFICE is not a reply     following up is exactly right
 *   a BOUNCE is not a candidate         a second send buys a second bounce
 *   an OPT-OUT since the send           compliance, not preference
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const Module = require("module");

function load(stubs) {
  const target = require.resolve("../shared/email-service.js");
  delete require.cache[target];
  const realLoad = Module._load;
  Module._load = function (request, parent) {
    if (parent && parent.filename === target) {
      const key = path.basename(String(request));
      if (stubs[key]) return stubs[key];
    }
    return realLoad.apply(this, arguments);
  };
  let mod;
  try { mod = require(target); } finally { Module._load = realLoad; }
  return mod;
}

const WHO = { id: "u-1", name: "bo@eicatlanta.com" };

function build({ messages, activity = {}, blocked = [] }) {
  const store = {
    getBatch: async () => ({ id: "B1", name: "Intro", parentBatchId: "", followUpSentUtc: "",
                             attachmentIds: [], etag: "e" }),
    listMessages: async () => messages,
    listActivity: async (crd) => activity[crd] || [],
    id: () => "new-id",
  };
  const suppress = {
    blockedAmong: async (rs) => new Set(rs.map(r => r.email).filter(e => blocked.includes(e))),
    manageUrl: () => "https://x/manage",
  };
  return load({ "email-store": store, "email-suppress": suppress });
}

const msg = (over) => ({ id: "m", state: "sent", contactId: "1", recipientEmail: "a@x.com",
  recipientName: "A", graphConversationId: "conv", graphMessageId: "g", subject: "Intro",
  bounceKind: "", ...over });

test("somebody who replied comes off the list", async () => {
  const svc = build({
    messages: [msg({ id: "m1", contactId: "1", recipientEmail: "one@x.com" })],
    activity: { "1": [{ direction: "inbound", classification: "reply", conversationId: "conv" }] },
  });
  const r = await svc.followUpCandidates(WHO, "B1");
  assert.equal(r.counts.replied, 1);
  assert.equal(r.counts.remaining, 0);
});

test("an OUT-OF-OFFICE is not a reply, so they stay on the list", async () => {
  const svc = build({
    messages: [msg({ id: "m1", contactId: "1", recipientEmail: "one@x.com" })],
    activity: { "1": [{ direction: "inbound", classification: "auto_reply", conversationId: "conv" }] },
  });
  const r = await svc.followUpCandidates(WHO, "B1");
  assert.equal(r.counts.replied, 0);
  assert.equal(r.counts.remaining, 1, "an auto-responder says nothing about whether they read it");
});

test("a hard bounce is excluded rather than mailed again", async () => {
  const svc = build({ messages: [msg({ bounceKind: "hard" })] });
  const r = await svc.followUpCandidates(WHO, "B1");
  assert.equal(r.counts.bounced, 1);
  assert.equal(r.counts.remaining, 0);
});

test("somebody who opted out after the send is excluded", async () => {
  const svc = build({
    messages: [msg({ recipientEmail: "gone@x.com" })],
    blocked: ["gone@x.com"],
  });
  const r = await svc.followUpCandidates(WHO, "B1");
  assert.equal(r.counts.suppressed, 1);
  assert.equal(r.counts.remaining, 0);
});

test("a message that never sent is not a first touch", async () => {
  const svc = build({ messages: [msg({ state: "failed" }), msg({ id: "m2", state: "sent",
    contactId: "2", recipientEmail: "two@x.com" })] });
  const r = await svc.followUpCandidates(WHO, "B1");
  assert.equal(r.counts.notSent, 1);
  assert.equal(r.counts.remaining, 1);
});

test("a reply on a DIFFERENT conversation does not silence this one", async () => {
  const svc = build({
    messages: [msg({ contactId: "1", recipientEmail: "one@x.com", graphConversationId: "conv-A" })],
    activity: { "1": [{ direction: "inbound", classification: "reply",
                        conversationId: "conv-B", batchId: "OTHER" }] },
  });
  const r = await svc.followUpCandidates(WHO, "B1");
  assert.equal(r.counts.remaining, 1);
});

test("outbound activity is not mistaken for a reply", async () => {
  const svc = build({
    messages: [msg({ contactId: "1", recipientEmail: "one@x.com" })],
    activity: { "1": [{ direction: "outbound", classification: "sent", conversationId: "conv" }] },
  });
  const r = await svc.followUpCandidates(WHO, "B1");
  assert.equal(r.counts.remaining, 1);
});
