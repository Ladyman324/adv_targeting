"use strict";

const crypto = require("crypto");
const { BlobServiceClient } = require("@azure/storage-blob");

const BASE = "https://graph.microsoft.com/v1.0";
const IMMUTABLE = 'IdType="ImmutableId"';
const APP_PROPERTY_ID = "String {4f3b4f80-286d-4d18-9225-0d454f410e75} Name EICMessageId";

class GraphError extends Error {
  constructor(message, info = {}) { super(message); Object.assign(this, info); }
}

async function request(token, method, path, body, options = {}) {
  let response;
  try {
    response = await fetch(path.startsWith("http") ? path : BASE + path, {
      method, headers: { Authorization: `Bearer ${token}`, Accept: "application/json",
        Prefer: IMMUTABLE, ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {}) }, body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(options.timeoutMs || 30000),
    });
  } catch (cause) {
    throw new GraphError("Microsoft Graph request did not return a definitive result.", { ambiguous: method !== "GET", cause });
  }
  const requestId = response.headers.get("request-id") || "";
  const retryAfter = Number(response.headers.get("retry-after") || 0);
  if (!response.ok) {
    let detail = {};
    try { detail = await response.json(); } catch { detail = {}; }
    const msg = detail.error && detail.error.message ? detail.error.message : `Microsoft Graph returned ${response.status}.`;
    throw new GraphError(msg, { statusCode: response.status, graphCode: detail.error && detail.error.code,
      requestId, retryAfter, ambiguous: method !== "GET" && response.status >= 500 });
  }
  if (response.status === 202 || response.status === 204) return { requestId };
  const text = await response.text();
  return { requestId, data: text ? JSON.parse(text) : null };
}

/* The message properties every lookup here asks for, named once.
 *
 * conversationId is the one that matters beyond sending: it is how a REPLY is
 * later tied back to the message it answers. Graph returns it on the created
 * draft and keeps it stable through Drafts -> Sent Items -> the advisor's reply,
 * which no other field does. It was not selected here originally because
 * sending never needed it, so every message sent before this change has to be
 * backfilled through findByAppId() rather than recovered from our own store.
 *
 * Keep this list and messageFromEntity()/patchMessage() in email-store.js in
 * step: a field selected here but not whitelisted there is fetched, silently
 * dropped on write, and impossible to notice from either side alone.
 */
const MESSAGE_FIELDS = "id,internetMessageId,conversationId,isDraft,sentDateTime,parentFolderId,subject";

