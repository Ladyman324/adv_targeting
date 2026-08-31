"use strict";

const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const assert = require("node:assert/strict");
const schedule = require("../shared/email-schedule");
const emailService = require("../shared/email-service");
const worker = require("../email-worker/index");
const repair = require("../email-campaign-repair/index");

const NOW = Date.parse("2026-08-29T12:00:00.000Z");

test("the production email service exports scheduled preflight", () => {
  assert.equal(typeof emailService.preflightScheduled, "function");
});

test("direct send is primary and Outlook drafts live under Other", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "..", "webapp", "email.js"), "utf8");
  const start = source.indexOf('<footer class="email-footer"');
  const end = source.indexOf("// requestAnimationFrame", start);
  const footer = source.slice(start, end);

  assert.match(footer, /class="ask-btn primary" data-email="approve-send"/);
  assert.match(footer, /<details class="email-other">/);
  assert.match(footer, /aria-label="Other sending options"[\s\S]*>Other<\/summary>/);
  assert.match(footer, /data-email="approve-drafts">Create Outlook drafts<\/button>/);
  assert.match(footer, /Nothing is sent\. You send the drafts manually from Outlook\./);
  assert.ok(footer.indexOf('data-email="approve-send"')
    < footer.indexOf('data-email="approve-drafts"'));
});

test("Other is visibly actionable, dismisses outside, and the footer reserves its height", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "..", "webapp", "email.js"), "utf8");
  const styles = fs.readFileSync(path.join(__dirname, "..", "..", "webapp", "email.css"), "utf8");

  assert.match(source, /<summary class="ask-btn email-other-trigger"/);
  assert.match(source, /const openOther = document\.querySelector\("\.email-other\[open\]"\);/);
  assert.match(source, /openOther && !openOther\.contains\(event\.target\)\) openOther\.open = false/);
  assert.match(styles, /\.email-footer\{position:sticky;left:auto;bottom:0;z-index:3;width:100%;flex:0 0 auto/);
  assert.match(styles, /summary\.email-other-trigger\{display:flex;align-items:center;justify-content:center;/);
  assert.match(styles, /summary\.email-other-trigger[\s\S]*box-shadow:inset 0 0 0 1px var\(--hair\)/);
});

test("editing schedule fields validates in place without replacing the active inputs", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "..", "webapp", "email.js"), "utf8");
  const listener = source.indexOf('document.addEventListener("change"');
  const start = source.indexOf('if (["emailScheduleDate", "emailScheduleTime"]', listener);
  const end = source.indexOf('if (event.target.id === "docFiles")', start);
  const branch = source.slice(start, end);

  assert.match(branch, /syncScheduleInputs\(\)/);
  assert.doesNotMatch(branch, /composerView\(\)/);
  assert.match(source, /document\.addEventListener\("input"[\s\S]*?emailScheduleDate[\s\S]*?syncScheduleInputs\(\)/);
});

test("scheduled instants are strict, leave cancellation time, and stop after seven Eastern dates", () => {
  assert.equal(schedule.scheduledInstant("2026-08-29T12:01:00Z", NOW, 60),
    "2026-08-29T12:01:00.000Z");
  assert.throws(() => schedule.scheduledInstant("2026-08-29 13:00", NOW, 60),
    (error) => error.code === "schedule_time_invalid");
  assert.throws(() => schedule.scheduledInstant("2026-08-29T12:00:59Z", NOW, 60),
    (error) => error.code === "schedule_time_too_soon");
  assert.equal(schedule.scheduledInstant("2026-09-05T12:00:01Z", NOW, 60),
    "2026-09-05T12:00:01.000Z");
  assert.throws(() => schedule.scheduledInstant("2026-09-06T12:00:01Z", NOW, 60),
    (error) => error.code === "schedule_time_too_late");
});

test("repair publishes one batch preflight only when a schedule is due", async () => {
  const old = process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED;
  process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED = "1";
  const queued = [];
  try {
    const result = await repair.run({ log() {} }, {
      now: () => NOW,
      core: { config: () => ({ mailboxIntervalSeconds: 5 }) },
      store: {
        listConnections: async () => [{ userId: "u1" }],
        listBatches: async () => [
          { id: "due", status: "scheduled", scheduleState: "pending", scheduleRevision: 3,
            scheduledForUtc: "2026-08-29T12:00:00Z" },
          { id: "backoff", status: "scheduled", scheduleState: "pending", scheduleRevision: 4,
            scheduledForUtc: "2026-08-29T11:59:00Z",
            scheduleRetryAfterUtc: "2026-08-29T12:00:30Z" },
          { id: "later", status: "scheduled", scheduleState: "pending", scheduleRevision: 2,
            scheduledForUtc: "2026-08-29T12:01:00Z" },
        ],
        listMessages: async () => { throw new Error("scheduled rows are batch work"); },
      },
      enqueue: async (work) => queued.push(work),
    });
    assert.equal(result.enqueued, 1);
    assert.deepEqual(queued, [{ kind: "preflight", userId: "u1", batchId: "due", scheduleRevision: 3 }]);
  } finally {
    if (old === undefined) delete process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED;
    else process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED = old;
  }
});

