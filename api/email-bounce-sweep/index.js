/* Hard-bounce sweeper.
 *
 * Reads each connected mailbox for non-delivery reports, matches them back to
 * messages we sent, and permanently suppresses addresses that are genuinely
 * dead. Runs on a timer -- there is no push notification for a bounce.
 *
 * Three deliberate constraints, all for the same reason: a FALSE bounce
 * silently and permanently stops us contacting a reachable advisor, and nobody
 * finds out until they ask why we went quiet.
 *
 *   1. HARD bounces only. Soft (4.x.x) recovers by itself. Permanent failures
 *      that are not about the address -- mailbox full, message too large,
 *      blocked by their policy -- are recorded and never suppressed.
 *   2. The address comes from OUR sent record, never from the report. Anyone can
 *      send us an NDR; reading the address out of it would let a stranger
 *      suppress an advisor by forging one.
 *   3. The mailbox is never modified. We were lent it for sending.
 *
 * Off unless EMAIL_BOUNCE_SWEEP_ENABLED=1.
 *
 * The schedule is a LITERAL in function.json -- every two hours, at 20 past --
 * rather than a %SETTING% reference. An unresolved %SETTING% makes the Functions
 * host fail to load the function at startup, which would take the sending API
 * down with it; a hard-coded cron plus a feature flag cannot. Bounces are not
 * time-critical, so two hours is ample.
 */
"use strict";

const auth = require("../shared/email-auth");
const store = require("../shared/email-store");
const graph = require("../shared/graph-mail");
const bounce = require("../shared/email-bounce");
const act = require("../shared/act");
const core = require("../shared/email-core");
const recipientRegistry = require("../shared/recipient-registry");
const worker = require("../email-worker/index");

const HOURS = 3600 * 1000;

