"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const sweeper = require("../email-reply-sweep/index");
const reply = require("../shared/email-reply");
const advisors = require("../shared/advisor-lookup");

const UNIVERSE = {
  byEmail: { "advisor@ml.com": "111", "other@ubs.com": "222" },
  ambiguous: ["johnsonc@stifel.com"],
  byDomain: { "ml.com": "7691", "stifel.com": "793" },
  contentHash: "hash-1",
};

function message(over = {}) {
  return {
    id: "graph-1", subject: "RE: EIC All-Cap Value",
    from: { emailAddress: { address: "advisor@ml.com" } },
    toRecipients: [{ emailAddress: { address: "rep@eicatlanta.com" } }],
    receivedDateTime: "2026-08-22T10:00:00Z",
    conversationId: "conv-1", internetMessageId: "<in-1@ml.com>",
    internetMessageHeaders: [], isDraft: false, ...over,
  };
}

function fixture(over = {}) {
  const activity = [], seen = [], state = {};
  const context = { log: Object.assign(() => {}, { error: () => {}, warn: () => {} }) };
  const index = advisors.useIndex(UNIVERSE);
  const deps = {
    auth: { tokenFor: async () => ({ accessToken: "t", mailboxId: "u1" }) },
    advisors: { load: async () => index, classifyAddress: advisors.classifyAddress },
    graph: { recentMail: async () => ({ items: [message()], truncated: false }) },
    bounce: { looksLikeNdr: () => false },
    reply,
    store: {
      listConnections: async () => [{ userId: "u1", mailbox: "rep@eicatlanta.com" }],
      getSweepState: async (u) => (state.byUser && state.byUser[u])
        || (u === "u1" ? state.current : null) || null,
      putSweepState: async (u, _s, v) => {
        state.written = v;
        state.writes = state.writes || [];
        state.writes.push({ userId: u, value: v });
        state.byUser = state.byUser || {};
        state.byUser[u] = {
          ...(state.byUser[u] || (u === "u1" ? state.current : null) || {}), ...v,
        };
        if (u === "u1") state.current = state.byUser[u];
      },
      replyAlreadySeen: async (_u, id) => seen.includes(id),
      markReplySeen: async (_u, id, outcome) => seen.push(id, outcome),
      recordActivity: async (e) => { activity.push(e); return e; },
      sentByInternetId: async () => new Map(),
      sentByConversation: async () => new Map(),
    },
    ...over,
  };
  return { deps, activity, seen, state, context };
}

test.beforeEach(() => {
  process.env.EMAIL_REPLY_SWEEP_ENABLED = "1";
  delete process.env.EMAIL_REPLY_SWEEP_USER_IDS;
});
test.afterEach(() => {
  delete process.env.EMAIL_REPLY_SWEEP_ENABLED;
  delete process.env.EMAIL_REPLY_SWEEP_USER_IDS;
  advisors.reset();
});

test("the sweep does nothing at all unless it is explicitly enabled", async () => {
  delete process.env.EMAIL_REPLY_SWEEP_ENABLED;
  const f = fixture();
  assert.deepEqual(await sweeper.sweep(f.context, f.deps), []);
  assert.equal(f.activity.length, 0);
});

test("a reply on a known thread is credited to the campaign it answers", async () => {
  const f = fixture();
  f.deps.store.sentByConversation = async () => new Map([["conv-1",
    { id: "msg-9", batchId: "batch-3", recipientEmail: "advisor@ml.com", contactId: "111" }]]);
  await sweeper.sweep(f.context, f.deps);
  assert.equal(f.activity.length, 1);
  assert.equal(f.activity[0].route, "thread_match");
  assert.equal(f.activity[0].batchId, "batch-3");
  assert.equal(f.activity[0].advisorCrd, "111");
  assert.equal(f.activity[0].classification, "reply");
});

test("a reply we cannot thread is recorded WITHOUT claiming a campaign", async () => {
  const f = fixture();                       // no sent index at all
  await sweeper.sweep(f.context, f.deps);
  assert.equal(f.activity.length, 1);
  assert.equal(f.activity[0].route, "sender_only");
  assert.equal(f.activity[0].batchId, "", "a sighting must never borrow a campaign");
  assert.equal(f.activity[0].campaignMessageId, "");
});

