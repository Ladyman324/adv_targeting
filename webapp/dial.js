/* Dial — the call queue, the session, and the disposition log.
 *
 * Shared by index.html and field.html deliberately. The desktop panel and the
 * field sheet already duplicate phone-kind labels and owner warnings, and that
 * duplication is scheduled to drift; the dialer is not going to add a second
 * copy of "what counts as a valid outcome" on top of it.
 *
 * THE ONE THING THIS DOES NOT KNOW
 * --------------------------------
 * Whether a call happened. Clicking a tel: link hands off to GoTo or to the
 * phone's dialer and the browser gets NO callback -- not on connect, not on
 * hangup, not ever. So there is no "call in progress" state here and no timer,
 * because a timer counting up while the rep refills their coffee is a lie the
 * UI would be telling on our behalf. The rep says what happened; we record it.
 *
 * A real answer exists -- GoTo Connect publishes call events -- and it needs an
 * OAuth app, a webhook endpoint and admin consent on the GoTo tenant. Worth
 * doing once the manual version has proved it earns its keep.
 *
 * FAILURE IS LOUD, ALWAYS
 * -----------------------
 * The entire reason for /api/log is that dispositions in localStorage are one
 * browser's private scratchpad. So a failed write NEVER silently falls back to
 * local storage and NEVER advances the queue: log() rejects, the caller shows
 * the error, and the rep stays on the person they just called. Losing the
 * session's place is a much smaller harm than believing a call was recorded.
 */
