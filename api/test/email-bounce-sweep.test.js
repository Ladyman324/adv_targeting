"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const sweeper = require("../email-bounce-sweep/index");
const bounce = require("../shared/email-bounce");

const context = { log: Object.assign(() => {}, { error: () => {}, warn: () => {} }) };

function fixture(inboxItems) {
  const suppressed = [], audits = [], seen = [], actCalls = [], patched = [], refreshed = [], events = [];
  return {
    suppressed, audits, seen, actCalls, patched, refreshed, events,
    connection: { userId: "user-1", mailbox: "rep@eicatlanta.com", needsReconnect: false },
    deps: {
      auth: { tokenFor: async () => ({ accessToken: "t" }) },
      graph: { recentInbox: async () => inboxItems },
      bounce,
      store: {
        sentByInternetId: async () => new Map([["orig-1@eicatlanta.com",
          { id: "m1", batchId: "b1", recipientEmail: "a.white@ms.com", contactId: "1000084", etag: "e1" }]]),
        bounceAlreadySeen: async (_u, id) => seen.some((s) => s.id === id),
        markBounceSeen: async (_u, id, outcome) => seen.push({ id, outcome }),
        suppressEmail: async (address, info) => suppressed.push({ address, ...info }),
        patchMessage: async (_u, _b, id, patch) => { patched.push({ id, patch }); return {}; },
        audit: async (...a) => audits.push(a),
        recordDeliveryEvent: async (u, e) => events.push({ userId: u, ...e }),
      },
      act: { markHardBounce: async (...a) => { actCalls.push(a); return { ok: true }; } },
      recipientRegistry: { verifyActPair: async () => ({ actContactId: "act-1000084" }) },
      core: require("../shared/email-core"),
      refreshBatch: async () => { refreshed.push(1); },
    },
  };
}

const hardNdr = {
  id: "ndr-1",
  from: { emailAddress: { address: "postmaster@ms.com" } },
  subject: "Undeliverable: Quarterly Commentary",
  internetMessageHeaders: [{ name: "References", value: "<orig-1@eicatlanta.com>" }],
  body: { contentType: "text",
    content: "Final-Recipient: rfc822; a.white@ms.com\nStatus: 5.1.1\nAction: failed" },
};

test("a hard bounce suppresses the address and reaches Act!", async () => {
  const f = fixture([hardNdr]);
  const s = await sweeper.sweepMailbox(f.connection, context, f.deps);
  assert.equal(s.suppressed, 1);
  assert.equal(f.suppressed[0].address, "a.white@ms.com");
  assert.equal(f.suppressed[0].kind, "hard_bounce");
  assert.equal(f.actCalls.length, 1);
  assert.equal(f.actCalls[0][0], "1000084", "the CRD travels to Act!");
  assert.equal(f.actCalls[0][3], "act-1000084", "the independently approved Act GUID travels too");
  assert.ok(f.audits.some((a) => a.includes("hard_bounce_suppressed")));
  assert.equal(f.patched[0].patch.bounceKind, "hard");
});

test("a soft bounce suppresses nothing", async () => {
  const f = fixture([{ ...hardNdr, body: { contentType: "text", content: "Status: 4.4.7" } }]);
  const s = await sweeper.sweepMailbox(f.connection, context, f.deps);
  assert.equal(s.suppressed, 0);
  assert.equal(f.suppressed.length, 0);
  assert.equal(f.actCalls.length, 0);
});

test("a mailbox-full report suppresses nothing", async () => {
  const f = fixture([{ ...hardNdr, body: { contentType: "text",
    content: "Final-Recipient: rfc822; a.white@ms.com\nStatus: 5.2.2" } }]);
  await sweeper.sweepMailbox(f.connection, context, f.deps);
  assert.equal(f.suppressed.length, 0, "permanent, but not the address's fault");
});

test("the same report is not processed twice", async () => {
  const f = fixture([hardNdr]);
  await sweeper.sweepMailbox(f.connection, context, f.deps);
  await sweeper.sweepMailbox(f.connection, context, f.deps);
  assert.equal(f.suppressed.length, 1, "second sweep must be a no-op");
});

test("a failed suppression is left unmarked so the next sweep retries it", async () => {
  const f = fixture([hardNdr]);
  f.deps.store.suppressEmail = async () => { throw new Error("storage down"); };
  const s = await sweeper.sweepMailbox(f.connection, context, f.deps);
  assert.equal(s.errors, 1);
  assert.equal(f.seen.length, 0, "must not record it as handled");
});

test("an Act! failure does not lose the local suppression", async () => {
  const f = fixture([hardNdr]);
  f.deps.act.markHardBounce = async () => { throw new Error("Act! unreachable"); };
  const s = await sweeper.sweepMailbox(f.connection, context, f.deps);
  assert.equal(s.suppressed, 1, "the address is still suppressed here");
  assert.equal(f.seen.at(-1).outcome, "suppressed");
});

test("an unverified CRD-to-Act pair writes no bounce history", async () => {
  const f = fixture([hardNdr]);
  f.deps.recipientRegistry.verifyActPair = async () => {
    const error = new Error("Act identity changed");
    error.code = "recipient_identity_changed"; throw error;
  };
  const s = await sweeper.sweepMailbox(f.connection, context, f.deps);
  assert.equal(s.suppressed, 1);
  assert.equal(f.actCalls.length, 0);
});