test("References matches a message id when the conversation is unknown", async () => {
  const f = fixture();
  f.deps.graph.recentMail = async () => ({ truncated: false, items: [message({
    conversationId: "conv-unknown",
    internetMessageHeaders: [{ name: "In-Reply-To", value: "<sent-7@eic.com>" }] })] });
  f.deps.store.sentByInternetId = async () => new Map([["sent-7@eic.com",
    { id: "msg-7", batchId: "batch-7", recipientEmail: "advisor@ml.com", contactId: "111" }]]);
  await sweeper.sweep(f.context, f.deps);
  assert.equal(f.activity[0].route, "references_match");
  assert.equal(f.activity[0].batchId, "batch-7");
});

test("an out-of-office is never counted as a reply", async () => {
  const f = fixture();
  f.deps.graph.recentMail = async () => ({ truncated: false, items: [message({
    subject: "Automatic reply: EIC All-Cap Value",
    internetMessageHeaders: [{ name: "X-Auto-Response-Suppress", value: "All" }] })] });
  const [summary] = await sweeper.sweep(f.context, f.deps);
  assert.equal(f.activity[0].classification, "auto_reply");
  assert.equal(summary.replies, 0);
  assert.equal(summary.autoReplies, 1);
});

test("Auto-Submitted: no is a human and still counts", async () => {
  const f = fixture();
  f.deps.graph.recentMail = async () => ({ truncated: false, items: [message({
    internetMessageHeaders: [{ name: "Auto-Submitted", value: "no" }] })] });
  const [summary] = await sweeper.sweep(f.context, f.deps);
  assert.equal(summary.replies, 1);
});

test("a delivery report is classified as a bounce, not a reply", async () => {
  const f = fixture();
  f.deps.bounce.looksLikeNdr = () => true;
  const [summary] = await sweeper.sweep(f.context, f.deps);
  assert.equal(f.activity[0].classification, "bounce");
  assert.equal(summary.replies, 0);
});

test("mail from outside the advisor universe leaves no trace whatsoever", async () => {
  const f = fixture();
  f.deps.graph.recentMail = async () => ({ truncated: false, items: [
    message({ from: { emailAddress: { address: "hr@eicatlanta.com" } } })] });
  const [summary] = await sweeper.sweep(f.context, f.deps);
  assert.equal(f.activity.length, 0, "a non-advisor message must never be stored");
  assert.equal(summary.ours, 0);
  assert.equal(summary.scanned, 1);
});

test("an address several advisors share is recorded, but never attributed to one", async () => {
  const f = fixture();
  f.deps.graph.recentMail = async () => ({ truncated: false, items: [
    message({ from: { emailAddress: { address: "johnsonc@stifel.com" } } })] });
  const [summary] = await sweeper.sweep(f.context, f.deps);
  assert.equal(summary.ambiguous, 1);
  assert.equal(f.activity[0].advisorCrd, "", "picking one of four would be a fabrication");
  assert.equal(f.activity[0].advisorEmail, "johnsonc@stifel.com");
});

test("an unknown address at a known firm is advisor traffic, not noise", async () => {
  const f = fixture();
  f.deps.graph.recentMail = async () => ({ truncated: false, items: [
    message({ from: { emailAddress: { address: "someone.new@ml.com" } } })] });
  await sweeper.sweep(f.context, f.deps);
  assert.equal(f.activity.length, 1);
  assert.equal(f.activity[0].firmCrd, "7691");
  assert.equal(f.activity[0].advisorCrd, "");
});

test("the rep's own Outlook send is recorded as outbound against the advisor", async () => {
  const f = fixture();
  f.deps.graph.recentMail = async () => ({ truncated: false, items: [message({
    from: { emailAddress: { address: "rep@eicatlanta.com" } },
    toRecipients: [{ emailAddress: { address: "advisor@ml.com" } },
                   { emailAddress: { address: "other@ubs.com" } }] })] });
  const [summary] = await sweeper.sweep(f.context, f.deps);
  assert.equal(summary.outbound, 1);
  assert.equal(f.activity.length, 2, "one row per advisor on the message");
  assert.deepEqual(f.activity.map((a) => a.advisorCrd).sort(), ["111", "222"]);
  assert.equal(f.activity[0].direction, "outbound");
  assert.equal(f.activity[0].source, "outlook");
});