async function sweepMailbox(connection, context, deps) {
  const lookbackHours = Number(process.env.EMAIL_BOUNCE_LOOKBACK_HOURS) || 48;
  const since = new Date(Date.now() - lookbackHours * HOURS).toISOString();
  const summary = { userId: connection.userId, mailbox: connection.mailbox,
                    scanned: 0, hard: 0, soft: 0, policy: 0, unmatched: 0, suppressed: 0, errors: 0 };

  let token;
  try {
    token = await deps.auth.tokenFor(connection.userId);
  } catch (err) {
    // A rep who needs to reconnect is not an error worth alarming about; their
    // bounces will be picked up whenever they do.
    summary.skipped = err.code || "no_token";
    return summary;
  }

  const [inbox, sent] = await Promise.all([
    deps.graph.recentInbox(token.accessToken, since),
    deps.store.sentByInternetId(connection.userId, new Date(Date.now() - 30 * 24 * HOURS).toISOString()),
  ]);

  for (const item of inbox) {
    /* recentInbox() now spans the WHOLE mailbox rather than the Inbox folder,
     * because a rep's Outlook rule could file a delivery report somewhere else
     * and it would never be seen. The cost is that their own SENT mail is in
     * view too. It cannot be a delivery report about itself, and keeping it out
     * of the classifier's input means the only thing that can ever suppress an
     * address is something that genuinely arrived from outside.
     */
    const sender = String((((item || {}).from || {}).emailAddress || {}).address || "").toLowerCase();
    if (sender && sender === String(connection.mailbox || "").toLowerCase()) continue;
    if (!deps.bounce.looksLikeNdr(item)) continue;
    summary.scanned++;
    if (await deps.store.bounceAlreadySeen(connection.userId, item.id)) continue;

    let verdict;
    try { verdict = deps.bounce.assess(item, sent); }
    catch (err) { summary.errors++; context.log.error(`bounce parse failed: ${err.message}`); continue; }

    if (!verdict.act) {
      summary[verdict.reason === "not-a-bounce" ? "unmatched" : (verdict.reason in summary ? verdict.reason : "unmatched")]++;
      // A deferral or a policy rejection is not something to act on, but it is
      // very much something to record: it is the earliest visible sign that a
      // receiving gateway is throttling us, and it arrives per domain, which is
      // the level any remedy has to work at.
      if (verdict.record) {
        try {
          await deps.store.recordDeliveryEvent(connection.userId, {
            kind: verdict.reason, code: (verdict.verdict || {}).code || "",
            domain: verdict.domain, address: verdict.address,
            batchId: (verdict.message || {}).batchId || "",
            messageId: (verdict.message || {}).id || "",
            detail: verdict.detail || "",
          });
        } catch (err) { context.log.warn(`delivery event not recorded: ${err.message}`); }
      }
      await deps.store.markBounceSeen(connection.userId, item.id, verdict.reason);
      continue;
    }

    summary.hard++;
    const address = verdict.address;
    try {
      // Our own suppression first. It is what actually stops the next send, and
      // it must not be lost if Act! is unavailable.
      await deps.store.suppressEmail(address, { kind: "hard_bounce",
        reason: verdict.reason, messageId: verdict.message.id });
      await deps.store.patchMessage(connection.userId, verdict.message.batchId, verdict.message.id,
        { bounceKind: "hard", bounceAtUtc: new Date().toISOString(),
          bounceReason: String(verdict.reason).slice(0, 500) }, verdict.message.etag).catch(() => {});
      await deps.store.audit(connection.userId, verdict.message.batchId, "hard_bounce_suppressed",
        { messageId: verdict.message.id, address, code: verdict.verdict.code });
      // Telemetry, in its own try: a failure to record the statistic must never
      // undo the suppression, which is the part that actually protects anybody.
      try {
        await deps.store.recordDeliveryEvent(connection.userId, {
          kind: "hard", code: verdict.verdict.code, domain: verdict.domain, address,
          batchId: verdict.message.batchId, messageId: verdict.message.id,
          detail: verdict.reason,
        });
      } catch (err) { context.log.warn(`delivery event not recorded: ${err.message}`); }
      summary.suppressed++;

      /* Campaign health.
       *
       * The counting and the brake live in refreshBatch(), which already has
       * every message in hand and cannot race itself. Asking it to recount here
       * matters because a batch may have finished sending before its bounces
       * arrive -- a paused batch is still the signal a rep needs before building
       * the next one from the same list.
       */
      try { await deps.refreshBatch(connection.userId, verdict.message.batchId, deps); }
      catch (err) { context.log.warn(`campaign health update failed: ${err.message}`); }

      // Then the CRM, best effort. Mail Code N, never downgrading a U.
      try {
        const approved = await deps.recipientRegistry.verifyActPair(
          verdict.message.contactId, address, { force: true });
        const pushed = await deps.act.markHardBounce(
          verdict.message.contactId, address, verdict.reason, approved.actContactId);
        if (!pushed.ok) context.log.warn(`Act! bounce not applied for ${address}: ${pushed.reason || ""}`);
      } catch (err) { context.log.error(`Act! bounce push failed for ${address}: ${err.message}`); }

      await deps.store.markBounceSeen(connection.userId, item.id, "suppressed");
    } catch (err) {
      // Left unmarked on purpose so the next sweep retries it.
      summary.errors++;
      context.log.error(`hard bounce handling failed for ${address}: ${err.message}`);
    }
  }
  return summary;
}

async function sweep(context, overrides = {}) {
  const deps = { auth, store, graph, bounce, act, core, recipientRegistry,
                 refreshBatch: worker.refreshBatch, ...overrides };
  if (process.env.EMAIL_BOUNCE_SWEEP_ENABLED !== "1") {
    context.log("Bounce sweep is disabled (EMAIL_BOUNCE_SWEEP_ENABLED is not 1).");
    return [];
  }
  const connections = (await deps.store.listConnections()).filter((c) => !c.needsReconnect);
  const results = [];
  for (const connection of connections) {
    try { results.push(await sweepMailbox(connection, context, deps)); }
    catch (err) {
      context.log.error(`bounce sweep failed for ${connection.mailbox}: ${err.message}`);
      results.push({ userId: connection.userId, mailbox: connection.mailbox, failed: err.message });
    }
  }
  const totals = results.reduce((a, r) => ({ scanned: a.scanned + (r.scanned || 0),
    suppressed: a.suppressed + (r.suppressed || 0) }), { scanned: 0, suppressed: 0 });
  context.log(`Bounce sweep: ${connections.length} mailbox(es), `
    + `${totals.scanned} report(s) examined, ${totals.suppressed} address(es) suppressed.`);
  return results;
}

module.exports = async function (context) { await sweep(context); };
module.exports.sweep = sweep;
module.exports.sweepMailbox = sweepMailbox;
