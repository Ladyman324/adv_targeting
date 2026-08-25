"use strict";

/* Durable ownership for one-to-one sends.
 *
 * This table is deliberately separate from EmailSendLedger. The ledger counts
 * rolling external recipients; this records whether one irreversible Graph
 * operation may run. Message bodies, recipients and attachment bytes stay in
 * Outlook and never enter this table or its queue markers.
 */

const crypto = require("crypto");
const { TableClient, odata } = require("@azure/data-tables");

const CONN = process.env.AZURE_STORAGE_CONNECTION_STRING || "";
const TABLE_NAME = "EmailDirectSendOps";
const OP_PREFIX = "op|";
const WORK_PREFIX = "q|";
const WORK_END = "q}";
const TERMINAL = new Set(["complete", "failed"]);
let client;
let ensured = false;

function clean(value, max = 1024) {
  return value == null ? "" : String(value).slice(0, max);
}

function at(value = Date.now()) { return new Date(value).toISOString(); }
function opKey(operationId) { return OP_PREFIX + String(operationId || "").toLowerCase(); }
function workKey(operationId) { return WORK_PREFIX + String(operationId || "").toLowerCase(); }

async function table() {
  if (!CONN) {
    const err = new Error("Direct-send storage is not configured.");
    err.statusCode = 503;
    err.code = "direct_send_storage_unavailable";
    throw err;
  }
  if (!client) client = TableClient.fromConnectionString(CONN, TABLE_NAME,
    { allowInsecureConnection: false });
  if (!ensured) {
    await client.createTable().catch((err) => { if (Number(err.statusCode) !== 409) throw err; });
    ensured = true;
  }
  return client;
}

async function optional(partitionKey, rowKey) {
  try { return await (await table()).getEntity(partitionKey, rowKey); }
  catch (err) { if (Number(err.statusCode) === 404) return null; throw err; }
}

function operationFrom(entity) {
  if (!entity) return null;
  return {
    userId: entity.partitionKey,
    operationId: String(entity.rowKey || "").slice(OP_PREFIX.length),
    schemaVersion: Number(entity.schemaVersion) || 1,
    kind: entity.kind || "",
    intentHash: entity.intentHash || "",
    advisorCrd: entity.advisorCrd || "",
    sourceGraphMessageId: entity.sourceGraphMessageId || "",
    replyAll: entity.replyAll === true,
    state: entity.state || "",
    graphDraftId: entity.graphDraftId || "",
    graphMessageId: entity.graphMessageId || "",
    graphInternetMessageId: entity.graphInternetMessageId || "",
    graphConversationId: entity.graphConversationId || "",
    graphRequestId: entity.graphRequestId || "",
    canonicalSentDateTime: entity.canonicalSentDateTime || "",
    subject: entity.subject || "",
    attachmentCount: Number(entity.attachmentCount) || 0,
    complianceCopied: entity.complianceCopied === true,
    leaseId: entity.leaseId || "",
    leaseUntilUtc: entity.leaseUntilUtc || "",
    nextAttemptUtc: entity.nextAttemptUtc || "",
    prepareAttempts: Number(entity.prepareAttempts) || 0,
    sendAttempts: Number(entity.sendAttempts) || 0,
    reconcileAttempts: Number(entity.reconcileAttempts) || 0,
    finalizeAttempts: Number(entity.finalizeAttempts) || 0,
    lastErrorCode: entity.lastErrorCode || "",
    needsVerification: entity.needsVerification === true,
    createdUtc: entity.createdUtc || "",
    updatedUtc: entity.updatedUtc || "",
    preparedUtc: entity.preparedUtc || "",
    sendStartedUtc: entity.sendStartedUtc || "",
    submittedUtc: entity.submittedUtc || "",
    reconciledUtc: entity.reconciledUtc || "",
    completedUtc: entity.completedUtc || "",
    expiresUtc: entity.expiresUtc || "",
    etag: entity.etag,
  };
}

function markerFrom(entity) {
  if (!entity) return null;
  return {
    userId: entity.partitionKey,
    operationId: String(entity.rowKey || "").slice(WORK_PREFIX.length),
    state: entity.state || "pending",
    dueUtc: entity.dueUtc || "",
    leaseId: entity.leaseId || "",
    leaseUntilUtc: entity.leaseUntilUtc || "",
    enqueueCount: Number(entity.enqueueCount) || 0,
    lastEnqueuedUtc: entity.lastEnqueuedUtc || "",
    updatedUtc: entity.updatedUtc || "",
    etag: entity.etag,
  };
}

function conflict(message = "This operation id is already bound to different message content.") {
  const err = new Error(message);
  err.statusCode = 409;
  err.code = "idempotency_conflict";
  return err;
}

async function getOperation(userId, operationId) {
  return operationFrom(await optional(String(userId), opKey(operationId)));
}

async function getMarker(userId, operationId) {
  return markerFrom(await optional(String(userId), workKey(operationId)));
}