(function (global) {
  "use strict";

  const API = {
    health: "/api/health",
    log: "/api/log",
    queue: "/api/queue",
    dnc: "/api/dnc",
    flags: "/api/flags",
    settings: "/api/settings",
    email: "/api/email",
  };

  // Must match MAX_QUEUE in api/shared/store.js. The server trims silently past
  // this; checking here means a bulk add can SAY how many did not fit rather
  // than letting the tail disappear.
  const MAX_QUEUE = 250;

  const AUTO_KEY = "advisorMap.autoDial.v1";

  const state = {
    ready: false,
    user: null,
    problem: "",            // non-empty means logging is unavailable, with why
    items: [],              // queue: [{crd,name,firm,phone,phoneKind,city,state,email}]
    cursor: 0,              // how far through the queue we are
    running: false,         // a session is under way (not "a call is happening")
    dnc: new Map(),         // crd -> {by, at, reason}
    flags: new Map(),       // crd -> {key, dd, name, firmCrd, by, at}
    saving: false,
    auto: { on: false, delay: 4, announce: true },
    pending: null,          // an auto-dial counting down: {crd, name, left}
    lists: [],              // summaries: {id, name, count, cycle, updatedUtc}
    listId: "current",
    listName: "Call list",
    etag: "",               // version of the open list, for conflict detection
    cycle: 1,
    cycleStartedUtc: "",
    // crd -> {disposition, at} for THIS cycle, derived from the log. A Map
    // rather than a Set because going back to someone has to be able to say
    // what was recorded, not merely that something was.
    done: new Map(),
    trail: [],              // crds left behind this session, for Back
    defaultId: "current",
    // Per-rep preferences, read once at load. Server-side rather than
    // localStorage because a default that differs between the desk and the
    // phone is not a default -- it is two settings sharing a name.
    settings: {},
  };

  const listeners = [];
  const emit = () => listeners.forEach((fn) => { try { fn(state); } catch {} });

  async function call(url, opts) {
    const r = await fetch(url, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    // An expired Entra session does NOT arrive as a 401 here. The site's
    // responseOverrides turn 401 into a 302 to the login page so that a browser
    // navigating to a stale tab is signed back in -- and fetch follows
    // redirects, so a dead session returns 200 with a page of HTML. Parsed
    // loosely that looks like a successful empty write, which would mean a rep
    // logging an afternoon of calls into a login screen.
    //
    // So the content type is the check, not the status code.
    const ctype = r.headers.get("Content-Type") || "";
    if (r.ok && !ctype.includes("json")) {
      const err = new Error("Your sign-in expired. Reload the page to sign in " +
                            "again — nothing was saved.");
      err.status = 401;
      throw err;
    }
    let body = null;
    try { body = await r.json(); } catch { /* empty or not JSON */ }
    if (!r.ok) {
      // 401 on this app means the Entra session expired mid-session, which is
      // worth naming precisely -- it is fixed by reloading, not by retrying.
      const msg = r.status === 401
        ? "Your sign-in expired. Reload the page to continue."
        : (body && body.error) || `Request failed (${r.status}).`;
      const err = new Error(msg);
      err.status = r.status;
      throw err;
    }
    return body || {};
  }

  /* ---------- start-up ---------------------------------------------------- */
  // Checked at load rather than at the moment a button is pressed. A rep who
  // works a list for an hour and then discovers nothing was saved has lost the
  // hour; one who is told at the top has lost nothing.
  /* ---------- preferences --------------------------------------------------
   * Never fatal. A failed read costs the rep an opening view, not a record, so
   * every path here degrades to "no preferences set" rather than to an error
   * the rep has to dismiss before they can work.
   */
  async function loadSettings() {
    try {
      const d = await call(API.settings);
      state.settings = d.settings || {};
    } catch { state.settings = {}; }
    // Auto-dial moves from localStorage to the account when a stored value
    // exists. Per-device was never a decision anybody made; it was where the
    // first version happened to put it.
    const s = state.settings;
    if (s.autoDialOn !== undefined) {
      state.auto = {
        on: s.autoDialOn === "1",
        delay: Math.min(AUTO_MAX, Math.max(AUTO_MIN, Number(s.autoDialDelay) || 4)),
        announce: s.autoDialAnnounce !== "0",
      };
    }
    emit();
    return state.settings;
  }

  async function saveSettings(patch) {
    // Written through OPTIMISTICALLY so the UI reflects the choice at once,
    // then corrected from the server's own reply -- which is authoritative
    // about what it actually stored, including the keys it dropped.
    state.settings = { ...state.settings, ...patch };
    emit();
    const d = await call(API.settings, {
      method: "PUT", body: JSON.stringify(patch),
    });
    state.settings = d.settings || {};
    emit();
    return state.settings;
  }

  const setting = (k, dflt) => {
    const v = state.settings[k];
    return v === undefined || v === "" ? (dflt === undefined ? "" : dflt) : v;
  };

  /* Four round trips, issued together rather than one after another.
   *
   * They used to be strictly sequential -- health, then settings, then dnc,
   * then the queue -- and each one waited for the last. On a cold Function App
   * the first request alone took 5s, and the other three queued behind it:
   * Dial.init() measured 7.5s. Nothing in the DATA required that order; only
   * two things do, and both are honoured below.
   *
   * ORDER THAT STILL MATTERS, and why:
   *
   *   settings BEFORE openList  -- preferredListId() reads settings.defaultListId,
   *                                so opening the list first would ignore the
   *                                rep's own choice and land on whatever was
   *                                most recently touched.
   *
   *   openList BEFORE prune     -- dropSuppressed() reads state.items. Pruning
   *                                an unopened list removes nobody and reports
   *                                success. See fetchDnc above.
   *
   * The do-not-call list still FAILS CLOSED. Its rejection is caught on its own
   * promise, never inside a Promise.all -- grouping them would let a queue
   * failure mask a dnc failure, or a dnc failure hide behind a queue error, and
   * an empty dnc Map means every suppression check silently passes. The app
   * would look healthy and block nobody.
   */
  async function init() {
    loadAuto();

    // Started NOW, all of them, so they share one cold start instead of paying
    // four in series. Each carries its own catch so a rejection can never
    // surface as an unhandled promise while we await the others in order.
    const healthP = call(API.health);
    const settingsP = loadSettings().catch(() => {});
    const dncP = fetchDnc();
    const listsP = loadLists();
    // Sales knowledge, not a compliance guard: a failure costs a star on a card,
    // so it is caught here and never allowed to block the dialer.
    const flagsP = fetchFlags().catch(() => {});
    // Parked so a failure that we reach later is not reported as unhandled in
    // the meantime. The real outcome is read from the same promise below.
    dncP.catch(() => {});
    listsP.catch(() => {});

    try {
      const h = await healthP;
      state.user = h.user || null;
      state.problem = (h.configured && h.storageOk)
        ? "" : (h.detail || "Call logging is unavailable.");
    } catch (e) {
      state.problem = e.message;
    }

    if (!state.problem) {
      // Neither blocks: both swallow their own failure.
      await settingsP;
      await flagsP;
      try {
        await dncP;
      } catch (e) {
        state.problem = "The do-not-call list could not be loaded, so calling "
                      + "is disabled — nobody can be checked against it. "
                      + "Reload the page. (" + e.message + ")";
      }
      // The queue is a convenience by comparison: failing to load it costs the
      // rep their list, not a call to someone who asked us never to ring again.
      if (!state.problem) {
        try {
          await listsP;
          await openList(preferredListId());
          // Only now is there a list to prune.
          await dropSuppressed();
        } catch { /* the dock shows an empty list; nothing unsafe follows */ }
      }
    }
    state.ready = true;
    emit();
    return state;
  }

  /* ---------- saved lists --------------------------------------------------
   * A rep with a territory of 15,000 does not need "who have I not called in
   * 60 days" -- that returns the territory. What makes a territory workable is
   * curation: the 148 people in it who are ranked AND reachable. So a list is
   * a saved judgement about who matters, and a CYCLE is one pass through it.
   *
   * Progress within a cycle is DERIVED from the call log rather than counted
   * here. A stored counter would be a second record of the same fact, and the
   * hand-written one is the one that goes wrong when the list is reordered,
   * added to, or worked from two devices in the same afternoon.
   */
  const ACTIVE_KEY = "advisorMap.activeList.v1";

  async function loadLists() {
    const d = await call(API.queue);
    state.lists = d.lists || [];
    state.defaultId = d.defaultId || "current";
    return state.lists;
  }

  // Falls back to the most recently touched list, which is what makes the
  // phone pick up where the desk left off without storing a per-device
  // preference anywhere.
  function preferredListId() {
    // The ACCOUNT's choice first, because that is the one the rep made on
    // purpose. localStorage is second: it records where this device last was,
    // which is the right answer only when nothing has been chosen deliberately.
    const pinned = setting("defaultListId");
    if (pinned && state.lists.some((l) => l.id === pinned)) return pinned;
    let saved = "";
    try { saved = localStorage.getItem(ACTIVE_KEY) || ""; } catch {}
    if (saved && state.lists.some((l) => l.id === saved)) return saved;
    return (state.lists[0] && state.lists[0].id) || state.defaultId;
  }

  async function openList(id) {
    const q = await call(`${API.queue}?id=${encodeURIComponent(id)}`);
    state.listId = q.id;
    state.listName = q.name;
    state.cycle = q.cycle || 1;
    state.cycleStartedUtc = q.cycleStartedUtc || "";
    state.items = Array.isArray(q.items) ? q.items : [];
    state.etag = q.etag || "";
    state.cursor = Math.max(0, Math.min(Number(q.cursor) || 0, state.items.length));
    state.running = false;
    state.trail = [];
    try { localStorage.setItem(ACTIVE_KEY, state.listId); } catch {}
    await dropSuppressed();
    await refreshProgress();
    emit();
    return q;
  }

  // Suppression was enforced only at the moment of ADDING, which left every
  // list saved before the suppression happened still carrying that person --
  // and a colleague adding someone to do-not-call on another device does not
  // touch the list already open on this one. So the queue is re-checked
  // whenever it, or the do-not-call list, is loaded.
  async function dropSuppressed() {
    if (!state.items.length || !state.dnc.size) return 0;
    const anchor = state.items[state.cursor] && String(state.items[state.cursor].crd);
    const keep = state.items.filter((it) => !isDnc(it.crd));
    const removed = state.items.length - keep.length;
    if (!removed) return 0;
    state.items = keep;
    // Anchor on the person, not the index: dropping someone above the cursor
    // shifts everyone up, and a stale index would step over the next call.
    const at = anchor ? idx(anchor) : -1;
    state.cursor = at !== -1 ? at
      : Math.max(0, Math.min(state.cursor, state.items.length));
    await save();
    return removed;
  }

  // Who on this list has already been handled on this pass. One query, and it
  // stops as soon as it passes the cycle start because the log is stored
  // newest-first.
  async function refreshProgress() {
    state.done = new Map();
    if (!state.cycleStartedUtc) { emit(); return; }
    try {
      const d = await call(`${API.log}?since=${encodeURIComponent(state.cycleStartedUtc)}&limit=1000`);
      const onList = new Set(state.items.map((i) => String(i.crd)));
      // Newest first, so the FIRST entry seen for a person is their latest --
      // which is what a correction means: the most recent word wins, and the
      // earlier one stays in the log rather than being rewritten.
      for (const e of d.events || []) {
        const crd = String(e.crd);
        if (e.kind !== "outcome" || !onList.has(crd) || state.done.has(crd)) continue;
        state.done.set(crd, { disposition: e.disposition, at: e.at });
      }
    } catch { /* progress is a nicety; its absence must not block calling */ }
    emit();
  }

  async function createList(name) {
    const id = `l${Date.now().toString(36)}`;
    await call(API.queue, {
      method: "PUT",
      body: JSON.stringify({ id, name: name || "New list", items: [], cursor: 0,
                             cycle: 1, cycleStartedUtc: new Date().toISOString() }),
    });
    await loadLists();
    return openList(id);
  }

  async function renameList(name) {
    state.listName = name;
    await save();
    await loadLists();
  }

  async function deleteList(id) {
    await call(`${API.queue}?id=${encodeURIComponent(id)}`, { method: "DELETE" });
    await loadLists();
    const next = preferredListId();
    if (id === state.listId) {
      try { localStorage.removeItem(ACTIVE_KEY); } catch {}
      await openList(next);
    }
  }

  // A new pass over the same people. The list is untouched; only the clock
  // moves, which is what makes "31 of 148 done" reset without deleting the
  // history that produced it.
  async function startCycle() {
    state.cycle = (state.cycle || 1) + 1;
    state.cycleStartedUtc = new Date().toISOString();
    state.cursor = 0;
    state.trail = [];
    await save();
    await refreshProgress();
  }

  async function refreshQueue() {
    await loadLists();
    await openList(state.listId || preferredListId());
  }

  /* Split in two on purpose.
   *
   * The FETCH can happen as early as we like. The PRUNE cannot: dropSuppressed
   * reads state.items, so running it before the queue is open finds an empty
   * list, removes nobody, and returns 0 -- looking exactly like "there was
   * nothing to suppress". init() below fetches early and prunes after the list
   * is open, which is the only order that is both fast and correct.
   */
  async function fetchDnc() {
    const d = await call(API.dnc);
    state.dnc = new Map((d.entries || []).map((e) => [String(e.crd), e]));
  }

  async function refreshDnc() {
    await fetchDnc();
    await dropSuppressed();
    emit();
  }

  async function save() {
    state.saving = true;
    emit();
    try {
      // The etag is what we last READ or WROTE for this row. Sending it turns
      // the save into "replace this list only if it still looks the way I think
      // it does" -- the desk and the phone are the same rep, and whole-row
      // replacement means the loser of a race loses their additions in silence.
      const q = await call(API.queue, {
        method: "PUT",
        body: JSON.stringify({
          id: state.listId, name: state.listName,
          items: state.items, cursor: state.cursor,
          cycle: state.cycle, cycleStartedUtc: state.cycleStartedUtc,
          etag: state.etag || "",
        }),
      });
      state.etag = q.etag || "";
      state.items = q.items || state.items;
      state.cursor = Number(q.cursor) || 0;
      // The picker labels every list "<name> (<count>)" from these summaries,
      // and they were only refetched on open/create/rename/delete -- so bulk
      // adding forty people left the picker insisting the list held none.
      const row = state.lists.find((l) => l.id === state.listId);
      if (row) { row.count = state.items.length; row.name = state.listName; }
      return q;
    } catch (e) {
      // Somebody else -- almost always this same rep on their other device --
      // wrote this list after we read it. Re-read rather than leave the page
      // holding a version the server has already rejected, and let the error
      // reach the rep so the reload is a decision and not a surprise.
      if (e.status === 409) { try { await openList(state.listId); } catch {} }
      throw e;
    } finally {
      state.saving = false;
      emit();
    }
  }

  /* ---------- the queue ---------------------------------------------------- */
  const idx = (crd) => state.items.findIndex((i) => String(i.crd) === String(crd));
  const inQueue = (crd) => idx(crd) !== -1;
  const isDnc = (crd) => state.dnc.has(String(crd));

  /* KEY CONTACT and DUE DILIGENCE.
   *
   * Two independent flags, not one role: they are usually the same person and
   * sometimes not, so "both" has to be expressible without inventing a third
   * value. Held firm-wide -- see api/flags/index.js for why.
   */
  const flagsOf = (crd) => state.flags.get(String(crd)) || null;

  /* WHOSE flag, because "somebody marked this" and "I marked this" are
   * different questions and the card asks both.
   *
   * The control has to reflect MINE -- pressing a lit star that somebody else
   * lit used to clear THEIR mark rather than add mine, so one rep could delete
   * another's and nobody could join a flag already set. The fact that a
   * colleague also marked them is shown alongside, not merged into the
   * button's state.
   */
  const flagMembersOf = (crd, kind) => {
    const f = flagsOf(crd);
    if (!f) return [];
    const raw = kind === "key" ? f.keyBy : f.ddBy;
    if (Array.isArray(raw)) return raw;
    // A server that predates the set sends one name, or nothing but `by`.
    const one = String(raw || f.by || "").trim();
    return one ? [one] : [];
  };
  const meName = () => String((state.user && state.user.name) || "").trim().toLowerCase();
  const flaggedByMe = (crd, kind) => {
    const me = meName();
    // Nobody signed in: claim nothing is mine rather than claiming it all is.
    return !!me && flagMembersOf(crd, kind).some((n) => String(n).trim().toLowerCase() === me);
  };
  const flaggedByOthers = (crd, kind) => {
    const me = meName();
    return flagMembersOf(crd, kind).filter((n) => String(n).trim().toLowerCase() !== me);
  };
  // Anyone at all -- what the map pin and the read-only glyphs care about.
  const isKeyContact = (crd) => !!(flagsOf(crd) && flagsOf(crd).key);
  const isDueDiligence = (crd) => !!(flagsOf(crd) && flagsOf(crd).dd);

  async function fetchFlags() {
    const d = await call(API.flags);
    state.flags = new Map((d.entries || []).map((e) => [String(e.crd), e]));
  }

  /* Optimistic, then reconciled.
   *
   * The star fills the instant it is clicked and rolls back if the write fails.
   * Waiting on a round trip to a cold Function App before the icon moves reads
   * as a broken button, and a rep clicks it again.
   */
  async function setFlag(crd, kind, on, name, firmCrd) {
    const id = String(crd);
    const before = state.flags.get(id) || null;
    // Optimism now has to model a SET: adding me must not drop a colleague,
    // and removing me must not drop the flag while they still hold it.
    const memberList = (k) => {
      const raw = before ? (k === "key" ? before.keyBy : before.ddBy) : null;
      if (Array.isArray(raw)) return raw.slice();
      const one = String(raw || (before && before.by) || "").trim();
      return before && (k === "key" ? before.key : before.dd) && one ? [one] : [];
    };
    const me = String((state.user && state.user.name) || "").trim();
    const next = { key: memberList("key"), dd: memberList("dd") };
    const target = kind === "key" ? "key" : "dd";
    next[target] = next[target].filter((n) => n.toLowerCase() !== me.toLowerCase());
    if (on && me) next[target].push(me);
    const optimistic = { crd: id, key: next.key.length > 0, dd: next.dd.length > 0,
                         keyBy: next.key, ddBy: next.dd,
                         name: name || (before && before.name) || "" };
    if (optimistic.key || optimistic.dd) state.flags.set(id, optimistic);
    else state.flags.delete(id);
    emit();
    try {
      const d = await call(API.flags, { method: "PUT",
        body: JSON.stringify({ crd: id, kind, on: !!on, name, firmCrd }) });
      const saved = d.saved || {};
      if (saved.key || saved.dd) state.flags.set(id, saved); else state.flags.delete(id);
    } catch (e) {
      if (before) state.flags.set(id, before); else state.flags.delete(id);
      emit();
      throw e;
    }
    emit();
    return state.flags.get(id) || null;
  }

  // THE ONLY WAY TO BUILD A tel: HREF. Both views used to write `href="tel:..."`
  // inline -- five places across two files, of which the do-not-call check
  // reached zero. Every one of them looked fine on its own; the guard simply
  // was not where the link was.
  //
  // Returning "" for a suppressed advisor means a caller who forgets to check
  // still cannot produce a dialable link: the worst case is a broken-looking
  // button, not a call we promised never to place. audit.py enforces that no
  // literal tel: href exists anywhere else.
  function telHref(crd, phone) {
    if (!phone || isDnc(crd)) return "";
    return "tel:" + String(phone).replace(/[^0-9+,;*#]/g, "");
  }

  // Suppression is enforced HERE, at the point of adding, so a name on the
  // firm-wide do-not-call list never reaches a queue at all -- rather than
  // being caught later by whichever view remembered to check.
  async function add(item) {
    if (!item || !item.crd) return { added: 0, blocked: 0 };
    return addMany([item]);
  }

  // Best number first. A third of the file is switchboards, shared lines and
  // toll-free numbers, and those calls mostly do not reach a person -- so the
  // productive ones should happen while the rep is fresh, and the low-yield
  // ones should be what falls off the end when the hour runs out.
  const REACHES = new Set(["direct", "extension"]);
  function insertRank(it) {
    return (REACHES.has(it.phoneKind) ? 0 : it.phone ? 1 : 2) * 10
         + (it.ranked ? 0 : 1);
  }

  async function addMany(items, opts) {
    const fresh = [];
    let blocked = 0, noPhone = 0, dupe = 0;
    for (const it of items || []) {
      if (!it || !it.crd) continue;
      if (isDnc(it.crd)) { blocked++; continue; }
      if (inQueue(it.crd)) { dupe++; continue; }
      if (fresh.some((f) => String(f.crd) === String(it.crd))) { dupe++; continue; }
      // A dial session has no use for someone with no number. Only enforced on
      // bulk adds: adding one person deliberately is a different intent.
      if (opts && opts.phoneOnly && !it.phone) { noPhone++; continue; }
      fresh.push(it);
    }
    // Only the incoming batch is sorted. Re-sorting the whole queue would
    // silently undo an order the rep arranged by hand.
    fresh.sort((a, b) => insertRank(a) - insertRank(b));

    const room = Math.max(0, MAX_QUEUE - state.items.length);
    const overflow = Math.max(0, fresh.length - room);
    const taken = fresh.slice(0, room);
    if (taken.length) {
      state.items = state.items.concat(taken);
      await save();
    } else {
      emit();
    }
    return { added: taken.length, blocked, noPhone, dupe, overflow,
             max: MAX_QUEUE };
  }

  async function remove(crd) {
    const i = idx(crd);
    if (i === -1) return;
    state.items.splice(i, 1);
    // Keep the cursor pointing at the same PERSON, not the same slot: removing
    // someone already dialled must not silently re-serve the next one.
    if (i < state.cursor) state.cursor = Math.max(0, state.cursor - 1);
    await save();
  }

  async function clear() {
    cancelAuto();
    state.items = [];
    state.cursor = 0;
    state.running = false;
    await save();
  }

  // The cursor follows the PERSON, not the slot.
  //
  // Reordering used to move the array and leave the cursor where it was, so
  // promoting the person currently being called swapped them for whoever landed
  // on that index -- typically someone already dialled and greyed out. Same
  // failure as removing a row from under the cursor: the list is correct, the
  // position is wrong, and nothing errors.
  async function move(crd, delta) {
    const i = idx(crd);
    const j = i + delta;
    if (i === -1 || j < 0 || j >= state.items.length) return;
    const anchor = state.items[state.cursor] && state.items[state.cursor].crd;
    const [it] = state.items.splice(i, 1);
    state.items.splice(j, 0, it);
    if (anchor !== undefined) {
      const moved = idx(anchor);
      if (moved !== -1) state.cursor = moved;
    }
    await save();
  }

  /* ---------- the session -------------------------------------------------- */
  // "Running" means the rep is working the list, not that a call is connected.
  // Pausing and ending are the same state change with different words; ending
  // keeps everything logged so far, because throwing away a morning's work as
  // the price of stopping would be an absurd thing to build.
  // The first person on this pass who has not been handled yet -- which is not
  // the same as "where the cursor was". A rep who worked 30 of a saved list on
  // Monday should land on number 31 on Tuesday even if the list has since been
  // reordered or added to, and a finished list should offer a new cycle rather
  // than silently replaying people who were just called.
  // Suppressed people are skipped here as well as purged from the list. The
  // purge is the real fix; this is the backstop for the gap between a colleague
  // suppressing someone and this device hearing about it.
  const pending = (it) => it && !state.done.has(String(it.crd)) && !isDnc(it.crd);

  function firstPending(from) {
    for (let i = Math.max(0, from || 0); i < state.items.length; i++)
      if (pending(state.items[i])) return i;
    for (let i = 0; i < Math.max(0, from || 0); i++)
      if (pending(state.items[i])) return i;
    return -1;
  }

  function start() {
    if (!state.items.length || state.problem) return false;
    const next = firstPending(state.cursor);
    if (next === -1) return false;        // everyone done; the UI offers a cycle
    state.cursor = next;
    state.running = true;
    emit();
    return true;
  }

  // Stopping cancels any armed auto-dial, and that belongs HERE rather than in
  // each view's pause handler. It was in the field view's and not the desktop's,
  // so pressing Pause there left a live timer: running went false and the phone
  // rang seven seconds later anyway. A rep who explicitly stops must not place a
  // call -- that is the exact accident this feature has to be safe against.
  function pause() { cancelAuto(); state.running = false; emit(); }
  function end() { cancelAuto(); state.running = false; emit(); }

  const current = () => (state.running ? state.items[state.cursor] || null : null);
  const remaining = () => Math.max(0, state.items.length - state.cursor);

  // Remember who we are leaving, so Back can return to them. A trail of CRDs
  // rather than of indices: "call back" moves people to the end and
  // "do not call" removes them, so a stored position would point at a
  // stranger by the time it was used.
  function leaving() {
    const it = state.items[state.cursor];
    if (it) state.trail.push(String(it.crd));
    if (state.trail.length > 50) state.trail.shift();
  }

  async function advance() {
    leaving();
    // Skips anyone already handled this cycle, so re-running a partly-worked
    // list does not serve people the rep spoke to an hour ago.
    const next = firstPending(state.cursor + 1);
    if (next === -1) { state.cursor = state.items.length; state.running = false; }
    else state.cursor = next;
    await save();
    return current();
  }

  // Back to the person just handled, so a mis-tapped outcome can be corrected
  // and a card can be re-read. It does NOT un-log anything: the log is
  // append-only, and logging again from here records a correction that leaves
  // the original in place. Rewriting history is how a call record stops being
  // a record.
  const canBack = () => state.trail.length > 0;

  async function back() {
    while (state.trail.length) {
      const crd = state.trail.pop();
      const i = idx(crd);
      // Gone from the list entirely -- a do-not-call removed them. Keep
      // walking back rather than landing on nobody.
      if (i === -1) continue;
      state.cursor = i;
      state.running = true;
      await save();
      return current();
    }
    emit();
    return current();
  }

  // "Call back": move this person to the END of the list and stay put, because
  // removing them shifts everyone up by one and the cursor is already pointing
  // at the next person. Advancing as well would step over somebody.
  //
  // The cursor is NOT decremented: the entry left the position the cursor is
  // on, so that position now holds the next person, which is exactly right.
  async function requeue(crd) {
    const i = idx(crd);
    if (i === -1) return current();
    leaving();
    const [it] = state.items.splice(i, 1);
    state.items.push(it);
    if (i < state.cursor) state.cursor = Math.max(0, state.cursor - 1);
    if (state.cursor >= state.items.length) state.running = false;
    await save();
    return current();
  }

  /* ---------- the log ------------------------------------------------------ */
  // `who` is never sent. The server takes it from x-ms-client-principal, which
  // Static Web Apps resolves at the edge from the session cookie -- so a call
  // is attributed to whoever is actually signed in, not to whatever the page
  // claims. That property is the reason this endpoint is worth having.
  async function log(entry) {
    if (!entry || !entry.crd) throw new Error("Nothing to log.");
    if (state.problem) throw new Error(state.problem);
    const saved = await call(API.log, {
      method: "POST",
      body: JSON.stringify({
        crd: String(entry.crd),
        name: entry.name || "",
        firm: entry.firm || "",
        phone: entry.phone || "",
        phoneKind: entry.phoneKind || "",
        kind: entry.kind || "outcome",
        disposition: entry.disposition || "",
        // Optional. Empty means "no purpose chosen", which is the state every
        // call logged before this existed is in -- so the CRM subject falls
        // back to what it has always been rather than to a guess.
        purpose: entry.purpose || "",
        note: entry.note || "",
        sessionId: entry.sessionId || "",
      }),
    });
    // Handled on this cycle. Recorded locally as well as derived from the log
    // so the count moves the instant the rep presses a button, rather than
    // after a round trip they would notice.
    if (entry.kind !== "call" && entry.kind !== "email" && entry.disposition)
      state.done.set(String(entry.crd),
                     { disposition: entry.disposition, at: saved.at || new Date().toISOString() });

    let removedCurrent = false;
    if (saved.dnc) {
      state.dnc.set(String(entry.crd), saved.dnc);
      // Anyone already queued gets pulled immediately, including later entries
      // in this same session.
      //
      // Removing rows from under the cursor is where this would quietly go
      // wrong: drop the person being called and the cursor is left pointing at
      // the NEXT one, so the caller's advance() would step over them and nobody
      // would ever notice a name had been skipped. So the cursor is corrected
      // for removals before it, and the caller is told whether the current
      // entry was the one removed.
      const curCrd = state.items[state.cursor] && state.items[state.cursor].crd;
      const kept = [];
      let removedBefore = 0;
      state.items.forEach((it, i) => {
        if (!isDnc(it.crd)) { kept.push(it); return; }
        if (i < state.cursor) removedBefore++;
        if (String(it.crd) === String(curCrd)) removedCurrent = true;
      });
      if (kept.length !== state.items.length) {
        state.items = kept;
        state.cursor = Math.max(0, Math.min(state.cursor - removedBefore, kept.length));
        if (state.cursor >= state.items.length) state.running = false;
        await save();
      }
    }
    emit();
    return { ...saved, removedCurrent };
  }

  // "Has anyone here already spoken to them?" -- across all users, which is
  // exactly what a per-browser log could never answer.
  async function history(crd, limit) {
    const d = await call(`${API.log}?crd=${encodeURIComponent(crd)}&limit=${limit || 10}`);
    return d.events || [];
  }

  /* The same question, answered from the CRM as well.
   *
   * Separate from history() rather than a flag on it, because this one costs a
   * round trip to Act! and most callers must not pay it: the queue's progress
   * query fetches a thousand rows and the session card's one-line summary is
   * fetched on every advance.
   *
   * Returns { events, crm } where crm says whether the CRM half arrived. That
   * is reported rather than inferred from an empty list on purpose -- "Act!
   * has nothing on them" and "Act! could not be reached" look identical on
   * screen and mean opposite things to a rep deciding whether to cold-call.
   */
  /* THREE SOURCES, ONE TIMELINE.
   *
   * "What have we done with this person" was answered from the call log and
   * Act!, and email was missing from it entirely -- because the emailer writes
   * neither. A campaign send is observed later by the reply sweep and lands in
   * EmailActivity, a table this panel never read. So a rep who emailed an
   * advisor an hour ago opened Full history and saw "Nothing recorded".
   *
   * Email is fetched ALONGSIDE rather than blocking: it is a different service
   * and a different failure mode, and losing the call log because the mail
   * timeline is slow would be a bad trade. A failure here is reported in the
   * notice, never as an empty history.
   */
  async function fullHistory(crd, limit) {
    const logP = call(`${API.log}?crd=${encodeURIComponent(crd)}`
                    + `&limit=${limit || 25}&act=1`);
    const mailP = call(`${API.email}?op=activity&crd=${encodeURIComponent(crd)}`)
      .then((d) => ({ ok: true, entries: d.entries || [] }))
      .catch((e) => ({ ok: false, entries: [], why: e.message || "unavailable" }));
    const [d, mail] = await Promise.all([logP, mailP]);
    return { events: d.events || [], crm: d.crm || { ok: false, why: "", count: 0 }, mail };
  }

  /* One merged history, described in words, WITHOUT any HTML.
   *
   * This module has no DOM of its own and is not about to grow one -- but the
   * two views must not each decide for themselves what a CRM row is called or
   * when to admit that Act! was unreachable. Those are the parts that would
   * quietly disagree; the markup is not. So the semantics live here and the
   * two views render the same structure their own way.
   */
  /* ACT! NOTES ARE HTML, and this turns them back into text.
   *
   * Act! stores history details as rich text and hands them over as markup --
   * `<span style="...">`, `<p>`, `<br>`, sometimes a whole `<style>` block
   * from a pasted Outlook message. Both views escape everything before
   * rendering, which is right and which is exactly why the tags appeared on
   * screen as literal `<span>` and `<p>`.
   *
   * PARSED, NOT REGEXED. A regex over HTML gets `<p title="a>b">` wrong, mixes
   * up `&lt;` with a real tag, and leaves entities like `&nbsp;` and `&#39;`
   * sitting in the output. DOMParser is a real parser, and `text/html` here
   * neither executes script nor fetches anything -- the document is never
   * inserted into a page. The result is still escaped by the views, so
   * nothing about the existing safety changes.
   *
   * Block boundaries become newlines rather than vanishing: a note written as
   * three paragraphs is three thoughts, and running them into one line loses
   * the only structure the rep wrote.
   */
  function plainText(s) {
    const raw = String(s || "");
    // Most rows are already plain -- our own notes, and Act! subjects. Skip
    // the parser entirely rather than paying for it on every row.
    if (!/[<&]/.test(raw)) return raw.trim();
    let doc;
    try {
      doc = new DOMParser().parseFromString(raw, "text/html");
    } catch { return raw.trim(); }
    // These carry text that is NOT content. A pasted Outlook signature brings
    // a <style> block with it, and textContent would happily return the CSS.
    doc.querySelectorAll("style, script, head, title").forEach((n) => n.remove());
    // `break` as well as `br`: Act! emits a non-standard <break> tag, which
    // textContent would silently close up, joining two lines into one.
    //
    // UNWRAPPED, NOT REPLACED, and the difference is not cosmetic. `<br>` is a
    // void element with no children, so replacing it is harmless. `<break>` is
    // not a real tag, so the parser treats it as an ordinary open element and
    // nests EVERYTHING AFTER IT inside it -- and replaceWith() then deletes
    // the node together with its children. That silently ate the rest of a
    // note: "Assistant is Dana." vanished with the tag that preceded it, and
    // the output looked entirely clean while missing a line.
    doc.querySelectorAll("br, break").forEach((n) => {
      n.before("\n");
      while (n.firstChild) n.parentNode.insertBefore(n.firstChild, n);
      n.remove();
    });
    doc.querySelectorAll("p, div, li, tr, blockquote, h1, h2, h3, h4, h5, h6")
      .forEach((n) => n.append("\n"));
    return (doc.body.textContent || "")
      .replace(/ /g, " ")        // &nbsp; is a space, not a glyph
      .replace(/[ \t]+/g, " ")
      .replace(/ *\n */g, "\n")
      .replace(/\n{3,}/g, "\n\n")     // keep a paragraph break, drop the rest
      .trim();
  }

  // Long enough to be useful in a list, short enough that one verbose note
  // does not push the rest of the history off the screen. Applied AFTER the
  // markup is gone -- trimming first would spend the budget on tags.
  const HIST_TEXT_MAX = 400;
  function clip(s) {
    return s.length > HIST_TEXT_MAX ? s.slice(0, HIST_TEXT_MAX - 1).trimEnd() + "…" : s;
  }

  /* Does an Act! row already describe this email?
   *
   * Act! carries emails a rep logged there by hand, and the sweep will also
   * observe the same message in the mailbox. Showing both is worse than showing
   * either: it reads as two separate contacts on the same day.
   *
   * Matched on the DAY plus the subject, because that is all the two sources
   * share -- Act! has no Graph id and our row has no Act! id. Same day and same
   * subject is one email; the false-positive case is emailing the same advisor
   * the same subject twice in one day, which is a mailing mistake anyway.
   */
  function sameDay(a, b) { return String(a || "").slice(0, 10) === String(b || "").slice(0, 10); }
  function normSubject(v) {
    return String(v || "").toLowerCase()
      .replace(/^\s*(re|fw|fwd)\s*:\s*/i, "").replace(/\s+/g, " ").trim();
  }

  function describeHistory(events, crm, mail) {
    const crmEmails = (events || []).filter((e) => e.source === "act");
    const rows = (events || []).map((e) => {
      const fromCrm = e.source === "act";
      return {
        crm: fromCrm,
        // The DATE is what shows; the full timestamp is what sorts. Sorting on
        // the sliced date put everything logged on one day in arbitrary order,
        // so a reply that arrived at 13:06 could sit below a call at 13:00 --
        // and the whole point of one merged timeline is the sequence.
        ts: String(e.at || ""),
        at: String(e.at || "").slice(0, 10),
        // The CRM's own words for what it was; ours is the outcome we recorded.
        what: fromCrm ? (plainText(e.type) || "History")
                      : (e.disposition ? outcomeLabel(e.disposition)
                                       : e.kind === "call" ? "Dialled"
                                       : e.kind === "email" ? "Emailed" : "Logged"),
        who: plainText(e.who),
        // On a CRM row the subject IS the summary; on ours the note is. Both
        // halves go through the cleaner: Act! subjects are usually plain, but
        // "usually" is not a thing to render markup on.
        text: clip(fromCrm
          ? [plainText(e.subject), plainText(e.note)].filter(Boolean).join(" — ")
          : [purposeLabel(e.purpose), plainText(e.note)].filter(Boolean).join(" — ")),
      };
    });
    // WHY THE CRM HALF IS MISSING, in words, and only when it is missing.
    // An empty panel means "nobody has ever contacted them", which is a
    // reason to cold-call. "We could not ask Act!" is not. On screen those
    // look identical, so the difference has to be stated.
    let notice = "";
    if (crm && !crm.ok) {
      if (crm.why === "no-contact")
        notice = "This advisor has no matched record in Act!, so this is only "
               + "what the app itself has logged.";
      else if (crm.why === "off")
        notice = "Act! history is not switched on, so this is only what the "
               + "app itself has logged.";
      else if (String(crm.why).startsWith("failed:"))
        notice = "Act! could not be reached, so its history is missing here — "
               + "this is not the full picture.";
    }
    /* Email rows, merged in and then the whole thing re-sorted. Newest first,
     * matching what both views already render. */
    const mailRows = ((mail && mail.entries) || [])
      .filter((m) => !crmEmails.some((c) =>
        sameDay(c.at, m.occurredAt) && normSubject(c.subject) === normSubject(m.subject)))
      .map((m) => ({
        crm: false,
        email: true,
        ts: String(m.occurredAt || ""),
        at: String(m.occurredAt || "").slice(0, 10),
        what: m.label || (m.direction === "outbound" ? "Emailed" : "Email received"),
        // The mailbox it was seen in, so a shared timeline still says WHO.
        who: plainText(m.who || ""),
        text: clip(plainText(m.subject || "")),
      }));

    const all = rows.concat(mailRows)
      .sort((a, b) => String(b.ts || b.at).localeCompare(String(a.ts || a.at)));

    if (mail && mail.ok === false) {
      notice = (notice ? notice + " " : "")
        + "Email activity could not be loaded, so any email is missing here.";
    }
    return { rows: all, notice };
  }

  /* ---------- auto-dial ------------------------------------------------------
   * Logs an outcome, waits, optionally says who is next, then places the call.
   *
   * OFF BY DEFAULT, and it should stay that way. Dialing without an explicit
   * press means dialing while the rep is still reading the card, or mid-sentence
   * with a colleague -- and an accidental call to a prospect costs far more than
   * the click it saved.
   *
   * THE ANNOUNCE IS THE POINT, not a flourish. Spoken aloud, the rep knows who
   * is being called without looking at the screen, which is the only version of
   * this that is defensible for someone between appointments.
   *
   * BROWSER CAVEAT: navigating to a tel: URL is subject to user-activation
   * rules, and activation from the outcome tap expires after a few seconds. The
   * delay is therefore capped low, and a blocked navigation is handled by
   * leaving the Call button armed rather than by pretending a call was placed.
   */
  const AUTO_MIN = 2, AUTO_MAX = 8;

  function loadAuto() {
    try {
      const v = JSON.parse(localStorage.getItem(AUTO_KEY)) || {};
      state.auto = {
        on: !!v.on,
        delay: Math.min(AUTO_MAX, Math.max(AUTO_MIN, Number(v.delay) || 4)),
        announce: v.announce !== false,
      };
    } catch { state.auto = { on: false, delay: 4, announce: true }; }
  }

  function setAuto(patch) {
    state.auto = { ...state.auto, ...patch };
    state.auto.delay = Math.min(AUTO_MAX, Math.max(AUTO_MIN, Number(state.auto.delay) || 4));
    try { localStorage.setItem(AUTO_KEY, JSON.stringify(state.auto)); } catch {}
    // Kept in localStorage as well as on the account: the local copy is what
    // makes the checkbox correct on the next load BEFORE the settings request
    // comes back, and the account copy is what makes the desk and the phone
    // agree. Best effort -- failing to remember a preference must not
    // interrupt anything.
    saveSettings({
      autoDialOn: state.auto.on ? "1" : "0",
      autoDialDelay: String(state.auto.delay),
      autoDialAnnounce: state.auto.announce ? "1" : "0",
    }).catch(() => {});
    if (!state.auto.on) cancelAuto();
    emit();
  }

  function say(text) {
    try {
      if (!("speechSynthesis" in window) || !text) return;
      // Cancel first: queued utterances would stack up over a long session and
      // start announcing people the rep has already moved past.
      speechSynthesis.cancel();
      const u = new SpeechSynthesisUtterance(text);
      u.rate = 1.0;
      speechSynthesis.speak(u);
    } catch { /* speech is a convenience, never a dependency */ }
  }

  function cancelAuto() {
    if (state.pending && state.pending.timer) clearInterval(state.pending.timer);
    state.pending = null;
    try { if ("speechSynthesis" in window) speechSynthesis.cancel(); } catch {}
    emit();
  }

  // `fire` is supplied by the view, because placing the call means activating
  // that view's own tel: anchor -- this module has no DOM of its own.
  function armAuto(item, fire) {
    cancelAuto();
    if (!state.auto.on || !item || !item.phone) return;
    if (state.auto.announce)
      say(`Next, ${item.name}${item.firm ? `, ${item.firm}` : ""}`);
    state.pending = { crd: item.crd, name: item.name, left: state.auto.delay, timer: null };
    state.pending.timer = setInterval(() => {
      if (!state.pending) return;
      state.pending.left -= 1;
      if (state.pending.left > 0) { emit(); return; }
      const p = state.pending;
      clearInterval(p.timer);
      state.pending = null;
      emit();
      try { fire(); } catch { /* a blocked tel: leaves the button for the rep */ }
    }, 1000);
    emit();
  }

  /* ---------- vocabulary ----------------------------------------------------
   * One definition, both views. Order is the order the buttons appear in: the
   * ordinary outcomes first, then the two that carry consequences.
   *
   * `act` IS THE ACT! CRM RESULT ID, and it is here rather than in the sync
   * code on purpose. Eight buttons collapse onto Act!'s four Call results, and
   * a collapse that lives somewhere else is a collapse nobody reading this list
   * can see. Proven against the live CRM on 2026-08-14 by
   * `act_write_test.py --outcome-map`: all four results round-trip to the type
   * they were sent as, so the distinctions below are ones the CRM can hold.
   *
   *     0 Call Attempted   1 Call Completed   2 Call Received   17 Call Left Message
   *
   * `actNote` exists because three outcomes share result 0 or 1. Without it a
   * wrong number and an unanswered ring are the same row in every Act! report,
   * and the meaning would survive only in our own database -- so it is appended
   * to the history details, where the rest of the firm can actually read it.
   */
  const OUTCOMES = [
    { key: "connected",    label: "Connected",     act: 1 },
    // Replaced "No answer" and absorbed "Gatekeeper": both meant "tried, did
    // not reach them", both wrote Call Attempted, and a rep mid-session should
    // not have to decide which flavour of not-reaching-them this was.
    { key: "attempted",    label: "Attempted",     act: 0 },
    { key: "voicemail",    label: "Voicemail",     act: 17 },
    // Keeps the person in the list instead of dropping them. Voicemail and a
    // plain attempt usually mean "try again at four", and without this the rep
    // had to remember, find them again and re-queue by hand.
    { key: "callback",     label: "Call back",     act: 0, requeue: true,
      actNote: "Call back requested." },
    // Rare on an outbound list, and it earns its square anyway: without it a
    // returned call gets logged as Connected, which quietly inflates the
    // outbound connect rate with calls we did not place.
    { key: "received",     label: "Call received", act: 2 },
    // Feeds back into data quality: this is the only signal we have on whether
    // the 84,222 numbers labelled "Direct" actually reach the person.
    /* "Wrong #", not "Wrong number": at the grid's column width the full words
       wrapped to two lines on the desktop, making one cell of nine a different
       height from its neighbours. The note written back to the CRM below is
       still the full phrase -- that is read by people, not squeezed into a
       button. */
    { key: "wrong-number", label: "Wrong #",  act: 0,
      actNote: "Wrong number." },
    { key: "do-not-call",  label: "Do not call",   act: 1, grave: true,
      actNote: "Asked not to be called again." },
  ];

  /* WHY THE CALL WAS MADE -- four chips, and they are NOT a second note.
   *
   * A note is free text and lands in Act!'s details, where it is readable one
   * record at a time. This lands in the history SUBJECT, which is the Title
   * column of the Act! grid -- the thing a colleague scans when they open a
   * contact. Every call we write today reads "Call — Allen White", so six of
   * them are six identical rows and the purpose of each survives only in prose
   * nobody opens.
   *
   * FOUR, and no more. This is a chip row on a phone under the rep's thumb; a
   * taxonomy that needs scrolling is one reps stop choosing from, and a purpose
   * chosen at random is worse than none. Deliberately OPTIONAL -- nothing is
   * selected by default, and skipping it writes exactly what today writes.
   *
   * Act!'s own per-type "regarding" dropdown was the alternative source for
   * this list. Rejected for now: it is administered inside Act! by people who
   * have never seen this app, so the vocabulary could change under us with no
   * signal, and the four below are the four the sales team actually named.
   */
  const PURPOSES = [
    { key: "meeting",   label: "Meeting" },
    { key: "materials", label: "Materials" },
    { key: "check-in",  label: "Check-in" },
    { key: "cold",      label: "Cold call" },
  ];

  function purposeLabel(key) {
    const p = PURPOSES.find((x) => x.key === key);
    return (p && p.label) || "";
  }

  /* Email composition now lives in email.js and all message generation is
   * repeated and validated on the server. This module keeps only the shared
   * purpose vocabulary used by call history and the composer labels. */

  // Retired keys. Events logged under them are already in storage and must keep
  // rendering as words rather than as a raw key, so the label lookup outlives
  // the button. Nothing offers these; they are read-only history.
  const RETIRED = { "no-answer": "No answer", gatekeeper: "Gatekeeper",
                    skipped: "Skipped" };

  // The one place a stored disposition becomes something a human reads.
  function outcomeLabel(key) {
    const o = OUTCOMES.find((x) => x.key === key);
    return (o && o.label) || RETIRED[key] || String(key || "").replace(/-/g, " ");
  }

  /* Whether an outcome reached the CRM, in words a rep can act on.
   *
   * Returns "" for every case where nothing is wrong or nothing can be done --
   * which is most of them. A rep cannot fix a missing crosswalk match or an Act!
   * outage, and a line on every single call would train them to stop reading the
   * one that matters. So this stays silent except where the rep's own
   * understanding of what happened would otherwise be wrong.
   */
  function actNotice(status) {
    const s = String(status || "");
    if (s.startsWith("failed:")) {
      return "Saved here, but it did not reach Act!. Nothing is lost — the "
           + "outcome is recorded and can be pushed again.";
    }
    if (s.startsWith("not-attributable:")) {
      // Only happens on a colleague's own contact record, where Act! insists on
      // filing the history under them. Said plainly rather than apologetically:
      // the outcome is recorded, the CRM copy is not, and that is the correct
      // trade rather than a fault the rep should chase.
      return "Saved here. Act! would have recorded this call under your "
           + "colleague's name rather than yours, so it was not kept there. "
           + "Only happens on EIC's own people.";
    }
    if (s.startsWith("misattributed:")) {
      return "Saved here, but Act! recorded the call against the wrong person "
           + "— " + s.replace(/^misattributed:\s*/, "") + ". The outcome itself "
           + "is not lost.";
    }
    if (s === "no-act-user") {
      return "Saved here. It was not written to Act! because your sign-in does "
           + "not match an Act! user, and it must not be filed under anyone else.";
    }
    if (s === "not-in-test-allowlist") {
      return "Saved here. Act! sync is limited to the test advisor at present.";
    }
    return "";
  }
  const REQUEUE = new Set(OUTCOMES.filter((o) => o.requeue).map((o) => o.key));

  // A grave outcome is confirmed, and the rule lives with the vocabulary rather
  // than in each view's handler -- so a future irreversible outcome inherits the
  // confirmation instead of needing someone to remember it in two places.
  //
  // The prompt NAMES the person. "Are you sure?" is answered yes reflexively;
  // a name has to be read, and reading it is the entire point of asking.
  function confirmGrave(disposition, item) {
    const o = OUTCOMES.find((x) => x.key === disposition);
    if (!o || !o.grave) return true;

    /* An UNCONFIRMED contact match cannot be used to silence somebody.
     *
     * Do-not-call is firm-wide, permanent, and has no undo in the application.
     * On a review-tier match the person on the phone is quite possibly not the
     * person on the card -- so honouring "stop calling me" would block an
     * advisor who was never contacted, and the record explaining why would name
     * the wrong human being. That is the one outcome here that cannot be
     * corrected by noticing later.
     */
    if (item && item.unconfirmed) {
      global.alert(`This contact was matched to ${(item && item.name) || "this advisor"} `
        + `on a name similarity rather than a confirmed identifier, so the person you reached `
        + `may not be them.

A do-not-call is firm-wide and permanent, and cannot be added `
        + `from an unconfirmed record. Confirm who this is first, or ask an administrator to `
        + `add them directly.`);
      return false;
    }
    return confirm(
      `Add ${(item && item.name) || "this advisor"} to the firm-wide `
      + `do-not-call list?\n\nEvery rep is blocked from calling them, and there `
      + `is no undo in the app.`);
  }

  /* ---------- identity and log out --------------------------------------
   * Static Web Apps resolves /.auth/me at the edge from the session cookie, so
   * it is the signed-in user rather than anything this page asserts.
   *
   * Log out goes through Entra's end-session endpoint, NOT just /.auth/logout.
   * Clearing this site's cookie alone is useless in practice: the browser still
   * holds a live Entra SSO session, so the redirect back into the app signs the
   * user straight in again with no prompt. It reads as a broken button. The
   * site cookie is still cleared first, so an interrupted redirect cannot leave
   * a live local session behind.
   */
  const TENANT = "c1efc78d-a7d7-4998-98b6-08e90af5661f";
  let me = null;

  async function whoAmI() {
    if (me) return me;
    try {
      const r = await fetch("/.auth/me");
      const { clientPrincipal } = await r.json();
      me = clientPrincipal || null;
    } catch { me = null; }   // local dev, no auth in front
    return me;
  }

  async function signOut() {
    // redirect:"manual" is load-bearing, not tidiness. /.auth/logout answers 302
    // to "/", which is an authenticated route, so a following fetch walks 401 ->
    // our own responseOverrides redirect -> /.auth/login/aad -> Entra satisfies
    // it silently from the SSO cookie that is still alive at that instant, and
    // hands back a FRESH session cookie. The logout call re-authenticates. Not
    // following the redirect takes the Set-Cookie that clears the session and
    // stops there.
    try {
      await fetch("/.auth/logout", { credentials: "include", redirect: "manual",
                                     cache: "no-store" });
    } catch { /* going to Entra regardless -- a dead site cookie is not worth blocking on */ }
    me = null;
    // Order matters: site cookie first, Entra SSO second. Reversed, the app is
    // still reachable on the surviving site cookie without touching Entra at all.
    global.location.href = `https://login.microsoftonline.com/${TENANT}/oauth2/v2.0/logout`;
  }

  global.Dial = {
    init, state, OUTCOMES, outcomeLabel, actNotice, PURPOSES, purposeLabel,
    onChange: (fn) => { listeners.push(fn); return () => {
      const i = listeners.indexOf(fn); if (i !== -1) listeners.splice(i, 1); }; },
    add, addMany, remove, clear, move, inQueue, isDnc, telHref,
    start, pause, end, current, remaining, advance, requeue, back, canBack,
    // What was recorded for this person on this pass, if anything. Drives the
    // "you logged X" line, so a correction is made knowingly.
    lastOutcome: (crd) => state.done.get(String(crd)) || null,
    log, history, fullHistory, describeHistory,
    refreshQueue, refreshDnc, dropSuppressed,
    isKeyContact, isDueDiligence, flagsOf, setFlag, fetchFlags,
    flagMembersOf, flaggedByMe, flaggedByOthers,
    loadSettings, saveSettings, setting,
    loadLists, openList, createList, renameList, deleteList, startCycle,
    refreshProgress, preferredListId,
    // How far through this pass the rep is. Derived, never stored.
    progress: () => ({ done: state.done.size, total: state.items.length,
                       left: Math.max(0, state.items.length - state.done.size) }),
    isDone: (crd) => state.done.has(String(crd)),
    setAuto, armAuto, cancelAuto, say, REQUEUE, confirmGrave,
    whoAmI, signOut,
    AUTO_MIN, AUTO_MAX, MAX_QUEUE,
  };
})(window);