test("an early seventh-day queue wakeup requeues preflight for the remaining delay", async () => {
  const due = new Date(Date.now() + 3600000).toISOString();
  const queued = [];
  await worker.processWork({ kind: "preflight", userId: "u1", batchId: "b1",
    scheduleRevision: 3 }, {
    store: { getBatch: async () => ({ id: "b1", status: "scheduled",
      scheduleState: "pending", scheduleRevision: 3, scheduledForUtc: due }) },
    enqueue: async (work, delay) => queued.push({ work, delay }),
  });
  assert.equal(queued.length, 1);
  assert.equal(queued[0].work.kind, "preflight");
  assert.ok(queued[0].delay >= 3598 && queued[0].delay <= 3600, String(queued[0].delay));
});

function scheduledFixture() {
  let version = 1;
  const batch = { id: "b1", userId: "u1", userName: "Rep", status: "scheduled",
    scheduleState: "pending", scheduleRevision: 4, scheduledForUtc: "2020-01-01T00:00:00Z",
    schedulePreflightAttempts: 0, scheduleRetryAfterUtc: "",
    senderMail: "rep@eicatlanta.com", graphMailbox: "rep@eicatlanta.com", graphMailboxId: "mailbox-1",
    etag: "b1" };
  const messages = [0, 1].map((i) => ({ id: `m${i}`, state: "scheduled_pending",
    scheduleRevision: 4, sendPosition: i, etag: `m${i}-1` }));
  const queued = [], audits = [];
  const store = {
    getBatch: async () => ({ ...batch }),
    patchBatch: async (_u, _b, patch, etag) => {
      assert.equal(etag, batch.etag); Object.assign(batch, patch, { etag: `b${++version}` }); return { ...batch };
    },
    getMessage: async (_u, _b, id) => ({ ...messages.find((m) => m.id === id) }),
    patchMessage: async (_u, _b, id, patch, etag) => {
      const message = messages.find((m) => m.id === id); assert.equal(etag, message.etag);
      Object.assign(message, patch, { etag: `${id}-${++version}` }); return { ...message };
    },
    audit: async (...args) => audits.push(args),
  };
  return { batch, messages, queued, audits, store,
    enqueue: async (work, delay) => queued.push({ work, delay }),
    core: { config: () => ({ mailboxIntervalSeconds: 7 }) } };
}

test("whole-batch preflight passes before any draft work is published", async () => {
  const f = scheduledFixture();
  await worker.processWork({ kind: "preflight", userId: "u1", batchId: "b1", scheduleRevision: 4 }, {
    ...f,
    service: { preflightScheduled: async () => ({ messages: f.messages.map((m) => ({ ...m })) }) },
  });
  assert.equal(f.batch.scheduleState, "passed");
  assert.equal(f.batch.status, "drafting");
  assert.deepEqual(f.messages.map((m) => m.state), ["draft_pending", "draft_pending"]);
  assert.deepEqual(f.queued.map((x) => x.work.kind), ["draft", "draft"]);
  assert.deepEqual(f.queued.map((x) => x.delay), [0, 7]);
});

test("one preflight failure holds every message and queues only owner notification", async () => {
  const f = scheduledFixture();
  await worker.processWork({ kind: "preflight", userId: "u1", batchId: "b1", scheduleRevision: 4 }, {
    ...f,
    service: { preflightScheduled: async () => {
      const error = new Error("advisor detail must not leak"); error.code = "schedule_validation_changed"; throw error;
    } },
  });
  assert.equal(f.batch.status, "schedule_held");
  assert.equal(f.batch.scheduleHoldMessage, "A recipient, attachment, or rendered message changed.");
  assert.deepEqual(f.messages.map((m) => m.state), ["scheduled_pending", "scheduled_pending"]);
  assert.deepEqual(f.queued.map((x) => x.work.kind), ["schedule_notify"]);
});

