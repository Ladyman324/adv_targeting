"use strict";

const crypto = require("crypto");
const { TableClient, odata } = require("@azure/data-tables");

const CONN = process.env.AZURE_STORAGE_CONNECTION_STRING || "";
const NAMES = {
  connections: "EmailConnections", auth: "EmailAuthStates",
  bounceSeen: "EmailBounceSeen", delivery: "EmailDeliveryEvents",
  batches: "EmailBatches", messages: "EmailMessages",
  templates: "EmailTemplates", documents: "EmailDocuments",
  suppressions: "EmailSuppressions", audit: "EmailAudit",
  policy: "EmailPolicy", ledger: "EmailSendLedger",
  // Reply sweep. `sweepState` holds one watermark per rep; `replySeen` makes an
  // overlapping window idempotent; `activity` is the advisor relationship log.
  sweepState: "EmailSweepState", replySeen: "EmailReplySeen",
  activity: "EmailActivity",
  // A PROJECTION over `activity`, not a second source of truth. Partitioned by
  // rep because the queue is always "who should I work next", never "who should
  // anyone work next".
  engagement: "EmailEngagement",
};
const clients = new Map();
const ensured = new Set();

function clean(v, max = 1024) { return v == null ? "" : String(v).slice(0, max); }
function json(v) { return JSON.stringify(v == null ? null : v); }
// Returns the fallback for anything that is not usable JSON, INCLUDING a parse
// that legitimately yields null.
//
// JSON.parse(null) does not throw -- null stringifies to "null", which parses
// back to null -- so the catch never fired and the fallback was skipped. Saving
// a NEW template read images from a row that did not exist yet, got null instead
// of [], and died on `images.map` with "Cannot read properties of null (reading
// 'map')". Every caller here wants a container, so a null result is a miss.
function parse(v, fallback) {
  if (typeof v !== "string" || !v) return fallback;
  try {
    const out = JSON.parse(v);
    return out === null || out === undefined ? fallback : out;
  } catch { return fallback; }
}
function now() { return new Date().toISOString(); }
function id() { return crypto.randomUUID(); }
function batchPartition(userId, batchId) { return `${userId}_${batchId}`; }

async function table(which) {
  const name = NAMES[which];
  if (!name) {
    const err = new Error(`Unknown email storage table key "${String(which)}".`);
    err.statusCode = 500;
    err.code = "email_table_not_declared";
    throw err;
  }
  if (!CONN) {
    const err = new Error("Email storage is not configured: AZURE_STORAGE_CONNECTION_STRING is unset.");
    err.statusCode = 503;
    throw err;
  }
  if (!clients.has(name)) clients.set(name, TableClient.fromConnectionString(CONN, name, { allowInsecureConnection: false }));
  const client = clients.get(name);
  if (!ensured.has(name)) {
    await client.createTable().catch((e) => { if (e.statusCode !== 409) throw e; });
    ensured.add(name);
  }
  return client;
}

async function getOptional(which, partitionKey, rowKey) {
  try { return await (await table(which)).getEntity(partitionKey, rowKey); }
  catch (e) { if (e.statusCode === 404) return null; throw e; }
}

async function putConnection(userId, entity, etag = "") {
  // Whitelist fields so SDK metadata/etags read from Table Storage can never
  // be serialized back as token-cache properties during a refresh.
  const saved = { partitionKey: userId, rowKey: "graph", updatedUtc: now() };
  for (const key of ["userName", "homeAccountId", "mailboxId", "mailbox", "profileJson",
    "tokenCache", "connectedUtc", "needsReconnect"]) {
    if (entity[key] !== undefined) saved[key] = entity[key];
  }
  const client = await table("connections");
  const result = etag
    ? await client.updateEntity(saved, "Replace", { etag })
    : await client.upsertEntity(saved, "Replace");
  // Azure returns the new etag on a successful mutation. Keep the fallback for
  // faithful doubles and older SDK responses, while returning a complete row
  // to callers that need to perform another conditional write.
  if (result && result.etag) return { ...saved, etag: result.etag };
  return getConnection(userId);
}
const getConnection = (userId) => getOptional("connections", userId, "graph");

async function putAuthState(who, state, nonce, returnTo) {
  await (await table("auth")).createEntity({ partitionKey: "oauth", rowKey: state,
    userId: who.id, userName: clean(who.name, 256), nonce, returnTo: clean(returnTo, 300),
    expiresUtc: new Date(Date.now() + 10 * 60 * 1000).toISOString() });
}
// userId is optional. Microsoft redirects the browser back to the callback as a
// cross-site navigation, and the Static Web Apps session cookie does not survive
// that trip -- the callback gets a 401, the config's 401 override bounces it to
// /.auth/login/aad, and the authorization code is discarded. So the callback
// cannot depend on x-ms-client-principal being present. When it is absent the
// state row is the identity: 24 random bytes, single-use, ten-minute expiry,
// written against this user when the flow began. The binding that actually
// matters is downstream in complete(), where the Graph profile id must equal
// this userId before any token is stored.
async function consumeAuthState(state, userId = null) {
  const client = await table("auth");
  const e = await getOptional("auth", "oauth", state);
  if (!e || (userId && e.userId !== userId) || new Date(e.expiresUtc).getTime() < Date.now()) {
    const err = new Error("Microsoft connection state is missing, expired, or belongs to another user.");
    err.statusCode = 400;
    throw err;
  }
  await client.deleteEntity("oauth", state, { etag: e.etag });
  return e;
}

function batchFromEntity(e) {
  if (!e) return null;
  return { id: e.rowKey, userId: e.partitionKey, userName: e.userName,
    status: e.status, mode: e.mode || "", name: e.name || "",
    templateId: e.templateId, templateName: e.templateName,
    commonSubject: e.commonSubject || "", commonBodyText: e.commonBodyText || "",
    commonRevision: Number(e.commonRevision) || 1,
    attachmentIds: parse(e.attachmentIdsJson, []), attachmentSummary: parse(e.attachmentSummaryJson, []),
    recipientCount: Number(e.recipientCount) || 0, externalCount: Number(e.externalCount) || 0,
    // Campaign health. Recomputed from the messages by refreshBatch() rather
    // than incremented, so concurrent workers cannot race the counter.
    sentCount: Number(e.sentCount) || 0, hardBounceCount: Number(e.hardBounceCount) || 0,
    warningLevel: e.warningLevel || "normal", warningMessage: e.warningMessage || "",
    signatureHtml: e.signatureHtml || "", graphMailboxId: e.graphMailboxId || "",
    graphMailbox: e.graphMailbox || "", reviewedUtc: e.reviewedUtc || "",
    copySelf: e.copySelf || "", copyInternal: e.copyInternal || "",
    ccTeammates: e.ccTeammates === "1", ccColleague: e.ccColleague || "",
    copiedInsteadNote: e.copiedInsteadNote || "",
    /* THE FOLLOW-UP CHAIN.
     *
     * followUpDays is the rep's answer to "if nobody replies, remind me when" --
     * chosen when the batch is built, because that is when they know what the
     * email is for. 0 means no reminder, and it is the default.
     *
     * parentBatchId points BACK at the campaign a follow-up came from, so the
     * chain is walkable in one direction and a follow-up can never be mistaken
     * for an original send. followUpSentUtc on the parent is the guard against
     * a rep running it twice and putting a third touch on 22 people.
     */
    followUpDays: Number(e.followUpDays) || 0,
    parentBatchId: e.parentBatchId || "",
    followUpSentUtc: e.followUpSentUtc || "",
    copyInternalTo: e.copyInternalTo || "", senderMail: e.senderMail || "",
    recipientRegistryHash: e.recipientRegistryHash || "",
    approvedUtc: e.approvedUtc || "", sendNotBeforeUtc: e.sendNotBeforeUtc || "",
    pausedUtc: e.pausedUtc || "", canceledUtc: e.canceledUtc || "",
    createdUtc: e.createdUtc, updatedUtc: e.updatedUtc, etag: e.etag };
}

