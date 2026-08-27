"use strict";

const baseStore = require("../shared/store");
const store = require("../shared/email-store");
const service = require("../shared/email-service");
const auth = require("../shared/email-auth");
const activity = require("../shared/email-activity");
const engagement = require("../shared/email-engagement");
const replySend = require("../shared/email-reply-send");
const directSend = require("../shared/email-direct-send");

function ok(context, body, status = 200) {
  context.res = { status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" }, body: JSON.stringify(body) };
}
function fail(context, err) {
  const status = err.statusCode || 500;
  context.res = { status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
    body: JSON.stringify({ error: err.message || "Unexpected email workflow error.", code: err.code || "" }) };
  if (status >= 500) context.log.error(err);
}
function isAdmin(who) {
  const roles = new Set((who.roles || []).map((x) => String(x).toLowerCase()));
  return roles.has("emailadministrator") || roles.has("administrator");
}

/* A successful truncated page means "working through a backlog", not "failed".
 * Older deployments wrote that condition into lastError, so recognize both the
 * old persisted vocabulary and the new truncatedRuns signal during rollout. */
function replySweepHealth(connection, state) {
  const st = state || {};
  const lastError = String(st.lastError || "");
  const legacyBacklog = lastError === "more waiting" || lastError === "window truncated";
  const consecutiveFailures = Number(st.consecutiveFailures || 0);
  const catchingUp = !!st.backfillUntilUtc || Number(st.truncatedRuns || 0) > 0 || legacyBacklog;
  // The connection row is authoritative.  A successful OAuth callback clears
  // needsReconnect immediately, while the sweep's lastError is only cleared by
  // its next pass.  Letting that historical error override the connection made
  // a freshly reconnected mailbox still look disconnected for several minutes.
  const needsReconnect = !!connection.needsReconnect;
  const failed = !needsReconnect && (consecutiveFailures > 0 || (!!lastError && !legacyBacklog));
  const lastOkMs = Date.parse(String(st.lastOkUtc || ""));
  const watermarkMs = Date.parse(String(st.watermarkUtc || ""));
  const neverRun = !Number.isFinite(lastOkMs) || !Number.isFinite(watermarkMs);
  const now = Date.now();
  const stale = !neverRun && (now - lastOkMs > 60 * 60 * 1000
    || now - watermarkMs >= 2 * 60 * 60 * 1000);

  let ingestionStatus = "healthy";
  if (needsReconnect) ingestionStatus = "reconnect_required";
  else if (failed) ingestionStatus = "failed";
  else if (neverRun) ingestionStatus = "never_run";
  else if (catchingUp) ingestionStatus = "catching_up";
  else if (stale) ingestionStatus = "stale";

  return {
    ingestionStatus,
    // Catch-up is operational but not current. Callers can distinguish it from
    // both a hard failure and a fully current mailbox without parsing text.
    // Catch-up can be executing successfully while intentionally far behind;
    // stale means unhealthy only when there is no declared backlog in progress.
    ingestionHealthy: !needsReconnect && !failed && !neverRun && (!stale || catchingUp),
    catchingUp,
    hasError: needsReconnect || failed,
  };
}

module.exports = async function (context, req) {
  try {
    const who = baseStore.identity(req);
    const op = String((req.query && req.query.op) || (req.body && req.body.op) || "catalog");
    if (req.method === "GET") {
      // isAdmin travels with the catalog so the composer knows whether to offer
      // document management at all. The server still checks it on every write --
      // this only decides what is drawn.
      if (op === "catalog") return ok(context,
        { ...await service.catalog(who, { isAdmin: isAdmin(who) }), isAdmin: isAdmin(who) });
      if (op === "batch") return ok(context, await service.getBatchDetail(who, String(req.query.id || "")));
      /* Who is left to follow up, and who came off the list and why.
       *
       * A GET because it computes nothing durable -- and it is asked twice, once
       * when the rep opens the screen and again when they commit, because
       * somebody may reply in between. */
      if (op === "follow_up_candidates")
        return ok(context, await service.followUpCandidates(who, String(req.query.id || "")));
      if (op === "batches") return ok(context, { batches: await store.listBatches(who.id) });
      if (op === "connection") return ok(context, await auth.status(who.id));
      if (op === "policy") return ok(context, await store.policy());
      /* The bytes behind an inline chart, so the on-screen preview can show the
       * actual image.
       *
       * The preview renders the same HTML the message carries, and that HTML
       * points at cid: parts -- which resolve inside a mail client and nowhere
       * else, so the browser drew a broken-image icon on the one screen whose
       * entire purpose is "this is exactly what they will receive".
       *
       * Any signed-in user, not just administrators: reps have to see their own
       * previews. It only ever serves images already approved onto a template,
       * addressed by template and image id, so there is nothing here that the
       * composer would not already show them.
       */
      /* The Act! record behind one CRD, verbatim.
       *
       * Admin-only: this is a named person's CRM record, not reference data, and
       * a rep has no reason to read one. Read-only -- there is no write path
       * anywhere near this.
       */
      /* The advisor relationship timeline, and one message's readable text.
       *
       * Deliberately NOT admin-gated. Knowing that a colleague already emailed
       * this advisor on Tuesday is what stops two reps working the same person
       * in the same week -- the same reasoning that makes /api/flags and
       * /api/dnc firm-wide rather than private.
       *
       * CONTENT is a different matter, and activity.messageContent refuses
       * anything outside the caller's own mailbox before it calls Graph.
       */
      if (op === "activity") {
        return ok(context, await activity.timeline(who, String(req.query.crd || "")));
      }
      if (op === "activity_message") {
        return ok(context, await activity.messageContent(who,
          { crd: String(req.query.crd || ""), id: String(req.query.id || "") }));
      }
      /* The work queue: who this rep should pick up now, and why.
       *
       * Per rep, always -- unlike the timeline, which is shared. "Who should I
       * work next" is a question about one person's day; a shared queue would
       * put the same advisor at the top of five screens at once.
       */
      if (op === "queue_work") {
        return ok(context, await engagement.queue(who.id));
      }
      /* Exactly who a Reply All would reach, so the rep can see it.
       *
       * Reply-all pulls in the original's To and Cc, which are not necessarily
       * advisors. Disclosure rather than refusal -- see replyAllAudience().
       */
      /* How the reply sweep is doing, per rep.
       *
       * A backfill runs for hours and an administrator who started one for
       * somebody else has no other way to see it -- the alternative was reading
       * the Function App log stream, which is a thin way to watch a long job.
       *
       * It is also the health readout the plan asked for: a stuck watermark or
       * a rep who needs to reconnect are both SILENT failures, where the sweep
       * keeps running and simply stops finding replies.
       *
       * An administrator sees every rep; a rep sees themselves.
       */
      if (op === "sweep_status") {
        const all = await store.listConnections();
        const mine = isAdmin(who) ? all : all.filter((c) => String(c.userId) === who.id);
        const now = Date.now();
        const rows = [];
        for (const connection of mine) {
          const st = await store.getSweepState(connection.userId, "reply");
          const watermark = st && st.watermarkUtc ? st.watermarkUtc : "";
          const until = st && st.backfillUntilUtc ? st.backfillUntilUtc : "";
          const behindMs = watermark ? now - new Date(watermark).getTime() : null;
          const health = replySweepHealth(connection, st);
          rows.push({
            userId: connection.userId, mailbox: connection.mailbox,
            needsReconnect: !!connection.needsReconnect,
            watermarkUtc: watermark,
            // How far behind real time the sweep has read up to. This is the
            // number that matters: a watermark that stops moving is the silent
            // failure this whole feature is most prone to.
            behindHours: behindMs === null ? null : Math.round(behindMs / 3600000),
            lastOkUtc: (st && st.lastOkUtc) || "",
            lastError: (st && st.lastError) || "",
            consecutiveFailures: Number((st && st.consecutiveFailures) || 0),
            truncatedRuns: Number((st && st.truncatedRuns) || 0),
            ...health,
            backfill: until
              ? { until, startedUtc: (st && st.backfillStartedUtc) || "",
                  // Whole days still to work through.
                  daysRemaining: Math.max(0, Math.ceil(
                    (new Date(until).getTime() - new Date(watermark || until).getTime()) / 86400000)) }
              : null,
          });
        }
        return ok(context, { reps: rows, count: rows.length, isAdmin: isAdmin(who) });
      }
      if (op === "reply_audience") {
        return ok(context, await replySend.audienceFor(who,
          { crd: String(req.query.crd || ""), id: String(req.query.id || "") }));
      }
      if (op === "direct_send_status") {
        return ok(context, await directSend.status(who, String(req.query.operationId || "")));
      }
      // Sender health. Admin-only: it names every rep and how their sending is
      // going, which is a management view rather than a rep's own screen.
      if (op === "sender_health") {
        if (!isAdmin(who)) throw service.httpError(403, "EmailAdministrator role is required.");
        return ok(context, await service.senderHealth(Number(req.query.days) || 90));
      }
      if (op === "act_contact") {
        if (!isAdmin(who)) throw service.httpError(403, "EmailAdministrator role is required.");
        return ok(context, await require("../shared/act").actContact(String(req.query.crd || "")));
      }
      if (op === "template_image") {
        const template = await store.getTemplate(String(req.query.templateId || ""));
        const image = ((template && template.images) || [])
          .find((i) => String(i.id) === String(req.query.imageId || ""));
        if (!image) throw service.httpError(404, "No such chart on that template.");
        const bytes = await store.templateImageBytes(image);
        context.res = { status: 200, isRaw: true, body: bytes, headers: {
          "Content-Type": image.contentType || "application/octet-stream",
          "Content-Length": String(bytes.length),
          // Immutable: the blob name carries a content hash, so a changed image
          // is a different URL and this can never serve a stale chart.
          "Cache-Control": "private, max-age=86400, immutable",
          "X-Content-Type-Options": "nosniff",
        } };
        return;
      }
      // Read-only Act! schema discovery, so the Mail Code property name is
      // LOOKED UP rather than guessed. Admin-only and it returns property names
      // plus any value that already looks like a Mail Code -- never the rest of
      // the record. Usage: ?op=act_fields&crd=1000084
      if (op === "act_fields") {
        if (!isAdmin(who)) throw service.httpError(403, "EmailAdministrator role is required.");
        return ok(context, await require("../shared/act").actFields(String(req.query.crd || "")));
      }
      throw service.httpError(400, `Unknown email query operation "${op}". `
        + `This usually means the API needs redeploying to match the app.`);
    }
    const body = req.body || {};
    /* A rep marking where they have got to on a reply.
     *
     * This is what stops the queue becoming a list of eight hundred people who
     * once replied: work enters as `new` and leaves when it has been dealt
     * with. The state is the rep's own decision and is NOT recomputed from
     * mail -- it survives every rebuild of the projection.
     */
    if (op === "reply_state") {
      return ok(context, await engagement.setReplyState(who.id,
        String(body.crd || ""), String(body.state || "")));
    }
    /* "Not now" without "never".
     *
     * The missing verb: Done cannot clear a quiet_warm or a bounce, by design,
     * so without this a rep had no way to put either aside. A snooze silences
     * every reason until it expires and then returns the row as due.
     */
    if (op === "queue_snooze") {
      return ok(context, await engagement.snooze(who.id,
        String(body.crd || ""), Number(body.days || 30)));
    }
    // An explicit statement that a bad address is as good as it is going to get.
    /* Reach back over a rep's own mail history.
     *
     * NOT a separate job and NOT a default. It moves this rep's watermark back
     * and records how far forward the catch-up runs; the ordinary sweep does
     * the rest, a page at a time, because reading is oldest-first and the
     * watermark advances to whatever was actually processed.
     *
     * Everything imported is stamped as already-seen, so a year of old replies
     * lands on the timeline WITHOUT four hundred of them appearing in "Needs
     * attention" as work nobody did. That would destroy the queue's credibility
     * on the first morning anybody used it.
     *
     * Per rep, on their own mailbox: the token is theirs, and nobody should be
     * able to start a multi-hour scan of somebody else's mail.
     */
    /* Reach back over a rep's own mail history.
     *
     * NOT a separate job and NOT a default. It moves a watermark back and
     * records how far forward the catch-up runs; the ordinary sweep does the
     * rest, a page at a time, because reading is oldest-first and the watermark
     * advances to whatever was actually processed.
     *
     * Everything imported is stamped as already-seen, so a year of old replies
     * lands on the timeline WITHOUT four hundred of them appearing in "Needs
     * attention" as work nobody did. That would destroy the queue's credibility
     * on the first morning anybody used it.
     *
     * AN ADMIN MAY RUN IT FOR SOMEBODY ELSE, and has to be able to.
     *
     * The first version acted only on the caller, which sounded safely minimal
     * and was unusable: an administrator cannot sign in as a rep -- correctly --
     * so nobody could start a backfill for anyone but themselves.
     *
     * It grants no new access. The sweep ALREADY reads every connected rep's
     * mailbox every fifteen minutes using that rep's own stored token; this
     * changes how far back it looks and nothing else. The token, the mailbox
     * and the permission are exactly as they were.
     */
    if (op === "backfill") {
      const target = String(body.userId || "").trim() || who.id;
      if (target !== who.id && !isAdmin(who)) {
        throw service.httpError(403,
          "Only an administrator can start a backfill for another rep.");
      }
      const known = await store.listConnections();
      const connection = known.find((c) => String(c.userId) === target);
      if (!connection) {
        throw service.httpError(404, "That rep has no connected mailbox, so there is "
          + "nothing to read. They need to connect Microsoft 365 first.");
      }
      if (connection.needsReconnect) {
        throw service.httpError(409, `${connection.mailbox} needs to reconnect Microsoft 365 `
          + "before a backfill can read anything.", "graph_reconnect_required");
      }
      const days = Math.min(Math.max(Number(body.days) || 365, 1), 3650);
      const at = new Date();
      await store.putSweepState(target, "reply", {
        watermarkUtc: new Date(at.getTime() - days * 86400000).toISOString(),
        backfillUntilUtc: at.toISOString(),
        backfillStartedUtc: at.toISOString(),
        lastError: "", consecutiveFailures: 0,
      });
      return ok(context, { ok: true, days, userId: target, mailbox: connection.mailbox,
        note: `The next sweeps will work forward through ${days} days of `
            + `${connection.mailbox}. History is recorded but not queued as work.` });
    }
    if (op === "queue_dismiss_bounce") {
      return ok(context, await engagement.dismissBounce(who.id, String(body.crd || "")));
    }
    /* Replying to an advisor from the timeline.
     *
     * Passes the same gates a campaign passes -- suppression and the compliance
     * blind copy -- because a reply is still an outbound email to an advisor,
     * and a route with fewer checks on it would be the route everything ends up
     * using.
     */
    if (op === "reply_send") {
      return ok(context, await directSend.start(who, body, "reply"), 202);
    }
    /* A NEW conversation with an advisor who has gone quiet.
     *
     * The recipient is taken from the activity log rather than from the
     * request: a client that could name the address could mail anybody from a
     * rep's mailbox through this endpoint.
     */
    if (op === "follow_up") {
      return ok(context, await directSend.start(who, body, "follow_up"), 202);
    }
    if (op === "connect") return ok(context, await auth.begin(who, body.returnTo));
    if (op === "create_batch") return ok(context, await service.createBatch(who, body), 201);
    /* The bulk follow-up: a new batch derived from a campaign, holding only the
     * people who never answered it. 201 like any other batch creation, because
     * that is exactly what it is. */
    if (op === "create_follow_up") return ok(context, await service.createFollowUp(who, body), 201);
    if (op === "update_common") return ok(context, await service.updateCommon(who, body));
    if (op === "update_message") return ok(context, await service.updateMessage(who, body));
    if (op === "update_message_cc") return ok(context, await service.updateMessageCc(who, body));
    if (op === "validate") return ok(context, await service.validateBatch(who, body.batchId));
    if (op === "remove_recipient") return ok(context, await service.removeRecipient(who, body));
    if (op === "approve") return ok(context, await service.approve(who, body), 202);
    if (["pause", "cancel", "resume", "retry"].includes(op)) return ok(context, await service.control(who, { ...body, action: op }));
    if (op === "policy") {
      if (!isAdmin(who)) throw service.httpError(403, "EmailAdministrator role is required.");
      return ok(context, await store.setPolicy(who, body.killed, body.reason));
    }
    // Template authoring. Linting is available to anyone so the editor can lint
    // as they type without a role round trip, but nothing is written without the
    // role -- and putTemplate lints again server-side before storing, because a
    // client-side check is a convenience, never a gate.
    if (op === "lint_template") return ok(context, require("../shared/email-core")
      .lintTemplate({ subject: body.subject, bodyText: body.bodyText }));
    // Every admin-only template operation must be NAMED here or its handler
    // below is unreachable -- the block is what enforces the role, so an op
    // added inside it without being listed simply never runs.
    if (["put_template", "publish_template", "delete_template",
         "put_template_image", "delete_template_image"].includes(op)) {
      if (!isAdmin(who)) throw service.httpError(403, "EmailAdministrator role is required.");
      if (op === "put_template") {
        const saved = await store.putTemplate(who, body);
        // "template_saved", not "template_published": publishing is now a
        // separate act with its own event, and one word cannot mean both
        // "the wording changed" and "the sales team may send this".
        await store.audit(who.id, `template:${saved.id}`, "template_saved", { id: saved.id, version: saved.version });
        return ok(context, { ok: true, saved, templates: await store.listTemplates() }, 201);
      }
      if (op === "publish_template") {
        if (!isAdmin(who)) throw service.httpError(403, "EmailAdministrator role is required.");
        const published = body.published === true;
        const saved = await store.setTemplatePublished(who, body.id, published);
        /* Two calls, each naming its event literally.
         *
         * A ternary in the event slot reads fine and makes the trail
         * un-greppable: "which templates were ever published" stops being a
         * text search and becomes an exercise in reading control flow. The
         * audit enforces this, and it is right to.
         */
        if (published) {
          await store.audit(who.id, `template:${saved.id}`, "template_published",
            { id: saved.id, name: saved.name });
        } else {
          await store.audit(who.id, `template:${saved.id}`, "template_withdrawn",
            { id: saved.id, name: saved.name });
        }
        return ok(context, { ok: true, saved,
          templates: await store.listTemplates() });
      }
      if (op === "delete_template") {
        const gone = await store.deleteTemplate(who, body.id);
        await store.audit(who.id, `template:${gone.id}`, "template_deleted", gone);
        return ok(context, { ok: true, removed: gone, templates: await store.listTemplates() });
      }
      if (op === "delete_template_image") {
        const gone = await store.deleteTemplateImage(who, body.templateId, body.imageId);
        return ok(context, { ok: true, removed: gone, templates: await store.listTemplates() });
      }
      const bytes = Buffer.from(String(body.dataBase64 || ""), "base64");
      if (!bytes.length) throw service.httpError(400, "The uploaded image was empty or not valid base64.");
      const saved = await store.putTemplateImage(who, { templateId: body.templateId,
        imageId: body.imageId, name: body.name, bytes, maxBytes: service.attachmentLimit() });
      return ok(context, { ok: true, saved, templates: await store.listTemplates() }, 201);
    }
    if (op === "put_document" || op === "delete_document") {
      if (!isAdmin(who)) throw service.httpError(403, "EmailAdministrator role is required.");
      if (op === "delete_document") {
        const gone = await store.deleteDocument(who, body.id);
        await store.audit(who.id, `document:${gone.id}`, "document_deleted", { id: gone.id, name: gone.name });
        return ok(context, { ok: true, removed: gone, documents: await store.listDocuments() });
      }
      // The PDF arrives base64 in JSON rather than as multipart: one code path,
      // no parser dependency, and the size ceiling is enforced either way.
      const raw = String(body.dataBase64 || "");
      if (!raw) throw service.httpError(400, "No file content was supplied.");
      const bytes = Buffer.from(raw, "base64");
      if (!bytes.length) throw service.httpError(400, "The uploaded file was empty or not valid base64.");
      // fileName rides alongside the display name: the advisor should receive
      // the document called what it was called when it was uploaded, while the
      // picker in the app keeps the readable label.
      const saved = await store.putDocument(who, { id: body.id, name: body.name,
        fileName: body.fileName, bytes, maxBytes: service.attachmentLimit() });
      await store.audit(who.id, `document:${saved.id}`, "document_published", { id: saved.id, name: saved.name,
        fileName: saved.fileName, version: saved.version, sha256: saved.sha256, replaced: saved.replaced });
      return ok(context, { ok: true, saved, documents: await store.listDocuments() }, 201);
    }
    // Name the operation. A bare "Unknown email operation" is indistinguishable
    // from a bug in the request, when in practice it almost always means the
    // deployed API is older than the page calling it -- a frontend deploy that
    // went out without its matching API deploy.
    throw service.httpError(400, `Unknown email operation "${op}". `
      + `This usually means the API needs redeploying to match the app.`);
  } catch (err) { fail(context, err); }
};