test("unexpected preflight failures persist a reference and emit redacted diagnostics", async () => {
  const f = scheduledFixture(), errors = [];
  const logger = () => {};
  logger.error = (line) => errors.push(line);
  await worker.processWork({ kind: "preflight", userId: "u1", batchId: "b1", scheduleRevision: 4 }, {
    ...f, logger, invocationId: "invocation-1",
    service: { preflightScheduled: async () => {
      throw new TypeError("lookup failed for advisor@example.com CRD 4784023");
    } },
  });
  assert.equal(f.batch.status, "schedule_held");
  assert.match(f.batch.scheduleLastErrorId, /^spf-[0-9a-f-]{36}$/);
  assert.equal(f.batch.scheduleLastErrorCode, "unexpected_error");
  assert.equal(f.batch.scheduleLastErrorStage, "scheduled_validation");
  assert.equal(errors.length, 1);
  assert.match(errors[0], /^scheduled_preflight_exception /);
  assert.match(errors[0], /\[email\]/);
  assert.match(errors[0], /\[id\]/);
  assert.doesNotMatch(errors[0], /advisor@example\.com|4784023/);
  assert.match(errors[0], /"invocationId":"invocation-1"/);
  const heldAudit = f.audits.find((entry) => entry[2] === "scheduled_preflight_held");
  assert.equal(heldAudit[3].reference, f.batch.scheduleLastErrorId);
});

test("a transient cold recipient-registry failure retries without alarming the rep", async () => {
  const f = scheduledFixture();
  await worker.processWork({ kind: "preflight", userId: "u1", batchId: "b1", scheduleRevision: 4 }, {
    ...f,
    service: { preflightScheduled: async () => {
      const error = new Error("storage detail must not leak");
      error.code = "recipient_registry_unavailable";
      error.statusCode = 503;
      error.preflightStage = "recipient_registry";
      throw error;
    } },
  });
  assert.equal(f.batch.status, "scheduled");
  assert.equal(f.batch.scheduleState, "pending");
  assert.equal(f.batch.schedulePreflightAttempts, 1);
  assert.equal(f.batch.scheduleLastErrorCode, "recipient_registry_unavailable");
  assert.equal(f.batch.scheduleLastErrorStage, "recipient_registry");
  assert.ok(Date.parse(f.batch.scheduleRetryAfterUtc) > Date.now());
  assert.deepEqual(f.queued.map((x) => [x.work.kind, x.delay]), [["preflight", 15]]);
  const retryAudit = f.audits.find((entry) => entry[2] === "scheduled_preflight_retry");
  assert.deepEqual(retryAudit[3].code, "recipient_registry_unavailable");
  assert.deepEqual(retryAudit[3].stage, "recipient_registry");
  assert.equal(JSON.stringify(retryAudit).includes("storage detail"), false);
});

test("a transient safety dependency holds only after three attempts", async () => {
  const f = scheduledFixture();
  f.batch.schedulePreflightAttempts = 2;
  await worker.processWork({ kind: "preflight", userId: "u1", batchId: "b1", scheduleRevision: 4 }, {
    ...f,
    service: { preflightScheduled: async () => {
      const error = new Error("storage detail must not leak");
      error.code = "recipient_registry_unavailable";
      error.statusCode = 503;
      error.preflightStage = "recipient_registry";
      throw error;
    } },
  });
  assert.equal(f.batch.status, "schedule_held");
  assert.equal(f.batch.schedulePreflightAttempts, 3);
  assert.equal(f.batch.scheduleHoldCode, "schedule_preflight_unavailable");
  assert.match(f.batch.scheduleHoldMessage, /safety data/i);
  assert.deepEqual(f.queued.map((x) => x.work.kind), ["schedule_notify"]);
  const heldAudit = f.audits.find((entry) => entry[2] === "scheduled_preflight_held");
  assert.equal(heldAudit[3].originalCode, "recipient_registry_unavailable");
  assert.equal(heldAudit[3].stage, "recipient_registry");
  assert.equal(JSON.stringify(heldAudit).includes("storage detail"), false);
});

test("a duplicate preflight delivery respects the durable retry time", async () => {
  const f = scheduledFixture();
  f.batch.scheduleRetryAfterUtc = new Date(Date.now() + 30000).toISOString();
  let checked = 0;
  await worker.processWork({ kind: "preflight", userId: "u1", batchId: "b1", scheduleRevision: 4 }, {
    ...f, service: { preflightScheduled: async () => { checked++; } },
  });
  assert.equal(checked, 0);
  assert.equal(f.batch.schedulePreflightAttempts, 0);
  assert.equal(f.queued.length, 1);
  assert.equal(f.queued[0].work.kind, "preflight");
  assert.ok(f.queued[0].delay >= 29 && f.queued[0].delay <= 30);
});

