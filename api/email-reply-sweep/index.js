/* Advisor reply sweeper.
 *
 * Reads each connected mailbox for mail to or from somebody in our advisor
 * universe, matches it back to what we sent, and records the relationship event.
 * Runs on a timer -- there is no push notification for a reply.
 *
 * WHY THIS IS NOT PART OF email-bounce-sweep
 * ------------------------------------------
 * They read the same mailboxes and it is tempting to do both in one pass. Three
 * reasons not to:
 *
 *   COST SCALES DIFFERENTLY. The bounce sweep re-reads a 48-hour window every
 *   run, so running it more often costs proportionally more. This sweep reads
 *   from a watermark, so a quiet run is nearly free and frequency is cheap.
 *   Coupling them forces the cheap one onto the expensive one's cadence.
 *
 *   URGENCY DIFFERS. A dead address stays dead; two hours is ample. A reply is
 *   worth knowing this morning.
 *
 *   RISK CLASS DIFFERS, and this is the real reason. Bounce suppression is
 *   PERMANENT and DESTRUCTIVE -- a false positive silently stops us contacting
 *   a reachable advisor. Reply detection is additive. A crash in new,
 *   fast-moving code must not be able to abort a half-finished suppression pass.
 *
 * THE CRON OFFSET IS LOAD-BEARING
 * -------------------------------
 * :02, :17, :32, :47 -- deliberately never :20, where the bounce sweep runs.
 * putConnection() upserts with "Replace" and no etag, while tokenFor() reads
 * the MSAL cache, refreshes it, and writes the rotated cache back. Two
 * functions touching one mailbox in the same minute can interleave and lose a
 * rotated refresh token, which surfaces later as a spurious needsReconnect for
 * a rep who did nothing wrong.
 *
 * FOLDER-AGNOSTIC BY CONSTRUCTION
 * -------------------------------
 * graph.recentMail() queries /me/messages, which spans every folder. A rep can
 * create any Outlook rule they like -- without telling anyone, and without
 * knowing this exists -- and their advisor mail is still seen. Polling named
 * folders would let an unremarkable workflow change silently end detection
 * while every screen went on saying "no reply recorded".
 *
 * WHAT IT REFUSES TO DO
 * ---------------------
 *   1. It never modifies the mailbox. Not read flags, not folders, not deletes.
 *   2. It stores NO message body. Metadata only; Exchange stays the system of
 *      record and the rep fetches content on demand. Anything else builds a
 *      second copy of our own reps' mailboxes.
 *   3. It persists nothing about a message that is not to or from a known
 *      advisor -- and logs nothing about it either, beyond an anonymous count.
 *      A rep's mailbox is mostly not our business and must not become our data.
 *   4. It never guesses WHICH advisor. An address several advisors share is
 *      recorded as ambiguous, for a human to resolve.
 *
 * Off unless EMAIL_REPLY_SWEEP_ENABLED=1.
 *
 * The schedule is a LITERAL in function.json rather than a %SETTING% reference:
 * an unresolved %SETTING% makes the Functions host fail to load the function at
 * startup, which would take the sending API down with it.
 */
"use strict";

const auth = require("../shared/email-auth");
const store = require("../shared/email-store");
const graph = require("../shared/graph-mail");
const bounce = require("../shared/email-bounce");
const reply = require("../shared/email-reply");
const advisors = require("../shared/advisor-lookup");
const engagement = require("../shared/email-engagement");

const HOURS = 3600 * 1000;
const MINUTES = 60 * 1000;

/* How far back a first run reaches, and how much a later run re-reads.
 *
 * OVERLAP exists because receivedDateTime is when the message arrived, and mail
 * can land slightly out of order. A watermark advanced to `now` would step over
 * anything delivered a moment late. The overlap costs nothing but a duplicate
 * read, because replySeen makes re-examination free. */
const FIRST_RUN_HOURS = 48;
const OVERLAP_MINUTES = 10;