test("a mailbox needing reconnection is skipped, not failed", async () => {
  const f = fixture([hardNdr]);
  f.deps.auth.tokenFor = async () => { const e = new Error("reconnect"); e.code = "graph_not_connected"; throw e; };
  const s = await sweeper.sweepMailbox(f.connection, context, f.deps);
  assert.equal(s.skipped, "graph_not_connected");
  assert.equal(f.suppressed.length, 0);
});

test("the sweep does nothing at all unless explicitly enabled", async () => {
  const saved = process.env.EMAIL_BOUNCE_SWEEP_ENABLED;
  delete process.env.EMAIL_BOUNCE_SWEEP_ENABLED;
  try {
    const calls = [];
    const out = await sweeper.sweep(context, { store: { listConnections: async () => { calls.push(1); return []; } } });
    assert.deepEqual(out, []);
    assert.equal(calls.length, 0, "must not even enumerate mailboxes");
  } finally { if (saved !== undefined) process.env.EMAIL_BOUNCE_SWEEP_ENABLED = saved; }
});

test("the mailbox is never modified", () => {
  const src = require("fs").readFileSync(require.resolve("../email-bounce-sweep/index.js"), "utf8")
    + require("fs").readFileSync(require.resolve("../shared/graph-mail.js"), "utf8")
      .split("async function recentInbox")[1].split(String.fromCharCode(10) + "}")[0];
  // No marking read, no moving, no deleting. We were lent this mailbox to send
  // from, not to tidy.
  assert.doesNotMatch(src, /isRead|\/move|DELETE.*messages/i);
});

test("a deferral is recorded but never suppresses", async () => {
  // The signal that used to be thrown away. A gateway slowing us down is the
  // earliest warning available, and it is worthless unless it is written down.
  const f = fixture([{ ...hardNdr, body: { contentType: "text",
    content: "Final-Recipient: rfc822; a.white@ms.com\nStatus: 4.7.0\nAction: delayed" } }]);
  await sweeper.sweepMailbox(f.connection, context, f.deps);
  assert.equal(f.suppressed.length, 0, "a deferral must not suppress anybody");
  assert.equal(f.events.length, 1, "but it must be recorded");
  assert.equal(f.events[0].kind, "soft");
  assert.equal(f.events[0].code, "4.7.0");
  assert.equal(f.events[0].domain, "ms.com", "attributed to the domain that deferred us");
  assert.equal(f.events[0].userId, "user-1", "and to the rep who sent it");
});

test("a policy refusal is recorded but never suppresses", async () => {
  const f = fixture([{ ...hardNdr, body: { contentType: "text",
    content: "Final-Recipient: rfc822; a.white@ms.com\nStatus: 5.7.1" } }]);
  await sweeper.sweepMailbox(f.connection, context, f.deps);
  assert.equal(f.suppressed.length, 0, "5.7.1 says nothing about the address");
  assert.equal(f.events.length, 1);
  assert.equal(f.events[0].kind, "policy");
});

test("a hard bounce is recorded as well as suppressed", async () => {
  const f = fixture([hardNdr]);
  await sweeper.sweepMailbox(f.connection, context, f.deps);
  assert.equal(f.suppressed.length, 1);
  assert.equal(f.events.length, 1);
  assert.equal(f.events[0].kind, "hard");
});

test("failing to record telemetry does not undo a suppression", async () => {
  // The statistic is worth having. It is not worth losing the thing that
  // actually protects an advisor from being mailed again.
  const f = fixture([hardNdr]);
  f.deps.store.recordDeliveryEvent = async () => { throw new Error("table unavailable"); };
  const s = await sweeper.sweepMailbox(f.connection, context, f.deps);
  assert.equal(s.suppressed, 1);
  assert.equal(f.suppressed.length, 1);
});

test("an unmatched report records nothing", async () => {
  // No sent message means no rep and no domain to attribute it to. Filing it
  // under a blank sender to keep a total tidy is how a dashboard starts lying.
  const f = fixture([{ ...hardNdr, internetMessageHeaders: [
    { name: "References", value: "<not-ours@example.com>" }] }]);
  await sweeper.sweepMailbox(f.connection, context, f.deps);
  assert.equal(f.events.length, 0);
  assert.equal(f.suppressed.length, 0);
});

/* The sweep reads the whole mailbox now, not just the Inbox, so that a rep's
 * own Outlook rules cannot hide a delivery report from it. These guard the two
 * consequences of that widening. */

test("a rep's own sent mail can never suppress an address", async () => {
  const f = fixture([{
    id: "own-1",
    from: { emailAddress: { address: "rep@eicatlanta.com" } },
    subject: "Undeliverable: Quarterly Commentary",
    internetMessageHeaders: [{ name: "References", value: "<orig-1@eicatlanta.com>" }],
    body: { contentType: "text",
      content: ["Final-Recipient: rfc822; a.white@ms.com", "Status: 5.1.1", "Action: failed"].join("\n") },
  }]);
  const s = await sweeper.sweepMailbox(f.connection, context, f.deps);
  assert.equal(f.suppressed.length, 0, "our own message is not a report about itself");
  assert.equal(s.scanned, 0, "it never even reaches the classifier");
});

test("a genuine report is still acted on whatever folder it was filed into", async () => {
  // The folder it lives in is no longer part of the query, so there is nothing
  // for a rule to hide it from. Same message, same outcome.
  const f = fixture([hardNdr]);
  const s = await sweeper.sweepMailbox(f.connection, context, f.deps);
  assert.equal(s.scanned, 1);
  assert.equal(s.suppressed, 1);
});