test("schedule review links use the anonymous sign-in relay", () => {
  const old = process.env.EMAIL_PUBLIC_BASE_URL;
  process.env.EMAIL_PUBLIC_BASE_URL = "https://map.example.com/old/path?discard=1";
  try {
    assert.equal(worker.scheduleLink("123e4567-e89b-12d3-a456-426614174000"),
      "https://map.example.com/review.html?emailBatch=123e4567-e89b-12d3-a456-426614174000");
  } finally {
    if (old === undefined) delete process.env.EMAIL_PUBLIC_BASE_URL;
    else process.env.EMAIL_PUBLIC_BASE_URL = old;
  }
});

test("hold notification reconciles its deterministic self-addressed message", async () => {
  const f = scheduledFixture();
  Object.assign(f.batch, { status: "schedule_held", scheduleState: "held",
    scheduleNotificationState: "pending", scheduleHoldMessage: "Review required.",
    scheduleLastErrorId: "spf-123e4567-e89b-12d3-a456-426614174000" });
  const created = [], sent = [];
  await worker.processWork({ kind: "schedule_notify", userId: "u1", batchId: "b1", scheduleRevision: 4 }, {
    ...f,
    auth: {
      status: async () => ({ connected: true, mailbox: "rep@eicatlanta.com",
        profile: { id: "mailbox-1", mail: "rep@eicatlanta.com" } }),
      tokenFor: async () => ({ accessToken: "token", mailboxId: "mailbox-1" }),
    },
    graph: {
      findByAppId: async () => null,
      createDraft: async (_token, message) => { created.push(message); return { id: "graph-1", isDraft: true }; },
      sendDraft: async (_token, id) => sent.push(id),
    },
  });
  assert.equal(created[0].recipientEmail, "rep@eicatlanta.com");
  assert.equal(created[0].id, "schedule-hold-b1-r4");
  assert.match(created[0].bodyHtml, /Support reference:.*spf-123e4567/);
  assert.deepEqual(sent, ["graph-1"]);
  assert.equal(f.batch.scheduleNotificationState, "submitted");
  await worker.processWork({ kind: "schedule_notify", userId: "u1", batchId: "b1", scheduleRevision: 4 }, {
    ...f,
    auth: {
      status: async () => ({ connected: true, mailbox: "rep@eicatlanta.com",
        profile: { id: "mailbox-1", mail: "rep@eicatlanta.com" } }),
      tokenFor: async () => ({ accessToken: "token", mailboxId: "mailbox-1" }),
    },
    graph: { getMessage: async () => ({ id: "graph-1", isDraft: false,
      sentDateTime: "2026-08-29T12:00:00Z" }) },
  });
  assert.equal(f.batch.scheduleNotificationState, "sent");
});
test("slot-aware send guard requeues position twenty without touching Graph", async () => {
  const now = Date.now(), queued = [];
  const batch = { id: "b1", status: "sending", mode: "send", scheduleState: "passed",
    scheduleRevision: 2, sendNotBeforeUtc: new Date(now).toISOString() };
  const message = { id: "m1", state: "send_scheduled", sendPosition: 20, scheduleRevision: 2 };
  await worker.processWork({ kind: "send", userId: "u1", batchId: "b1", messageId: "m1", scheduleRevision: 2 }, {
    store: { getBatch: async () => batch, getMessage: async () => message },
    core: { config: () => ({ mailboxIntervalSeconds: 5 }) },
    enqueue: async (work, delay) => queued.push({ work, delay }),
    graph: new Proxy({}, { get() { throw new Error("Graph must not be touched before the slot due time"); } }),
  });
  assert.equal(queued.length, 1);
  assert.ok(queued[0].delay >= 99 && queued[0].delay <= 100, String(queued[0].delay));
});