async function createBatch(who, batch) {
  const at = now();
  const entity = { partitionKey: who.id, rowKey: batch.id, userName: clean(who.name, 256),
    status: batch.status || "editing", mode: "", name: clean(batch.name, 120),
    templateId: clean(batch.templateId, 80), templateName: clean(batch.templateName, 120),
    commonSubject: clean(batch.commonSubject, 500), commonBodyText: clean(batch.commonBodyText, 50000),
    commonRevision: 1, attachmentIdsJson: json(batch.attachmentIds || []),
    attachmentSummaryJson: json(batch.attachmentSummary || []), recipientCount: batch.recipientCount,
    externalCount: batch.externalCount, warningLevel: batch.warningLevel || "normal",
    warningMessage: clean(batch.warningMessage, 500), signatureHtml: clean(batch.signatureHtml, 50000),
    graphMailboxId: clean(batch.graphMailboxId, 100), graphMailbox: clean(batch.graphMailbox, 256),
    /* The rep's copy preferences AS THEY WERE when the batch was built, plus
     * the address a self-copy goes to.
     *
     * Snapshotted rather than read at draft time on purpose: a rep who changes
     * the setting while a batch is going out would otherwise split it, half
     * copied and half not, with nothing recording which was which. The
     * compliance blind copy is the opposite case and stays recomputed at draft
     * time -- it is policy, not preference.
     */
    copySelf: clean(batch.copySelf, 8), copyInternal: clean(batch.copyInternal, 8),
    ccTeammates: clean(batch.ccTeammates, 2), ccColleague: clean(batch.ccColleague, 254),
    copiedInsteadNote: clean(batch.copiedInsteadNote, 500),
    copyInternalTo: clean(batch.copyInternalTo, 254), senderMail: clean(batch.senderMail, 254),
    recipientRegistryHash: clean(batch.recipientRegistryHash, 128),
    createdUtc: at, updatedUtc: at };
  await (await table("batches")).createEntity(entity);
  return batchFromEntity(entity);
}
const getBatch = async (userId, batchId) => batchFromEntity(await getOptional("batches", userId, batchId));

async function patchBatch(userId, batchId, patch, etag) {
  const entity = { partitionKey: userId, rowKey: batchId, updatedUtc: now() };
  const strings = ["status", "mode", "name", "commonSubject", "commonBodyText", "warningLevel",
    "warningMessage", "reviewedUtc", "approvedUtc", "sendNotBeforeUtc", "pausedUtc", "canceledUtc",
    "parentBatchId", "followUpSentUtc", "recipientRegistryHash"];
  for (const k of strings) if (k in patch) entity[k] = clean(patch[k], k.includes("Body") ? 50000 : 500);
  for (const k of ["commonRevision", "recipientCount", "externalCount", "sentCount",
                   "hardBounceCount", "followUpDays"]) if (k in patch) entity[k] = Number(patch[k]) || 0;
  for (const [key, field] of [["attachmentIds", "attachmentIdsJson"], ["attachmentSummary", "attachmentSummaryJson"]])
    if (key in patch) entity[field] = json(patch[key]);
  await (await table("batches")).updateEntity(entity, "Merge", etag ? { etag } : undefined);
  return getBatch(userId, batchId);
}

async function listBatches(userId, limit = 30) {
  const out = [];
  const iter = (await table("batches")).listEntities({ queryOptions: { filter: odata`PartitionKey eq ${userId}` } });
  for await (const e of iter) out.push(batchFromEntity(e));
  return out.sort((a, b) => String(b.createdUtc).localeCompare(String(a.createdUtc))).slice(0, limit);
}

function messageFromEntity(e) {
  if (!e) return null;
  return { id: e.rowKey, batchId: e.batchId, userId: e.userId, ordinal: Number(e.ordinal) || 0,
    contactId: e.contactId || "", recipientName: e.recipientName || "", recipientEmail: e.recipientEmail || "",
    greetingName: e.greetingName || "", recipientLastName: e.recipientLastName || "",
    recipientRegistryHash: e.recipientRegistryHash || "", recipientRoutingHash: e.recipientRoutingHash || "",
    recipientTier: e.recipientTier || "",
    recipientSource: e.recipientSource || "",
    recipientMatchScore: Number(e.recipientMatchScore) >= 0 ? Number(e.recipientMatchScore) : null,
    recipientMatchGap: Number(e.recipientMatchGap) >= 0 ? Number(e.recipientMatchGap) : null,
    recipientPolicyVersion: e.recipientPolicyVersion || "",
    teammateCc: parse(e.teammateCcJson, []),
    teammateCcCrds: parse(e.teammateCcCrdsJson, []),
    teammatesAvailable: parse(e.teammatesAvailableJson, []),
    companyName: e.companyName || "", subject: e.subject || "", bodyText: e.bodyText || "",
    bodyHtml: e.bodyHtml || "", signatureHtml: e.signatureHtml || "",
    inlineImages: parse(e.inlineImagesJson, []), state: e.state,
    subjectOverridden: !!e.subjectOverridden, bodyOverridden: !!e.bodyOverridden,
    baseRevision: Number(e.baseRevision) || 1, reviewed: !!e.reviewed,
    validation: parse(e.validationJson, { errors: [], warnings: [] }),
    attachments: parse(e.attachmentsJson, []), graphMessageId: e.graphMessageId || "",
    graphInternetMessageId: e.graphInternetMessageId || "",
    graphConversationId: e.graphConversationId || "", graphRequestId: e.graphRequestId || "",
    followUpOfGraphId: e.followUpOfGraphId || "",
    draftCreatedUtc: e.draftCreatedUtc || "", queuedUtc: e.queuedUtc || "",
    sendStartedUtc: e.sendStartedUtc || "", submittedUtc: e.submittedUtc || "",
    failureCode: e.failureCode || "", failureMessage: e.failureMessage || "",
    bounceKind: e.bounceKind || "", bounceAtUtc: e.bounceAtUtc || "", bounceReason: e.bounceReason || "",
    retryAfterUtc: e.retryAfterUtc || "", attemptCount: Number(e.attemptCount) || 0,
    // Per-phase counters. attemptCount above is the total across every phase and
    // is kept for the audit trail; the retry ceilings use these, because one
    // shared counter meant a message that fought through five draft retries
    // arrived at its first SEND attempt with no allowance left.
    // Where this message sits in the SEND order, which is not list order --
    // see core.interleaveByDomain(). -1 until approval assigns one.
    sendPosition: e.sendPosition === undefined || e.sendPosition === null
      ? -1 : Number(e.sendPosition),
    draftAttempts: Number(e.draftAttempts) || 0,
    sendAttempts: Number(e.sendAttempts) || 0,
    reconcileAttempts: Number(e.reconcileAttempts) || 0,
    leaseUntilUtc: e.leaseUntilUtc || "", createdUtc: e.createdUtc, updatedUtc: e.updatedUtc, etag: e.etag };
}

async function createMessage(userId, batchId, message) {
  const at = now();
  await (await table("messages")).createEntity({ partitionKey: batchPartition(userId, batchId), rowKey: message.id,
    batchId, userId, ordinal: message.ordinal, contactId: clean(message.contactId, 80),
    recipientName: clean(message.recipientName, 256), recipientEmail: clean(message.recipientEmail, 320).toLowerCase(),
    greetingName: clean(message.greetingName, 120), recipientLastName: clean(message.recipientLastName, 120),
    recipientRegistryHash: clean(message.recipientRegistryHash, 128),
    recipientRoutingHash: clean(message.recipientRoutingHash, 128),
    recipientTier: clean(message.recipientTier, 40),
    recipientSource: clean(message.recipientSource, 120),
    recipientMatchScore: message.recipientMatchScore === null
      || message.recipientMatchScore === undefined || message.recipientMatchScore === ""
      ? -1 : (Number.isFinite(Number(message.recipientMatchScore)) ? Number(message.recipientMatchScore) : -1),
    recipientMatchGap: message.recipientMatchGap === null
      || message.recipientMatchGap === undefined || message.recipientMatchGap === ""
      ? -1 : (Number.isFinite(Number(message.recipientMatchGap)) ? Number(message.recipientMatchGap) : -1),
    recipientPolicyVersion: clean(message.recipientPolicyVersion, 80),
    teammateCcJson: clean(message.teammateCcJson, 2000),
    teammateCcCrdsJson: clean(message.teammateCcCrdsJson, 2000),
    teammatesAvailableJson: clean(message.teammatesAvailableJson, 8000),
    companyName: clean(message.companyName, 256), subject: clean(message.subject, 500),
    bodyText: clean(message.bodyText, 50000), bodyHtml: clean(message.bodyHtml, 50000),
    signatureHtml: clean(message.signatureHtml, 50000),
    inlineImagesJson: json(message.inlineImages || []), state: message.state || "editing",
    subjectOverridden: false, bodyOverridden: false, baseRevision: message.baseRevision || 1,
    reviewed: false, validationJson: json(message.validation || { errors: [], warnings: [] }),
    attachmentsJson: json(message.attachments || []), attemptCount: 0,
    draftAttempts: 0, sendAttempts: 0, reconcileAttempts: 0, createdUtc: at, updatedUtc: at });
}
const getMessage = async (userId, batchId, messageId) => messageFromEntity(
  await getOptional("messages", batchPartition(userId, batchId), messageId));