test("no activity row ever carries a message body", async () => {
  const f = fixture();
  f.deps.graph.recentMail = async () => ({ truncated: false, items: [
    message({ body: { content: "<p>secret</p>" }, bodyPreview: "secret" })] });
  await sweeper.sweep(f.context, f.deps);
  for (const key of ["body", "bodyPreview", "uniqueBody", "attachments"])
    assert.ok(!(key in f.activity[0]), `${key} must never reach storage`);
});

test("a message already seen is not recorded twice", async () => {
  const f = fixture();
  f.deps.store.replyAlreadySeen = async () => true;
  await sweeper.sweep(f.context, f.deps);
  assert.equal(f.activity.length, 0);
});

/* THE DEADLOCK THIS REPLACES.
 *
 * The old rule was "never advance past a truncated window", which sounds safe
 * and was not: combined with a newest-first read it meant a busy mailbox
 * returned the same newest pages every run, the older mail behind them was
 * never reached, and the watermark could never move because the window never
 * completed. A sweep that looked healthy and silently never found those
 * replies.
 *
 * Messages now arrive OLDEST first, so a truncated pass is a contiguous block
 * from the watermark forward. Advancing to the last message actually processed
 * is safe, and is the only thing that guarantees progress. */

test("a truncated window still advances to what it actually processed", async () => {
  const f = fixture();
  f.deps.graph.recentMail = async () => ({
    items: [message({ receivedDateTime: "2026-08-22T09:00:00Z" })], truncated: true });
  const [summary] = await sweeper.sweep(f.context, f.deps);
  assert.equal(f.state.written.watermarkUtc, "2026-08-22T09:00:00Z",
    "without this a busy mailbox never gets past its newest page");
  assert.equal(summary.truncated, true, "and the caller still knows there is more");
  assert.equal(f.state.written.lastError, "",
    "a successful oldest-first page is backlog, not a failure");
  assert.equal(f.state.written.consecutiveFailures, 0);
});

test("an ERROR mid-pass does not advance the watermark", async () => {
  const f = fixture();
  // A message that fails to record leaves a hole, so the block is no longer
  // contiguous and moving past it would lose that message for good.
  f.deps.store.recordActivity = async () => { throw new Error("storage down"); };
  await sweeper.sweep(f.context, f.deps);
  assert.equal(f.state.written.watermarkUtc, undefined);
  assert.match(String(f.state.written.lastError), /errors/);
  assert.equal(f.state.written.consecutiveFailures, 1,
    "one failed pass increments health once, regardless of failed messages");
});

test("the watermark advances on a clean pass", async () => {
  const f = fixture();
  await sweeper.sweep(f.context, f.deps);
  assert.ok(f.state.written.watermarkUtc, "a clean pass must move forward");
  assert.equal(f.state.written.lastError, "");
  assert.equal(f.state.written.lookupHash, "hash-1");
});

test("a rep needing to reconnect is skipped LOUDLY, not silently", async () => {
  const f = fixture();
  f.deps.auth.tokenFor = async () => {
    const e = new Error("reconnect"); e.code = "graph_reconnect_required"; throw e;
  };
  const [summary] = await sweeper.sweep(f.context, f.deps);
  assert.equal(summary.skipped, "graph_reconnect_required");
  assert.equal(f.state.written.lastError, "graph_reconnect_required",
    "a lapsed token must be visible; their screens still say 'no reply recorded'");
});

test("the optional user allowlist makes global enablement a real mailbox canary", async () => {
  process.env.EMAIL_REPLY_SWEEP_USER_IDS = "U2";
  const f = fixture();
  f.deps.store.listConnections = async () => [
    { userId: "u1", mailbox: "one@eicatlanta.com" },
    { userId: "u2", mailbox: "two@eicatlanta.com" },
  ];
  const summaries = await sweeper.sweep(f.context, f.deps);
  assert.deepEqual(summaries.map((row) => row.userId), ["u2"]);
  assert.equal(f.activity.length, 1);
  assert.equal(f.activity[0].userId, "u2");
});

test("an unmatched canary allowlist performs no mailbox or advisor reads", async () => {
  process.env.EMAIL_REPLY_SWEEP_USER_IDS = "not-connected";
  const f = fixture();
  let loaded = false;
  f.deps.advisors.load = async () => { loaded = true; throw new Error("must not load"); };
  assert.deepEqual(await sweeper.sweep(f.context, f.deps), []);
  assert.equal(loaded, false);
  assert.equal(f.activity.length, 0);
});

