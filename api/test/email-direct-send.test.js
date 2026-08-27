"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const direct = require("../shared/email-direct-send");
const repair = require("../email-direct-repair/index");

const OP = "11111111-1111-4111-8111-111111111111";
const recipientSetHash = (addresses) => crypto.createHash("sha256")
  .update(JSON.stringify([...addresses].sort()), "utf8").digest("hex");

function harness(initial, graphOverrides = {}) {
  let operation = { userId: "u1", operationId: OP, kind: "follow_up", advisorCrd: "123",
    state: "prepared", graphDraftId: "draft-1", graphMessageId: "draft-1",
    recipientSetHash: recipientSetHash(["advisor@example.com"]),
    createdUtc: "2026-08-25T12:00:00Z", etag: "e1", ...initial };
  let version = 1;
  const calls = { send: 0, scheduled: [], activity: [], completed: 0, queue: [] };
  const save = (patch) => { operation = { ...operation, ...patch, etag: `e${++version}` }; return operation; };
  const opsStore = {
    getOperation: async () => ({ ...operation }),
    claimOperation: async (_u, _id, allowed, options) => {
      if (!allowed.includes(operation.state) || (operation.leaseUntilUtc
          && Date.parse(operation.leaseUntilUtc) > Date.now())) return null;
      return save({ state: options.nextState || operation.state,
        leaseId: "lease", leaseUntilUtc: "2099-01-01T00:00:00Z",
        [`${options.phase}Attempts`]: Number(operation[`${options.phase}Attempts`]) + 1 });
    },
    patchOperation: async (_u, _id, patch, etag) => {
      assert.equal(etag, operation.etag); return save(patch);
    },
    scheduleOperation: async (_u, _id, patch, due, etag) => {
      assert.equal(etag, operation.etag); calls.scheduled.push({ ...patch, due });
      return save({ ...patch, nextAttemptUtc: due, leaseId: "", leaseUntilUtc: "" });
    },
    completeOperation: async (_u, _id, patch, etag) => {
      assert.equal(etag, operation.etag); calls.completed++; return save({ ...patch, state: "complete" });
    },
    failOperation: async (_u, _id, patch, etag) => {
      assert.equal(etag, operation.etag); return save({ ...patch, state: "failed" });
    },
    markEnqueued: async () => {},
  };
  const graph = {
    getMessage: async () => ({ id: "draft-1", isDraft: true, subject: "Hello",
      toRecipients: [{ emailAddress: { address: "advisor@example.com" } }] }),
    findByAppId: async () => null,
    sendDraft: async () => { calls.send++; return { requestId: "req-1" }; },
    ...graphOverrides,
  };
  return { calls, current: () => ({ ...operation }), deps: {
    opsStore, graph,
    workQueue: { parse: (value) => value,
      enqueue: async (kind) => { calls.queue.push(kind); } },
    auth: { tokenFor: async () => ({ accessToken: "token" }) },
    core: { config: () => ({ directSendEnvironmentEnabled: true,
      testAllowlist: new Set(), internalDomains: new Set(["eicatlanta.com"]),
      rollingExternalLimit: 5000, mailboxIntervalSeconds: 1 }),
      isExternal: () => true },
    activityStore: { policy: async () => ({ killed: false }),
      recordActivity: async (entry) => { calls.activity.push(entry); return entry; } },
    suppress: { blockedAmong: async () => new Map() },
    recipientRegistry: {
      resolve: async () => ({ crd: "123", email: "advisor@example.com",
        routingHash: "route", teammates: [] }),
      verify: async () => ({ crd: "123", email: "advisor@example.com",
        routingHash: "route", teammates: [] }),
    },
    limitGuard: { reserve: async () => ({ alreadyReserved: false }) },
    mailboxGate: { acquire: async () => 0 }, wait: async () => {},
    advisors: { emailForCrd: async () => "advisor@example.com" },
    engagement: { refresh: async () => {}, completeOutbound: async (_u, _c, options) => {
      calls.actedAt = options.actedAt;
    } },
  } };
}

