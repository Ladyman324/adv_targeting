"use strict";

/* What is happening with this advisor right now, and who should a rep work next.
 *
 * WHY A PROJECTION AND NOT A QUERY
 * --------------------------------
 * The activity log is the truth, and it is the wrong shape for a dashboard.
 * Answering "who needs attention" from raw events means reading every event for
 * every advisor a rep has ever touched, on every render, in a store with no
 * joins and no aggregation. So the events are folded into one row per
 * (rep, advisor) and the screens read that.
 *
 * It is a CACHE, not a second source of truth. Everything here is recomputed
 * from activity rows by fold(), and rebuild() throws a rep's whole projection
 * away and regenerates it -- which is both the repair path and how the audit
 * proves the cache has not drifted from the log.
 *
 * WHAT IT DELIBERATELY DOES NOT MEASURE
 * -------------------------------------
 * Not volume. "117 emails sent this week" is the number that makes a dashboard
 * look busy and makes a rep behave worse -- it rewards sending, which is the
 * behaviour the 25/day limits already exist to restrain. The queue is built
 * from things that indicate a RELATIONSHIP moved: somebody replied, somebody
 * has gone quiet, a follow-up is due.
 *
 * A REPLY IS WORK, NOT A TROPHY
 * -----------------------------
 * A reply enters the queue as `new` and leaves when a rep has dealt with it.
 * Without that, six months in, a rep has a list of eight hundred people who
 * once replied and no way to see the five that need them today.
 */

const store = require("./email-store");

/* Ordered. `index` is what a caller compares -- a reply that has been reviewed
 * must never fall back to `new` because the sweep saw a later message on the
 * same thread. */
const REPLY_STATES = ["none", "new", "reviewed", "follow_up", "scheduled", "done"];
const stateIndex = (value) => Math.max(0, REPLY_STATES.indexOf(String(value || "none")));

/* Why somebody is in the queue, most urgent first. The ORDER is the product
 * decision: a person who answered us outranks a person we have merely not
 * spoken to in a while, every time. */
const REASONS = [
  { key: "reply_new",     label: "Replied — needs attention" },
  { key: "reply_followup", label: "Follow-up needed" },
  { key: "due",           label: "Follow-up due" },
  { key: "bounced",       label: "Email bounced — address needs fixing" },
  { key: "quiet_warm",    label: "Warm, no contact in a while" },
];
const REASON_RANK = new Map(REASONS.map((r, i) => [r.key, i]));

/* The server owns the verbs as well as the reasons.  Clients keep a matching
 * fallback for a staggered static/API deployment, but once this field is
 * present they must render only these keys.  In particular, `done` cannot
 * resolve a time-derived due/quiet row, and a bounced address must not offer a
 * follow-up that suppression will refuse. */
const ACTIONS_BY_REASON = Object.freeze({
  reply_new: ["mark_reviewed", "snooze"],
  reply_followup: ["follow_up", "done", "snooze"],
  due: ["follow_up", "snooze"],
  bounced: ["dismiss_bounce", "snooze"],
  quiet_warm: ["follow_up", "snooze"],
});

const DAY = 24 * 3600 * 1000;
const QUIET_DAYS = Number(process.env.ENGAGEMENT_QUIET_DAYS || 30);

function newest(a, b) {
  return String(a || "") > String(b || "") ? String(a || "") : String(b || "");
}

/* Fold one advisor's activity into a single state.
 *
 * `previous` carries the rep's own decisions -- replyState, nextActionAt -- which
 * are NOT derivable from mail and must survive a rebuild. Everything else is
 * recomputed, so a projection can always be regenerated from the log.
 */