async function listMessages(userId, batchId) {
  const pk = batchPartition(userId, batchId), out = [];
  const iter = (await table("messages")).listEntities({ queryOptions: { filter: odata`PartitionKey eq ${pk}` } });
  for await (const e of iter) out.push(messageFromEntity(e));
  return out.sort((a, b) => a.ordinal - b.ordinal);
}

async function patchMessage(userId, batchId, messageId, patch, etag) {
  const entity = { partitionKey: batchPartition(userId, batchId), rowKey: messageId, updatedUtc: now() };
  const strings = ["subject", "bodyText", "bodyHtml", "state", "graphMessageId", "graphInternetMessageId",
    "graphConversationId",
    // The sent message this one is a follow-up to. Carries the Graph id rather
    // than the conversation id because the worker replies to a MESSAGE.
    "followUpOfGraphId",
    "graphRequestId", "draftCreatedUtc", "queuedUtc", "sendStartedUtc", "submittedUtc", "failureCode",
    "failureMessage", "bounceKind", "bounceAtUtc", "bounceReason", "retryAfterUtc", "leaseUntilUtc",
    /* THE THIRD TIME THIS WHITELIST ATE A FEATURE.
     *
     * updateMessageCc() writes teammateCcJson here. It was not in this list, so
     * patchMessage accepted the call, returned success, and stored nothing --
     * a rep ticked a teammate, the picker re-rendered from a message that had
     * never changed, and the tick was gone. No error, anywhere.
     *
     * The same omission cost conversationId on the way in and the teammate list
     * on the way out of the client. A hand-kept list of field names, in a
     * language with no compiler to notice one missing, is a place features go
     * to disappear quietly -- so it is checked in audit.py now rather than
     * remembered.
     */
    "teammateCcJson", "teammateCcCrdsJson", "teammatesAvailableJson",
    "greetingName", "recipientLastName",
    "recipientRegistryHash", "recipientRoutingHash",
    "recipientTier", "recipientSource", "recipientPolicyVersion"];
  for (const k of strings) if (k in patch) entity[k] = clean(patch[k], ["bodyText", "bodyHtml"].includes(k) ? 50000 : 2000);
  for (const k of ["subjectOverridden", "bodyOverridden", "reviewed"]) if (k in patch) entity[k] = !!patch[k];
  for (const k of ["baseRevision", "attemptCount", "draftAttempts", "sendAttempts",
                   "reconcileAttempts", "sendPosition", "hardBounceCount"])
    if (k in patch) entity[k] = Number(patch[k]) || 0;
  for (const k of ["recipientMatchScore", "recipientMatchGap"]) {
    if (!(k in patch)) continue;
    const value = patch[k];
    entity[k] = value === null || value === undefined || value === ""
      ? -1 : (Number.isFinite(Number(value)) ? Number(value) : -1);
  }
  if ("validation" in patch) entity.validationJson = json(patch.validation);
  if ("attachments" in patch) entity.attachmentsJson = json(patch.attachments);
  await (await table("messages")).updateEntity(entity, "Merge", etag ? { etag } : undefined);
  return getMessage(userId, batchId, messageId);
}

// `phase` is "draft", "send" or "reconcile". Each keeps its own attempt counter
// so a phase spends only its own budget -- see the note in messageFromEntity.
async function claimMessage(userId, batchId, messageId, allowedStates, nextState,
                            leaseSeconds = 120, phase = "") {
  const m = await getMessage(userId, batchId, messageId);
  if (!m || !allowedStates.includes(m.state)) return null;
  if (m.leaseUntilUtc && new Date(m.leaseUntilUtc).getTime() > Date.now()) return null;
  const counter = phase ? `${phase}Attempts` : "";
  try {
    return await patchMessage(userId, batchId, messageId, { state: nextState,
      leaseUntilUtc: new Date(Date.now() + leaseSeconds * 1000).toISOString(),
      attemptCount: m.attemptCount + 1,
      ...(counter ? { [counter]: (Number(m[counter]) || 0) + 1 } : {}) }, m.etag);
  } catch (e) { if (e.statusCode === 412) return null; throw e; }
}

async function deleteMessage(userId, batchId, messageId) {
  await (await table("messages")).deleteEntity(batchPartition(userId, batchId), messageId);
}

async function listTemplates() {
  const client = await table("templates");
  let out = [];
  for await (const e of client.listEntities({ queryOptions: { filter: odata`PartitionKey eq ${"approved"}` } })) {
    if (e.approved !== false) out.push({ id: e.rowKey, name: e.name, subject: e.subject,
      bodyText: e.bodyText, defaultAttachmentIds: parse(e.defaultAttachmentIdsJson, []),
      requiredDocumentIds: parse(e.requiredDocumentIdsJson, []),
      images: parse(e.imagesJson, []), documentNumber: e.documentNumber || "",
      author: e.author || "", approvalDate: e.approvalDate || "",
      repNotes: e.repNotes || "", status: e.status || "approved",
      /* THREE STATES, NOT TWO.
       *
       * `retired` already existed and hides a template from everyone, admins
       * included -- a soft delete with no way back. What was missing is a
       * template that LIVES in the library but is not yet cleared for the sales
       * team: written, being reviewed, not to be sent.
       *
       * Absent means published, so every template that exists today keeps
       * working. Only templates created after this default to held.
       */
      published: e.published !== false,
      version: Number(e.version) || 1 });
  }
  /* The starter templates are seeded ONCE, not whenever the list is empty.
   *
   * "Empty means seed" meant an administrator who removed all four got them back
   * on the next page load, with no way to refuse them -- and these are exactly
   * the templates that have NOT been through compliance review, so a catalog
   * that is supposed to contain only approved wording kept refilling itself with
   * unapproved wording.
   *
   * The marker lives in this table under a different partition key, so it never
   * appears in the list itself.
   */
  if (!out.length && !(await getOptional("templates", "seed", "builtin"))) {
    const core = require("./email-core");
    for (const t of core.BUILTIN_TEMPLATES) await client.upsertEntity({ partitionKey: "approved", rowKey: t.id,
      name: t.name, subject: t.subject, bodyText: t.bodyText,
      defaultAttachmentIdsJson: json(t.defaultAttachmentIds), version: 1, approved: true, updatedUtc: now() }, "Replace");
    await markTemplatesSeeded("first run");
    out = core.BUILTIN_TEMPLATES.map((t) => ({ ...t, version: 1 }));
  }
  return out;
}

async function markTemplatesSeeded(why) {
  await (await table("templates")).upsertEntity({ partitionKey: "seed", rowKey: "builtin",
    seededUtc: now(), reason: clean(why, 200) }, "Replace");
}
const getTemplate = async (templateId) => (await listTemplates()).find((t) => t.id === templateId) || null;

// ---------------------------------------------------------------------------
// Template authoring, mirroring the Word header block the marketing library
// already uses: Title, Original Author, Document #, Attachments required,
// Approval Date. Moving templates into the application should not lose the
// compliance record that made the Word version trustworthy.
//
// requiredDocumentIds is the field that changes behaviour rather than merely
// recording something: a batch cannot be approved without those attachments
// present and current, which turns a line of prose into a control.
// ---------------------------------------------------------------------------
async function putTemplate(who, input) {
  const core = require("./email-core");
  const id = safeDocId(input.id || input.name);
  const subject = clean(input.subject, 500);
  const bodyText = clean(input.bodyText, 30000);
  const lint = core.lintTemplate({ subject, bodyText, maxBodyChars: core.config().maxBodyChars });
  const existing = await getOptional("templates", "approved", id);
  // existing is null for a template being created, which is the normal case here.
  const images = parse(existing ? existing.imagesJson : "", []);
  // Every {{image:x}} must name an image approved onto THIS template. Otherwise
  // the token is sent as literal text, which is the same silent failure the
  // merge-field linting exists to prevent.
  const known = new Set(images.map((i) => String(i.id).toLowerCase()));
  const missing = [...new Set([...`${subject}\n${bodyText}`.matchAll(core.IMAGE_TOKEN)]
    .map((m) => String(m[1]).toLowerCase()).filter((x) => !known.has(x)))];
  if (missing.length) lint.errors.push({ code: "unknown_image",
    message: `No image on this template is called ${missing.map((x) => `"${x}"`).join(", ")}. `
      + `Upload it first, or remove the placeholder.` });
  if (lint.errors.length) {
    const err = new Error(lint.errors.map((e) => e.message).join(" "));
    err.statusCode = 400; err.code = "template_invalid"; err.errors = lint.errors;
    throw err;
  }
  const entity = { partitionKey: "approved", rowKey: id,
    name: clean(input.name || id, 256), subject, bodyText,
    documentNumber: clean(input.documentNumber, 60),
    author: clean(input.author, 120),
    approvalDate: clean(input.approvalDate, 40),
    repNotes: clean(input.repNotes, 4000),
    requiredDocumentIdsJson: json((input.requiredDocumentIds || []).map((x) => safeDocId(x))),
    defaultAttachmentIdsJson: json(input.requiredDocumentIds || []),
    imagesJson: json(images),
    status: input.status === "retired" ? "retired" : "approved",
    approved: input.status !== "retired",
    // A NEW template starts held: the whole point is that nobody can send it
    // until an administrator says so. An EXISTING one keeps whatever it had,
    // so editing the wording of a live template does not silently withdraw it
    // from the team mid-campaign.
    published: existing ? existing.published !== false : false,
    version: (Number(existing && existing.version) || 0) + 1,
    updatedUtc: now(), updatedBy: clean(who && who.name, 256) };
  await (await table("templates")).upsertEntity(entity, "Replace");
  return { id, version: entity.version, warnings: lint.warnings, replaced: !!existing };
}