function enabled(fn) {
  const before = { enabled: process.env.EMAIL_DIRECT_SEND_OPS_ENABLED,
    users: process.env.EMAIL_DIRECT_SEND_OPS_USER_IDS };
  process.env.EMAIL_DIRECT_SEND_OPS_ENABLED = "1";
  process.env.EMAIL_DIRECT_SEND_OPS_USER_IDS = "u1";
  return Promise.resolve().then(fn).finally(() => {
    for (const [key, value] of Object.entries(before)) {
      const env = key === "enabled" ? "EMAIL_DIRECT_SEND_OPS_ENABLED" : "EMAIL_DIRECT_SEND_OPS_USER_IDS";
      if (value === undefined) delete process.env[env]; else process.env[env] = value;
    }
  });
}

test("an ambiguous Graph send is never automatically submitted twice", () => enabled(async () => {
  const error = new Error("timeout"); error.ambiguous = true;
  const h = harness({}, { sendDraft: async () => { h.calls.send++; throw error; } });
  await direct.processWork({ v: 1, kind: "direct_send", userId: "u1", operationId: OP }, h.deps);
  assert.equal(h.calls.send, 1);
  assert.equal(h.current().state, "ambiguous");
  await direct.processWork({ v: 1, kind: "direct_send", userId: "u1", operationId: OP }, h.deps);
  assert.equal(h.calls.send, 1, "duplicate delivery may not cross /send again");
}));

test("Graph success becomes submitted, not sent, until a sent item is observed", () => enabled(async () => {
  const h = harness();
  await direct.processWork({ v: 1, kind: "direct_send", userId: "u1", operationId: OP }, h.deps);
  assert.equal(h.calls.send, 1);
  assert.equal(h.current().state, "submitted");
  assert.deepEqual(h.calls.queue, ["direct_reconcile"]);
  assert.equal(direct.publicStatus(h.current()).status, "confirming");
}));

test("turning off the canary after preparation defers without Graph or poison failure", async () => {
  const before = process.env.EMAIL_DIRECT_SEND_OPS_ENABLED;
  delete process.env.EMAIL_DIRECT_SEND_OPS_ENABLED;
  const h = harness();
  try {
    await direct.processWork({ v: 1, kind: "direct_send", userId: "u1", operationId: OP }, h.deps);
    assert.equal(h.calls.send, 0);
    assert.equal(h.current().state, "prepared");
    assert.equal(h.current().lastErrorCode, "direct_send_ops_disabled");
  } finally {
    if (before === undefined) delete process.env.EMAIL_DIRECT_SEND_OPS_ENABLED;
    else process.env.EMAIL_DIRECT_SEND_OPS_ENABLED = before;
  }
});

test("follow-up suppression is rechecked for To but not an internal compliance Bcc", () => enabled(async () => {
  const checked = [];
  const h = harness({ recipientSetHash: recipientSetHash(
    ["advisor@example.com", "compliance@eicatlanta.com"]) }, {
    getMessage: async () => ({ id: "draft-1", isDraft: true,
    toRecipients: [{ emailAddress: { address: "advisor@example.com" } }],
    bccRecipients: [{ emailAddress: { address: "compliance@eicatlanta.com" } }] }) });
  h.deps.suppress.blockedAmong = async (addresses) => {
    checked.push(...addresses);
    return new Map([["compliance@eicatlanta.com", true]]);
  };
  h.deps.core.complianceBcc = () => ["compliance@eicatlanta.com"];
  await direct.processWork({ v: 1, kind: "direct_send", userId: "u1", operationId: OP }, h.deps);
  assert.deepEqual(checked, ["advisor@example.com"]);
  assert.equal(h.calls.send, 1);
}));

test("a changed direct-send recipient set fails before Graph send", () => enabled(async () => {
  const h = harness({}, { getMessage: async () => ({ id: "draft-1", isDraft: true,
    toRecipients: [{ emailAddress: { address: "wrong@example.com" } }] }) });
  await direct.processWork(
    { v: 1, kind: "direct_send", userId: "u1", operationId: OP }, h.deps);
  assert.equal(h.calls.send, 0);
  assert.equal(h.current().state, "failed");
  assert.equal(h.current().lastErrorCode, "recipient_routing_changed");
}));