function fold(entries, previous = {}) {
  const state = {
    lastOutboundAt: "", lastInboundAt: "", lastReplyAt: "", lastActivityAt: "",
    outbound30d: 0, inbound30d: 0,
    hasBounce: false, everReplied: false,
    replyState: String(previous.replyState || "none"),
    nextActionAt: String(previous.nextActionAt || ""),
    nextActionType: String(previous.nextActionType || ""),
    advisorEmail: String(previous.advisorEmail || ""),
    /* CARRIED FORWARD, and the omission was a real bug.
     *
     * fold() consulted previous.actedAt to decide whether a reply was already
     * dealt with, and then did not return it. putEngagement() replaced the
     * stored row with what fold() produced, so actedAt was deleted on every
     * refresh -- and the next sweep, finding no record that anybody had acted,
     * put a handled reply back at the top of the queue as new.
     *
     * It is the rep's decision, not a derived value, so it belongs with
     * replyState and nextActionAt in the set that survives a rebuild.
     */
    actedAt: String(previous.actedAt || ""),
    // Same class as actedAt: rep decisions the log cannot re-derive.
    snoozedUntilUtc: String(previous.snoozedUntilUtc || ""),
    bounceDismissed: !!previous.bounceDismissed,
    // Persist the legacy entity timestamp as a fixed boundary on its first
    // new-code fold. Otherwise every later projection refresh would move
    // updatedUtc and could hide a delayed bounce whose event time is older.
    bounceDismissedAt: String(previous.bounceDismissedAt
      || (previous.bounceDismissed ? previous.updatedUtc : "") || ""),
  };
  const cutoff = new Date(Date.now() - 30 * DAY).toISOString();
  let newestEmailAt = "";
  let newestBounceAt = "";

  for (const e of entries) {
    const at = String(e.occurredAt || "");
    state.lastActivityAt = newest(state.lastActivityAt, at);

    /* The address on the LATEST row, whichever direction it was.
     *
     * An unconditional overwrite left whichever row happened to come last in
     * the iteration -- and rows arrive newest-first, so that was the OLDEST
     * address. A follow-up would then have gone to an address the advisor may
     * have left years ago, which is a message that silently reaches nobody.
     */
    if (e.advisorEmail && at >= newestEmailAt) {
      newestEmailAt = at;
      state.advisorEmail = e.advisorEmail;
    }

    if (e.direction === "outbound") {
      state.lastOutboundAt = newest(state.lastOutboundAt, at);
      if (at >= cutoff) state.outbound30d++;
      continue;
    }
    state.lastInboundAt = newest(state.lastInboundAt, at);
    if (at >= cutoff) state.inbound30d++;

    if (e.classification === "bounce") {
      state.hasBounce = true;
      newestBounceAt = newest(newestBounceAt, at);
      continue;
    }
    // An out-of-office is inbound mail and is NOT a reply. Counting it would
    // put an away message at the top of a rep's morning queue, and the first
    // time that happened they would stop trusting the queue.
    if (e.classification !== "reply") continue;

    state.everReplied = true;
    state.lastReplyAt = newest(state.lastReplyAt, at);
  }

  /* A NEW reply only moves the state forward, never back.
   *
   * If a rep has already reviewed this conversation, a later message on it does
   * not undo that. But a genuinely newer reply than the one they reviewed does
   * deserve their attention again -- so the comparison is against when they
   * last acted, not against the state alone. */
  if (state.lastReplyAt) {
    const actedAt = String(previous.actedAt || "");
    const unseen = !actedAt || state.lastReplyAt > actedAt;
    if (unseen) {
      state.replyState = "new";
    } else if (stateIndex(state.replyState) === 0) {
      /* Seen, but never given a state. REVIEWED, not new.
       *
       * This used to say "new", which made actedAt unable to suppress anything
       * whose state was still `none` -- and that is precisely the shape a
       * BACKFILL produces: a real reply from eight months ago, stamped as
       * already accounted for, with no state because nobody ever touched it.
       * Every one of them would have arrived in the morning queue as work
       * somebody had failed to do.
       *
       * "There is a reply, so it must at least be new" was the intuition, and
       * it is wrong the moment something else has told us the reply is already
       * accounted for.
       */
      state.replyState = "reviewed";
    }
  }

  /* A dismissal acknowledges one observed bounce, not every future bounce.
   *
   * bounceDismissedAt is deliberately separate from actedAt: the latter means a
   * rep handled a REPLY, and reusing it here could make an unread reply look
   * reviewed. Existing boolean-only rows fall back to updatedUtc, which every
   * stored entity already has. A bounce newer than the decision reopens address
   * work on the next fold. */
  if (state.bounceDismissed && newestBounceAt) {
    const dismissedAt = state.bounceDismissedAt;
    if (dismissedAt && newestBounceAt > dismissedAt) state.bounceDismissed = false;
  }
  return state;
}

/* Why this advisor is worth a rep's time, or null.
 *
 * Returns ONE reason -- the most urgent -- rather than a list. A queue that
 * shows a person three times is a queue a rep stops reading. */