/* Persist one failed PASS, not one failed message.
 *
 * consecutiveFailures belongs to EmailSweepState. It is deliberately read from
 * there rather than from the mailbox connection: connections describe Graph
 * authentication and have never carried this counter. Reading the connection
 * made every authentication lapse write `1`, however many runs it lasted.
 *
 * Best effort because the original failure remains the useful one to report. A
 * storage failure while recording health is logged rather than replacing it.
 */
async function recordFailure(connection, code, context, deps, knownState = null) {
  try {
    const state = knownState || await deps.store.getSweepState(connection.userId, "reply") || {};
    await deps.store.putSweepState(connection.userId, "reply", {
      lastError: String(code || "reply_sweep_failed").slice(0, 500),
      consecutiveFailures: Number(state.consecutiveFailures || 0) + 1,
    });
  } catch (healthErr) {
    context.log.error(`could not persist reply sweep failure for ${connection.userId}: ${healthErr.message}`);
  }
}

async function sweepMailbox(connection, context, deps) {
  const summary = { userId: connection.userId, mailbox: connection.mailbox,
                    scanned: 0, ours: 0, replies: 0, autoReplies: 0, bounces: 0,
                    ambiguous: 0, outbound: 0, errors: 0 };
  /* Advisors whose activity changed this pass, so only their queue rows are
   * recomputed. Sets, because one thread can produce several rows.
   *
   * TWO of them, and the split matters. A backfill seeds imported history so a
   * year of old replies does not arrive as a year of unfinished work -- but the
   * run that finishes catching up also contains mail that arrived WHILE it was
   * catching up, and seeding that would mark genuinely new replies as already
   * handled. They would never appear in the queue and nobody would know to look.
   *
   * So the decision is per MESSAGE, not per run: an advisor touched only by
   * historic mail is seeded, and one touched by anything current is not. An
   * advisor touched by both is left unseeded, which is the safe direction --
   * surfacing something already dealt with costs a moment, missing a reply
   * costs the reply.
   */
  const touchedHistoric = new Set();
  const touchedCurrent = new Set();
  /* The timestamp of the last message this pass fully handled.
   *
   * Messages come oldest-first, so this is the end of a CONTIGUOUS block
   * from the previous watermark -- which is what makes advancing past a
   * truncated window safe rather than lossy. */
  let lastProcessed = "";

  // Load health before authentication: the auth failure path needs the actual
  // persisted counter, not a similarly named (and nonexistent) connection field.
  const state = await deps.store.getSweepState(connection.userId, "reply") || {};

  let token;
  try {
    token = await deps.auth.tokenFor(connection.userId);
  } catch (err) {
    /* A rep who must reconnect is skipped, but NOT silently.
     *
     * This is where reply detection differs from bounce detection. A missed
     * bounce resurfaces on the next send. A missed reply is gone, and the rep's
     * screens go on saying "no reply recorded" with total confidence. So the
     * lapse is written to sweep state, where a health check and the rep's own
     * UI can both see it and prompt them to sign in -- which is the entire fix.
    */
    summary.skipped = err.code || "no_token";
    await recordFailure(connection, summary.skipped, context, deps, state);
    return summary;
  }

  /* CATCHING UP ON HISTORY.
   *
   * A backfill sets the watermark back and records how far forward the catch-up
   * runs. While the watermark is still behind that mark, everything the sweep
   * imports is HISTORY: it is recorded on the timeline in full, but the queue
   * projection is seeded so none of it presents itself as work nobody did.
   *
   * There is no separate backfill job. It is the ordinary sweep, reading from
   * an older watermark -- which only works because reading is oldest-first and
   * the watermark advances to whatever was actually processed, so a year of
   * mail is simply a few hours of ordinary runs.
   */
  const backfillUntil = (state && state.backfillUntilUtc) || "";
  const watermark = (state && state.watermarkUtc)
    ? new Date(new Date(state.watermarkUtc).getTime() - OVERLAP_MINUTES * MINUTES)
    : new Date(Date.now() - FIRST_RUN_HOURS * HOURS);

  const index = await deps.advisors.load();
  const lookup = (address) => deps.advisors.classifyAddress(index, address);

  const started = new Date();
  const catchingUp = !!backfillUntil && watermark.toISOString() < backfillUntil;
  // A message is HISTORY if it predates the moment the backfill was asked for.
  // Anything after that is ordinary new mail, however far behind the sweep is.
  const isHistoric = (item) => !!backfillUntil
    && String(item.receivedDateTime || item.sentDateTime || "") < backfillUntil;
  const { items, truncated } = await deps.graph.recentMail(token.accessToken, watermark.toISOString(),
    { select: graph.ACTIVITY_FIELDS, top: 200 });

  const [sent, byConversation] = await Promise.all([
    deps.store.sentByInternetId(connection.userId, new Date(Date.now() - 90 * 24 * HOURS).toISOString()),
    deps.store.sentByConversation(connection.userId, new Date(Date.now() - 90 * 24 * HOURS).toISOString()),
  ]);

  for (const item of items) {
    if (item.isDraft) continue;
    summary.scanned++;
    if (await deps.store.replyAlreadySeen(connection.userId, item.id)) {
      lastProcessed = item.receivedDateTime || lastProcessed;
      continue;
    }

    let verdict;
    try {
      verdict = deps.reply.assess(item, { lookup, sent, byConversation,
        mailbox: connection.mailbox, isNdr: deps.bounce.looksLikeNdr(item) });
    } catch (err) {
      summary.errors++;
      context.log.error(`reply assess failed: ${err.message}`);
      continue;
    }

    /* Not ours. NOTHING is written -- not the sender, not the subject, not the
     * id. The only trace is that `scanned` went up.
     *
     * It used to write the message id to replySeen, which contradicted exactly
     * this promise: a message id IS metadata about a message we said we would
     * never record, and the table grew a row for every newsletter and internal
     * mail every rep ever received, forever.
     *
     * Dropping it costs re-assessing the same message if it falls inside the
     * ten-minute overlap, and a re-assessment is one lookup in an in-memory
     * Map. Cheap enough that keeping the record was never worth what it cost in
     * promises.
     */
    if (!verdict) { lastProcessed = item.receivedDateTime || lastProcessed; continue; }

    summary.ours++;
    try {
      if (verdict.direction === "outbound") {
        for (const target of verdict.advisors) {
          await deps.store.recordActivity({
            userId: connection.userId, direction: "outbound",
            // "app" when the message carries our own X-EIC-Message-Id, so a
            // scheduled campaign is never shown as though the rep typed it.
            source: verdict.source || "outlook",
            classification: "sent", route: verdict.route,
            advisorCrd: target.who.crd || "", firmCrd: target.who.firmCrd || "",
            advisorEmail: target.address,
            // Recorded, not discounted: copying somebody is nearly always
            // copying their practice, and touching the team is what counts.
            recipientRole: target.role || "to",
            occurredAt: item.sentDateTime || item.receivedDateTime,
            subject: item.subject, conversationId: verdict.conversationId,
            internetMessageId: verdict.internetMessageId, graphMessageId: item.id,
            campaignMessageId: verdict.appMessageId || "",
          });
          if (target.who.crd) (isHistoric(item) ? touchedHistoric : touchedCurrent)
            .add(target.who.crd);
        }
        summary.outbound++;
      } else {
        const answered = verdict.answers || null;
        await deps.store.recordActivity({
          userId: connection.userId, direction: "inbound", source: "outlook",
          classification: verdict.classification, route: verdict.route,
          advisorCrd: verdict.who.crd || "", firmCrd: verdict.who.firmCrd || "",
          advisorEmail: verdict.from, occurredAt: verdict.receivedAt,
          subject: verdict.subject, conversationId: verdict.conversationId,
          internetMessageId: verdict.internetMessageId, graphMessageId: item.id,
          // Only a THREAD route may claim a campaign. A sender-only sighting is
          // real advisor activity and is recorded as such, but attributing it to
          // a campaign it may have nothing to do with would be a fabrication.
          batchId: answered ? answered.batchId : "",
          campaignMessageId: answered ? answered.id : "",
        });
        if (verdict.who.crd) (isHistoric(item) ? touchedHistoric : touchedCurrent)
          .add(verdict.who.crd);
        if (verdict.who.kind === "ambiguous") summary.ambiguous++;
        if (verdict.classification === "reply") summary.replies++;
        else if (verdict.classification === "auto_reply") summary.autoReplies++;
        else if (verdict.classification === "bounce") summary.bounces++;
      }
      await deps.store.markReplySeen(connection.userId, item.id, verdict.classification || verdict.direction);
      // Only after the row is safely written: a message whose activity failed
      // to record must NOT move the watermark past itself.
      lastProcessed = item.receivedDateTime || lastProcessed;
    } catch (err) {
      // Left unmarked on purpose so the next sweep retries it.
      summary.errors++;
      context.log.error(`activity not recorded: ${err.message}`);
    }
  }

  /* Refresh the queue projection for whoever was touched this pass.
   *
   * Only the advisors that actually changed, and AFTER their activity rows are
   * written. Doing it here rather than on read means the morning dashboard is a
   * single partition query instead of a fold over every event a rep has ever
   * generated.
   *
   * Failures are logged and swallowed. The projection is a cache: a stale queue
   * row is a nuisance, whereas letting this abort the sweep would cost real
   * activity that no later run would go back for.
   */
  for (const advisorCrd of new Set([...touchedHistoric, ...touchedCurrent])) {
    try {
      // Seeded only when everything we saw for them this pass was history.
      const seed = touchedHistoric.has(advisorCrd) && !touchedCurrent.has(advisorCrd);
      await deps.engagement.refresh(connection.userId, advisorCrd,
        { store: deps.store, seed });
    } catch (err) {
      summary.projectionErrors = (summary.projectionErrors || 0) + 1;
      context.log.warn(`engagement refresh failed for ${advisorCrd}: ${err.message}`);
    }
  }

  /* THE WATERMARK ADVANCES TO WHAT WAS ACTUALLY PROCESSED.
   *
   * Messages arrive oldest-first, so whatever this pass read is a contiguous
   * block starting at the previous watermark. That makes a truncated window
   * safe to advance past: everything up to `lastProcessed` really was handled,
   * and the next run continues from there rather than starting over.
   *
   * The old rule -- "only advance on a clean pass" -- was a DEADLOCK when
   * combined with a newest-first read. A busy mailbox returned the same newest
   * pages every run, the older mail behind them was never reached, and the
   * watermark could never move because the window never completed. It would
   * have looked like a sweep that worked and simply never found those replies.
   *
   * On an ERROR the watermark still does not move, because an error means a
   * message in the middle of the block may not have been recorded and the block
   * is no longer contiguous.
   *
   * `started` is deliberately not used: it is when the run began, not what it
   * read, and using it would skip anything that arrived during the run.
   */
  const advanceTo = lastProcessed || (truncated ? "" : started.toISOString());
  if (advanceTo && !summary.errors) {
    await deps.store.putSweepState(connection.userId, "reply", {
      watermarkUtc: advanceTo, lastOkUtc: new Date().toISOString(),
      // A truncated oldest-first page is successful progress. `truncatedRuns`
      // describes its backlog without making a healthy pass look like an error.
      lastError: "", consecutiveFailures: 0,
      lookupHash: index.contentHash,
      seen: summary.scanned, recorded: summary.ours,
      truncatedRuns: truncated
        ? Number((state && state.truncatedRuns) || 0) + 1
        : 0,
      // Cleared once the watermark passes it, so the sweep returns to normal
      // without anybody having to remember to switch it off.
      ...(backfillUntil && advanceTo >= backfillUntil ? { backfillUntilUtc: "" } : {}),
    });
  } else if (summary.errors) {
    // No progress to record. putSweepState MERGES, so this cannot erase the
    // watermark -- which it used to, under Replace, sending the rep back to a
    // 48-hour window every time anything went wrong.
    await deps.store.putSweepState(connection.userId, "reply", {
      lastError: "errors during pass",
      consecutiveFailures: Number(state.consecutiveFailures || 0) + 1,
      truncatedRuns: Number((state && state.truncatedRuns) || 0) + (truncated ? 1 : 0),
    });
  } else {
    // A truncated empty page made no progress, but Graph completed normally.
    // Keep it visibly in catch-up state without inflating failure accounting.
    await deps.store.putSweepState(connection.userId, "reply", {
      lastOkUtc: new Date().toISOString(), lastError: "", consecutiveFailures: 0,
      truncatedRuns: Number((state && state.truncatedRuns) || 0) + 1,
    });
  }
  summary.truncated = !!truncated;
  summary.catchingUp = catchingUp;
  return summary;
}