test("a reply sender must match the current approved CRD identity", () => enabled(async () => {
  const err = new Error("changed"); err.code = "recipient_identity_changed";
  let operationWrites = 0;
  await assert.rejects(() => direct.start({ id: "u1" }, {
    operationId: OP, crd: "123", id: "inbound-1", text: "Thank you.",
  }, "reply", {
    advisors: { isInternalCrd: async () => false },
    activityStore: { activityOwner: async () => "u1" },
    auth: { tokenFor: async () => ({ accessToken: "token" }) },
    graph: { getMessageContent: async () => ({ subject: "Re",
      from: { emailAddress: { address: "wrong@example.com" } } }) },
    suppress: { blockedAmong: async () => new Map() },
    core: { config: () => ({ internalDomains: new Set(["eicatlanta.com"]) }),
      isExternal: () => true },
    recipientRegistry: { verify: async () => { throw err; } },
    opsStore: { createOperation: async () => { operationWrites++; } },
  }), (error) => error.code === "recipient_identity_changed");
  assert.equal(operationWrites, 0);
}));

test("canonical Graph metadata drives activity and a retry-stable actedAt", async () => {
  const sentAt = "2026-08-25T12:34:56Z";
  const h = harness({ state: "reconciled", canonicalSentDateTime: sentAt }, {
    getMessage: async () => ({ id: "sent-immutable", isDraft: false, sentDateTime: sentAt,
      internetMessageId: "<one@example>", conversationId: "conversation", subject: "Canonical",
      toRecipients: [{ emailAddress: { address: "advisor@example.com" } }] }),
  });
  await direct.processWork({ v: 1, kind: "direct_finalize", userId: "u1", operationId: OP }, h.deps);
  assert.equal(h.calls.activity[0].occurredAt, sentAt);
  assert.equal(h.calls.activity[0].graphMessageId, "sent-immutable");
  assert.equal(h.calls.actedAt, sentAt);
  assert.equal(h.current().state, "complete");
});

test("an expired submitting lease is reconciled and never re-enqueued for send", async () => {
  const now = Date.parse("2026-08-25T13:00:00Z");
  let operation = { userId: "u1", operationId: OP, state: "submitting",
    leaseUntilUtc: "2026-08-25T12:59:00Z", etag: "e1" };
  const queued = [];
  const store = {
    getRepairCursor: async () => null,
    putRepairCursor: async () => {},
    listWorkPage: async () => ({ markers: [{ userId: "u1", operationId: OP,
      dueUtc: "2026-08-25T12:59:00Z", leaseUntilUtc: "", etag: "m1" }], continuationToken: "" }),
    getOperation: async () => operation,
    scheduleOperation: async (_u, _id, patch) => (operation = { ...operation, ...patch, etag: "e2" }),
    claimMarker: async (marker) => marker,
    markEnqueued: async () => {},
  };
  const before = process.env.EMAIL_DIRECT_REPAIR_ENABLED;
  process.env.EMAIL_DIRECT_REPAIR_ENABLED = "1";
  try {
    const report = await repair.run({}, { store, now: () => now,
      queue: { enqueue: async (kind) => queued.push(kind) } });
    assert.equal(report.enqueued, 1);
    assert.equal(operation.state, "ambiguous");
    assert.deepEqual(queued, ["direct_reconcile"]);
  } finally {
    if (before === undefined) delete process.env.EMAIL_DIRECT_REPAIR_ENABLED;
    else process.env.EMAIL_DIRECT_REPAIR_ENABLED = before;
  }
});

test("status reads only the authenticated user's partition", async () => {
  const calls = [];
  const result = await direct.status({ id: "u1" }, OP, { opsStore: {
    getOperation: async (...args) => { calls.push(args); return { userId: "u1", operationId: OP,
      state: "ambiguous", needsVerification: true }; },
  } });
  assert.deepEqual(calls, [["u1", OP]]);
  assert.equal(result.status, "needs_verification");
  assert.match(result.message, /Do not resend/);
});