/* Clear a template for the sales team, or withdraw it.
 *
 * Separate from putTemplate on purpose: publishing is an approval, not an edit.
 * Bundling the two would mean a rep-visible template could change wording and
 * approval state in one write, and the audit trail could not tell which of the
 * two an administrator actually intended.
 */
async function setTemplatePublished(who, rawId, published) {
  const id = safeDocId(rawId);
  const existing = await getOptional("templates", "approved", id);
  if (!existing) {
    const err = new Error("That template no longer exists.");
    err.statusCode = 404; throw err;
  }
  await (await table("templates")).updateEntity({
    partitionKey: "approved", rowKey: id, published: published === true,
    updatedUtc: now(), updatedBy: clean(who && who.name, 256),
  }, "Merge");
  return { id, name: existing.name || id, published: published === true };
}

async function deleteTemplate(who, rawId) {
  const id = safeDocId(rawId);
  const existing = await getOptional("templates", "approved", id);
  if (!existing) { const e = new Error("That template does not exist."); e.statusCode = 404; throw e; }
  await (await table("templates")).deleteEntity("approved", id);
  await markTemplatesSeeded(`curated: ${id} removed`);
  for (const image of parse(existing.imagesJson, [])) {
    try { if (image.blobName) await (await imageContainer()).getBlockBlobClient(image.blobName).deleteIfExists(); }
    catch { /* orphan blob costs pennies */ }
  }
  return { id, name: existing.name || id };
}

// PNG, GIF and JPEG only, checked by their own leading bytes rather than by the
// extension or the browser's claimed type -- both are supplied by the uploader.
const IMAGE_MAGIC = [
  ["image/png", Buffer.from([0x89, 0x50, 0x4e, 0x47])],
  ["image/jpeg", Buffer.from([0xff, 0xd8, 0xff])],
  ["image/gif", Buffer.from([0x47, 0x49, 0x46, 0x38])],
];
function imageTypeOf(bytes) {
  for (const [type, magic] of IMAGE_MAGIC) if (bytes.slice(0, magic.length).equals(magic)) return type;
  const err = new Error("Charts must be PNG, JPEG or GIF images.");
  err.statusCode = 400;
  throw err;
}

async function imageContainer() {
  if (!CONN) { const e = new Error("Email storage is not configured."); e.statusCode = 503; throw e; }
  const { BlobServiceClient } = require("@azure/storage-blob");
  const name = process.env.EMAIL_IMAGE_CONTAINER || "email-images";
  const c = BlobServiceClient.fromConnectionString(CONN).getContainerClient(name);
  await c.createIfNotExists();
  return c;
}

async function putTemplateImage(who, { templateId, imageId, name, bytes, maxBytes }) {
  const tid = safeDocId(templateId), iid = safeDocId(imageId || name);
  const existing = await getOptional("templates", "approved", tid);
  if (!existing) { const e = new Error("Save the template before adding charts to it."); e.statusCode = 404; throw e; }
  const contentType = imageTypeOf(bytes);
  if (maxBytes && bytes.length > maxBytes) {
    const e = new Error("That image exceeds the application attachment limit."); e.statusCode = 400; throw e;
  }
  const sha256 = crypto.createHash("sha256").update(bytes).digest("hex");
  const blobName = `${tid}/${iid}-${sha256.slice(0, 16)}`;
  await (await imageContainer()).getBlockBlobClient(blobName).uploadData(bytes,
    { blobHTTPHeaders: { blobContentType: contentType }, metadata: { sha256 } });
  const images = parse(existing.imagesJson, []).filter((i) => i.id !== iid);
  images.push({ id: iid, name: clean(name || iid, 200), blobName, contentType,
    size: bytes.length, sha256, cid: `${iid}@eicadvisormap` });
  await (await table("templates")).upsertEntity({ partitionKey: "approved", rowKey: tid,
    imagesJson: json(images), updatedUtc: now(), updatedBy: clean(who && who.name, 256) }, "Merge");
  return { templateId: tid, imageId: iid, placeholder: `{{image:${iid}}}`, images };
}

async function deleteTemplateImage(who, templateId, imageId) {
  const tid = safeDocId(templateId), iid = safeDocId(imageId);
  const existing = await getOptional("templates", "approved", tid);
  if (!existing) { const e = new Error("That template does not exist."); e.statusCode = 404; throw e; }
  const all = parse(existing.imagesJson, []);
  const gone = all.find((i) => i.id === iid);
  // Refuse while the body still references it: allowing the delete would turn a
  // rendered chart into the literal text "{{image:chart}}" on the next send.
  if (String(existing.bodyText || "").toLowerCase().includes(`{{image:${iid}}}`)) {
    const e = new Error(`The body still uses {{image:${iid}}}. Remove the placeholder first.`);
    e.statusCode = 409; throw e;
  }
  await (await table("templates")).upsertEntity({ partitionKey: "approved", rowKey: tid,
    imagesJson: json(all.filter((i) => i.id !== iid)), updatedUtc: now() }, "Merge");
  try { if (gone && gone.blobName) await (await imageContainer()).getBlockBlobClient(gone.blobName).deleteIfExists(); }
  catch { /* orphan blob costs pennies */ }
  return { templateId: tid, imageId: iid };
}

async function templateImageBytes(image) {
  const blob = (await imageContainer()).getBlobClient(image.blobName);
  const bytes = await blob.downloadToBuffer();
  const sha256 = crypto.createHash("sha256").update(bytes).digest("hex");
  if (image.sha256 && sha256 !== image.sha256)
    throw new Error(`Chart ${image.name} changed since it was approved; re-upload it.`);
  return bytes;
}

async function listDocuments() {
  const out = [];
  for await (const e of (await table("documents")).listEntities({ queryOptions: { filter: odata`PartitionKey eq ${"approved"}` } })) {
    if (e.approved !== false) out.push({ id: e.rowKey, name: e.name, blobName: e.blobName,
      // The name the file was uploaded under, kept apart from the display name
      // so an advisor receives the document called what it is actually called.
      // Empty on anything published before this was recorded, which is why
      // attachmentFileName() still falls back to the display name.
      fileName: e.fileName || "",
      contentType: e.contentType || "application/octet-stream", size: Number(e.size) || 0,
      sha256: e.sha256 || "", version: Number(e.version) || 1, approved: true });
  }
  return out.sort((a, b) => a.name.localeCompare(b.name));
}
async function getDocuments(ids) {
  const wanted = new Set(ids || []); return (await listDocuments()).filter((d) => wanted.has(d.id));
}

// ---------------------------------------------------------------------------
// Approved-document management, so a compliance-approved PDF can be published
// or withdrawn from the application instead of from a shell session.
//
// PDF only, and checked by CONTENT rather than by the browser's claimed MIME
// type or the file extension -- both are supplied by whoever is uploading and
// neither is evidence. A renamed .docx would otherwise reach an advisor as a
// file Outlook cannot open, and we would find out from the advisor.
// ---------------------------------------------------------------------------
function assertPdf(bytes) {
  // %PDF-  -- the header every conforming PDF opens with.
  const header = bytes.slice(0, 5).toString("latin1");
  if (header !== "%PDF-") {
    const err = new Error("That file is not a PDF. Only PDF documents can be approved as attachments.");
    err.statusCode = 400;
    throw err;
  }
}

function safeDocId(value) {
  const out = String(value || "").toLowerCase().replace(/[^a-z0-9-]/g, "-")
    .replace(/-+/g, "-").replace(/^-|-$/g, "").slice(0, 80);
  if (!out) {
    const err = new Error("Document id must contain letters or numbers.");
    err.statusCode = 400;
    throw err;
  }
  return out;
}