async function sweep(context, overrides = {}) {
  const deps = { auth, store, graph, bounce, reply, advisors, engagement, ...overrides };
  if (process.env.EMAIL_REPLY_SWEEP_ENABLED !== "1") {
    context.log("reply sweep disabled (EMAIL_REPLY_SWEEP_ENABLED != 1)");
    return [];
  }

  const allConnections = await deps.store.listConnections();
  const canaryIds = new Set(String(process.env.EMAIL_REPLY_SWEEP_USER_IDS || "")
    .split(/[;,\s]+/).map((value) => value.trim().toLowerCase()).filter(Boolean));
  const connections = canaryIds.size
    ? allConnections.filter((connection) => canaryIds.has(String(connection.userId || "").toLowerCase()))
    : allConnections;
  if (canaryIds.size && !connections.length) {
    context.log("reply sweep canary allowlist matched no connected mailboxes");
    return [];
  }

  // Fails CLOSED. Without the advisor universe every address looks unknown, and
  // the sweep would conclude that nobody wrote to us -- indistinguishable from a
  // quiet week, and it would mark every message seen on the way past.
  try {
    await deps.advisors.load();
  } catch (err) {
    context.log.error(`advisor lookup unavailable, sweep aborted: ${err.message}`);
    for (const connection of connections) {
      await recordFailure(connection, err.code || "advisor_lookup_unavailable", context, deps);
    }
    return [];
  }

  const summaries = [];
  for (const connection of connections) {
    try {
      summaries.push(await sweepMailbox(connection, context, deps));
    } catch (err) {
      context.log.error(`reply sweep failed for ${connection.userId}: ${err.message}`);
      await recordFailure(connection, err.code || "mailbox_sweep_failed", context, deps);
      summaries.push({ userId: connection.userId, errors: 1, failed: err.message });
    }
  }
  for (const s of summaries) {
    context.log(`reply sweep ${s.mailbox || s.userId}: scanned ${s.scanned || 0}, ` +
      `ours ${s.ours || 0}, replies ${s.replies || 0}, auto ${s.autoReplies || 0}, ` +
      `ambiguous ${s.ambiguous || 0}, outbound ${s.outbound || 0}, errors ${s.errors || 0}` +
      (s.catchingUp ? " [backfill]" : "") +
      (s.skipped ? `, skipped ${s.skipped}` : "") + (s.truncated ? ", TRUNCATED" : ""));
  }
  return summaries;
}

module.exports = async function (context) { await sweep(context); };
module.exports.sweep = sweep;
module.exports.sweepMailbox = sweepMailbox;