test("HTTP orchestration prepares a stamped Outlook draft, queues identifiers, and does not send", async () => {
  const saved = { enabled: process.env.EMAIL_DIRECT_SEND_OPS_ENABLED,
    users: process.env.EMAIL_DIRECT_SEND_OPS_USER_IDS,
    hmac: process.env.EMAIL_DIRECT_SEND_HMAC_KEY };
  process.env.EMAIL_DIRECT_SEND_OPS_ENABLED = "1";
  process.env.EMAIL_DIRECT_SEND_OPS_USER_IDS = "u1";
  process.env.EMAIL_DIRECT_SEND_HMAC_KEY = "x".repeat(32);
  const queued = [], created = [], graphMutations = [];
  let operation = null, version = 0;
  const deps = {
    opsStore: {
      createOperation: async (userId, input) => {
        created.push({ userId, input });
        operation = { userId, ...input, state: "preparing", leaseId: "http",
          leaseUntilUtc: "2099-01-01T00:00:00Z", prepareAttempts: 1, etag: `e${++version}` };
        return { created: true, operation };
      },
      scheduleOperation: async (_u, _id, patch, due, etag) => {
        assert.equal(etag, operation.etag);
        operation = { ...operation, ...patch, nextAttemptUtc: due, etag: `e${++version}` };
        return operation;
      },
      markEnqueued: async () => {}, getOperation: async () => operation,
      failOperation: async () => { throw new Error("preparation should not fail"); },
    },
    workQueue: { enqueue: async (...args) => queued.push(args) },
    activityStore: { policy: async () => ({ killed: false }), getDocuments: async () => [],
      listActivity: async () => [] },
    auth: { tokenFor: async () => ({ accessToken: "token", profile: {} }) },
    advisors: { isInternalCrd: async () => false, emailForCrd: async () => "advisor@example.com" },
    suppress: { blockedAmong: async () => new Map() },
    recipientRegistry: {
      resolve: async () => ({ crd: "123", email: "advisor@example.com",
        routingHash: "route", teammates: [] }),
      verify: async () => ({ crd: "123", email: "advisor@example.com",
        routingHash: "route", teammates: [] }),
    },
    core: { config: () => ({ maxAttachmentBytes: 1000000, directSendEnvironmentEnabled: true,
      testAllowlist: new Set(), internalDomains: new Set(["eicatlanta.com"]),
      rollingExternalLimit: 5000 }), isExternal: () => true,
      corporateSignature: () => "<div>Signature</div>", complianceBcc: () => [] },
    limitGuard: { reserve: async () => ({ alreadyReserved: false }) },
    graph: {
      findByAppId: async () => null,
      request: async (_token, method, path, body) => {
        graphMutations.push({ method, path, body });
        if (method === "POST" && path === "/me/messages") return { data: { id: "draft-1" }, requestId: "r1" };
        return { data: {} };
      },
      getMessage: async () => ({ id: "draft-1", isDraft: true, subject: "Hello",
        conversationId: "c1", internetMessageId: "<draft@example>" }),
      attachDocuments: async () => {}, attachFiles: async () => {},
      APP_PROPERTY_ID: "app-property",
      sendDraft: async () => { throw new Error("HTTP preparation must never send"); },
    },
  };
  try {
    const result = await direct.start({ id: "u1" }, { operationId: OP, crd: "123",
      subject: "Hello", text: "Checking in." }, "follow_up", deps);
    assert.equal(result.status, "queued");
    assert.equal(queued[0][0], "direct_send");
    assert.deepEqual(queued[0].slice(1, 3), ["u1", OP]);
    assert.equal(graphMutations.some((call) => call.method === "POST" && /send/.test(call.path)), false);
    assert.equal(graphMutations.at(-1).method, "PATCH", "the operation stamp is the last mutation");
    assert.ok(created[0].input.intentHash);
    for (const forbidden of ["text", "body", "recipients", "files", "recipientEmail"])
      assert.equal(Object.hasOwn(created[0].input, forbidden), false, forbidden);

    // If the initial reconciliation lookup is unavailable, the browser still
    // receives this same durable id as pending. It must not receive a failure
    // that would permit a reload to generate a second operation.
    const outageOp = "22222222-2222-4222-8222-222222222222";
    operation = null;
    queued.length = 0;
    deps.graph.findByAppId = async () => {
      const err = new Error("Graph lookup unavailable"); err.graphCode = "ServiceUnavailable";
      throw err;
    };
    const pending = await direct.start({ id: "u1" }, { operationId: outageOp, crd: "123",
      subject: "Hello", text: "Checking in." }, "follow_up", deps);
    assert.equal(pending.status, "preparing");
    assert.equal(pending.pending, true);
    assert.equal(operation.state, "preparing");
    assert.equal(queued[0][0], "direct_recover");
    assert.deepEqual(queued[0].slice(1, 3), ["u1", outageOp]);
  } finally {
    for (const [key, value] of Object.entries(saved)) {
      const env = { enabled: "EMAIL_DIRECT_SEND_OPS_ENABLED", users: "EMAIL_DIRECT_SEND_OPS_USER_IDS",
        hmac: "EMAIL_DIRECT_SEND_HMAC_KEY" }[key];
      if (value === undefined) delete process.env[env]; else process.env[env] = value;
    }
  }
});