async function container() {
  if (!CONN) {
    const err = new Error("Email storage is not configured.");
    err.statusCode = 503;
    throw err;
  }
  const { BlobServiceClient } = require("@azure/storage-blob");
  const name = process.env.EMAIL_DOCUMENT_CONTAINER || "email-documents";
  const c = BlobServiceClient.fromConnectionString(CONN).getContainerClient(name);
  await c.createIfNotExists();
  return c;
}

/* TWO NAMES, ON PURPOSE.
 *
 * `name` is the display name -- what an administrator types so the picker in
 * the app reads well ("Q2 2026 ACV & LCV Client Commentary"). `fileName` is
 * what the file was called on the way in ("EIC_ACV_LCV_Q2_2026.pdf"), and that
 * is what the advisor should see land in their inbox: it is the name their
 * compliance archive, their filing and any later conversation will use.
 *
 * Conflating the two meant the attachment arrived under a display name shaped
 * for a dropdown, which is a fine label and a poor filename.
 */
async function putDocument(who, { id: rawId, name, fileName, bytes, maxBytes }) {
  const docId = safeDocId(rawId || name);
  assertPdf(bytes);
  if (maxBytes && bytes.length > maxBytes) {
    const err = new Error("That PDF exceeds the application attachment limit.");
    err.statusCode = 400;
    throw err;
  }
  const sha256 = crypto.createHash("sha256").update(bytes).digest("hex");
  const c = await container();
  const blobName = `${docId}/${sha256.slice(0, 16)}.pdf`;
  await c.getBlockBlobClient(blobName).uploadData(bytes, {
    blobHTTPHeaders: { blobContentType: "application/pdf" },
    metadata: { approved: "true", sha256 },
  });
  const client = await table("documents");
  const old = await getOptional("documents", "approved", docId);
  // Version increments on every publish. validateMessage() compares the version
  // and hash a batch was built with against the current row, so replacing a
  // document automatically invalidates batches still carrying the old one --
  // which is the entire point of doing it this way.
  // "Replace", not "Merge" -- so an upload that supplies no fileName clears a
  // stale one rather than leaving the previous file's name attached to new
  // bytes. Falling back to the old value would be worse than falling back to
  // the display name: it would be confidently wrong.
  const storedFileName = clean(fileName || "", 256);
  await client.upsertEntity({ partitionKey: "approved", rowKey: docId,
    name: clean(name || docId, 256), fileName: storedFileName,
    blobName, contentType: "application/pdf",
    size: bytes.length, sha256, version: (Number(old && old.version) || 0) + 1,
    approved: true, updatedUtc: now(), updatedBy: clean(who && who.name, 256) }, "Replace");
  return { id: docId, name: clean(name || docId, 256), fileName: storedFileName,
           size: bytes.length, sha256,
           version: (Number(old && old.version) || 0) + 1, replaced: !!old };
}

async function deleteDocument(who, rawId) {
  const docId = safeDocId(rawId);
  const existing = await getOptional("documents", "approved", docId);
  if (!existing) {
    const err = new Error("That document is not in the approved catalog.");
    err.statusCode = 404;
    throw err;
  }
  // The table row goes first: it is what listDocuments() reads, so removing it
  // takes the document out of circulation immediately even if the blob delete
  // fails. The reverse order would leave a selectable document whose bytes are
  // gone, which fails at send time in front of a rep.
  await (await table("documents")).deleteEntity("approved", docId);
  try {
    if (existing.blobName) await (await container()).getBlockBlobClient(existing.blobName).deleteIfExists();
  } catch { /* orphan blob costs pennies; a listed-but-missing document costs a send */ }
  return { id: docId, name: existing.name || docId };
}

async function getSuppression(email) {
  const key = crypto.createHash("sha256").update(String(email || "").trim().toLowerCase()).digest("hex");
  return getOptional("suppressions", "email", key);
}
async function suppressEmail(email, info) {
  const normalized = String(email || "").trim().toLowerCase();
  const key = crypto.createHash("sha256").update(normalized).digest("hex");
  await (await table("suppressions")).upsertEntity({ partitionKey: "email", rowKey: key,
    email: normalized, kind: clean(info.kind, 40), reason: clean(info.reason, 1000),
    messageId: clean(info.messageId, 80), atUtc: info.atUtc || now(), active: true }, "Replace");
}


/* ---------- bounce sweeping support -------------------------------------- */

// Every mailbox the sweeper should look at. Connections are one row per user.
async function listConnections() {
  const out = [];
  for await (const e of (await table("connections")).listEntities()) {
    if (e.rowKey !== "graph") continue;
    out.push({ userId: e.partitionKey, mailbox: e.mailbox || "",
               needsReconnect: !!e.needsReconnect });
  }
  return out;
}

// Messages this user actually sent, indexed by the Internet message id an NDR
// will quote back at us. Bounded by age: a report arriving three months after
// the send is not something to act on automatically.
async function sentByInternetId(userId, sinceUtc) {
  const index = new Map();
  for await (const e of (await table("messages")).listEntities({
    queryOptions: { filter: odata`PartitionKey ge ${userId} and PartitionKey lt ${userId + "~"}` } })) {
    if (!["submitted", "sent"].includes(e.state)) continue;
    const id = String(e.graphInternetMessageId || "").replace(/^<|>$/g, "").toLowerCase();
    if (!id) continue;
    if (sinceUtc && e.submittedUtc && e.submittedUtc < sinceUtc) continue;
    // batchPartition() is `${userId}_${batchId}`; slice rather than split, since
    // a batch id is a UUID and splitting on "_" would be right by luck only.
    index.set(id, { id: e.rowKey, batchId: e.partitionKey.slice(String(userId).length + 1),
                    recipientEmail: e.recipientEmail, recipientName: e.recipientName,
                    contactId: e.contactId || "", conversationId: e.graphConversationId || "",
                    etag: e.etag });
  }
  return index;
}

// One row per NDR already looked at, so a sweep is idempotent and a rep's inbox
// is never modified -- marking their mail read or moving it would be our tool
// reaching into a mailbox it was only lent for sending.
async function bounceAlreadySeen(userId, graphMessageId) {
  const row = await getOptional("bounceSeen", userId, clean(graphMessageId, 500));
  return !!row;
}
async function markBounceSeen(userId, graphMessageId, outcome) {
  await (await table("bounceSeen")).upsertEntity({ partitionKey: userId,
    rowKey: clean(graphMessageId, 500), outcome: clean(outcome, 80), atUtc: now() }, "Replace");
}


/* ---------- delivery events, for sender health ---------------------------
 *
 * One row per delivery report we could attribute to a message we sent. Hard
 * bounces, soft deferrals and policy rejections all land here; only the hard
 * ones additionally suppress.
 *
 * Deferrals and policy rejections were previously classified and thrown away,
 * which meant the earliest evidence that a receiving gateway is throttling you
 * was invisible. That evidence arrives weeks before outright refusals, and it
 * arrives per DOMAIN, which is the level any remedy has to act at.
 *
 * Partitioned by user so a per-rep read is a single partition scan; the row key
 * is time-ordered newest-first so recent history is cheap to take.
 */
async function recordDeliveryEvent(userId, event) {
  const at = new Date();
  const rowKey = `${String(9999999999999 - at.getTime()).padStart(13, "0")}-${crypto.randomBytes(4).toString("hex")}`;
  await (await table("delivery")).createEntity({
    partitionKey: String(userId), rowKey, atUtc: at.toISOString(),
    kind: clean(event.kind, 20),                 // hard | soft | policy
    code: clean(event.code, 20),                 // 5.1.1, 4.4.7, 5.7.1 ...
    domain: clean(event.domain, 120).toLowerCase(),
    address: clean(event.address, 320).toLowerCase(),
    batchId: clean(event.batchId, 80),
    messageId: clean(event.messageId, 80),
    detail: clean(event.detail, 300),
  });
}

async function deliveryEvents(userId, sinceUtc = "") {
  const out = [];
  for await (const e of (await table("delivery")).listEntities({
    queryOptions: { filter: odata`PartitionKey eq ${String(userId)}` } })) {
    if (sinceUtc && e.atUtc < sinceUtc) continue;
    out.push({ atUtc: e.atUtc, kind: e.kind, code: e.code, domain: e.domain,
               address: e.address, batchId: e.batchId, messageId: e.messageId,
               detail: e.detail });
  }
  return out;
}

async function audit(userId, batchId, event, details = {}) {
  const at = new Date();
  const rowKey = `${String(9999999999999 - at.getTime()).padStart(13, "0")}-${crypto.randomBytes(4).toString("hex")}`;
  await (await table("audit")).createEntity({ partitionKey: batchPartition(userId, batchId), rowKey,
    atUtc: at.toISOString(), event: clean(event, 80), detailsJson: json(details) });
}