function odataString(value) { return String(value).replace(/'/g, "''"); }

async function findByAppId(token, appMessageId) {
  const filter = `singleValueExtendedProperties/Any(ep: ep/id eq '${odataString(APP_PROPERTY_ID)}' and ep/value eq '${odataString(appMessageId)}')`;
  const params = new URLSearchParams({ "$filter": filter, "$select": MESSAGE_FIELDS });
  const result = await request(token, "GET", `/me/messages?${params.toString()}`);
  return (result.data && result.data.value && result.data.value[0]) || null;
}

async function createDraft(token, message) {
  const body = {
    subject: message.subject,
    body: { contentType: "HTML", content: message.bodyHtml + message.signatureHtml },
    toRecipients: [{ emailAddress: { address: message.recipientEmail, name: message.recipientName || undefined } }],
    // Copies are decided by the caller, not here, so the draft and the preview
    // are built from the same one function -- core.extraRecipients().
    //
    // No `name` is supplied. Exchange resolves an internal address to its
    // directory display name when the message is created, so the recipient sees
    // "Kate Renta" whether or not we send one -- and this app has User.Read
    // only, so it could not look one up for a colleague anyway.
    ...(Array.isArray(message.cc) && message.cc.length
      ? { ccRecipients: message.cc.map((address) => ({ emailAddress: { address } })) } : {}),
    ...(Array.isArray(message.bcc) && message.bcc.length
      ? { bccRecipients: message.bcc.map((address) => ({ emailAddress: { address } })) } : {}),
    singleValueExtendedProperties: [{ id: APP_PROPERTY_ID, value: message.id }],
    internetMessageHeaders: [{ name: "X-EIC-Message-Id", value: message.id }],
  };
  const r = await request(token, "POST", "/me/messages", body, { timeoutMs: 45000 });
  return { ...r.data, requestId: r.requestId };
}

async function getMessage(token, id) {
  const params = new URLSearchParams({ "$select": MESSAGE_FIELDS });
  const r = await request(token, "GET", `/me/messages/${encodeURIComponent(id)}?${params.toString()}`);
  return r.data;
}

// The filename Outlook actually sees.
//
// Documents are stored under an administrator's DISPLAY name -- "Q226 EIC ACV &
// LCV Client Commentary - 26071102" -- which is right for the picker in the app
// and wrong on the wire. Outlook chooses its handler from the file extension,
// not from contentType, so an extensionless attachment opens the "how do you
// want to open this file?" dialog even though it is a perfectly good PDF, and
// the advisor concludes we sent them something broken.
//
// Sanitised as well: Windows rejects \ / : * ? " < > | in filenames, and an
// admin display name is free text.
function attachmentFileName(doc) {
  const base = String(doc.name || doc.id || "attachment")
    .replace(/[\/:*?"<>|]/g, "-").replace(/\s+/g, " ").trim().slice(0, 200);
  /* `.pdf` is appended for APPROVED documents only, because that library is
   * PDF-by-construction and its names are admin display names with no extension
   * at all -- Outlook picks its handler from the extension, so without this the
   * advisor gets a "how do you want to open this?" dialog on a perfectly good
   * PDF.
   *
   * A file a rep attached from their own device already has a real extension,
   * and forcing this on it turns a spreadsheet into `forecast.xlsx.pdf` --
   * which Outlook then cannot open, producing exactly the bug this line exists
   * to prevent.
   */
  if (doc.keepName) return base;
  return /\.pdf$/i.test(base) ? base : `${base}.pdf`;
}

async function simpleAttachment(token, messageId, doc, bytes) {
  return request(token, "POST", `/me/messages/${encodeURIComponent(messageId)}/attachments`, {
    "@odata.type": "#microsoft.graph.fileAttachment", name: attachmentFileName(doc),
    contentType: doc.contentType, contentBytes: bytes.toString("base64"),
  }, { timeoutMs: 60000 });
}

async function largeAttachment(token, messageId, doc, bytes) {
  const session = await request(token, "POST", `/me/messages/${encodeURIComponent(messageId)}/attachments/createUploadSession`, {
    AttachmentItem: { attachmentType: "file", name: attachmentFileName(doc), size: bytes.length, contentType: doc.contentType },
  });
  const uploadUrl = session.data.uploadUrl;
  const chunkSize = 10 * 320 * 1024;

  // Where Graph says it still wants bytes, e.g. "12345-67890". Used to resume
  // after a network failure: the chunk may well have landed and only the
  // response was lost, and re-sending it blindly is how a 12 MB attachment
  // turns into 24 MB of upload.
  const nextExpectedStart = async () => {
    try {
      const probe = await fetch(uploadUrl, { method: "GET", signal: AbortSignal.timeout(30000) });
      if (!probe.ok) return null;
      const body = await probe.json();
      const range = (body.nextExpectedRanges || [])[0];
      const from = Number(String(range || "").split("-")[0]);
      return Number.isFinite(from) ? from : null;
    } catch { return null; }
  };

  let start = 0;
  let networkFailures = 0;
  while (start < bytes.length) {
    const end = Math.min(bytes.length, start + chunkSize) - 1;
    let response;
    try {
      response = await fetch(uploadUrl, { method: "PUT", headers: {
        "Content-Type": "application/octet-stream", "Content-Length": String(end - start + 1),
        "Content-Range": `bytes ${start}-${end}/${bytes.length}`,
      }, body: bytes.subarray(start, end + 1), signal: AbortSignal.timeout(120000) });
    } catch (err) {
      /* A NETWORK failure, not an HTTP one -- a timeout, a reset, a dropped
       * connection. These arrive as a bare TypeError or AbortError with no
       * statusCode, so failOrRetry() saw retryable === false and marked the
       * whole message PERMANENTLY failed on a blip it should have shrugged off.
       * Only this path was affected: an HTTP error response below already
       * carries ambiguous.
       *
       * Retried in place a few times, resuming from wherever Graph says it got
       * to, before giving up and handing the worker something it knows is worth
       * retrying.
       */
      if (++networkFailures > 3) {
        throw new GraphError(`Attachment upload failed after ${networkFailures} network errors: ${err.message}`,
          { ambiguous: true, retryAfter: 60 });
      }
      await new Promise((r) => setTimeout(r, 2000 * networkFailures));
      const resumeAt = await nextExpectedStart();
      if (resumeAt !== null) start = resumeAt;
      continue;
    }
    if (!response.ok && response.status !== 202) throw new GraphError(`Attachment upload failed (${response.status}).`, {
      statusCode: response.status, ambiguous: true, retryAfter: Number(response.headers.get("retry-after") || 0) });
    networkFailures = 0;                       // progress resets the allowance
    start = end + 1;
  }
}

async function documentBytes(doc) {
  const conn = process.env.AZURE_STORAGE_CONNECTION_STRING || "";
  const container = process.env.EMAIL_DOCUMENT_CONTAINER || "email-documents";
  if (!conn) throw new Error("Document storage is not configured.");
  const blob = BlobServiceClient.fromConnectionString(conn).getContainerClient(container).getBlobClient(doc.blobName);
  const properties = await blob.getProperties();
  if (Number(properties.contentLength) !== Number(doc.size)) throw new Error(`Approved document ${doc.name} changed size; re-approve it before sending.`);
  const bytes = await blob.downloadToBuffer();
  const sha256 = crypto.createHash("sha256").update(bytes).digest("hex");
  if (doc.sha256 && sha256 !== doc.sha256) throw new Error(`Approved document ${doc.name} changed content; re-approve it before sending.`);
  return bytes;
}

async function attachDocuments(token, messageId, docs) {
  const listed = await request(token, "GET", `/me/messages/${encodeURIComponent(messageId)}/attachments?$select=name,size`);
  const existing = new Set(((listed.data && listed.data.value) || []).map((a) => `${a.name}|${a.size}`));
  for (const doc of docs || []) {
    // Compared on the name as SENT. Comparing on doc.name would never match
    // what Graph reports back, so every retry would attach another copy.
    if (existing.has(`${attachmentFileName(doc)}|${doc.size}`)) continue;
    const bytes = await documentBytes(doc);
    if (bytes.length < 3 * 1024 * 1024) await simpleAttachment(token, messageId, doc, bytes);
    else await largeAttachment(token, messageId, doc, bytes);
  }
}

/* Files the rep picked off their own device, for a one-to-one reply.
 *
 * Separate from attachDocuments() because that one loads from blob storage and
 * verifies the bytes against the approved size and hash -- the whole point of
 * the approved library. These bytes never touch our storage: they arrive on the
 * request, go onto the draft, and are gone. Nothing is kept, so nothing has to
 * be curated, expired or deleted later.
 *
 * The compliance blind copy is what makes that acceptable. Anything a rep
 * attaches to an advisor reaches the same mailbox the approved material does,
 * so the obligation is met by the same mechanism rather than by a second
 * approval workflow nobody would use on a reply.
 */
async function attachFiles(token, messageId, files) {
  for (const file of files || []) {
    const bytes = Buffer.isBuffer(file.bytes) ? file.bytes : Buffer.from(file.bytes || "", "base64");
    if (!bytes.length) continue;
    const doc = { name: file.name, contentType: file.contentType || "application/octet-stream",
                  size: bytes.length, keepName: true };
    if (bytes.length < 3 * 1024 * 1024) await simpleAttachment(token, messageId, doc, bytes);
    else await largeAttachment(token, messageId, doc, bytes);
  }
}

// Inline charts. Same fileAttachment type as a document, but carrying isInline
// and a contentId, which is what <img src="cid:..."> in the body resolves
// against. Charts are exported slides rather than scans, so the simple path is
// always sufficient here; the 3 MB check is a guard, not a limit anyone should
// be meeting in practice.
async function attachInlineImages(token, messageId, images, loadBytes) {
  if (!images || !images.length) return;
  // $select names BASE-TYPE properties only.
  //
  // This previously asked for "name,contentId". contentId is declared on
  // microsoft.graph.fileAttachment -- a DERIVED type -- while this collection is
  // typed as microsoft.graph.attachment, so Graph answers 400 "Could not find a
  // property named 'contentId'". The request failed before a single image was
  // attached, which is why a batch with a chart produced a draft holding its PDF
  // and a red X where the chart belonged: the document step above uses
  // "name,size", both base properties, and succeeded.
  //
  // Idempotency therefore keys on the attachment NAME, exactly as
  // attachDocuments does. Inline names are derived from the template's own image
  // ids, which are unique per template, so a name collision cannot occur between
  // two charts on one message.
  const listed = await request(token, "GET",
    `/me/messages/${encodeURIComponent(messageId)}/attachments?$select=name,size`);
  const existing = new Set(((listed.data && listed.data.value) || []).map((a) => a.name).filter(Boolean));
  for (const image of images) {
    const name = image.name || image.id;
    if (existing.has(name)) continue;
    const bytes = await loadBytes(image);
    if (bytes.length >= 3 * 1024 * 1024)
      throw new Error(`Inline chart ${image.name} is too large to embed; export it smaller.`);
    await request(token, "POST", `/me/messages/${encodeURIComponent(messageId)}/attachments`, {
      "@odata.type": "#microsoft.graph.fileAttachment", name,
      contentType: image.contentType, contentBytes: bytes.toString("base64"),
      isInline: true, contentId: image.cid,
    }, { timeoutMs: 60000 });
  }
}


/* Properties each reader needs. NDR parsing needs the BODY, because that is
 * where the 5.x.x status code is written. Activity tracking must never fetch a
 * body -- it stores metadata only, and a body it never requested is a body it
 * can never accidentally persist. */
const NDR_FIELDS = "id,subject,from,receivedDateTime,body,internetMessageHeaders";
const ACTIVITY_FIELDS = "id,subject,from,toRecipients,ccRecipients,receivedDateTime," +
  "sentDateTime,conversationId,internetMessageId,internetMessageHeaders,isDraft";

/* Recent mail across the WHOLE mailbox.
 *
 * READ ONLY, and it never modifies the mailbox: no marking read, no moving, no
 * deleting. This tool was lent a rep's mailbox for sending; reaching in and
 * rearranging it is not something they agreed to. Idempotency comes from our own
 * record of which messages we have already looked at.
 *
 * NOT /me/mailFolders/inbox/messages, which is what this used to be. A rep can
 * create an Outlook rule at any time -- without telling anyone, and without
 * knowing this exists -- that files advisor mail into a subfolder. Polling named
 * folders means that rule silently ends detection while every screen goes on
 * confidently reporting "no reply recorded". /me/messages spans every folder, so
 * there is no folder assumption left to invalidate.
 *
 * PAGED, because $top is a ceiling and a full page is silent. The previous
 * version asked for the newest 100 over a 48-hour window with no paging: a rep
 * receiving more than that lost the oldest reports from view entirely, and
 * nothing anywhere said so. Same failure as the Baird roster truncation --
 * a plausible answer, quietly incomplete.
 *
 * OLDEST FIRST, and that ordering is load-bearing.
 *
 * It used to read newest-first, which combined with "do not advance the
 * watermark on a truncated window" into a DEADLOCK: a mailbox with more than
 * maxPages of traffic in the window returned the same newest pages forever
 * while the older replies behind them were never reached, and the watermark
 * could not move because the window never completed. It would have looked like
 * a working sweep that simply never found those replies.
 *
 * Ascending from the watermark makes truncation harmless instead: whatever was
 * read is a CONTIGUOUS block starting at the watermark, so the caller can
 * advance to the last message it actually processed and continue from there
 * next run. Progress is always made, and nothing is skipped.
 *
 * `truncated` still comes back, because the caller needs to know there is more
 * waiting and should not treat the run as having caught up.
 *
 * internetMessageHeaders is requested explicitly -- it is not returned by
 * default, and it is where NDR codes and In-Reply-To live.
 */
async function recentMail(token, sinceUtc, options = {}) {
  const select = options.select || NDR_FIELDS;
  const pageSize = Math.min(Math.max(Number(options.top) || 100, 1), 200);
  const maxPages = Math.max(Number(options.maxPages) || 10, 1);
  const params = new URLSearchParams({
    "$select": select,
    "$top": String(pageSize),
    "$orderby": "receivedDateTime asc",
    "$filter": `receivedDateTime ge ${new Date(sinceUtc).toISOString()}`,
  });

  const items = [];
  let next = `/me/messages?${params}`;
  let pages = 0;
  while (next && pages < maxPages) {
    const r = await request(token, "GET", next, undefined, { timeoutMs: 45000 });
    items.push(...((r.data && r.data.value) || []));
    next = (r.data && r.data["@odata.nextLink"]) || "";
    pages++;
  }
  return { items, truncated: !!next, pages };
}

/* The bounce sweeper's view. Kept as its own name because it carries the NDR
 * field set; the folder scope is now the whole mailbox, as above. */
async function recentInbox(token, sinceUtc, top = 100) {
  const { items } = await recentMail(token, sinceUtc, { select: NDR_FIELDS, top });
  return items;
}

/* ONE message's readable content, fetched only when a rep asks to read it.
 *
 * The activity log stores metadata and nothing else; this is the other half of
 * that bargain. Exchange stays the system of record, and content crosses the
 * wire when somebody clicks and not before -- so a timeline of two hundred rows
 * costs two hundred rows, not two hundred message bodies.
 *
 * uniqueBody, not body. `body` is the whole quoted chain, so a five-word reply
 * arrives wrapped in every message that preceded it; uniqueBody is the part
 * that is actually new in this message, which is the part a rep wants to read
 * before a call.
 *
 * TEXT, not HTML, and that is a security decision rather than a taste one. This
 * is mail written by people outside the firm, rendered inside our own page. As
 * plain text there is no markup to sanitise and no way for a crafted message to
 * become script in our DOM. Rendering advisor-authored HTML would need a real
 * sanitiser, and the reply is worth reading either way.
 *
 * webLink comes along so a rep can open the real thing in Outlook when they
 * need to do more than read it. Microsoft does not allow it in an iframe -- it
 * must open Outlook itself.
 */
async function getMessageContent(token, messageId) {
  const params = new URLSearchParams({
    "$select": "id,subject,from,toRecipients,ccRecipients,sentDateTime," +
               "receivedDateTime,conversationId,uniqueBody,webLink",
  });
  const r = await request(token, "GET",
    `/me/messages/${encodeURIComponent(messageId)}?${params.toString()}`, undefined,
    // Both preferences on one header: the helper's default Prefer is REPLACED by
    // anything passed here, so dropping IMMUTABLE would silently change which
    // id space this call answers in.
    { headers: { Prefer: `${IMMUTABLE}, outlook.body-content-type="text"` }, timeoutMs: 30000 });
  return r.data;
}

/* Start a reply to a message, as a real Outlook reply.
 *
 * createReply, NOT a fresh message with "RE:" glued to the subject. Graph
 * builds a draft that Exchange knows is part of the conversation: the advisor's
 * client threads it, the quoted history comes along, and -- the part that
 * matters here -- it carries the SAME conversationId, so when they answer it
 * the sweep matches their reply back to the thread on the strongest route we
 * have. A fabricated "RE:" message would start a new conversation and quietly
 * degrade every later match to references or sender-only.
 *
 * A failure part-way through leaves a genuine Outlook draft rather than losing
 * what the rep typed. That is a real advantage of this flow over composing in
 * our own page and posting at the end.
 */
async function createReply(token, messageId, replyAll = false) {
  const action = replyAll ? "createReplyAll" : "createReply";
  const r = await request(token, "POST",
    `/me/messages/${encodeURIComponent(messageId)}/${action}`, {}, { timeoutMs: 45000 });
  return r.data;
}

/* Put the rep's text into the reply draft Graph just made.
 *
 * PREPENDED to what Graph generated, never replacing it. The generated body
 * holds the quoted original, and replacing it would send the advisor a bare
 * sentence with no sign of what it answers -- which is exactly the mail people
 * find unreadable on a phone three days later.
 */
/* Set who a reply draft actually goes to.
 *
 * createReply addresses its draft to the SENDER of the message it answers. For
 * a follow-up on our own sent mail that is the rep, so the draft has to be
 * re-addressed before it is sent. Done as an explicit PATCH rather than by
 * building a fresh message, because the reply draft is what carries the
 * conversationId and the quoted history -- the two things that make it thread.
 *
 * Cc and Bcc are SET, not merged. Graph inherits the original's recipients into
 * a reply, and a follow-up that quietly re-copied everybody who was on the
 * first message would fan out to people the rep never chose again.
 */
async function patchDraftRecipients(token, messageId, fields) {
  const r = await request(token, "PATCH",
    `/me/messages/${encodeURIComponent(messageId)}`, fields, { timeoutMs: 45000 });
  return r.data;
}

async function updateDraftBody(token, messageId, html) {
  const existing = await request(token, "GET",
    `/me/messages/${encodeURIComponent(messageId)}?$select=body`);
  const original = ((existing.data || {}).body || {}).content || "";
  const r = await request(token, "PATCH", `/me/messages/${encodeURIComponent(messageId)}`,
    { body: { contentType: "HTML", content: String(html || "") + original } },
    { timeoutMs: 45000 });
  return r.data;
}

async function sendDraft(token, messageId) {
  return request(token, "POST", `/me/messages/${encodeURIComponent(messageId)}/send`, undefined, { timeoutMs: 45000 });
}

module.exports = { GraphError, APP_PROPERTY_ID, NDR_FIELDS, ACTIVITY_FIELDS,
  findByAppId, createDraft, getMessage, getMessageContent, attachDocuments,
  attachInlineImages, attachFiles, createReply, patchDraftRecipients,
  updateDraftBody, sendDraft,
  recentMail, recentInbox, request, attachmentFileName };