test("authentication failures accumulate from sweep state, not the connection", async () => {
  const f = fixture();
  f.deps.auth.tokenFor = async () => {
    const e = new Error("reconnect"); e.code = "graph_reconnect_required"; throw e;
  };
  await sweeper.sweep(f.context, f.deps);
  await sweeper.sweep(f.context, f.deps);
  assert.equal(f.state.current.consecutiveFailures, 2,
    "a prolonged token lapse must not report as one isolated failure forever");
});

test("a failure outside the message loop is persisted for that mailbox", async () => {
  const f = fixture();
  f.deps.graph.recentMail = async () => { throw new Error("Graph unavailable"); };
  const [summary] = await sweeper.sweep(f.context, f.deps);
  assert.match(summary.failed, /Graph unavailable/);
  assert.equal(f.state.current.lastError, "mailbox_sweep_failed");
  assert.equal(f.state.current.consecutiveFailures, 1);
});

test("the sweep aborts rather than treating everyone as a stranger", async () => {
  const f = fixture();
  f.deps.advisors.load = async () => { throw new Error("blob unavailable"); };
  const summaries = await sweeper.sweep(f.context, f.deps);
  assert.deepEqual(summaries, [], "failing open would mark every message seen and lose it");
  assert.equal(f.activity.length, 0);
  assert.equal(f.state.current.lastError, "advisor_lookup_unavailable",
    "a global prerequisite outage must not leave the mailbox looking healthy");
  assert.equal(f.state.current.consecutiveFailures, 1);
});

test("an advisor-index outage marks every affected connection", async () => {
  const f = fixture();
  f.deps.store.listConnections = async () => [
    { userId: "u1", mailbox: "one@eicatlanta.com" },
    { userId: "u2", mailbox: "two@eicatlanta.com" },
  ];
  f.deps.advisors.load = async () => { throw new Error("blob unavailable"); };
  await sweeper.sweep(f.context, f.deps);
  assert.deepEqual(f.state.writes.map((w) => w.userId).sort(), ["u1", "u2"]);
  assert.equal(f.state.byUser.u1.lastError, "advisor_lookup_unavailable");
  assert.equal(f.state.byUser.u2.consecutiveFailures, 1);
});

test("subject text alone never creates a campaign link", () => {
  // The rule this encodes: "RE: <our subject>" is evidence of nothing. Two
  // advisors can be sent the same campaign and one can forward it to a third.
  const matched = reply.match(message({ conversationId: "", internetMessageHeaders: [] }),
    { sent: new Map(), byConversation: new Map() });
  assert.equal(matched.route, "sender_only");
  assert.equal(matched.message, null);
});

test("an app-sent campaign is not shown as though the rep typed it in Outlook", async () => {
  const f = fixture();
  f.deps.graph.recentMail = async () => ({ truncated: false, items: [message({
    from: { emailAddress: { address: "rep@eicatlanta.com" } },
    toRecipients: [{ emailAddress: { address: "advisor@ml.com" } }],
    internetMessageHeaders: [{ name: "X-EIC-Message-Id", value: "msg-42" }] })] });
  await sweeper.sweep(f.context, f.deps);
  assert.equal(f.activity[0].source, "app");
  assert.equal(f.activity[0].campaignMessageId, "msg-42",
    "our own send links straight back to the message record that made it");
});

test("a genuinely manual Outlook send is still labelled outlook", async () => {
  const f = fixture();
  f.deps.graph.recentMail = async () => ({ truncated: false, items: [message({
    from: { emailAddress: { address: "rep@eicatlanta.com" } },
    toRecipients: [{ emailAddress: { address: "advisor@ml.com" } }],
    internetMessageHeaders: [] })] });
  await sweeper.sweep(f.context, f.deps);
  assert.equal(f.activity[0].source, "outlook");
  assert.equal(f.activity[0].campaignMessageId, "");
});