async function policy() {
  const p = await getOptional("policy", "global", "direct-send");
  return { killed: !!(p && p.killed), reason: (p && p.reason) || "", updatedUtc: (p && p.updatedUtc) || "", by: (p && p.by) || "" };
}
async function setPolicy(who, killed, reason) {
  await (await table("policy")).upsertEntity({ partitionKey: "global", rowKey: "direct-send",
    killed: !!killed, reason: clean(reason, 500), updatedUtc: now(), by: clean(who.name, 256), byId: who.id }, "Replace");
  return policy();
}

/* ---------- approval passcode attempts ---------------------------------
 * Server-side, because a client-side attempt counter is one page refresh away
 * from being no counter at all. Five wrong tries locks approval for fifteen
 * minutes, which turns a four-digit space that is trivially walkable (10,000
 * guesses is seconds of scripting) into one that would take weeks -- and, more
 * to the point, into one that raises an audit trail long before it succeeds.
 */
const PASSCODE_MAX_TRIES = 5;
const PASSCODE_LOCK_MINUTES = 15;

async function passcodeAttempts(userId) {
  const row = await getOptional("policy", "passcode", userId);
  if (!row) return { failures: 0, lockedOut: false, minutesRemaining: 0 };
  const lockedUntil = new Date(row.lockedUntilUtc || 0).getTime();
  const remaining = Math.ceil((lockedUntil - Date.now()) / 60000);
  return { failures: Number(row.failures) || 0, lockedOut: remaining > 0,
           minutesRemaining: Math.max(0, remaining) };
}

async function recordPasscodeFailure(userId) {
  const current = await passcodeAttempts(userId);
  const failures = current.failures + 1;
  const lockedOut = failures >= PASSCODE_MAX_TRIES;
  await (await table("policy")).upsertEntity({ partitionKey: "passcode", rowKey: userId,
    failures, lastFailureUtc: now(),
    lockedUntilUtc: lockedOut ? new Date(Date.now() + PASSCODE_LOCK_MINUTES * 60000).toISOString() : "",
  }, "Replace");
  return { failures, lockedOut, remaining: Math.max(0, PASSCODE_MAX_TRIES - failures) };
}

async function clearPasscodeFailures(userId) {
  await (await table("policy")).upsertEntity({ partitionKey: "passcode", rowKey: userId,
    failures: 0, lockedUntilUtc: "", lastSuccessUtc: now() }, "Replace");
}

async function rollingExternalCount(userId, since = new Date(Date.now() - 86400000)) {
  let total = 0;
  for await (const e of (await table("ledger")).listEntities({ queryOptions: { filter: odata`PartitionKey eq ${userId}` } })) {
    if (new Date(e.reservedUtc).getTime() >= since.getTime()) total += Number(e.externalCount) || 0;
  }
  return total;
}
async function reserveExternal(userId, batchId, count) {
  await (await table("ledger")).createEntity({ partitionKey: userId, rowKey: batchId,
    externalCount: Number(count) || 0, reservedUtc: now() });
}

/* ---- reply sweep -------------------------------------------------------- */

/* Where each rep's sweep has read up to.
 *
 * ONE watermark per rep, not one per folder. The sweep queries /me/messages,
 * which spans every folder, precisely so that a rep inventing an Outlook rule
 * cannot silently end detection. Per-folder state would put that assumption
 * straight back.
 */
async function getSweepState(userId, sweep) {
  return getOptional("sweepState", userId, clean(sweep, 80));
}

/* MERGE, never Replace.
 *
 * THE BUG THIS FIXES: a failed sweep erased its own progress. The error path
 * writes {lastError} and nothing else, and under Replace the stored entity
 * BECOMES that -- so watermarkUtc vanished and the next run went back to a
 * 48-hour window. A token lapse or one truncated page silently undid days.
 *
 * Merge is the correct mode for a record several code paths update different
 * halves of. Replace is right for putConnection(), where the whole entity is
 * rewritten deliberately; it was wrong here and the difference is invisible
 * until something fails.
 */
async function putSweepState(userId, sweep, state, options = {}) {
  const saved = { partitionKey: userId, rowKey: clean(sweep, 80), updatedUtc: now() };
  for (const key of ["watermarkUtc", "lastOkUtc", "lastError", "lookupHash",
                     "backfillUntilUtc", "backfillStartedUtc"])
    if (state[key] !== undefined) saved[key] = clean(state[key], 500);
  for (const key of ["consecutiveFailures", "seen", "recorded", "truncatedRuns"])
    if (state[key] !== undefined) saved[key] = Number(state[key]) || 0;
  const client = await table("sweepState");
  /* Success progress is compare-and-swap against the state read when a sweep
   * began. A concurrent backfill request must not be cleared by that older
   * sweep as it finishes. Failure health remains an unconditional partial
   * Merge, because several passes may legitimately contribute health fields.
   *
   * Explicit null means "there was no row at start", hence create-only. The
   * property-presence check distinguishes that from an omitted option. */
  if (Object.prototype.hasOwnProperty.call(options, "ifMatch")) {
    if (options.ifMatch === null) await client.createEntity(saved);
    else {
      if (options.ifMatch === undefined || options.ifMatch === "") {
        const err = new Error("Conditional sweep progress requires an ETag or explicit null.");
        err.statusCode = 500;
        err.code = "sweep_state_etag_required";
        throw err;
      }
      await client.updateEntity(saved, "Merge", { etag: String(options.ifMatch) });
    }
  } else {
    await client.upsertEntity(saved, "Merge");
  }
  return getSweepState(userId, sweep);
}

/* One row per message already examined.
 *
 * The sweep deliberately re-reads an overlapping window, because a message can
 * be delivered slightly out of order and a watermark advanced to `now` would
 * step straight over it. That overlap is only safe because of this table.
 *
 * Mirrors bounceSeen exactly, including the reason: the rep's mailbox is never
 * modified to record that we looked at something. Marking mail read or moving
 * it would be this tool reaching into a mailbox it was lent for sending.
 */
/* HASHED, because a Graph immutable id is not a legal row key.
 *
 * Those ids are base64-flavoured and can contain "/" and "+". Azure Table
 * Storage rejects "/" in a key outright, and clean() only truncates -- so the
 * first advisor whose message id happened to contain a slash would have thrown
 * mid-sweep, on a code path with no test covering it. A fixed-length hash is
 * legal by construction and bounds the key at 64 characters.
 */
function seenKey(graphMessageId) {
  return crypto.createHash("sha256").update(String(graphMessageId || "")).digest("hex");
}

const ENGAGEMENT_DIRTY_PREFIX = "zD|";
const ENGAGEMENT_DIRTY_END = "zE|";
const ACTIVITY_WRITE_ATTEMPTS = 4;

function dirtyMarkerKey(userId) {
  return ENGAGEMENT_DIRTY_PREFIX + crypto.createHash("sha256")
    .update(String(userId || "")).digest("hex");
}

function storageConflict(err) {
  // 404 is also a retryable generation race here: a repair may conditionally
  // delete the marker after recordActivity read it but before its transaction.
  return [404, 409, 412].includes(Number(err && err.statusCode));
}

async function optionalFromClient(client, partitionKey, rowKey) {
  try { return await client.getEntity(partitionKey, rowKey); }
  catch (err) { if (Number(err && err.statusCode) === 404) return null; throw err; }
}

async function replyAlreadySeen(userId, graphMessageId) {
  return !!(await getOptional("replySeen", userId, seenKey(graphMessageId)));
}

async function markReplySeen(userId, graphMessageId, outcome) {
  await (await table("replySeen")).upsertEntity({ partitionKey: userId,
    rowKey: seenKey(graphMessageId), outcome: clean(outcome, 80), atUtc: now() }, "Replace");
}

/* An advisor relationship event.
 *
 * METADATA ONLY. No body, no bodyPreview, no attachments -- Exchange stays the
 * system of record for content, and the rep fetches it on demand when they
 * click. Storing bodies here would build a second copy of our own reps'
 * mailboxes, which is not what anybody agreed to when they connected one.
 *
 * Partitioned by advisor so a profile timeline is one query. `route` and
 * `classification` travel with every row: a caller must always be able to tell
 * a thread match from a sighting, and a human reply from an out-of-office.
 */
/* REVERSE TICKS, so ascending key order is newest-first.
 *
 * THE BUG THIS FIXES: listActivity() read rows in ascending key order and
 * stopped at a row count. The key began with a FORWARD timestamp, so ascending
 * meant oldest-first -- the cut kept the oldest rows, sorted them, and returned
 * them as "recent activity". Every consumer inherited it: the timeline, the
 * engagement fold, ownership checks, and which address a follow-up went to.
 *
 * The same pattern is already used elsewhere in this file for exactly this
 * reason; it was simply not applied here.
 *
 * The Graph id is HASHED into the suffix rather than appended raw: those ids
 * can contain "/", which Azure rejects in a key.
 */