test("repair asks storage to retain active schedules beyond the 500 recent cap", async () => {
  const old = process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED;
  process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED = "1";
  let args;
  try {
    await repair.run({ log() {} }, {
      now: () => NOW, core: { config: () => ({ mailboxIntervalSeconds: 5 }) },
      store: {
        listConnections: async () => [{ userId: "u1" }],
        listBatches: async (...value) => { args = value; return [{ id: "old-scheduled", status: "scheduled",
          scheduleState: "pending", scheduleRevision: 9, scheduledForUtc: "2026-08-29T12:00:00Z" }]; },
      }, enqueue: async () => {},
    });
    assert.deepEqual(args, ["u1", 500, true]);
  } finally {
    if (old === undefined) delete process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED; else process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED = old;
  }
});
test("lost notification submit response reconciles without a second create or send", async () => {
  const f = scheduledFixture();
  Object.assign(f.batch, { status: "schedule_held", scheduleState: "held",
    scheduleNotificationState: "pending", scheduleNotificationId: "schedule-hold-b1-r4",
    scheduleHoldMessage: "Review required." });
  let creates = 0, sends = 0, lookup = 0;
  const auth = {
    status: async () => ({ connected: true, mailbox: "rep@eicatlanta.com",
      profile: { id: "mailbox-1", mail: "rep@eicatlanta.com" } }),
    tokenFor: async () => ({ accessToken: "token", mailboxId: "mailbox-1" }),
  };
  const graph = {
    findByAppId: async () => null,
    createDraft: async () => { creates++; return { id: "graph-1", isDraft: true }; },
    sendDraft: async () => { sends++; throw new Error("accepted response lost"); },
    getMessage: async () => {
      lookup++;
      if (lookup === 1) { const error = new Error("not visible yet"); error.statusCode = 404; throw error; }
      return { id: "graph-1", isDraft: false, sentDateTime: "2026-08-29T12:00:00Z" };
    },
  };
  const work = { kind: "schedule_notify", userId: "u1", batchId: "b1", scheduleRevision: 4 };
  await worker.processWork(work, { ...f, auth, graph });
  assert.equal(f.batch.scheduleNotificationState, "ambiguous");
  await worker.processWork(work, { ...f, auth, graph });
  assert.equal(f.batch.scheduleNotificationState, "ambiguous", "temporary Graph absence stays reconciliation-only");
  await worker.processWork(work, { ...f, auth, graph });
  assert.equal(f.batch.scheduleNotificationState, "sent");
  assert.equal(creates, 1);
  assert.equal(sends, 1);
});

test("a patch failure after notification draft creation finds that draft instead of creating another", async () => {
  const f = scheduledFixture();
  Object.assign(f.batch, { status: "schedule_held", scheduleState: "held",
    scheduleNotificationState: "pending", scheduleNotificationId: "schedule-hold-b1-r4" });
  const ordinaryPatch = f.store.patchBatch;
  let failPatch = true, created = 0, found = null;
  f.store.patchBatch = async (...args) => {
    if (args[2].scheduleNotificationState === "draft_ready" && failPatch) {
      failPatch = false; throw new Error("host lost patch");
    }
    return ordinaryPatch(...args);
  };
  const auth = {
    status: async () => ({ connected: true, mailbox: "rep@eicatlanta.com",
      profile: { id: "mailbox-1", mail: "rep@eicatlanta.com" } }),
    tokenFor: async () => ({ accessToken: "token", mailboxId: "mailbox-1" }),
  };
  const graph = {
    findByAppId: async () => found,
    createDraft: async () => { created++; found = { id: "graph-1", isDraft: true }; return found; },
    sendDraft: async () => {},
  };
  const work = { kind: "schedule_notify", userId: "u1", batchId: "b1", scheduleRevision: 4 };
  await worker.processWork(work, { ...f, auth, graph });
  assert.equal(f.batch.scheduleNotificationState, "creating");
  await worker.processWork(work, { ...f, auth, graph });
  assert.equal(created, 1);
  assert.equal(f.batch.scheduleNotificationState, "submitted");
});
test("a stale notification revision produces no owner email", async () => {
  const f = scheduledFixture();
  Object.assign(f.batch, { status: "schedule_held", scheduleState: "held",
    scheduleNotificationState: "pending", scheduleRevision: 5 });
  let reads = 0;
  await worker.processWork({ kind: "schedule_notify", userId: "u1", batchId: "b1", scheduleRevision: 4 }, {
    ...f, auth: new Proxy({}, { get() { reads++; throw new Error("stale work must stop before auth"); } }),
  });
  assert.equal(reads, 0);
  assert.equal(f.batch.scheduleNotificationState, "pending");
});
test("a crash after claiming create never recreates and terminalizes at its horizon", async () => {
  const f = scheduledFixture();
  Object.assign(f.batch, { status: "schedule_held", scheduleState: "held",
    scheduleNotificationState: "creating", scheduleNotificationPhase: "create",
    scheduleNotificationId: "schedule-hold-b1-r4",
    scheduleNotificationReconcileUntilUtc: new Date(Date.now() + 60000).toISOString() });
  let creates = 0;
  const auth = {
    status: async () => ({ connected: true, mailbox: "rep@eicatlanta.com",
      profile: { id: "mailbox-1", mail: "rep@eicatlanta.com" } }),
    tokenFor: async () => ({ accessToken: "token", mailboxId: "mailbox-1" }),
  };
  const graph = { findByAppId: async () => null, createDraft: async () => { creates++; } };
  const work = { kind: "schedule_notify", userId: "u1", batchId: "b1", scheduleRevision: 4 };
  await worker.processWork(work, { ...f, auth, graph });
  assert.equal(creates, 0);
  assert.equal(f.batch.scheduleNotificationState, "ambiguous");
  f.batch.scheduleNotificationReconcileUntilUtc = new Date(Date.now() - 1000).toISOString();
  await worker.processWork(work, { ...f, auth, graph });
  assert.equal(creates, 0);
  assert.equal(f.batch.scheduleNotificationState, "outcome_unknown");
  assert.ok(f.batch.scheduleNotificationCompletedUtc);
  assert.ok(f.audits.some((entry) => entry[2] === "schedule_notification_outcome_unknown"));
});