async function createOperation(userId, input, options = {}) {
  const nowUtc = at(options.nowMs);
  const operationId = String(input.operationId || "").toLowerCase();
  const partitionKey = String(userId);
  const operation = {
    partitionKey, rowKey: opKey(operationId), schemaVersion: 1,
    kind: clean(input.kind, 20), intentHash: clean(input.intentHash, 128),
    advisorCrd: clean(input.advisorCrd, 40),
    sourceGraphMessageId: clean(input.sourceGraphMessageId, 1024),
    replyAll: input.replyAll === true, state: "preparing",
    attachmentCount: Number(input.attachmentCount) || 0,
    leaseId: clean(input.leaseId || crypto.randomUUID(), 80),
    leaseUntilUtc: at((options.nowMs === undefined ? Date.now() : options.nowMs)
      + (Number(options.leaseSeconds) || 300) * 1000),
    nextAttemptUtc: "", prepareAttempts: 1, sendAttempts: 0,
    reconcileAttempts: 0, finalizeAttempts: 0,
    needsVerification: false, lastErrorCode: "",
    createdUtc: nowUtc, updatedUtc: nowUtc,
  };
  const marker = {
    partitionKey, rowKey: workKey(operationId), state: "pending",
    dueUtc: at((options.nowMs === undefined ? Date.now() : options.nowMs) + 300000),
    leaseId: "", leaseUntilUtc: "", enqueueCount: 0,
    lastEnqueuedUtc: "", updatedUtc: nowUtc,
  };
  try {
    await (await table()).submitTransaction([["create", operation], ["create", marker]]);
    return { created: true, operation: await getOperation(partitionKey, operationId) };
  } catch (err) {
    if (Number(err.statusCode) !== 409) throw err;
    const existing = await getOperation(partitionKey, operationId);
    if (!existing || existing.intentHash !== operation.intentHash) throw conflict();
    return { created: false, operation: existing };
  }
}

const PATCH_STRINGS = new Set([
  "state", "graphDraftId", "graphMessageId", "graphInternetMessageId",
  "graphConversationId", "graphRequestId", "canonicalSentDateTime", "subject",
  "leaseId", "leaseUntilUtc", "nextAttemptUtc", "lastErrorCode", "preparedUtc",
  "sendStartedUtc", "submittedUtc", "reconciledUtc", "completedUtc", "expiresUtc",
]);
const PATCH_NUMBERS = new Set(["prepareAttempts", "sendAttempts", "reconcileAttempts",
  "finalizeAttempts", "attachmentCount"]);

function operationPatch(userId, operationId, patch, nowMs) {
  const entity = { partitionKey: String(userId), rowKey: opKey(operationId), updatedUtc: at(nowMs) };
  for (const [key, value] of Object.entries(patch || {})) {
    if (PATCH_STRINGS.has(key)) entity[key] = clean(value, key === "subject" ? 400 : 1024);
    else if (PATCH_NUMBERS.has(key)) entity[key] = Math.max(0, Number(value) || 0);
    else if (["needsVerification", "complianceCopied"].includes(key)) entity[key] = value === true;
  }
  return entity;
}

async function patchOperation(userId, operationId, patch, etag, options = {}) {
  if (!etag) {
    const err = new Error("A current operation ETag is required.");
    err.statusCode = 409;
    err.code = "direct_send_claim_required";
    throw err;
  }
  await (await table()).updateEntity(operationPatch(userId, operationId, patch, options.nowMs),
    "Merge", { etag });
  return getOperation(userId, operationId);
}

async function claimOperation(userId, operationId, allowedStates, options = {}) {
  const current = await getOperation(userId, operationId);
  if (!current || !allowedStates.includes(current.state) || TERMINAL.has(current.state)) return null;
  const nowMs = options.nowMs === undefined ? Date.now() : Number(options.nowMs);
  if (current.leaseUntilUtc && Date.parse(current.leaseUntilUtc) > nowMs) return null;
  const phase = String(options.phase || "");
  const counter = phase ? `${phase}Attempts` : "";
  const patch = {
    state: options.nextState || current.state,
    leaseId: options.leaseId || crypto.randomUUID(),
    leaseUntilUtc: at(nowMs + (Number(options.leaseSeconds) || 300) * 1000),
    ...(counter && PATCH_NUMBERS.has(counter) ? { [counter]: Number(current[counter]) + 1 } : {}),
  };
  try { return await patchOperation(userId, operationId, patch, current.etag, { nowMs }); }
  catch (err) { if ([404, 412].includes(Number(err.statusCode))) return null; throw err; }
}

async function scheduleOperation(userId, operationId, patch, dueUtc, etag, options = {}) {
  const marker = await optional(String(userId), workKey(operationId));
  if (!marker) {
    const err = new Error("The durable direct-send work marker is missing.");
    err.statusCode = 500;
    err.code = "direct_send_marker_missing";
    throw err;
  }
  const nowUtc = at(options.nowMs);
  const opEntity = operationPatch(userId, operationId,
    { ...patch, leaseId: "", leaseUntilUtc: "", nextAttemptUtc: dueUtc || "" }, options.nowMs);
  const workEntity = { partitionKey: String(userId), rowKey: workKey(operationId),
    state: "pending", dueUtc: dueUtc || nowUtc, leaseId: "", leaseUntilUtc: "", updatedUtc: nowUtc };
  await (await table()).submitTransaction([
    ["update", opEntity, "Merge", { etag }],
    ["update", workEntity, "Merge", { etag: marker.etag }],
  ]);
  return getOperation(userId, operationId);
}