async function recordActivity(entry) {
  const at = new Date(entry.occurredAt || now());
  const stamp = String(9999999999999 - (isNaN(at) ? Date.now() : at.getTime())).padStart(13, "0");
  const suffix = crypto.createHash("sha256")
    .update(String(entry.graphMessageId || crypto.randomUUID())).digest("hex").slice(0, 16);
  const rowKey = `${stamp}-${suffix}`;
  const seedBeforeUtc = clean(entry.seedBeforeUtc || entry.seedThroughUtc, 40);
  const saved = {
    partitionKey: clean(entry.advisorCrd || entry.firmCrd || "unknown", 120),
    rowKey: clean(rowKey, 500),
    userId: clean(entry.userId, 120), advisorCrd: clean(entry.advisorCrd, 40),
    firmCrd: clean(entry.firmCrd, 40), advisorEmail: clean(entry.advisorEmail, 320),
    direction: clean(entry.direction, 20), source: clean(entry.source, 40),
    recipientRole: clean(entry.recipientRole, 8),
    classification: clean(entry.classification, 40), route: clean(entry.route, 40),
    occurredAt: clean(entry.occurredAt, 40), subject: clean(entry.subject, 400),
    conversationId: clean(entry.conversationId, 500),
    internetMessageId: clean(entry.internetMessageId, 500),
    graphMessageId: clean(entry.graphMessageId, 500),
    batchId: clean(entry.batchId, 120), campaignMessageId: clean(entry.campaignMessageId, 120),
    // First observation owns import provenance. Re-reading current mail during
    // a later backfill must never retroactively make it historical.
    historicalImport: entry.historicalImport === true,
    seedBeforeUtc,
    recordedUtc: now(),
  };
  const client = await table("activity");

  // Firm-only and ambiguous sightings have no advisor projection to repair.
  if (!saved.advisorCrd || !saved.userId) {
    const existing = await optionalFromClient(client, saved.partitionKey, saved.rowKey);
    if (!existing) {
      try { await client.createEntity(saved); }
      catch (err) { if (!storageConflict(err)) throw err; }
    }
    return { ...(existing || saved), dirtyMarker: null };
  }

  const markerRowKey = dirtyMarkerKey(saved.userId);
  let conflict = null;
  for (let attempt = 0; attempt < ACTIVITY_WRITE_ATTEMPTS; attempt++) {
    const [existingEvent, currentMarker] = await Promise.all([
      optionalFromClient(client, saved.partitionKey, saved.rowKey),
      optionalFromClient(client, saved.partitionKey, markerRowKey),
    ]);
    const event = existingEvent || saved;
    const eventHistorical = event.historicalImport === true
      && !!String(event.seedBeforeUtc || "")
      && String(event.occurredAt || "") < String(event.seedBeforeUtc || "");
    // Within one pending generation, one current event defeats every historic
    // event. A successful conditional ack deletes the generation entirely.
    const historicalOnly = eventHistorical && (!currentMarker || currentMarker.historicalOnly === true);
    const marker = {
      partitionKey: saved.partitionKey, rowKey: markerRowKey,
      kind: "engagement_dirty", userId: saved.userId, advisorCrd: saved.advisorCrd,
      status: "pending", historicalOnly,
      seedBeforeUtc: historicalOnly
        ? [String((currentMarker || {}).seedBeforeUtc || ""), String(event.seedBeforeUtc || "")]
          .sort().pop()
        : "",
      // Oldest dirty age belongs to the pending generation, not its newest
      // event. Deletion followed by recreation naturally starts a new age.
      dirtyAtUtc: String((currentMarker || {}).dirtyAtUtc || now()),
      latestActivityRowKey: currentMarker && currentMarker.latestActivityRowKey
        ? [String(currentMarker.latestActivityRowKey), saved.rowKey].sort()[0]
        : saved.rowKey,
      attemptCount: 0, retryAfterUtc: "", leaseId: "", leaseUntilUtc: "",
      lastAttemptUtc: "", lastErrorCode: "", updatedUtc: now(),
    };
    const actions = [];
    if (!existingEvent) actions.push(["create", saved]);
    if (currentMarker) actions.push(["update", marker, "Replace", { etag: currentMarker.etag }]);
    else actions.push(["create", marker]);
    try {
      const result = await client.submitTransaction(actions);
      const sub = (result && result.subResponses) || [];
      const markerResponse = sub[sub.length - 1] || {};
      const latestMarker = markerResponse.etag
        ? { ...marker, etag: markerResponse.etag }
        : await optionalFromClient(client, marker.partitionKey, marker.rowKey);
      return { ...event, dirtyMarker: latestMarker };
    } catch (err) {
      if (!storageConflict(err)) throw err;
      conflict = err;
    }
  }
  const err = new Error("Email activity changed repeatedly while it was being recorded.");
  err.statusCode = 409;
  err.code = "email_activity_changed";
  err.cause = conflict;
  throw err;
}

/* Newest first, and the cut is now safe to make.
 *
 * Row keys are reverse ticks, so the service returns newest-first and stopping
 * at `limit` keeps the most recent rows. Previously this stopped at limit * 2
 * of the OLDEST rows and then sorted them, which is how a busy advisor's
 * timeline could show nothing from the last month.
 */
async function listActivity(advisorCrd, limit = 200) {
  const out = [];
  for await (const e of (await table("activity")).listEntities({
    queryOptions: { filter: odata`PartitionKey eq ${String(advisorCrd)} and RowKey lt ${ENGAGEMENT_DIRTY_PREFIX}` } })) {
    out.push(e);
    if (out.length >= limit) break;
  }
  return out;
}

/* Correctness path for projection refreshes: scoped by rep at the service and
 * fully paginated. Another rep's volume cannot consume this rep's row budget. */
async function listActivityForUser(advisorCrd, userId) {
  const out = [];
  for await (const e of (await table("activity")).listEntities({
    queryOptions: { filter: odata`PartitionKey eq ${String(advisorCrd)} and RowKey lt ${ENGAGEMENT_DIRTY_PREFIX} and userId eq ${String(userId)}` } })) out.push(e);
  return out;
}

async function getEngagementDirty(advisorCrd, userId) {
  return getOptional("activity", clean(advisorCrd, 120), dirtyMarkerKey(userId));
}

async function listEngagementDirtyPage(continuationToken = "", maxPageSize = 100) {
  const client = await table("activity");
  const size = Math.max(1, Math.min(500, Number(maxPageSize) || 100));
  const iter = client.listEntities({ queryOptions: {
    filter: odata`RowKey ge ${ENGAGEMENT_DIRTY_PREFIX} and RowKey lt ${ENGAGEMENT_DIRTY_END}`,
  } });
  const pages = iter.byPage({ continuationToken: continuationToken || undefined, maxPageSize: size });
  for await (const page of pages)
    return { rows: Array.from(page), continuationToken: page.continuationToken || "" };
  return { rows: [], continuationToken: "" };
}

function dueForClaim(marker, atMs) {
  if (!marker || marker.status === "poison") return false;
  if (marker.status === "processing")
    return !marker.leaseUntilUtc || new Date(marker.leaseUntilUtc).getTime() <= atMs;
  if (marker.status === "retry" && marker.retryAfterUtc
      && new Date(marker.retryAfterUtc).getTime() > atMs) return false;
  return ["", "pending", "retry"].includes(String(marker.status || ""));
}

async function claimEngagementDirty(marker, options = {}) {
  const at = options.now instanceof Date ? options.now : new Date(options.now || Date.now());
  if (!dueForClaim(marker, at.getTime())) return null;
  const leaseSeconds = Math.max(30, Math.min(600, Number(options.leaseSeconds) || 180));
  const claimed = { partitionKey: marker.partitionKey, rowKey: marker.rowKey,
    status: "processing", leaseId: crypto.randomUUID(),
    leaseUntilUtc: new Date(at.getTime() + leaseSeconds * 1000).toISOString(),
    lastAttemptUtc: at.toISOString(), attemptCount: Number(marker.attemptCount || 0) + 1,
    updatedUtc: at.toISOString() };
  try {
    await (await table("activity")).updateEntity(claimed, "Merge", { etag: marker.etag });
    return getEngagementDirty(marker.advisorCrd || marker.partitionKey, marker.userId);
  } catch (err) { if ([404, 412].includes(Number(err && err.statusCode))) return null; throw err; }
}