function reason(state, now = Date.now()) {
  /* A snooze silences everything until it expires.
   *
   * Checked FIRST, and it has to be: a rep who says "not now" about a reply
   * means about the reply, and a snooze that only suppressed the weaker reasons
   * would leave the row exactly where it was. When it expires the row returns
   * as `due`, which is the point -- "not now" is not "never".
   */
  const snoozed = state.snoozedUntilUtc && new Date(state.snoozedUntilUtc).getTime() > now;
  if (snoozed) return null;

  if (state.replyState === "new" && state.lastReplyAt) return "reply_new";
  if (state.replyState === "follow_up") return "reply_followup";
  /* A bounce stays in the queue until somebody FIXES the address, because that
   * is the only thing that resolves it -- and `Done` must not clear it, or the
   * row would vanish with a dead address still on file. Dismissing it is what
   * `dismissed` is for: an explicit statement that the address is as good as it
   * is going to get.
   *
   * It also precedes `due`. Snoozing a bounce temporarily hides address work;
   * when the snooze expires it is still address work, not permission to offer a
   * follow-up to an address known to be bad.
   */
  if (state.hasBounce && !state.bounceDismissed) return "bounced";
  if (state.nextActionAt && new Date(state.nextActionAt).getTime() <= now) return "due";
  /* Warm means they have answered us at some point. Somebody who has NEVER
   * replied going quiet is not a lapse -- it is the ordinary state of cold
   * outreach, and putting it in the queue would bury the real signals under
   * every advisor who ever ignored an email.
   *
   * `done` is deliberately NOT excluded. A conversation a rep finished three
   * months ago is exactly the warm relationship worth reviving; excluding it
   * would mean every advisor we ever successfully spoke to silently left the
   * queue forever, which is the opposite of what a book of relationships needs.
   * Recent activity of any kind resets the clock, so acting on it removes it.
   */
  if (state.everReplied && state.lastActivityAt
      && now - new Date(state.lastActivityAt).getTime() > QUIET_DAYS * DAY) return "quiet_warm";
  return null;
}

/* Sort key. LOWER is more urgent, and the list is sorted ascending.
 *
 * Reason dominates: a reply outranks a quiet contact however long that contact
 * has been quiet, because answering somebody who answered you is always the
 * better use of the next ten minutes. Within one reason, subtracting age puts
 * the longest-waiting first -- that is the one most at risk of being forgotten.
 *
 * The 1e15 multiplier is larger than any plausible age in milliseconds
 * (~31,700 years), so age can never promote an entry past a more urgent reason.
 */
function rank(entry, now = Date.now()) {
  const byReason = REASON_RANK.has(entry.reason) ? REASON_RANK.get(entry.reason) : 99;
  const age = entry.lastActivityAt ? now - new Date(entry.lastActivityAt).getTime() : 0;
  return byReason * 1e15 - age;
}

/* Recompute one advisor's state for one rep, from the log. */
async function refresh(userId, advisorCrd, deps = {}) {
  const st = deps.store || store;
  const rows = await st.listActivity(advisorCrd, 500);
  const mine = rows.filter((r) => String(r.userId || "") === String(userId));
  const previous = (await st.getEngagement(userId, advisorCrd)) || {};

  /* SEEDING: history arrives already handled.
   *
   * A backfill of a year would otherwise mark every reply in it `new`, and a
   * rep would open "Needs attention" to four hundred rows from eight months
   * ago. They would never trust the queue again -- and the queue's entire value
   * is that the five things in it are the five things that matter today.
   *
   * So during a backfill, actedAt is stamped forward past everything imported.
   * The history is all there on the timeline; it simply does not present itself
   * as work somebody failed to do. Genuinely new replies, arriving after the
   * backfill catches up, still surface normally.
   */
  const seeded = deps.seed
    ? { ...previous, actedAt: new Date().toISOString() }
    : previous;

  const folded = fold(mine, seeded);
  return st.putEngagement(userId, advisorCrd, folded);
}

/* The queue: who this rep should work now, and why. */
async function queue(userId, deps = {}) {
  const st = deps.store || store;
  const now = deps.now || Date.now();
  const rows = await st.listEngagement(userId);
  const out = [];
  for (const row of rows) {
    const why = reason(row, now);
    if (!why) continue;
    out.push({
      advisorCrd: row.advisorCrd || row.rowKey,
      advisorEmail: row.advisorEmail || "",
      reason: why,
      reasonLabel: (REASONS.find((r) => r.key === why) || {}).label || why,
      actions: [...(ACTIONS_BY_REASON[why] || [])],
      replyState: row.replyState || "none",
      lastReplyAt: row.lastReplyAt || "",
      lastActivityAt: row.lastActivityAt || "",
      lastOutboundAt: row.lastOutboundAt || "",
      nextActionAt: row.nextActionAt || "",
    });
  }
  out.sort((a, b) => rank(a, now) - rank(b, now));

  const counts = {};
  for (const r of REASONS) counts[r.key] = out.filter((x) => x.reason === r.key).length;
  return { entries: out, count: out.length, counts, reasons: REASONS };
}