async function completeOperation(userId, operationId, patch, etag, options = {}) {
  const marker = await optional(String(userId), workKey(operationId));
  const nowMs = options.nowMs === undefined ? Date.now() : Number(options.nowMs);
  const finalPatch = operationPatch(userId, operationId, {
    ...patch, state: "complete", leaseId: "", leaseUntilUtc: "", nextAttemptUtc: "",
    completedUtc: patch.completedUtc || at(nowMs),
    expiresUtc: patch.expiresUtc || at(nowMs + 90 * 86400000),
  }, nowMs);
  const actions = [["update", finalPatch, "Merge", { etag }]];
  if (marker) actions.push(["delete", { partitionKey: String(userId), rowKey: workKey(operationId) },
    { etag: marker.etag }]);
  await (await table()).submitTransaction(actions);
  return getOperation(userId, operationId);
}

async function failOperation(userId, operationId, patch, etag, options = {}) {
  const marker = await optional(String(userId), workKey(operationId));
  const nowMs = options.nowMs === undefined ? Date.now() : Number(options.nowMs);
  const finalPatch = operationPatch(userId, operationId, {
    ...patch, state: "failed", leaseId: "", leaseUntilUtc: "", nextAttemptUtc: "",
    expiresUtc: patch.expiresUtc || at(nowMs + 90 * 86400000),
  }, nowMs);
  const actions = [["update", finalPatch, "Merge", { etag }]];
  if (marker) actions.push(["delete", { partitionKey: String(userId), rowKey: workKey(operationId) },
    { etag: marker.etag }]);
  await (await table()).submitTransaction(actions);
  return getOperation(userId, operationId);
}

async function markEnqueued(userId, operationId, dueUtc, options = {}) {
  const marker = await optional(String(userId), workKey(operationId));
  if (!marker) return null;
  const nowUtc = at(options.nowMs);
  try {
    await (await table()).updateEntity({ partitionKey: String(userId), rowKey: workKey(operationId),
      lastEnqueuedUtc: nowUtc, enqueueCount: Number(marker.enqueueCount || 0) + 1,
      dueUtc: dueUtc || at((options.nowMs === undefined ? Date.now() : options.nowMs) + 120000),
      leaseId: "", leaseUntilUtc: "", updatedUtc: nowUtc }, "Merge", { etag: marker.etag });
  } catch (err) { if (![404, 412].includes(Number(err.statusCode))) throw err; }
  return markerFrom(await optional(String(userId), workKey(operationId)));
}

async function listWorkPage(continuationToken = "", maxPageSize = 50) {
  const iter = (await table()).listEntities({ queryOptions: {
    filter: odata`RowKey ge ${WORK_PREFIX} and RowKey lt ${WORK_END}`,
  } }).byPage({ continuationToken: continuationToken || undefined,
    maxPageSize: Math.max(1, Math.min(Number(maxPageSize) || 50, 100)) });
  const page = await iter.next();
  if (page.done) return { markers: [], continuationToken: "" };
  return { markers: [...page.value].map(markerFrom),
    continuationToken: page.value.continuationToken || "" };
}

async function claimMarker(marker, options = {}) {
  const nowMs = options.nowMs === undefined ? Date.now() : Number(options.nowMs);
  if (!marker || (marker.dueUtc && Date.parse(marker.dueUtc) > nowMs)
      || (marker.leaseUntilUtc && Date.parse(marker.leaseUntilUtc) > nowMs)) return null;
  try {
    await (await table()).updateEntity({ partitionKey: marker.userId,
      rowKey: workKey(marker.operationId), state: "dispatching",
      leaseId: options.leaseId || crypto.randomUUID(),
      leaseUntilUtc: at(nowMs + (Number(options.leaseSeconds) || 120) * 1000),
      updatedUtc: at(nowMs) }, "Merge", { etag: marker.etag });
    return markerFrom(await optional(marker.userId, workKey(marker.operationId)));
  } catch (err) { if ([404, 412].includes(Number(err.statusCode))) return null; throw err; }
}

async function getRepairCursor() {
  const row = await optional("__system__", "cursor|direct-repair");
  return row ? { scopeHash: row.scopeHash || "", continuationToken: row.continuationToken || "",
    etag: row.etag } : null;
}

async function putRepairCursor(scopeHash, continuationToken) {
  await (await table()).upsertEntity({ partitionKey: "__system__", rowKey: "cursor|direct-repair",
    scopeHash: clean(scopeHash, 128), continuationToken: clean(continuationToken, 2048), updatedUtc: at() }, "Replace");
}

module.exports = {
  TABLE_NAME, TERMINAL, opKey, workKey, operationFrom, markerFrom,
  getOperation, getMarker, createOperation, patchOperation, claimOperation, scheduleOperation,
  completeOperation, failOperation, markEnqueued, listWorkPage, claimMarker,
  getRepairCursor, putRepairCursor,
};