async function ackEngagementDirty(marker) {
  try {
    await (await table("activity")).deleteEntity(marker.partitionKey, marker.rowKey,
      { etag: marker.etag });
    return true;
  } catch (err) {
    if (Number(err && err.statusCode) === 404) return true;
    if (Number(err && err.statusCode) === 412) return false;
    throw err;
  }
}

function repairErrorCode(err) {
  const raw = String((err && err.code) || "");
  if (raw && /^[A-Za-z0-9_.-]{1,80}$/.test(raw)) return raw;
  const status = Number(err && err.statusCode);
  return status ? `http_${status}` : "engagement_refresh_failed";
}

async function failEngagementDirty(marker, failure, options = {}) {
  const at = options.now instanceof Date ? options.now : new Date(options.now || Date.now());
  const attempts = Number(marker.attemptCount || 0);
  const poisonAfter = Math.max(1, Math.min(20, Number(options.poisonAfter) || 8));
  const delays = [15, 30, 60, 120, 240, 360, 360, 360];
  const poisoned = attempts >= poisonAfter;
  const patch = { partitionKey: marker.partitionKey, rowKey: marker.rowKey,
    status: poisoned ? "poison" : "retry", leaseId: "", leaseUntilUtc: "",
    retryAfterUtc: poisoned ? "" : new Date(at.getTime()
      + delays[Math.min(Math.max(attempts - 1, 0), delays.length - 1)] * 60000).toISOString(),
    lastErrorCode: repairErrorCode(failure), updatedUtc: at.toISOString() };
  try {
    await (await table("activity")).updateEntity(patch, "Merge", { etag: marker.etag });
    return getEngagementDirty(marker.advisorCrd || marker.partitionKey, marker.userId);
  } catch (err) { if ([404, 412].includes(Number(err && err.statusCode))) return null; throw err; }
}

async function getEngagementRepairCursor() {
  return getOptional("sweepState", "engagement-repair", "cursor");
}

async function putEngagementRepairCursor(scope, continuationToken) {
  const saved = { partitionKey: "engagement-repair", rowKey: "cursor",
    scope: clean(scope, 200), continuationToken: clean(continuationToken, 4000), updatedUtc: now() };
  await (await table("sweepState")).upsertEntity(saved, "Replace");
  return saved;
}

/* ---- engagement projection ---------------------------------------------- */

/* One row per (rep, advisor): what the activity log adds up to right now.
 *
 * Rebuildable by construction. The only fields that are NOT derived from
 * activity are the ones a rep decided -- replyState, actedAt, nextActionAt --
 * and those are carried across a refresh rather than recomputed. Everything
 * else can be thrown away and regenerated, which is what makes the audit's
 * rebuild-and-compare check possible.
 */
async function getEngagement(userId, advisorCrd) {
  return getOptional("engagement", userId, clean(advisorCrd, 120));
}

async function putEngagement(userId, advisorCrd, state) {
  const saved = engagementEntity(userId, advisorCrd, state);
  /* MERGE, for the same reason putSweepState() does.
   *
   * This looks like a full projection write and mostly is -- fold() emits every
   * derived field. But it is built with the same "only if the caller supplied
   * it" loop, so ANY caller passing a partial state would silently delete the
   * rest, which is precisely the bug that deleted actedAt and put handled
   * replies back at the top of the queue.
   *
   * Merge makes that class of mistake impossible rather than merely absent
   * today. rebuild() can still correct every derived field, because fold()
   * always emits all of them.
   */
  await (await table("engagement")).upsertEntity(saved, "Merge");
  return saved;
}

function engagementEntity(userId, advisorCrd, state) {
  const saved = { partitionKey: userId, rowKey: clean(advisorCrd, 120),
                  advisorCrd: clean(advisorCrd, 120), updatedUtc: now() };
  for (const key of ["lastOutboundAt", "lastInboundAt", "lastReplyAt", "lastActivityAt",
                     "lastCurrentActivityAt", "historySeedBeforeUtc",
                     "replyState", "nextActionAt", "nextActionType", "actedAt",
                     "snoozedUntilUtc", "bounceDismissedAt", "advisorEmail"])
    if (state[key] !== undefined) saved[key] = clean(state[key], 320);
  for (const key of ["outbound30d", "inbound30d"])
    if (state[key] !== undefined) saved[key] = Number(state[key]) || 0;
  for (const key of ["hasBounce", "everReplied", "bounceDismissed"])
    if (state[key] !== undefined) saved[key] = !!state[key];
  return saved;
}

/* The projection writer, distinct from putEngagement's manual partial writes.
 *
 * A refresh folds activity after reading the previous projection. If a rep
 * snoozes or completes work between that read and this write, an unconditional
 * Merge would replay the stale decision fields over their newer action. A
 * conditional update refuses that lost update. A missing projection is created
 * rather than upserted, so two first writers also have a single winner (409).
 */
async function putEngagementProjection(userId, advisorCrd, state, etag = "") {
  const client = await table("engagement");
  const saved = engagementEntity(userId, advisorCrd, state);
  if (etag) await client.updateEntity(saved, "Merge", { etag });
  else await client.createEntity(saved);
  // Merge deliberately preserves manual decision fields a projection does not
  // own. Return the full row rather than only the derived payload, so callers
  // never mistake an omitted-but-preserved field for a deletion.
  return getEngagement(userId, advisorCrd);
}

async function listEngagement(userId) {
  const out = [];
  for await (const e of (await table("engagement")).listEntities({
    queryOptions: { filter: odata`PartitionKey eq ${String(userId)}` } })) out.push(e);
  return out;
}

/* Which rep's mailbox one activity row came from.
 *
 * Scoped by advisor because that is the partition key, so this is one small
 * read rather than a scan. Returns "" when the row is not there, which the
 * caller must treat as a refusal and not as permission.
 */
async function activityOwner(advisorCrd, graphMessageId) {
  const wanted = String(graphMessageId || "");
  for await (const e of (await table("activity")).listEntities({
    queryOptions: { filter: odata`PartitionKey eq ${String(advisorCrd)} and RowKey lt ${ENGAGEMENT_DIRTY_PREFIX}` } })) {
    if (String(e.graphMessageId || "") === wanted) return String(e.userId || "");
  }
  return "";
}

/* Sent messages indexed by the conversation they belong to.
 *
 * The strongest reply-matching route: a conversationId survives subject edits,
 * forwards, and clients that mangle References. Empty for anything sent before
 * conversationId was captured, which is why sentByInternetId() remains the
 * fallback rather than being replaced.
 */
async function sentByConversation(userId, sinceUtc) {
  const index = new Map();
  for await (const e of (await table("messages")).listEntities({
    queryOptions: { filter: odata`PartitionKey ge ${userId} and PartitionKey lt ${userId + "~"}` } })) {
    if (!["submitted", "sent"].includes(e.state)) continue;
    const conversationId = String(e.graphConversationId || "");
    if (!conversationId) continue;
    if (sinceUtc && e.submittedUtc && e.submittedUtc < sinceUtc) continue;
    // First writer wins: the OLDEST message in a thread is the one the advisor
    // is answering, and it carries the campaign that earned the reply.
    if (!index.has(conversationId)) {
      index.set(conversationId, { id: e.rowKey,
        batchId: e.partitionKey.slice(String(userId).length + 1),
        recipientEmail: e.recipientEmail, recipientName: e.recipientName,
        contactId: e.contactId || "", submittedUtc: e.submittedUtc || "" });
    }
  }
  return index;
}

module.exports = {
  getSweepState, putSweepState, replyAlreadySeen, markReplySeen,
  recordActivity, listActivity, listActivityForUser, activityOwner, sentByConversation,
  dirtyMarkerKey, getEngagementDirty, listEngagementDirtyPage,
  claimEngagementDirty, ackEngagementDirty, failEngagementDirty,
  getEngagementRepairCursor, putEngagementRepairCursor,
  getEngagement, putEngagement, putEngagementProjection, listEngagement,
  passcodeAttempts, recordPasscodeFailure, clearPasscodeFailures,
  id, now, batchPartition, putConnection, getConnection, putAuthState, consumeAuthState,
  createBatch, getBatch, patchBatch, listBatches, createMessage, getMessage, listMessages,
  patchMessage, deleteMessage, claimMessage, listTemplates, getTemplate, setTemplatePublished, listDocuments, getDocuments,
  putDocument, deleteDocument, putTemplate, deleteTemplate,
  putTemplateImage, deleteTemplateImage, templateImageBytes,
  getSuppression, suppressEmail, listConnections, sentByInternetId,
  bounceAlreadySeen, markBounceSeen, recordDeliveryEvent, deliveryEvents,
  audit, policy, setPolicy, rollingExternalCount, reserveExternal,
};