/* Throw a rep's whole projection away and regenerate it from the log.
 *
 * The repair path, and the proof that this really is a cache. Anything the log
 * can explain is recomputed; the rep's own decisions -- replyState, actedAt,
 * nextActionAt -- are carried across, because no amount of mail can tell you
 * that somebody decided they had dealt with something.
 *
 * Returns what changed, so a caller (or the audit) can see drift rather than
 * merely fixing it silently.
 */
async function rebuild(userId, deps = {}) {
  const st = deps.store || store;
  const rows = await st.listEngagement(userId);
  const changed = [];
  for (const row of rows) {
    const crd = String(row.advisorCrd || row.rowKey || "");
    if (!crd) continue;
    const before = { ...row };
    const after = await refresh(userId, crd, deps);
    for (const key of ["lastOutboundAt", "lastInboundAt", "lastReplyAt", "lastActivityAt",
                       "hasBounce", "everReplied", "advisorEmail"]) {
      if (String(before[key] ?? "") !== String(after[key] ?? "")) {
        changed.push({ crd, key, before: before[key], after: after[key] });
        break;
      }
    }
  }
  return { advisors: rows.length, changed };
}

/* Put somebody out of the queue until a date, without pretending they are done.
 *
 * THE GAP THIS FILLS: `nextActionAt`, `scheduled` and the "Follow-up due"
 * reason all existed in the model with no way to set any of them, so that
 * reason could never fire and a `quiet_warm` row could not be dismissed at all
 * -- `Done` does not clear it, by design, because a finished conversation is
 * exactly what should resurface later.
 *
 * Snoozing is the missing verb. It answers "not now" without asserting "never",
 * which is the honest thing a rep usually means.
 */
async function snooze(userId, advisorCrd, days, deps = {}) {
  const st = deps.store || store;
  const span = Number(days);
  if (!Number.isFinite(span) || span < 1 || span > 365) {
    const err = new Error("Snooze must be between 1 and 365 days.");
    err.statusCode = 400;
    throw err;
  }
  return st.putEngagement(userId, advisorCrd, {
    nextActionAt: new Date(Date.now() + span * DAY).toISOString(),
    nextActionType: "follow_up",
    // Stamped so a reply already seen does not immediately re-open the row the
    // moment it is snoozed.
    actedAt: new Date().toISOString(),
    snoozedUntilUtc: new Date(Date.now() + span * DAY).toISOString(),
  });
}

/* A rep marking where they have got to.
 *
 * `actedAt` is stamped here rather than derived, and it is what stops a
 * reviewed conversation reappearing as new on the next sweep. */
async function setReplyState(userId, advisorCrd, next, deps = {}) {
  const st = deps.store || store;
  if (!REPLY_STATES.includes(String(next))) {
    const err = new Error(`Unknown reply state "${next}".`);
    err.statusCode = 400;
    throw err;
  }
  return st.putEngagement(userId, advisorCrd, {
    replyState: String(next), actedAt: new Date().toISOString(),
  });
}

async function dismissBounce(userId, advisorCrd, deps = {}) {
  const st = deps.store || store;
  return st.putEngagement(userId, advisorCrd, {
    bounceDismissed: true,
    bounceDismissedAt: new Date().toISOString(),
  });
}

/* A successful reply/follow-up completes the action the queue asked for.
 * These are deliberately partial Merge fields: carrying a previously-read
 * projection through this write could restore stale derived mail state. */
async function completeOutbound(userId, advisorCrd, deps = {}) {
  const st = deps.store || store;
  if (!String(advisorCrd || "")) {
    const err = new Error("advisor CRD is required.");
    err.statusCode = 400;
    throw err;
  }
  return st.putEngagement(userId, advisorCrd, {
    replyState: "done",
    actedAt: new Date().toISOString(),
    nextActionAt: "",
    nextActionType: "",
    snoozedUntilUtc: "",
  });
}

module.exports = { fold, reason, rank, refresh, rebuild, queue, setReplyState,
                   snooze, dismissBounce, completeOutbound,
                   REPLY_STATES, REASONS, ACTIONS_BY_REASON, stateIndex };