test("an advisor CC'd on a rep's mail is contact, tagged as a cc", async () => {
  const f = fixture();
  f.deps.graph.recentMail = async () => ({ truncated: false, items: [message({
    from: { emailAddress: { address: "rep@eicatlanta.com" } },
    toRecipients: [{ emailAddress: { address: "advisor@ml.com" } }],
    ccRecipients: [{ emailAddress: { address: "other@ubs.com" } }] })] });
  await sweeper.sweep(f.context, f.deps);
  const byCrd = Object.fromEntries(f.activity.map((a) => [a.advisorCrd, a]));
  assert.equal(byCrd["111"].recipientRole, "to");
  assert.equal(byCrd["222"].recipientRole, "cc");
  // Copying somebody is nearly always copying their practice, and touching the
  // team is what counts -- so it is recorded as contact, merely labelled.
  assert.equal(byCrd["222"].direction, "outbound");
  assert.equal(byCrd["222"].classification, "sent");
});

/* ---- our own people are never tracked ------------------------------------ */

test("mail from a colleague leaves no trace, even if the blob still lists them", async () => {
  const f = fixture();
  // Deliberately IN the universe: this is the stale-blob case, where somebody
  // was added to EMAIL_INTERNAL_DOMAINS after the last export ran.
  advisors.useIndex({ ...UNIVERSE,
    byEmail: { ...UNIVERSE.byEmail, "colleague@eicatlanta.com": "999" } });
  f.deps.advisors.load = async () => advisors.useIndex({ ...UNIVERSE,
    byEmail: { ...UNIVERSE.byEmail, "colleague@eicatlanta.com": "999" } });
  f.deps.graph.recentMail = async () => ({ truncated: false, items: [
    message({ from: { emailAddress: { address: "colleague@eicatlanta.com" } } })] });
  const [summary] = await sweeper.sweep(f.context, f.deps);
  assert.equal(f.activity.length, 0,
    "the timeline is firm-wide; a colleague's mail is nobody else's business");
  assert.equal(summary.ours, 0);
});

test("a colleague CC'd on a rep's advisor mail is dropped, the advisor is kept", async () => {
  const f = fixture();
  const idx = { ...UNIVERSE, byEmail: { ...UNIVERSE.byEmail, "colleague@eicatlanta.com": "999" } };
  f.deps.advisors.load = async () => advisors.useIndex(idx);
  f.deps.graph.recentMail = async () => ({ truncated: false, items: [message({
    from: { emailAddress: { address: "rep@eicatlanta.com" } },
    toRecipients: [{ emailAddress: { address: "advisor@ml.com" } }],
    ccRecipients: [{ emailAddress: { address: "colleague@eicatlanta.com" } }] })] });
  await sweeper.sweep(f.context, f.deps);
  assert.equal(f.activity.length, 1);
  assert.equal(f.activity[0].advisorCrd, "111");
});

/* ---- the backfill boundary ----------------------------------------------- */

test("a reply that arrives DURING a backfill is not seeded away", async () => {
  const f = fixture();
  const backfillUntil = "2026-08-20T00:00:00Z";
  f.state.current = { watermarkUtc: "2025-09-01T00:00:00Z", backfillUntilUtc: backfillUntil };
  const seeds = [];
  f.deps.engagement = { refresh: async (_u, crd, o) => { seeds.push({ crd, seed: !!o.seed }); } };
  f.deps.graph.recentMail = async () => ({ truncated: false, items: [
    // history: predates the moment the backfill was asked for
    message({ id: "old", receivedDateTime: "2025-10-01T00:00:00Z",
              from: { emailAddress: { address: "advisor@ml.com" } } }),
    // arrived while the sweep was still catching up -- REAL new work
    message({ id: "new", receivedDateTime: "2026-08-21T00:00:00Z",
              from: { emailAddress: { address: "other@ubs.com" } } })] });
  await sweeper.sweep(f.context, f.deps);
  const byCrd = Object.fromEntries(seeds.map((s) => [s.crd, s.seed]));
  assert.equal(byCrd["111"], true, "a year of history must not arrive as a year of work");
  assert.equal(byCrd["222"], false,
    "but a reply that came in while we were catching up is work, and must surface");
});

test("with no backfill running, nothing is ever seeded", async () => {
  const f = fixture();
  const seeds = [];
  f.deps.engagement = { refresh: async (_u, crd, o) => { seeds.push(!!o.seed); } };
  await sweeper.sweep(f.context, f.deps);
  assert.deepEqual(seeds, [false]);
});