test("an ambiguous submitted notification terminalizes when reconciliation expires", async () => {
  const f = scheduledFixture();
  Object.assign(f.batch, { status: "schedule_held", scheduleState: "held",
    scheduleNotificationState: "ambiguous", scheduleNotificationPhase: "submit",
    scheduleNotificationId: "schedule-hold-b1-r4", scheduleNotificationGraphId: "graph-1",
    scheduleNotificationReconcileUntilUtc: new Date(Date.now() - 1000).toISOString() });
  const auth = {
    status: async () => ({ connected: true, mailbox: "rep@eicatlanta.com",
      profile: { id: "mailbox-1", mail: "rep@eicatlanta.com" } }),
    tokenFor: async () => ({ accessToken: "token", mailboxId: "mailbox-1" }),
  };
  await worker.processWork({ kind: "schedule_notify", userId: "u1", batchId: "b1", scheduleRevision: 4 }, {
    ...f, auth, graph: { getMessage: async () => ({ id: "graph-1", isDraft: true }) },
  });
  assert.equal(f.batch.scheduleNotificationState, "outcome_unknown");
  assert.ok(f.batch.scheduleNotificationCompletedUtc);
});

test("repair never re-enqueues a terminal notification outcome", async () => {
  const old = process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED;
  process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED = "1";
  let enqueued = 0;
  try {
    await repair.run({ log() {} }, {
      now: () => NOW, core: { config: () => ({ mailboxIntervalSeconds: 5 }) },
      store: {
        listConnections: async () => [{ userId: "u1" }],
        listBatches: async () => [{ id: "b1", status: "schedule_held",
          scheduleNotificationState: "outcome_unknown", updatedUtc: "2026-08-01T00:00:00Z" }],
      }, enqueue: async () => { enqueued++; },
    });
    assert.equal(enqueued, 0);
  } finally {
    if (old === undefined) delete process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED; else process.env.EMAIL_CAMPAIGN_REPAIR_ENABLED = old;
  }
});
test("a stale queued hint cannot revive an outcome-unknown notification", async () => {
  const f = scheduledFixture();
  Object.assign(f.batch, { status: "schedule_held", scheduleState: "held",
    scheduleNotificationState: "outcome_unknown", scheduleNotificationPhase: "submit",
    scheduleNotificationId: "schedule-hold-b1-r4", scheduleNotificationGraphId: "graph-1",
    scheduleNotificationCompletedUtc: "2026-08-29T12:00:00Z" });
  let authReads = 0, graphReads = 0;
  await worker.processWork({ kind: "schedule_notify", userId: "u1", batchId: "b1", scheduleRevision: 4 }, {
    ...f,
    auth: new Proxy({}, { get() { authReads++; throw new Error("terminal hint reached auth"); } }),
    graph: new Proxy({}, { get() { graphReads++; throw new Error("terminal hint reached Graph"); } }),
  });
  assert.equal(authReads, 0);
  assert.equal(graphReads, 0);
  assert.equal(f.batch.scheduleNotificationState, "outcome_unknown");
  assert.equal(f.batch.scheduleNotificationGraphId, "graph-1");
});
