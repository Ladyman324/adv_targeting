(function (global) {
  "use strict";

  let catalog = null;
  let detail = null;
  let recipients = [];
  // Addresses the rep has excluded on the setup screen. A Set of lowercase
  // addresses rather than an index, so it survives the list being re-sorted or
  // re-grouped underneath it.
  let excluded = new Set();
  let openDomain = null;   // which domain group is drilled into, if any
  let cursor = 0;
  let pollTimer = null;
  let tickTimer = null;
  // Set when the rep explicitly asks to see a finished batch's messages, so the
  // completion panel does not immediately replace what they opened.
  let forceDetail = false;
  // Edits to a single message used to be lost by moving to the next recipient:
  // the override only existed once "Save override" was pressed, and nothing on
  // screen said so. Now the edit is the thing that matters and saving is
  // automatic -- on blur, and before any navigation that would discard it.
  let dirty = false;
  let saving = false;
  // Approval is the only browser-bound portion of a send. Once its 202 response
  // arrives, Azure owns the work and this window may close.
  let approvalPending = false;
  let sendTiming = "now";
  let scheduleDate = "";
  let scheduleTime = "09:00";
  global.addEventListener("beforeunload", (event) => {
    if (!approvalPending) return;
    event.preventDefault();
    event.returnValue = "";
  });

  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
  const bytes = (n) => n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.ceil(n / 1024)} KB`;

  const EASTERN = "America/New_York";
  function easternParts(date) {
    const parts = new Intl.DateTimeFormat("en-CA", { timeZone: EASTERN, year: "numeric",
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
      hourCycle: "h23" }).formatToParts(date);
    return Object.fromEntries(parts.filter((p) => p.type !== "literal").map((p) => [p.type, p.value]));
  }
  function easternLocalToDate(day, time) {
    const match = `${day}T${time}`.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/);
    if (!match) return null;
    const wanted = match.slice(1).map(Number), anchor = Date.UTC(wanted[0], wanted[1] - 1,
      wanted[2], wanted[3], wanted[4]);
    const matches = [];
    for (let t = anchor - 12 * 3600000; t <= anchor + 12 * 3600000; t += 15 * 60000) {
      const p = easternParts(new Date(t));
      if (+p.year === wanted[0] && +p.month === wanted[1] && +p.day === wanted[2]
          && +p.hour === wanted[3] && +p.minute === wanted[4]) matches.push(new Date(t));
    }
    return matches.length === 1 ? matches[0] : null;
  }
  function easternLabel(date) {
    return new Intl.DateTimeFormat("en-US", { timeZone: EASTERN, weekday: "long", year: "numeric",
      month: "long", day: "numeric", hour: "numeric", minute: "2-digit",
      timeZoneName: "short" }).format(date);
  }
  function ensureScheduleDefaults() {
    if (scheduleDate) return;
    const soon = new Date(Date.now() + 3600000);
    const p = easternParts(soon);
    scheduleDate = `${p.year}-${p.month}-${p.day}`;
    scheduleTime = `${p.hour}:${String(Math.ceil(+p.minute / 15) * 15 % 60).padStart(2, "0")}`;
    if (+p.minute > 45) {
      const later = easternParts(new Date(soon.getTime() + 3600000));
      scheduleDate = `${later.year}-${later.month}-${later.day}`; scheduleTime = `${later.hour}:00`;
    }
  }
  function scheduleCheck(batch) {
    if (sendTiming !== "later") return { date: null, error: "", quarter: "" };
    const date = easternLocalToDate(scheduleDate, scheduleTime);
    if (!date) return { date: null, error: "Choose a valid, unambiguous Eastern time.", quarter: "" };
    const earliest = Date.now() + Number(((catalog || {}).limits || {}).cancellationSeconds || 0) * 1000;
    if (date.getTime() < earliest) return { date, error: "Choose a time after the cancellation window.", quarter: "" };
    if (date.getTime() > Date.now() + 7 * 86400000)
      return { date, error: "Scheduled sending can begin no more than 7 days from now.", quarter: "" };
    const now = easternParts(new Date()), then = easternParts(date);
    const nowQuarter = `${now.year}-Q${Math.floor((+now.month - 1) / 3) + 1}`;
    const thenQuarter = `${then.year}-Q${Math.floor((+then.month - 1) / 3) + 1}`;
    if (nowQuarter !== thenQuarter) {
      const ids = new Set((batch.attachmentIds || []).map(String));
      const quarterly = ((catalog && catalog.documents) || []).filter((d) =>
        ids.has(String(d.id)) && d.periodKind === "quarter");
      if (quarterly.length) return { date, error: "", quarter:
        `This schedule crosses a calendar-quarter boundary and includes quarterly material (${quarterly.map((d) => d.name).join(", ")}). After the quarter ends, select the refreshed material and schedule the batch then.` };
    }
    return { date, error: "", quarter: "" };
  }

  async function api(op, body, method = "POST") {
    const [operation, extraQuery = ""] = String(op).split("&", 2);
    const response = await fetch(`/api/email?op=${encodeURIComponent(operation)}${extraQuery ? `&${extraQuery}` : ""}`, {
      method, headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify({ ...body, op }) : undefined,
    });
    let data = {};
    try { data = await response.json(); } catch {}
    if (!response.ok) throw new Error(data.error || `Email service returned ${response.status}.`);
    return data;
  }

  /* One-to-one sends are asynchronous now: Graph accepting /send is not proof
   * that the immutable sent item can already be read. Keep only enough local
   * metadata to resume confirmation after a reload. Draft text, recipients and
   * attachment names never enter browser storage. */
  const DIRECT_OPS_KEY = "advisorMap.directSendOps.v1";
  const directPollers = new Map();

  function directRows() {
    try {
      const rows = JSON.parse(localStorage.getItem(DIRECT_OPS_KEY) || "[]");
      const cutoff = Date.now() - 90 * 86400000;
      return Array.isArray(rows) ? rows.filter((row) => row && row.operationId
        && (!row.createdUtc || Date.parse(row.createdUtc) >= cutoff)) : [];
    } catch { return []; }
  }

  function saveDirectRows(rows) {
    try { localStorage.setItem(DIRECT_OPS_KEY, JSON.stringify(rows.slice(-50))); }
    catch { /* status recovery is useful, not permission to break sending */ }
  }

  function rememberDirect(meta) {
    const safe = { operationId: String(meta.operationId), kind: String(meta.kind || ""),
      crd: String(meta.crd || ""), sourceId: String(meta.sourceId || ""),
      createdUtc: String(meta.createdUtc || new Date().toISOString()) };
    const rows = directRows().filter((row) => row.operationId !== safe.operationId);
    rows.push(safe); saveDirectRows(rows);
    return safe;
  }

  function forgetDirect(operationId) {
    saveDirectRows(directRows().filter((row) => row.operationId !== operationId));
  }

  function emitDirect(meta, state) {
    global.dispatchEvent(new CustomEvent("directsendstatus", { detail: { ...meta, state } }));
  }

  async function directStatus(operationId) {
    const response = await fetch(`/api/email?op=direct_send_status&operationId=${encodeURIComponent(operationId)}`,
      { headers: { Accept: "application/json" } });
    let data = {};
    try { data = await response.json(); } catch {}
    if (!response.ok) {
      const error = new Error(data.error || `Email service returned ${response.status}.`);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function watchDirect(meta, onUpdate) {
    const id = String(meta.operationId);
    let poller = directPollers.get(id);
    if (poller) { if (onUpdate) poller.listeners.add(onUpdate); return poller.promise; }
    poller = { listeners: new Set(onUpdate ? [onUpdate] : []) };
    const notify = (state) => {
      for (const listener of poller.listeners) {
        try { listener(state); } catch { /* a closed dialog is not a send failure */ }
      }
      emitDirect(meta, state);
    };
    poller.promise = (async () => {
      let wait = 3000;
      for (let attempt = 0; attempt < 30; attempt++) {
        if (attempt) await new Promise((resolve) => setTimeout(resolve, wait));
        try {
          const state = await directStatus(id);
          notify(state);
          if (!state.pending) {
            // An uncertain operation remains a tombstone on this device too:
            // reopening the composer must warn rather than offer a fresh send.
            if (state.status !== "needs_verification") forgetDirect(id);
            return state;
          }
          wait = Math.min(60000, Math.round(wait * 1.6));
        } catch (error) {
          if (error && error.status === 404) {
            // Most often a different signed-in user on a shared browser profile
            // or a terminal tombstone past retention. It cannot be polled from
            // this identity and must not block their unrelated composer.
            forgetDirect(id);
            return null;
          }
          // Keep the durable local pointer. A reload or the next app session
          // resumes; network failure is never translated into permission to
          // submit a second message.
          wait = Math.min(60000, Math.round(wait * 1.8));
        }
      }
      return null;
    })().finally(() => directPollers.delete(id));
    directPollers.set(id, poller);
    return poller.promise;
  }

  function acceptDirect(state, meta, onUpdate) {
    if (!state || !/^[0-9a-f-]{36}$/i.test(String(state.operationId || ""))) {
      // Static/API version skew: the legacy endpoint may already have sent but
      // cannot provide durable status. Fail closed in the UI; never translate
      // an untrackable response into a green "Sent" or a retry button.
      const unknown = { status: "needs_verification", pending: false,
        message: "The API did not return a durable operation status. Do not resend; verify in Outlook." };
      if (onUpdate) onUpdate(unknown);
      return Promise.resolve(unknown);
    }
    const saved = rememberDirect({ ...meta, operationId: state.operationId });
    if (onUpdate) onUpdate(state);
    emitDirect(saved, state);
    if (!state.pending) {
      if (state.status !== "needs_verification") forgetDirect(saved.operationId);
      return Promise.resolve(state);
    }
    return watchDirect(saved, onUpdate);
  }

  function pendingDirect(kind, crd, sourceId = "") {
    return directRows().find((row) => row.kind === String(kind)
      && row.crd === String(crd) && row.sourceId === String(sourceId)) || null;
  }

  function resumeDirect() {
    for (const row of directRows()) watchDirect(row);
  }

  function shell() {
    let back = document.getElementById("emailComposerBack");
    if (!back) {
      back = document.createElement("div");
      back.id = "emailComposerBack";
      back.className = "email-back";
      back.hidden = true;
      back.innerHTML = `<section class="email-compose" role="dialog" aria-modal="true" aria-labelledby="emailTitle">
        <header class="email-head"><div><p class="eyebrow">Microsoft 365</p><h2 id="emailTitle">Email</h2></div>
          <button type="button" class="rclose" data-email="close" aria-label="Close">&times;</button></header>
        <div id="emailBody" class="email-body"></div></section>`;
      document.body.appendChild(back);
    }
    return back;
  }

  function notice(message, bad = false) {
    const el = document.getElementById("emailNotice");
    if (el) { el.textContent = message || ""; el.classList.toggle("bad", bad); }
  }

  async function loadCatalog() {
    catalog = await api("catalog", null, "GET");
    if (global.Dial && catalog && catalog.recipientEligibility)
      global.Dial.setEmailPolicy(catalog.recipientEligibility);
    return catalog;
  }

  function connectView(error = "") {
    document.getElementById("emailBody").innerHTML = `<div class="email-connect">
      <h3>Connect your Microsoft 365 mailbox</h3>
      <p>The app will create traceable drafts in your own mailbox. It cannot create or send from another employee’s mailbox.</p>
      ${error ? `<p class="email-error">${esc(error)}</p>` : ""}
      <button type="button" class="ask-btn primary" data-email="connect">Connect Microsoft 365</button>
      <p class="email-fine">Delegated permissions: User.Read, Mail.ReadWrite, and Mail.Send. Tokens remain server-side and encrypted.</p></div>`;
  }


  /* ---------- recipients, grouped by email domain -------------------------
   * The failure this exists to prevent: a batch written for Morgan Stanley
   * advisors that quietly also goes to the eight Raymond James contacts who
   * happened to be on the same list. A flat list of sixty names does not
   * surface that -- the wrong eight look exactly like the right fifty-two.
   * Grouped by domain, a stray firm is a whole row that does not belong, which
   * is a thing a person notices in one glance rather than by reading.
   *
   * Exclusion is client-side and pre-batch: what the rep excludes here is never
   * sent to the server, so an excluded person never becomes a message that
   * could later be approved by accident.
   */
  const domainOf = (r) => String(r.email || "").toLowerCase().split("@")[1] || "(no email)";
  const keptRecipients = () => recipients.filter((r) => !excluded.has(String(r.email || "").toLowerCase()));

  function domainGroups() {
    const map = new Map();
    for (const r of recipients) {
      const d = domainOf(r);
      if (!map.has(d)) map.set(d, []);
      map.get(d).push(r);
    }
    // Biggest first: the dominant domain is almost always the intended audience,
    // so the odd ones out fall to the bottom where they read as exceptions.
    return [...map.entries()].map(([domain, people]) => ({ domain, people,
      kept: people.filter((r) => !excluded.has(String(r.email || "").toLowerCase())).length }))
      .sort((a, b) => b.people.length - a.people.length || a.domain.localeCompare(b.domain));
  }

  /* The element that actually scrolls above `el`.
   *
   * Not assumed to be any particular container: the composer has been reshaped
   * more than once, and a hard-coded id would silently stop working the next
   * time rather than obviously breaking.
   */
  function scrollParent(el) {
    for (let n = el && el.parentElement; n; n = n.parentElement) {
      const style = getComputedStyle(n);
      if (/(auto|scroll)/.test(style.overflowY) && n.scrollHeight > n.clientHeight) return n;
    }
    return document.scrollingElement;
  }

  function domainPicker() {
    const groups = domainGroups();
    const kept = keptRecipients().length, off = recipients.length - kept;
    return `<fieldset class="email-domains"><legend>Recipients by email domain</legend>
      <p class="email-fine">Check a domain to include everyone on it. Open one to
        exclude individuals. Only checked recipients are emailed.</p>
      ${groups.length > 1 ? `<p class="email-domain-acts">
        <button type="button" class="email-small" data-email="dom-all">Include all</button>
        <button type="button" class="email-small" data-email="dom-none">Exclude all</button>
        <span class="email-domain-warn">${groups.length} different domains in this list.</span>
      </p>` : ""}
      <ul class="email-domain-list">${groups.map((g) => {
        const state = g.kept === g.people.length ? "all" : g.kept === 0 ? "none" : "some";
        return `<li class="dom-${state}">
          <div class="email-domain-row">
            <label><input type="checkbox" data-email="dom-toggle" data-domain="${esc(g.domain)}"
              ${state === "all" ? "checked" : ""}${state === "some" ? ' data-mixed="1"' : ""}>
              <b>${esc(g.domain)}</b></label>
            <span class="email-domain-count ${state === "none" ? "off" : ""}">${g.kept} of ${g.people.length}</span>
            <button type="button" class="email-small" data-email="dom-open"
              data-domain="${esc(g.domain)}">${openDomain === g.domain ? "Hide" : "People"}</button>
          </div>
          ${openDomain === g.domain ? `<ol class="email-domain-people">${g.people.map((r) => {
            const addr = String(r.email || "").toLowerCase();
            return `<li><label><input type="checkbox" data-email="person-toggle"
              data-address="${esc(addr)}" ${excluded.has(addr) ? "" : "checked"}>
              <b>${esc(r.name || "Unnamed contact")}</b>
              <span>${esc(r.email || "No email on file")}</span></label></li>`;
          }).join("")}</ol>` : ""}</li>`;
      }).join("")}</ul>
      ${off ? `<p class="email-domain-off">${off} recipient${off === 1 ? "" : "s"} excluded.</p>` : ""}
    </fieldset>`;
  }

  // Mixed groups get the indeterminate dash rather than an unticked box, which
  // would read as "nobody on this domain" when in fact most are included.
  function paintMixed() {
    for (const box of document.querySelectorAll('[data-email="dom-toggle"][data-mixed]'))
      box.indeterminate = true;
  }


  /* Say it before the work, not after it.
   *
   * A rep who selects forty-eight people and presses on will build the batch,
   * review forty-eight messages, press Approve & Send, confirm a dialog, and
   * only then be told the send was never possible. The count is known right
   * here, on the screen where the list can still cheaply be made smaller.
   */
  function setupSendWarning() {
    const lim = (catalog && catalog.limits) || {};
    const kept = keptRecipients();
    const n = kept.length;
    if (!n) return "";
    const bits = [];
    if (lim.directBatchMax && n > lim.directBatchMax) {
      bits.push(`${n} recipients is over the ${lim.directBatchMax}-recipient limit for a single `
        + `send. You can still create Outlook drafts for all of them, or reduce the list to `
        + `${lim.directBatchMax} to send from the app.`);
    } else if (lim.rollingRemaining != null && lim.rollingExternalLimit) {
      const ext = kept.filter((r) => !INTERNAL.test(String(r.email || ""))).length;
      if (ext > lim.rollingRemaining) {
        bits.push(`${ext} external recipients, and you have ${lim.rollingRemaining} of `
          + `${lim.rollingExternalLimit} left in the last 24 hours. Drafts are unaffected.`);
      }
    }
    return bits.length
      ? `<p class="email-error email-setup-warn">&#9888; ${esc(bits[0])}</p>` : "";
  }

  // Mirrors EMAIL_INTERNAL_DOMAINS on the server. Only used to estimate what
  // counts against the rolling window before the batch exists; the server does
  // the real arithmetic.
  const INTERNAL = /@eicatlanta\.com$/i;

  /* Who else goes on THIS batch.
   *
   * Separate from the two standing choices in Settings, which apply to
   * everything a rep sends. This is the one-off: an introduction that a
   * teammate should see, or a colleague who asked to be kept in the loop on a
   * particular firm.
   *
   * CC, never To. Everything downstream -- logging, suppression, the
   * unsubscribe footer, {{first_name}} -- is built around one recipient per
   * message, and multiplying that is a much larger change than it looks. A CC
   * carries none of those assumptions.
   *
   * Teammates are per RECIPIENT: each advisor has their own practice, so the
   * server resolves them message by message. The colleague is per BATCH.
   */
  /* WHY an address is on this message.
   *
   * The envelope used to show Bcc with one hard-coded explanation and not show
   * Cc at all -- so a rep who had asked for a colleague to be copied, or for
   * their own address to be blind-copied, saw no evidence of either and had to
   * take it on trust. Every copied address now says where it came from.
   */
  function ccReason(address, m, b) {
    const a = String(address).toLowerCase();
    if ((m.teammateCc || []).includes(a)) return " — on the advisor's team";
    if (String(b.ccColleague || "").toLowerCase() === a) return " — the colleague you chose for this batch";
    if (String(b.copyInternalTo || "").toLowerCase() === a && b.copyInternal === "cc")
      return " — your standing setting: copy a colleague";
    if (String(b.senderMail || "").toLowerCase() === a && b.copySelf === "cc")
      return " — your standing setting: copy me";
    return "";
  }

  function bccReason(address, b) {
    const a = String(address).toLowerCase();
    if (String(b.senderMail || "").toLowerCase() === a && b.copySelf === "bcc")
      return " — your standing setting: copy me";
    if (String(b.copyInternalTo || "").toLowerCase() === a && b.copyInternal === "bcc")
      return " — your standing setting: copy a colleague";
    return " — added automatically because this email carries an attachment";
  }

  /* Copy specific teammates on THIS message.
   *
   * The batch-level switch is all-or-nothing across every recipient. This is
   * the per-message version, offered where the rep is actually reading the
   * email rather than before any of them exist.
   *
   * The warning is stated on each teammate who is also a recipient of this
   * batch, because ticking them DELETES their own email -- the rep's explicit
   * choice, but not one to discover afterwards.
   */
  /* Which messages have had the teammate list opened.
   *
   * Every toggle re-renders the whole step from the server's answer, so without
   * this the list collapsed the moment a rep ticked somebody -- and unticking
   * the LAST one closed it under them while they were still working in it.
   *
   * Keyed by message id rather than a single flag: moving to the next advisor
   * is a fresh decision, and their list should not be open because the previous
   * one was. Same reasoning as outcomeOpen in field.js.
   */
  const matesOpen = new Set();

  function teammatePicker(m, b, locked) {
    const mates = (m.teammatesAvailable || []).filter((t) => t.email);
    if (!mates.length) return "";
    const on = new Set((m.teammateCc || []).map((a) => String(a).toLowerCase()));
    const alsoWritten = new Set((detail.messages || [])
      .filter((x) => x.id !== m.id)
      .map((x) => String(x.recipientEmail || "").toLowerCase()));

    /* COLLAPSED, because copying a teammate is the exception.
     *
     * This was a permanently expanded list, and on a twelve-person practice it
     * pushed the subject and body -- the things a rep is actually here to edit
     * -- off the screen entirely. Most messages copy nobody, so the default
     * state should cost nothing.
     *
     * Open when somebody is already copied, so a decision already taken is
     * never hidden behind a disclosure triangle.
     */
    const chosen = mates.filter((t) => on.has(String(t.email).toLowerCase()));
    // Open if the rep opened it, or if somebody is already copied -- a decision
    // already taken is never hidden behind a disclosure triangle.
    const open = matesOpen.has(m.id) || chosen.length > 0;
    return `<details class="email-mates"${open ? " open" : ""}>
      <summary data-email="mates-open" data-id="${esc(m.id)}">
        <span class="email-mates-title">Copy someone on their team</span>
        <span class="email-mates-count">${chosen.length
          ? `${chosen.length} copied`
          : `${mates.length} on this team`}</span>
      </summary>
      <div class="email-mates-list">
      ${mates.map((t) => {
        const a = String(t.email).toLowerCase();
        const clash = alsoWritten.has(a);
        return `<label class="email-mate${clash ? " clash" : ""}"${
          clash ? ' title="Also in this batch — copying them here removes their own email"' : ""}>
          <input type="checkbox" data-email="mate-toggle" data-address="${esc(a)}"
            ${on.has(a) ? "checked" : ""}${locked ? " disabled" : ""}>
          <span class="email-mate-name">${esc(t.name || a)}</span>
          <span class="email-mate-addr">${esc(a)}</span>
          ${clash ? `<span class="email-mate-flag">replaces their own email</span>` : ""}
        </label>`;
      }).join("")}
      </div>
      <p class="email-fine">A copy is visible to the advisor. Anyone who has
        unsubscribed is skipped.</p></details>`;
  }

  function copyPicker() {
    const colleagues = (catalog && catalog.internalRecipients) || [];
    /* NO TEAMMATE CONTROL HERE ANY MORE.
     *
     * There used to be a "the advisor's own teammates" checkbox on this step,
     * and it was wrong in three ways at once: it sat at the BATCH level, so it
     * applied to every recipient; it was all-or-nothing, so a rep could not
     * pick which teammate; and it ran before anyone had seen the individual
     * emails it would change.
     *
     * Copying a teammate is a per-message decision -- see teammatePicker(),
     * which renders in Step 2 beside the message it affects, names each
     * teammate, and says which of them would lose their own email as a result.
     * Two controls doing the same job differently is worse than either, so this
     * one is gone rather than left as a shortcut.
     */
    return `<fieldset class="email-copy"><legend>Copy someone on this batch</legend>
      ${colleagues.length ? `<label class="email-label">An EIC colleague
        <select id="ccColleague"><option value="">Nobody</option>${colleagues.map((r) =>
          `<option value="${esc(r.address)}">${esc(r.name)}</option>`).join("")}</select></label>`
        : `<p class="email-fine">No internal recipients are configured, so there is
           nobody to copy. An administrator sets <code>EMAIL_INTERNAL_RECIPIENTS</code>
           on the Function App.</p>`}
    </fieldset>`;
  }

  /* "If nobody replies, remind me when."
   *
   * Asked HERE, at the setup step, because this is the moment a rep knows what
   * the email is for -- an introduction wants a week, a document somebody asked
   * for wants three days, and a newsletter wants nothing. Asking later means
   * asking someone who has moved on.
   *
   * PRESETS, NOT A NUMBER FIELD. This is a decision made once per batch and it
   * only has to be roughly right; a free-text box is a small tax on every send
   * and invites 1-day reminders that arrive before anyone has read the mail.
   *
   * DEFAULT OFF. A batch of 400 that all come due on one morning is a queue
   * nobody reads, which is the failure the work queue is built to avoid.
   */
  const FOLLOW_UP_PRESETS = [
    { days: 0, label: "No reminder" },
    { days: 3, label: "3 days" },
    { days: 7, label: "1 week" },
    { days: 14, label: "2 weeks" },
  ];

  function followUpPicker(){
    return `<fieldset class="email-copy"><legend>If nobody replies</legend>
      <label class="email-label">Remind me to follow up
        <select id="followUpDays">${FOLLOW_UP_PRESETS.map((p) =>
          `<option value="${p.days}"${p.days === 0 ? " selected" : ""}>${esc(p.label)}</option>`).join("")}</select></label>
      <p class="email-fine">Anyone who replies drops off the reminder on their own.
        An out-of-office is not a reply.</p>
    </fieldset>`;
  }


  function setupView(){
    const templates = catalog.templates || [], docs = catalog.documents || [];
    const legacyDocs = docs.filter((d) => !d.familyId && materialStatus(d) === "current");

    const familyMap = new Map();
    docs.filter((d) => d.familyId && materialStatus(d) === "current").forEach((d) => {
      const family = familyMap.get(d.familyId) || { id: d.familyId, name: d.name, category: d.category, channels: new Set() };
      family.channels.add(d.channel || "generic"); familyMap.set(d.familyId, family);
    });
    const families = [...familyMap.values()];
    const routeRules = (((catalog || {}).materialRoutes || {}).rules || []);

    const domains = [...new Set(keptRecipients().map((r) => String(r.email || "").split("@")[1]).filter(Boolean))];
    const preflight = families.length ? '<div class="email-material-preflight"><b>Recipient routing</b><span>'
      + domains.map((domain) => esc(domain) + ' ->  ' + esc(channelLabel(routedChannel(domain, routeRules)))).join(' / ')
      + '</span><small>The server verifies the exact approved version for every recipient when emails are generated.</small></div>' : '';
    const familyPicker = families.length ? '<fieldset class="email-docs email-families"><legend>Material families</legend>'
      + families.map((f) => '<label><input type="checkbox" class="email-family" value="' + esc(f.id) + '"><span><b>'
        + esc(f.name) + '</b><small>' + esc(f.category || 'Material') + ' / '
        + [...f.channels].map(channelLabel).map(esc).join(', ') + '</small></span></label>').join('')
      + '<p>Choose the material once; the approved client-group version is selected per recipient.</p></fieldset>' + preflight : '';
    document.getElementById("emailBody").innerHTML = `<div class="email-setup">
      <p class="email-summary"><b id="emailKeptCount">${keptRecipients().length}</b> recipient${keptRecipients().length === 1 ? "" : "s"} · From <b>${esc(catalog.connection.mailbox)}</b></p>
      ${templates.length ? `<label class="email-label">Template<select id="emailTemplate">${templates.map((t) =>
        `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("")}</select></label>`
        : `<p class="email-error">There are no approved templates yet.${catalog.isAdmin
            ? ` Use <b>Manage templates</b> below to publish one.`
            : ` An email administrator needs to publish one before you can send.`}</p>`}
      <p id="emailTplNotes" class="email-tpl-notes"></p>
      ${familyPicker}
      <fieldset class="email-docs" id="emailDocs"><legend>Approved attachments</legend>${legacyDocs.length ? legacyDocs.map((d) =>
        `<label data-doc="${esc(d.id)}"><input type="checkbox" value="${esc(d.id)}"> <span>${esc(d.name)}</span><small>${bytes(d.size)}</small></label>`).join("")
        : `<p>No additional legacy attachments are available. You can continue without them.</p>`}</fieldset>
      ${catalog.isAdmin ? `<p class="email-admin-link">
        <button type="button" class="email-small" data-email="templates">Manage templates</button>
        <button type="button" class="email-small" data-email="docs">Manage approved documents</button></p>` : ""}
      ${copyPicker()}
      ${followUpPicker()}
      ${domainPicker()}
      ${setupSendWarning()}
      <p id="emailNotice" class="email-notice"></p>
      <button type="button" class="ask-btn primary" data-email="create"${
        templates.length ? "" : " disabled"}>Generate personalized emails</button></div>`;
    // The Word templates carried instructions to the rep inside the body --
    // "INSERT LCV PERFORMANCE PAGE HERE AND ATTACH...". Those belong on screen,
    // not in the message, so they live on the template and are shown here.
    // Attachments the chosen template REQUIRES are ticked and locked.
    //
    // They were drawn unticked, which was a straightforward lie: createBatch
    // merges the template's requiredDocumentIds in regardless, so the document
    // went out while the screen said it would not. A rep checking their work saw
    // an empty box next to the one attachment compliance mandates.
    const showRequired = () => {
      const picker = document.getElementById("emailTemplate");
      const chosen = picker && templates.find((x) => x.id === picker.value);
      const required = new Set((chosen && chosen.requiredDocumentIds) || []);
      for (const label of document.querySelectorAll("#emailDocs label[data-doc]")) {
        const box = label.querySelector("input");
        const isRequired = required.has(label.dataset.doc);
        label.classList.toggle("required", isRequired);
        if (isRequired) { box.checked = true; box.disabled = true; }
        else if (box.disabled) { box.disabled = false; box.checked = false; }
        let tag = label.querySelector(".email-req-tag");
        if (isRequired && !tag) {
          tag = document.createElement("em");
          tag.className = "email-req-tag";
          tag.textContent = "required by this template";
          label.appendChild(tag);
        } else if (!isRequired && tag) tag.remove();
      }
    };

    const showNotes = () => {
      const picker = document.getElementById("emailTemplate");
      const chosen = picker && templates.find((x) => x.id === picker.value);
      const box = document.getElementById("emailTplNotes");
      box.textContent = (chosen && chosen.repNotes) || "";
      box.hidden = !box.textContent;
    };
    const picker = document.getElementById("emailTemplate");
    if (picker) picker.addEventListener("change", () => { showNotes(); showRequired(); });
    showNotes();
    showRequired();
    paintMixed();
  }


  /* ---- sender health (EmailAdministrator only) ----------------------------
   * Deliberately not a spam-complaint dashboard. Of 114,309 mailable advisor
   * addresses, 20 are consumer mailboxes -- everyone else sits behind a
   * corporate gateway, and corporate tenants do not run feedback loops. A
   * complaint rate here would read zero forever and mean nothing.
   *
   * These four are what a corporate gateway actually tells us, and each row
   * carries what to DO about the number, because four percentages with no
   * interpretation just moves the problem along.
   */
  let healthDays = 90;

  function healthView(data, message = "", bad = false) {
    document.getElementById("emailTitle").textContent = "Sender health";
    const reps = (data && data.reps) || [];
    const chip = (lvl, label, value, count, total) =>
      `<div class="hz ${esc(lvl)}"><span class="hz-n">${value}</span>
        <span class="hz-l">${esc(label)}</span>
        <small>${count} of ${total}</small></div>`;

    document.getElementById("emailBody").innerHTML = `<div class="email-health">
      <p class="email-next">What corporate mail gateways tell us about how our sending is
        landing. There is no spam-complaint figure here because the firms we email do not
        report one &mdash; these four signals are what they do send back.</p>
      ${message ? `<p class="${bad ? "email-error" : "email-ok"}">${esc(message)}</p>` : ""}
      <p class="email-health-range">Last
        ${[30, 90, 180].map((d) => `<button type="button" class="email-small${
          d === healthDays ? " on" : ""}" data-email="health-days" data-days="${d}">${d} days</button>`).join(" ")}
        ${data ? `<span class="email-fine">${data.totals.sends.toLocaleString()} messages,
          ${data.totals.events.toLocaleString()} delivery reports</span>` : ""}</p>

      ${reps.length ? reps.map((r) => `<section class="email-health-rep">
        <div class="email-section-head"><div>
          <p class="eyebrow">${r.sent.toLocaleString()} sent${r.lastSentUtc
            ? ` &middot; last ${esc(String(r.lastSentUtc).slice(0, 10))}` : ""}</p>
          <h3>${esc(r.userName)}</h3></div></div>
        <div class="hz-row">
          ${chip(r.levels.hard, "hard bounces", r.rates.hard.toFixed(1) + "%", r.hard, r.sent)}
          ${chip(r.levels.soft, "deferrals", r.rates.soft.toFixed(1) + "%", r.soft, r.sent)}
          ${chip(r.levels.policy, "policy refusals", r.rates.policy.toFixed(1) + "%", r.policy, r.sent)}
          ${chip(r.levels.unsubscribe, "unsubscribes", r.rates.unsubscribe.toFixed(1) + "%", r.unsubscribed, r.sent)}
        </div>
        <ul class="email-advice">${(r.advice || []).map((a) =>
          `<li class="adv-${esc(a.level)}">${esc(a.text)}</li>`).join("")}</ul>
        ${r.domains.length ? `<details class="email-jump"><summary>By recipient firm
          (${r.domains.length})</summary>
          <table class="email-health-table"><thead><tr><th>Domain</th><th>Sent</th>
            <th>Hard</th><th>Deferred</th><th>Policy</th><th>Unsub</th></tr></thead><tbody>
            ${r.domains.map((d) => `<tr>
              <td>${esc(d.domain)}</td><td>${d.sent}</td>
              <td class="${esc(d.levels.hard)}">${d.hard}</td>
              <td class="${esc(d.levels.soft)}">${d.soft}</td>
              <td class="${esc(d.levels.policy)}">${d.policy}</td>
              <td>${d.unsubscribed}</td></tr>`).join("")}
          </tbody></table></details>` : ""}
        ${(r.codes || []).length ? `<p class="email-fine">Most common codes:
          ${r.codes.map((c) => `<code>${esc(c.code)}</code> &times;${c.n}`).join(", ")}</p>` : ""}
      </section>`).join("")
        : `<p class="email-doc-none">No sending activity in this window yet. Delivery reports
           are collected by the bounce sweeper, which must be switched on
           (<code>EMAIL_BOUNCE_SWEEP_ENABLED=1</code>).</p>`}

      <div class="email-done-actions">
        <button type="button" class="ask-btn" data-email="docs-back">Back</button></div></div>`;
  }

  async function openHealth() {
    document.getElementById("emailTitle").textContent = "Sender health";
    document.getElementById("emailBody").innerHTML = `<p class="email-next">Loading…</p>`;
    try {
      healthView(await api(`sender_health&days=${healthDays}`, null, "GET"));
    } catch (e) { healthView(null, e.message, true); }
  }

  // ---- approved document management (EmailAdministrator only) --------------
  // Reps pick attachments from this catalog and cannot attach anything else, so
  // publishing and withdrawing documents is the compliance control that makes
  // the whole attachment story hold. It used to require a Cloud Shell session.
  /* Which document a publish is REPLACING, if any.
   *
   * putDocument() keys on the id, so publishing with an existing id keeps that
   * id, bumps the version, and leaves every template's requiredDocumentIds
   * pointing at the same row -- the replacement lands exactly where the old one
   * was. That has always worked; there was simply no button for it, only a note
   * telling administrators to retype the display name exactly.
   *
   * Retyping is the part that fails: a display name off by a character
   * publishes a SECOND document, the template still requires the first, and
   * nothing says so.
   */
  let replacing = null;      // { id, name } or null

  const MATERIAL_CHANNELS = [["generic", "Generic"], ["ubs", "UBS"], ["rj", "Raymond James"],
    ["mswm", "Morgan Stanley"], ["ml", "Merrill"]];
  const MATERIAL_CATEGORIES = ["Presentation", "Update & Positioning", "Case for Value vs Growth",
    "Performance", "Cash Allocation", "Periodic Table", "Standard Deviation", "Tax Policy", "Other"];
  let materialQueue = [], materialSearch = "", routeSearch = "";

function suggestMaterial(file) {
    const raw = String(file.name || "").replace(/\.pdf$/i, "").replace(/^p\s+/i, "")
      .replace(/\s*-\s*/g, " - ").replace(/\s+/g, " ").trim();
    let channel = "generic";
    if (/\bUBS\b/i.test(raw)) channel = "ubs";
    else if (/\b(RJ|Raymond James)\b/i.test(raw)) channel = "rj";
    else if (/\b(MSWM|Morgan Stanley)\b/i.test(raw)) channel = "mswm";
    else if (/\b(ML|Merrill)\b/i.test(raw)) channel = "ml";
    let category = "Other";
    if (/standard deviation/i.test(raw)) category = "Standard Deviation";
    else if (/periodic table/i.test(raw)) category = "Periodic Table";
    else if (/cash allocation/i.test(raw)) category = "Cash Allocation";
    else if (/performance page/i.test(raw)) category = "Performance";
    else if (/update\s*&\s*positioning/i.test(raw)) category = "Update & Positioning";
    else if (/case for value vs growth/i.test(raw)) category = "Case for Value vs Growth";
    else if (/presentation/i.test(raw)) category = "Presentation";
    else if (/tax selling policy/i.test(raw)) category = "Tax Policy";
    const q = raw.match(/\bQ([1-4])\s*(\d{2})\b/i);
    const months = ["january", "february", "march", "april", "may", "june",
      "july", "august", "september", "october", "november", "december"];
    const month = raw.match(/\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d{2})\b/i);
    let periodKind = q ? "quarter" : month ? "month" : "as_of";
    let periodKey = q ? "20" + q[2] + "-Q" + q[1]
      : month ? month[2] + "-" + String(months.indexOf(month[1].toLowerCase()) + 1).padStart(2, "0") : "";
    let asOfDate = "";
    const code = raw.match(/(?:^|\s)(\d{6})(?:\d{2})?(?:-|$)/);
    if (code) {
      const yy = +code[1].slice(0, 2), mm = +code[1].slice(2, 4), dd = +code[1].slice(4, 6);
      if (mm > 0 && mm < 13 && dd > 0 && dd < 32)
        asOfDate = (2000 + yy) + "-" + String(mm).padStart(2, "0") + "-" + String(dd).padStart(2, "0");
    }
    const displayName = raw.replace(/\s+-\s+\d{6,}(?:\s*-\s*\d+)*\s*$/i, "").trim();
    const familyName = displayName
      .replace(/\b(UBS|RJ|MSWM|ML|Raymond James|Morgan Stanley|Merrill)\b/ig, "")
      .replace(/\bQ[1-4]\s*\d{2}\b/ig, "")
      .replace(/\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+20\d{2}\b/ig, "")
      .replace(/\s+-\s*/g, " ").replace(/\s+/g, " ").trim();
    const familyId = (familyName || displayName).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
    const logicalId = (displayName + "-" + channel + "-" + (periodKey || asOfDate || "current"))
      .toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 80);
    return { file, name: displayName, familyId, logicalId, category, channel, periodKind, periodKey,
      asOfDate, status: "ready", error: "", duplicate: "" };
  }

  const channelLabel = (value) => (MATERIAL_CHANNELS.find(([key]) => key === String(value || "").toLowerCase())
    || [null, value || "Generic"])[1];
function latestEndedQuarter(now = new Date()) {
    let year = now.getFullYear(), quarter = Math.floor(now.getMonth() / 3) + 1;
    quarter -= 1;
    if (!quarter) { quarter = 4; year -= 1; }
    return year * 4 + quarter;
  }
  function materialStatus(doc) {
    const explicit = String(doc.freshness || "").toLowerCase();
    if (["withdrawn", "expired", "superseded"].includes(explicit)) return explicit;
    const q = String(doc.periodKey || "").match(/^(\d{4})-Q([1-4])$/i);
    if (q) {
      const value = Number(q[1]) * 4 + Number(q[2]), latest = latestEndedQuarter();
      return value === latest ? "current" : value < latest ? "stale" : "future";
    }
    return ["current", "stale", "future", "missing"].includes(explicit) ? explicit : "current";
  }

  function routeSource(rule) {
    if (Array.isArray(rule && rule.sources) && rule.sources.length) return rule.sources.join(", ");
    if (typeof (rule && rule.source) === "string" && rule.source) return rule.source;
    if (Array.isArray(rule && rule.provenance) && rule.provenance.length) return rule.provenance.join(", ");
    if (typeof (rule && rule.provenance) === "string" && rule.provenance) return rule.provenance;
    return rule && rule.seeded ? "Firm roster seed" : "Administrator";
  }

  function routedChannel(domain, rules) {
    const value = String(domain || "").toLowerCase();
    const matches = (rules || []).filter((r) => !r.disabled && r.domain
      && (value === String(r.domain).toLowerCase() || value.endsWith("." + String(r.domain).toLowerCase())));
    matches.sort((a, b) => String(b.domain).length - String(a.domain).length);
    return matches.length ? matches[0].channel : "generic";
  }


  function uploadLogicalId(row) {
    return (String(row.name || "") + "-" + String(row.channel || "generic") + "-"
      + String(row.periodKey || row.asOfDate || "current")).toLowerCase()
      .replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 80);
  }
  function uploadTuple(row) {
    return [row.familyId, row.channel || "generic", row.periodKind || "", row.periodKey || "", row.asOfDate || ""].join("|");
  }

  function queueRows() {
    return materialQueue.map((row, i) => '<div class="email-material-upload ' + esc(row.status) + '" data-upload-row="' + i + '">'
      + '<div class="email-material-file"><b>' + esc(row.file.name) + '</b><small>' + bytes(row.file.size)
      + (row.duplicate ? ' / ' + esc(row.duplicate) : '') + '</small></div>'
      + '<label>Display name<input data-upload-field="name" value="' + esc(row.name) + '"></label>'
      + '<label>Family<input data-upload-field="familyId" value="' + esc(row.familyId) + '"></label>'
      + '<label>Category<select data-upload-field="category">' + MATERIAL_CATEGORIES.map((v) =>
        '<option' + (v === row.category ? ' selected' : '') + '>' + esc(v) + '</option>').join('') + '</select></label>'
      + '<label>Version for<select data-upload-field="channel">' + MATERIAL_CHANNELS.map(([v, label]) =>
        '<option value="' + v + '"' + (v === row.channel ? ' selected' : '') + '>' + esc(label) + '</option>').join('') + '</select></label>'
      + '<label>Period<select data-upload-field="periodKind"><option value="quarter"' + (row.periodKind === 'quarter' ? ' selected' : '') + '>Quarter</option>'
      + '<option value="month"' + (row.periodKind === 'month' ? ' selected' : '') + '>Month</option><option value="as_of"' + (row.periodKind === 'as_of' ? ' selected' : '') + '>As of</option>'
      + '<option value="evergreen"' + (row.periodKind === 'evergreen' ? ' selected' : '') + '>Evergreen</option></select></label>'
      + '<label>Period key<input data-upload-field="periodKey" value="' + esc(row.periodKey) + '" placeholder="2026-Q2"></label>'
      + '<label>As of<input type="date" data-upload-field="asOfDate" value="' + esc(row.asOfDate) + '"></label>'
      + '<button type="button" class="email-small grave" data-email="material-queue-remove" data-index="' + i + '">Remove</button>'
      + '<p class="email-material-result">' + (row.status === 'uploading' ? 'Publishing...' : row.status === 'done' ? 'Published'
        : row.error ? esc(row.error) : row.duplicate ? 'Check this duplicate before publishing.' : 'Ready') + '</p></div>').join('');
  }

function materialEditFields(d) {
    const option = (value, label, selected) => '<option value="' + value + '"' + (value === selected ? ' selected' : '') + '>' + label + '</option>';
    return '<div class="email-material-edit">'
      + '<label>Display name<input data-material-field="name" value="' + esc(d.name) + '"></label>'
      + '<label>Family<input data-material-field="familyId" value="' + esc(d.familyId || d.id) + '"></label>'
      + '<label>Category<select data-material-field="category">' + MATERIAL_CATEGORIES.map((v) =>
        '<option' + (v === (d.category || 'Other') ? ' selected' : '') + '>' + esc(v) + '</option>').join('') + '</select></label>'
      + '<label>Version for<select data-material-field="channel">' + MATERIAL_CHANNELS.map(([v, label]) =>
        option(v, esc(label), d.channel || 'generic')).join('') + '</select></label>'
      + '<label>Period type<select data-material-field="periodKind">' + [['quarter','Quarter'],['month','Month'],['as_of','As of'],['evergreen','Evergreen']].map((v) =>
        option(v[0], v[1], d.periodKind || 'as_of')).join('') + '</select></label>'
      + '<label>Period key<input data-material-field="periodKey" value="' + esc(d.periodKey || '') + '"></label>'
      + '<label>As of<input type="date" data-material-field="asOfDate" value="' + esc(d.asOfDate || '') + '"></label>'
      + '<label>Freshness<select data-material-field="freshness">' + [['current','Current'],['stale','Stale'],['future','Future'],['superseded','Superseded'],['expired','Expired'],['withdrawn','Withdrawn']].map((v) =>
        option(v[0], v[1], materialStatus(d))).join('') + '</select></label>'
      + '<button type="button" class="email-small primary" data-email="material-save" data-id="' + esc(d.id) + '">Save metadata</button></div>';
  }

  function libraryHtml(docs) {
    const q = materialSearch.toLowerCase();
    const shown = docs.filter((d) => !q || [d.name, d.familyId, d.category, d.channel, d.periodKey, d.fileName].join(' ').toLowerCase().includes(q));
    const groups = new Map();
    shown.forEach((d) => {
      const key = d.familyId || d.id, group = groups.get(key) || { key, category: d.category || 'Uncategorized', docs: [] };
      group.docs.push(d); groups.set(key, group);
    });
    if (!groups.size) return '<p class="email-doc-none">No materials match this search.</p>';
    return [...groups.values()].sort((a, b) => a.category.localeCompare(b.category) || a.key.localeCompare(b.key)).map((g) => {
      const active = g.docs.filter((d) => materialStatus(d) === 'current');
      const periodOf = (d) => String(d.periodKey || (d.periodKind === 'evergreen' ? 'evergreen' : d.asOfDate || 'unspecified'));
      const targetPeriod = active.map(periodOf).sort((a, b) => b.localeCompare(a))[0] || '';
      const targetDocs = active.filter((d) => periodOf(d) === targetPeriod);
      const channels = new Set(targetDocs.map((d) => d.channel || 'generic'));
      const missing = MATERIAL_CHANNELS.filter(([key]) => !channels.has(key)).map(([, label]) => label);
      const health = !targetPeriod ? 'No current period'
        : missing.length ? missing.length + ' versions missing for ' + targetPeriod : 'All ' + targetPeriod + ' versions current';
      return '<section class="email-material-family"><header><div><span class="email-material-category">' + esc(g.category)
        + '</span><h3>' + esc(g.docs[0].name || g.key) + '</h3></div><span class="email-material-health '
        + (missing.length || !targetPeriod ? 'incomplete' : 'complete') + '">' + esc(health) + '</span></header>'
        + '<div class="email-channel-badges">' + MATERIAL_CHANNELS.map(([key, label]) =>
          '<span class="' + (channels.has(key) ? 'on' : 'off') + '">' + esc(label) + '</span>').join('') + '</div>'
        + g.docs.sort((a, b) => String(b.periodKey || b.asOfDate || '').localeCompare(String(a.periodKey || a.asOfDate || ''))).map((d) =>
          '<details class="email-material-version"><summary><b>' + esc(channelLabel(d.channel)) + '</b> / '
          + esc(d.periodKey || d.asOfDate || 'Evergreen') + '<span class="email-fresh ' + esc(materialStatus(d)) + '">'
          + esc(materialStatus(d)) + '</span></summary><p>' + esc(d.fileName || d.name + '.pdf') + ' / ' + bytes(d.size)
          + ' / v' + d.version + '</p>' + materialEditFields(d)
          + '<p><button type="button" class="email-small" data-email="doc-replace" data-id="' + esc(d.id) + '" data-name="' + esc(d.name) + '">Replace PDF</button> '
          + '<button type="button" class="grave email-small" data-email="doc-delete" data-id="' + esc(d.id) + '" data-name="' + esc(d.name) + '">'
          + 'Remove' + '</button></p></details>').join('')
        + '</section>';
    }).join('');
  }

function routesHtml() {
    const routeSet = catalog.materialRoutes || {}, rules = routeSet.rules || [];
    const active = rules.filter((r) => !r.disabled);
    const removed = rules.filter((r) => r.disabled);
    const q = routeSearch.toLowerCase(), shown = active.filter((r) => !q || (r.domain + ' ' + r.channel + ' ' + routeSource(r)).toLowerCase().includes(q));
    return '<section class="email-route-card"><header><div><h3>Recipient routing</h3><p>Domains recommend the approved client-group version. Canonical contact identity is still checked by the server.</p></div>'
      + '<label class="email-search">Search routes<input id="routeSearch" value="' + esc(routeSearch) + '" placeholder="ubs.com"></label></header>'
      + '<div id="routeRows">' + shown.map((r) => '<div class="email-route-row" data-route-key="' + esc(r.domain) + '"><input data-route-field="domain" value="' + esc(r.domain) + '" aria-label="Domain">'
        + '<select data-route-field="channel">' + MATERIAL_CHANNELS.filter(([key]) => key !== 'generic').map(([key, label]) =>
          '<option value="' + key + '"' + (key === r.channel ? ' selected' : '') + '>' + esc(label) + '</option>').join('') + '</select>'
        + '<small>' + esc(routeSource(r)) + '</small><button type="button" class="email-small grave" data-email="route-remove" data-domain="' + esc(r.domain) + '">Remove</button></div>').join('')
      + '</div><button type="button" class="email-small" data-email="route-add">Add domain</button> '
      + '<button type="button" class="ask-btn primary" data-email="routes-save">Save routing rules</button>'
      + (removed.length ? '<details class="email-route-removed"><summary>Removed routes (' + removed.length + ')</summary>'
        + removed.map((r) => '<p><span>' + esc(r.domain) + ' / ' + esc(channelLabel(r.channel)) + '</span> <button type="button" class="email-small" data-email="route-restore" data-domain="' + esc(r.domain) + '">Restore</button></p>').join('') + '</details>' : '')
      + '</section>';
  }

  function docsView(message = "", bad = false) {
    const docs = (catalog && catalog.documents) || [];
    document.getElementById("emailTitle").textContent = "Materials Library";
    document.getElementById("emailBody").innerHTML = '<div class="email-docs-admin email-materials">'
      + '<div class="email-material-intro"><div><h2>Approved sales materials</h2><p>Upload PDFs together, confirm the suggested organization, and publish. Reps see only approved, current versions.</p></div>'
      + '<label class="email-upload-button">Choose PDFs<input id="docFiles" type="file" accept="application/pdf,.pdf" multiple></label></div>'
      + (replacing ? '<fieldset class="email-doc-add"><legend>Replace ' + esc(replacing.name) + '</legend>'
        + '<p class="email-fine">The new PDF keeps this material ID and advances its version. Existing unapproved batches carrying the old version become invalid.</p>'
        + '<label class="email-label">Display name<input id="docName" maxlength="120" value="' + esc(replacing.name) + '"></label>'
        + '<label class="email-label">Replacement PDF<input id="docFile" type="file" accept="application/pdf,.pdf"></label>'
        + '<button type="button" class="ask-btn primary" data-email="doc-upload">Publish replacement</button> '
        + '<button type="button" class="email-small" data-email="doc-replace-cancel">Cancel</button></fieldset>' : '')
      + (message ? '<p class="' + (bad ? 'email-error' : 'email-ok') + '">' + esc(message) + '</p>' : '')
      + (materialQueue.length ? '<section class="email-upload-queue"><header><h3>Ready to publish</h3><button type="button" class="ask-btn primary" data-email="materials-upload">Publish ready PDFs</button></header>'
        + '<div id="materialQueue">' + queueRows() + '</div></section>' : '')
      + '<div class="email-material-tools"><label class="email-search">Search materials<input id="materialSearch" value="' + esc(materialSearch) + '" placeholder="ACV, UBS, Q2 2026..."></label>'
      + '<span>' + docs.length + ' approved version' + (docs.length === 1 ? '' : 's') + '</span></div>'
      + '<div class="email-material-library">' + libraryHtml(docs) + '</div>'
      + routesHtml()
      + '<div class="email-done-actions"><button type="button" class="ask-btn" data-email="docs-back">Back</button></div></div>';
  }

  // ---- template authoring (EmailAdministrator only) ------------------------
  // The Word library carries a header block -- Title, Original Author,
  // Document #, Attachments required, Approval Date -- and that is a compliance
  // record, not decoration. Moving templates in here keeps it rather than
  // reducing every template to a subject and a body.
  let editing = null;    // the template being edited, or null
  let lintTimer = null;
  // A snapshot of the template as last SAVED, so the button can say whether
  // there is anything to save. Clicking a button and getting no visible
  // response is indistinguishable from a broken button -- which is exactly how
  // this one read while it was silently failing on the IMAGE_TOKEN bug.
  let savedSnapshot = null;
  const templateFingerprint = (t) => JSON.stringify([t.name, t.documentNumber, t.author,
    t.approvalDate, t.subject, t.bodyText, t.repNotes, [...(t.requiredDocumentIds || [])].sort()]);
  const templateDirty = () => !savedSnapshot
    || templateFingerprint(collectTemplate()) !== savedSnapshot;

  function paintSaveState() {
    const btn = document.getElementById("tplSave");
    if (!btn) return;
    const dirtyNow = templateDirty();
    btn.textContent = dirtyNow ? "Save template" : "Saved";
    btn.disabled = !dirtyNow;
    btn.className = `ask-btn ${dirtyNow ? "primary" : "email-saved-btn"}`;
  }

  const SAMPLE = { name: "Dana Whitfield", firstName: "Dana", lastName: "Whitfield",
    companyName: "Whitfield Wealth Partners" };
  const SAMPLE_THIN = { name: "", firstName: "", lastName: "", companyName: "Northgate Advisors" };

  /* The signed-in sender, from the same Microsoft 365 profile the signature is
   * built from -- so {{sender_name}} in the preview reads exactly as the name
   * in the signature block below it. */
  function senderFields() {
    const p = (catalog && catalog.connection && catalog.connection.profile) || {};
    return { sender_name: String(p.displayName || ""),
             sender_title: String(p.jobTitle || "") };
  }

  /* A MISSING VALUE LEAVES THE TOKEN, exactly as the server does.
   *
   * This used to substitute a friendly placeholder -- "your job title" for an
   * empty jobTitle, "" for an absent first name -- and both were wrong in the
   * same way: the preview read as a finished sentence while the SEND left
   * "{{sender_title}}" in the text, tripped unresolved_merge_fields, and
   * refused the batch with a 400. An administrator saw "I am EIC's your job
   * title covering your area", thought it fine, and had no way to connect it to
   * the refusal that followed.
   *
   * renderTemplate() on the server does this: an unrecognised or empty field is
   * returned UNTOUCHED and recorded as missing. The preview now matches, and
   * unresolvedFields() below turns the same list into something an
   * administrator can act on.
   */
  function mergePreview(text, r) {
    const values = Object.assign({
      first_name: r.firstName, last_name: r.lastName, company_name: r.companyName,
    }, senderFields());
    return String(text || "").replace(/\{\{\s*([a-zA-Z0-9_]+)\s*\}\}/g,
      (whole, field) => {
        const key = String(field).toLowerCase();
        if (!(key in values)) return whole;      // image tokens, typos: untouched
        return values[key] ? values[key] : whole;
      });
  }

  /* Which merge fields this text cannot fill for THIS sender.
   *
   * Only the sender ones: a blank first name is a property of one recipient and
   * the composer already reports those per message, while a blank job title is
   * a property of the account and blocks every message in every batch until
   * somebody fixes the Microsoft 365 profile. That is worth saying once, loudly,
   * where the template is written.
   */
  function unresolvedSenderFields(text) {
    const s = senderFields();
    return Object.keys(s).filter(key =>
      !s[key] && new RegExp(`\{\{\s*${key}\s*\}\}`, "i").test(String(text || "")));
  }


  /* The editor preview has to agree with what actually sends.
   *
   * The server turns bullet lines into real <ul>/<li>. If this pane kept
   * rendering them as plain text, an administrator would format a list, see it
   * unformatted, and "fix" it into something worse. Same shapes the server
   * recognises: Word's bullet-plus-tab, dashes, stars, and numbers.
   */
  const P_BULLET = /^\s*(?:[\u2022\u00b7\u25e6*-]|o)[\s\t]+(.*)$/;
  const P_NUMBER = /^\s*\d{1,2}[.)][\s\t]+(.*)$/;

  function previewBlock(text) {
    const out = [];
    let list = null, para = [];
    const flushList = () => {
      if (!list) return;
      out.push(`<${list.tag}>${list.items.map((t) => `<li>${t}</li>`).join("")}</${list.tag}>`);
      list = null;
    };
    const flushPara = () => {
      if (para.length) out.push(`<p>${para.join("<br>")}</p>`);
      para = [];
    };
    for (const line of String(text).split(/\n/)) {
      const b = line.match(P_BULLET);
      const n = b ? null : line.match(P_NUMBER);
      if (b || n) {
        flushPara();
        const tag = b ? "ul" : "ol";
        if (list && list.tag !== tag) flushList();
        if (!list) list = { tag, items: [] };
        list.items.push((b ? b[1] : n[1]).trim());
        continue;
      }
      flushList();
      if (line.trim()) para.push(line);
    }
    flushList();
    flushPara();
    return out.join("");
  }

  function previewHtml(tpl, r) {
    const images = tpl.images || [];
    const body = mergePreview(tpl.bodyText, r);
    const html = esc(body).split(/\n{2,}/).map(previewBlock).join("")
      .replace(/\{\{\s*image:([a-z0-9-]+)\s*\}\}/gi, (whole, id) => {
        const img = images.find((i) => i.id.toLowerCase() === String(id).toLowerCase());
        return img ? `<span class="email-img-chip">&#128200; ${esc(img.name)}</span>`
                   : `<span class="email-error">${esc(whole)} — no such chart</span>`;
      });
    return `<p class="email-prev-subject"><b>Subject:</b> ${esc(mergePreview(tpl.subject, r)) || "<i>empty</i>"}</p>${html}`;
  }

  function templatesView(message = "", bad = false) {
    const list = (catalog && catalog.templates) || [];
    document.getElementById("emailTitle").textContent = "Email templates";
    document.getElementById("emailBody").innerHTML = `<div class="email-docs-admin">
      <p class="email-next">Reps choose from these and cannot write their own. Required
        attachments set here are added to every batch automatically.</p>
      ${message ? `<p class="${bad ? "email-error" : "email-ok"}">${esc(message)}</p>` : ""}
      <ul class="email-doclist">${list.length ? list.map((t) => `<li>
        <span class="email-doc-main"><b>${esc(t.name)}</b>${
          // The state a rep would see, said plainly on the admin's own row.
          t.published === false ? `<span class="email-held">Held &mdash; not visible to the team</span>` : ""}
          <small>${esc(t.documentNumber || "no document #")}
            ${t.approvalDate ? `&middot; approved ${esc(t.approvalDate)}` : ""}
            &middot; v${t.version}${(t.images || []).length ? ` &middot; ${t.images.length} chart(s)` : ""}</small></span>
        <label class="email-live" title="${t.published === false
            ? "Publish this template so the sales team can send it"
            : "Withdraw it: the team stops seeing it, and it stays here"}">
          <input type="checkbox" data-email="tpl-publish" data-id="${esc(t.id)}"
            data-name="${esc(t.name)}"${t.published === false ? "" : " checked"}>
          <span>Live</span></label>
        <button type="button" class="email-small" data-email="tpl-test" data-id="${esc(t.id)}"
          data-name="${esc(t.name)}">Test to me</button>
        <button type="button" class="email-small" data-email="tpl-edit" data-id="${esc(t.id)}">Edit</button>
        <button type="button" class="grave email-small" data-email="tpl-delete"
          data-id="${esc(t.id)}" data-name="${esc(t.name)}">Remove</button></li>`).join("")
        : `<li class="email-doc-none">No templates yet.</li>`}</ul>
      <div class="email-done-actions">
        <button type="button" class="ask-btn primary" data-email="tpl-new">New template</button>
        <button type="button" class="ask-btn" data-email="docs-back">Back</button></div></div>`;
  }

  function templateEditView(message = "", bad = false) {
    const t = editing, docs = (catalog && catalog.documents) || [];
    const req = new Set(t.requiredDocumentIds || []);
    document.getElementById("emailTitle").textContent = t.id ? `Edit: ${t.name}` : "New template";
    document.getElementById("emailBody").innerHTML = `<div class="email-tpl">
      ${message ? `<p class="${bad ? "email-error" : "email-ok"}">${esc(message)}</p>` : ""}
      <div class="email-tpl-meta">
        <label class="email-label">Title<input id="tplName" maxlength="120" value="${esc(t.name || "")}"></label>
        <label class="email-label">Document #<input id="tplDoc" maxlength="60" value="${esc(t.documentNumber || "")}"></label>
        <label class="email-label">Original author<input id="tplAuthor" maxlength="60" value="${esc(t.author || "")}"></label>
        <label class="email-label">Approval date<input id="tplApproved" maxlength="40"
          placeholder="2026-08-17" value="${esc(t.approvalDate || "")}"></label>
      </div>
      <label class="email-label">Subject<input id="tplSubject" maxlength="500" value="${esc(t.subject || "")}"></label>
      <label class="email-label">Body<textarea id="tplBody" rows="12" maxlength="30000">${esc(t.bodyText || "")}</textarea></label>
      <p class="email-fine">Merge fields: <code>{{first_name}}</code> <code>{{last_name}}</code>
        <code>{{company_name}}</code> <code>{{sender_name}}</code> <code>{{sender_title}}</code>.
        Charts: <code>{{image:id}}</code>.</p>
      <div id="tplLint" class="email-lint"></div>
      <label class="email-label">Notes for the rep (never sent)<textarea id="tplNotes" rows="2"
        maxlength="4000">${esc(t.repNotes || "")}</textarea></label>
      <fieldset class="email-docs"><legend>Required attachments</legend>${docs.length ? docs.map((d) =>
        `<label><input type="checkbox" class="tpl-req" value="${esc(d.id)}"${req.has(d.id) ? " checked" : ""}>
          <span>${esc(d.name)}</span><small>${bytes(d.size)}</small></label>`).join("")
        : `<p>No approved documents yet.</p>`}</fieldset>
      <fieldset class="email-doc-add"><legend>Charts in the body</legend>
        <ul class="email-doclist">${(t.images || []).length ? t.images.map((i) => `<li>
          <span class="email-doc-main"><b>${esc(i.name)}</b><small><code>{{image:${esc(i.id)}}}</code>
            &middot; ${bytes(i.size)}</small></span>
          <button type="button" class="email-small" data-email="tpl-img-insert" data-id="${esc(i.id)}">Insert</button>
          <button type="button" class="grave email-small" data-email="tpl-img-delete" data-id="${esc(i.id)}">Remove</button>
          </li>`).join("") : `<li class="email-doc-none">No charts on this template.</li>`}</ul>
        ${t.id ? `<label class="email-label">Add a chart (PNG, JPEG or GIF)
          <input id="tplImgFile" type="file" accept="image/png,image/jpeg,image/gif"></label>
          <label class="email-label">Chart name<input id="tplImgName" maxlength="80" placeholder="LCV performance"></label>
          <button type="button" class="ask-btn" data-email="tpl-img-upload">Upload chart</button>`
          : `<p class="email-fine">Save the template once before adding charts.</p>`}
      </fieldset>
      <div class="email-tpl-prev">
        <div><p class="eyebrow">Preview — complete contact</p><div id="tplPrev1" class="email-rendered"></div></div>
        <div><p class="eyebrow">Preview — missing first name</p><div id="tplPrev2" class="email-rendered"></div></div>
      </div>
      <div class="email-done-actions">
        <button type="button" id="tplSave" class="ask-btn primary" data-email="tpl-save">Save template</button>
        ${t.id ? `<button type="button" class="ask-btn" data-email="tpl-test"
          data-id="${esc(t.id)}" data-name="${esc(t.name)}">Test to me</button>` : ""}
        <button type="button" class="ask-btn" data-email="tpl-back">Back</button></div></div>`;
    for (const id of ["tplSubject", "tplBody"]) {
      const el = document.getElementById(id);
      if (el) el.addEventListener("input", () => { clearTimeout(lintTimer); lintTimer = setTimeout(runLint, 350); });
    }
    // The button reflects EVERY field, not only the two that trigger linting --
    // a changed document number is just as unsaved as a changed body.
    for (const id of ["tplName", "tplDoc", "tplAuthor", "tplApproved",
                      "tplSubject", "tplBody", "tplNotes"]) {
      const el = document.getElementById(id);
      if (el) el.addEventListener("input", paintSaveState);
    }
    for (const box of document.querySelectorAll(".tpl-req"))
      box.addEventListener("change", paintSaveState);
    runLint();
    paintSaveState();
  }

  function collectTemplate() {
    const g = (id) => (document.getElementById(id) || {}).value || "";
    return { ...editing, name: g("tplName").trim(), documentNumber: g("tplDoc").trim(),
      author: g("tplAuthor").trim(), approvalDate: g("tplApproved").trim(),
      subject: g("tplSubject"), bodyText: g("tplBody"), repNotes: g("tplNotes"),
      requiredDocumentIds: [...document.querySelectorAll(".tpl-req:checked")].map((x) => x.value) };
  }

  async function runLint() {
    const t = collectTemplate();
    document.getElementById("tplPrev1").innerHTML = previewHtml(t, SAMPLE);
    document.getElementById("tplPrev2").innerHTML = previewHtml(t, SAMPLE_THIN);
    const box = document.getElementById("tplLint");
    /* A sender field the ACCOUNT cannot fill blocks every batch this template
     * is ever used for, and the server reports it only at approval time as
     * "Resolve template fields: sender_title" -- by which point the rep is
     * looking at a refusal with no idea it is a profile setting. Said here
     * instead, next to the text that uses it.
     *
     * Not an error on the template: the template is correct and will work the
     * moment the profile is filled in. It is a fact about this account. */
    const gaps = unresolvedSenderFields(`${t.subject}
${t.bodyText}`);
    const LABEL = { sender_name: "display name", sender_title: "job title" };
    const gapHtml = gaps.map((key) =>
      `<p class="bad">&#9888; Your Microsoft 365 profile has no ${esc(LABEL[key])}, so
        <code>{{${esc(key)}}}</code> cannot be filled and this template will be
        refused at send. Ask IT to set it on your profile — nothing here needs changing.</p>`
    ).join("");
    try {
      const r = await api("lint_template", { subject: t.subject, bodyText: t.bodyText });
      box.innerHTML = gapHtml + ([...r.errors.map((e) => `<p class="bad">&#9888; ${esc(e.message)}</p>`),
        ...r.warnings.map((w) => `<p>&#9432; ${esc(w.message)}</p>`)].join("")
        || (gaps.length ? "" : `<p class="ok">&#10003; No problems found.</p>`));
    } catch (e) { box.innerHTML = `<p class="bad">${esc(e.message)}</p>`; }
  }

  function readAsBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(new Error("That file could not be read."));
      // Strip the "data:application/pdf;base64," prefix; the server wants the
      // payload only, and decodes it to check the %PDF- header itself.
      reader.onload = () => resolve(String(reader.result || "").split(",", 2)[1] || "");
      reader.readAsDataURL(file);
    });
  }

  function stateLabel(state) {
    return ({ editing: "Editing", invalid: "Needs attention", draft_pending: "Queued for draft",
      draft_creating: "Creating draft", draft_ambiguous: "Reconciling draft", draft_ready: "Outlook draft ready",
      send_scheduled: "Scheduled", scheduled: "Scheduled", schedule_held: "Held for review", sending: "Submitting", send_ambiguous: "Reconciling send",
      submitted: "Submitted — checking Sent Items", sent: "Sent · no known failure", auth_required: "Reconnect Microsoft 365",
      failed: "Failed", canceled: "Canceled", paused: "Paused", held: "Held for review", needs_review: "Needs review", action_required: "Action required", drafts_ready: "Drafts ready",
      completed: "Complete", partial_failure: "Completed with failures" })[state] || state;
  }

  // Terminal states used to leave the full editor on screen with a green label
  // buried in the recipient list, so a finished batch looked identical to a
  // working one. Show what happened, and what to do next, instead.
  const DONE = ["drafts_ready", "completed", "partial_failure", "canceled"];

  function doneView() {
    const b = detail.batch, counts = {};
    for (const m of detail.messages) counts[m.state] = (counts[m.state] || 0) + 1;
    const n = (k) => counts[k] || 0;
    const ready = n("draft_ready"), sent = n("sent") + n("submitted");
    const failed = n("failed"), canceled = n("canceled");
    const headline = { drafts_ready: "Drafts are in your Outlook",
      completed: "Batch complete", partial_failure: "Completed, with failures",
      canceled: "Batch canceled" }[b.status] || stateLabel(b.status);
    const rows = [
      ready ? [`${ready} draft${ready === 1 ? "" : "s"} created`, "good"] : null,
      sent ? [`${sent} sent`, "good"] : null,
      failed ? [`${failed} failed`, "bad"] : null,
      canceled ? [`${canceled} canceled`, ""] : null,
    ].filter(Boolean);
    document.getElementById("emailTitle").textContent = b.name || "Email batch";
    document.getElementById("emailBody").innerHTML = `<div class="email-done">
      <h3 class="${failed ? "bad" : "good"}">${esc(headline)}</h3>
      <ul class="email-tally">${rows.map(([label, tone]) =>
        `<li class="${tone}">${esc(label)}</li>`).join("")}</ul>
      ${b.status === "drafts_ready" ? `<p class="email-next">Open Outlook and go to
        <b>Drafts</b> to review and send. Nothing has been sent from this app.</p>` : ""}
      ${failed ? `<p class="email-next">Open the details below to see which recipients
        failed and why. Retrying only affects those.</p>` : ""}
      <div class="email-done-actions">
        <button type="button" class="ask-btn" data-email="details">Review messages</button>
        ${failed ? `<button type="button" class="ask-btn" data-email="retry">Retry failed</button>` : ""}
        <button type="button" class="ask-btn primary" data-email="close">Close</button>
      </div></div>`;
  }


  // The recipient list used to occupy a third of a phone screen and clip after
  // two names. A pager and an honest tally do the same job in two lines.
  //
  // The wording here is deliberate and was got wrong first time round. The app
  // checks addresses, duplicates, merge fields, attachment versions and size.
  // It has no view on whether the template suited this advisor or whether the
  // merged sentence reads correctly -- so nothing may be labelled "ready".
  // "No automatic problems found" says what actually happened; "not yet opened"
  // is a fact about what the REP has done, and is the number that should feel
  // uncomfortable when it is large.
  function tally() {
    const all = (detail && detail.messages) || [];
    const blocked = all.filter((m) => (m.validation && m.validation.errors || []).length).length;
    const reviewed = all.filter((m) => m.reviewed).length;
    const behind = all.filter((m) => (m.validation && m.validation.warnings || [])
      .some((w) => w.code === "behind_common_text")).length;
    const failed = all.filter((m) => m.failureMessage).length;
    const bounced = all.filter((m) => m.bounceKind === "hard").length;
    return { total: all.length, blocked, reviewed, behind, failed, bounced,
             unopened: all.length - blocked - reviewed };
  }

  function identityLabel(message) {
    const tier = String((message && (message.recipientTier || message.contactTier)) || "").toLowerCase();
    const source = String((message && (message.recipientSource || message.contactSource)) || "");
    if (tier === "confirmed") return "ACT-confirmed CRD";
    if (tier === "high") return `High-confidence roster match${source ? " · " + source : ""}`;
    if (tier === "self_test") return "Connected mailbox test";
    return "Identity evidence unavailable";
  }

  function identitySummary(messages) {
    const counts = { confirmed: 0, high: 0, other: 0 };
    for (const message of messages || []) {
      const tier = String(message.recipientTier || message.contactTier || "").toLowerCase();
      if (tier === "confirmed") counts.confirmed += 1;
      else if (tier === "high") counts.high += 1;
      else counts.other += 1;
    }
    return [counts.confirmed ? `${counts.confirmed} ACT-confirmed` : "",
      counts.high ? `${counts.high} high-confidence` : "",
      counts.other ? `${counts.other} other/test` : ""]
      .filter(Boolean).join(" · ");
  }

  function tallyRow() {
    const c = tally();
    return `<div class="email-tally-row">
      <span class="count">${c.total} recipient${c.total === 1 ? "" : "s"}</span>
      <span class="identity">${esc(identitySummary(detail.messages))}</span>
      ${c.blocked ? `<button type="button" class="bad" data-email="next-problem">${c.blocked} blocked</button>` : ""}
      ${c.reviewed ? `<span class="good">${c.reviewed} reviewed by you</span>` : ""}
      ${c.unopened ? `<span class="warnish">${c.unopened} not yet opened</span>` : ""}
      ${c.behind ? `<button type="button" class="bad" data-email="next-behind">${c.behind} keep own wording</button>` : ""}
      ${c.failed ? `<button type="button" class="bad" data-email="next-failed">${c.failed} failed &mdash; see why</button>` : ""}
      ${c.bounced ? `<span class="bad">${c.bounced} bounced</span>` : ""}
    </div>`;
  }

  function pagerRow() {
    const m = detail.messages[cursor], n = detail.messages.length;
    const flag = (m.validation && m.validation.errors || []).length ? "bad"
      : m.reviewed ? "good" : "warnish";
    return `<div class="email-pager">
      <button type="button" data-email="step" data-by="-1" ${cursor ? "" : "disabled"} aria-label="Previous recipient">&#9664;</button>
      <span class="email-pager-who"><b>${esc(m.recipientName || m.recipientEmail)}</b>
        <small>${esc(identityLabel(m))}</small>
        <small class="${flag}">${cursor + 1} of ${n} &middot; ${esc(stateLabel(m.state))}</small></span>
      <button type="button" data-email="step" data-by="1" ${cursor < n - 1 ? "" : "disabled"} aria-label="Next recipient">&#9654;</button>
      ${["editing", "invalid"].includes(detail.batch.status) && n > 1
        ? `<button type="button" class="email-drop" data-email="drop-recipient"
             data-id="${esc(m.id)}" data-name="${esc(m.recipientName || m.recipientEmail)}"
             aria-label="Remove this recipient from the batch" title="Remove from this batch">&times;</button>` : ""}
    </div>`;
  }


  function markDirty(on) {
    dirty = on;
    const tag = document.getElementById("emailSaveState");
    if (tag) {
      tag.textContent = saving ? "Saving…" : on ? "Unsaved changes" : "Saved";
      tag.className = `email-savestate ${saving ? "" : on ? "warnish" : "good"}`;
    }
  }


  // Swipe between recipients on a touch screen. Guarded fairly tightly: a
  // mostly-horizontal, deliberate movement, ignored when it starts inside a
  // text box or the preview, because those scroll and select in their own
  // right and stealing that gesture would make editing infuriating.
  function wireSwipe() {
    const area = document.getElementById("emailBody");
    if (!area || area._swipeWired) return;
    area._swipeWired = true;
    let x0 = 0, y0 = 0, live = false;
    area.addEventListener("touchstart", (e) => {
      if (e.touches.length !== 1) { live = false; return; }
      /* The preview is swipeable; the boxes you type in are not.
       *
       * .email-rendered used to be excluded alongside the text controls, on the
       * theory that it selects and scrolls in its own right. On a phone that
       * made the feature almost unreachable: the two textareas and the preview
       * cover nearly the whole screen, so only the thin gaps between them
       * responded, and swiping felt broken rather than absent.
       *
       * It is read-only, and the guard below already demands a deliberate,
       * mostly-horizontal 60px movement -- while selecting text on iOS starts
       * with a long press, which is a different gesture entirely. The text
       * controls stay excluded, because stealing a drag from somebody midway
       * through editing a sentence is a real cost with no upside.
       */
      const from = e.target.closest("input, textarea, select, .email-jump");
      live = !from;
      x0 = e.touches[0].clientX; y0 = e.touches[0].clientY;
    }, { passive: true });
    area.addEventListener("touchend", async (e) => {
      if (!live || !detail || !detail.messages) return;
      live = false;
      const touch = e.changedTouches && e.changedTouches[0];
      if (!touch) return;
      const dx = touch.clientX - x0, dy = touch.clientY - y0;
      // Horizontal, decisive, and clearly not a scroll.
      if (Math.abs(dx) < 60 || Math.abs(dx) < Math.abs(dy) * 1.8) return;
      const next = cursor + (dx < 0 ? 1 : -1);
      if (next < 0 || next >= detail.messages.length) return;
      if (!await saveOne()) return;
      cursor = next;
      swipeFrom = dx < 0 ? "left" : "right";
      composerView();
    }, { passive: true });
  }

  function wireAutoSave() {
    for (const id of ["emailOneSubject", "emailOneBody"]) {
      const el = document.getElementById(id);
      if (!el) continue;
      el.addEventListener("input", () => markDirty(true));
      el.addEventListener("blur", () => { if (dirty) saveOne(); });
    }
    markDirty(false);
  }

  // Returns true when it is safe to move on. Any navigation away from an edited
  // message goes through here first, so an edit cannot be silently discarded.
  async function saveOne() {
    if (!dirty || saving || !detail) return true;
    const m = detail.messages[cursor];
    const subject = document.getElementById("emailOneSubject");
    const body = document.getElementById("emailOneBody");
    if (!subject || !body) return true;
    saving = true; markDirty(true);
    try {
      detail = await api("update_message", { batchId: detail.batch.id, messageId: m.id,
        subject: subject.value, bodyText: body.value, reviewed: true });
      saving = false; dirty = false;
      composerView();
      return true;
    } catch (e) {
      saving = false;
      markDirty(true);
      notice(`Not saved: ${e.message}`, true);
      return false;
    }
  }


  /* Make the preview show the actual charts.
   *
   * The message body carries <img src="cid:people@eicadvisormap">, which is
   * correct on the wire -- it resolves to the attached part once the mail client
   * has it. A browser cannot resolve cid: at all, so the screen whose whole job
   * is "this is exactly what they will receive" was drawing a broken-image icon
   * next to text claiming otherwise.
   *
   * Swapped for a URL that serves the same approved bytes. The message's own
   * inlineImages list supplies the mapping, so nothing is guessed from the
   * markup, and an unknown cid is left alone rather than pointed somewhere.
   */
  function previewWithImages(html, images, templateId) {
    if (!html || !(images || []).length || !templateId) return html || "";
    const byCid = new Map((images || []).map((i) => [String(i.cid || "").toLowerCase(), i]));
    return String(html).replace(/src="cid:([^"]+)"/gi, (whole, cid) => {
      const image = byCid.get(String(cid).toLowerCase());
      if (!image) return whole;
      const url = `/api/email?op=template_image&templateId=${encodeURIComponent(templateId)}`
        + `&imageId=${encodeURIComponent(image.id)}`;
      return `src="${url}"`;
    });
  }


  /* Lint the rep's common text as they type.
   *
   * The administrator's template editor has done this since it was built; the
   * box a rep edits had nothing. Same endpoint, same rules -- the point is that
   * a single brace or a mistyped chart is caught before Apply, not at approval
   * after it has been copied onto sixty messages.
   *
   * Advisory only. The server lints again on update_common and refuses there;
   * this is the fast feedback, never the gate.
   */
  let commonLintTimer = null;
  function wireCommonLint() {
    const box = document.getElementById("emailCommonLint");
    if (!box) return;
    const run = async () => {
      const subject = document.getElementById("emailCommonSubject");
      const body = document.getElementById("emailCommonBody");
      if (!subject || !body) return;
      try {
        const r = await api("lint_template", { subject: subject.value, bodyText: body.value });
        const known = new Set((detail.templateImages || []).map((i) => String(i.id).toLowerCase()));
        const bad = [...`${subject.value}
${body.value}`.matchAll(/\{\{\s*image:([^}]+)\s*\}\}/gi)]
          .map((m) => String(m[1]).trim().toLowerCase()).filter((id) => !known.has(id));
        const errors = [...(r.errors || []),
          ...[...new Set(bad)].map((id) => ({ message: `No chart called "${id}" on this template.` }))];
        box.innerHTML = errors.map((e) => `<p class="bad">&#9888; ${esc(e.message)}</p>`).join("")
          + (r.warnings || []).map((w) => `<p>&#9432; ${esc(w.message)}</p>`).join("");
      } catch { /* advisory: the server checks again on Apply */ }
    };
    for (const id of ["emailCommonSubject", "emailCommonBody"]) {
      const el = document.getElementById(id);
      if (!el || el.disabled) continue;
      el.addEventListener("input", () => { clearTimeout(commonLintTimer); commonLintTimer = setTimeout(run, 350); });
    }
    run();
  }


  /* Why "Approve & Send" cannot work for this batch, if it cannot.
   *
   * The server refuses over-sized batches at approval, which is correct but
   * arrives far too late: a rep reviews forty-eight messages, presses the
   * button, confirms a dialog, and only then learns the send was never possible.
   * Everything needed to say so is known the moment the batch is built.
   *
   * Returns "" when direct sending is available for this batch. The server
   * still enforces all of it; this only stops the rep wasting the work.
   */
  function sendBlockedReason(batch) {
    const lim = (catalog && catalog.limits) || {};
    const n = Number(batch.recipientCount) || 0;
    if (!catalog.policy.directSendAvailable) {
      return catalog.policy.directSendBlockedBy
        || "Direct sending is switched off for this application.";
    }
    if (lim.directBatchMax && n > lim.directBatchMax) {
      return `${n} recipients is over the ${lim.directBatchMax}-recipient limit for a single `
        + `send. You can create Outlook drafts, or split this into batches of `
        + `${lim.directBatchMax} or fewer to send from the app.`;
    }
    // externalCount rather than recipientCount: internal addresses do not count
    // against the rolling window, so neither should they here.
    const ext = Number(batch.externalCount) || 0;
    if (lim.rollingRemaining != null && ext > lim.rollingRemaining) {
      return `This batch has ${ext} external recipients and you have `
        + `${lim.rollingRemaining} of ${lim.rollingExternalLimit} left in the last 24 hours. `
        + `Outlook drafts are unaffected.`;
    }
    return "";
  }


  /* Say whether the common edit has actually been applied.
   *
   * Typing in the common box changes nothing until Apply is pressed, and
   * nothing on screen said so -- a rep could rewrite the template, look at a
   * preview showing the OLD text, and send. The button now changes state the
   * moment the text diverges, and a line under the box says what is pending.
   *
   * Not auto-applied on a timer: applying resets every recipient's reviewed
   * flag, so an autosave would quietly undo review progress while somebody was
   * still typing.
   */
  let commonBaseline = null;

  function commonDirty() {
    const subject = document.getElementById("emailCommonSubject");
    const body = document.getElementById("emailCommonBody");
    if (!subject || !body || !commonBaseline) return false;
    return subject.value !== commonBaseline.subject || body.value !== commonBaseline.bodyText;
  }

  function paintApplyState() {
    const btn = document.getElementById("emailApply");
    const note = document.getElementById("emailApplyState");
    if (!btn) return;
    const dirty = commonDirty();
    const takers = detail.messages.filter((x) => !x.subjectOverridden && !x.bodyOverridden).length;
    btn.textContent = dirty ? `Apply to ${takers} of ${detail.messages.length}` : "Applied";
    btn.classList.toggle("primary", dirty);
    btn.disabled = !dirty;
    const overridden = detail.messages.length - takers;
    if (note) {
      note.hidden = !dirty;
      // Two different warnings, because they are two different problems. The
      // first is "you have not pressed the button". The second is "pressing it
      // will not be enough", which is the one a rep can miss entirely and send
      // stale wording to the people they took the most care over.
      note.className = `email-applystate${dirty && overridden ? " severe" : ""}`;
      note.innerHTML = !dirty ? ""
        : overridden
          ? `<b>Not applied, and ${overridden} of ${detail.messages.length} email`
            + `${overridden === 1 ? "" : "s"} will never take this change.</b> `
            + `Apply reaches the other ${takers}. The ${overridden} you edited individually `
            + `keep${overridden === 1 ? "s" : ""} the wording you gave `
            + `${overridden === 1 ? "it" : "them"}.`
          : `Not applied yet. These changes reach all ${takers} emails when you press Apply.`;
    }
    // The overwrite escape hatch only appears when there is something to
    // overwrite AND an unapplied change to overwrite it with.
    const force = document.getElementById("emailApplyAll");
    if (force) force.hidden = !(dirty && overridden);
  }

  function wireCommonApply() {
    const subject = document.getElementById("emailCommonSubject");
    const body = document.getElementById("emailCommonBody");
    if (!subject || !body) return;
    commonBaseline = { subject: subject.value, bodyText: body.value };
    for (const el of [subject, body]) el.addEventListener("input", paintApplyState);
    paintApplyState();
  }


  /* "Not yet opened" has to react to opening.
   *
   * reviewed was set only by saveOne(), which runs on an EDIT -- so a rep who
   * read every one of forty-eight messages carefully and changed none of them
   * still saw "48 not yet opened". The counter measured typing, while its label
   * promised reading.
   *
   * Marked after a short dwell rather than instantly: stepping through six
   * people to reach the seventh is not reading six emails, and counting it as
   * such would put the number back to being untrue in the other direction.
   */
  /* Marked on sight, with no dwell.
   *
   * There was a 1.2-second timer here on the theory that stepping through six
   * people to reach the seventh should not count as having read six emails.
   * In practice it mostly meant a rep who HAD read one still saw "not yet
   * opened" against it, which is the more damaging of the two errors: the
   * counter is there to find the messages nobody has looked at, and one that
   * cries wolf gets ignored. Approval does its own checking regardless. */
  function markOpened() {
    if (!detail || !detail.messages) return;
    const m = detail.messages[cursor];
    if (!m || m.reviewed) return;
    if (!["editing", "invalid"].includes(detail.batch.status)) return;
    const id = m.id;
    (async () => {
      try {
        detail = await api("update_message", { batchId: detail.batch.id, messageId: id,
          reviewed: true });
        // Only repaint if the rep is still on this message -- the request is
        // in flight while they are free to move on, and re-rendering under
        // them would throw away where they had got to.
        const still = detail && detail.messages && detail.messages[cursor];
        if (still && still.id === id) composerView();
      } catch { /* advisory; approval still checks for itself */ }
    })();
  }

  function composerView() {
    if (!detail || !detail.messages || !detail.messages.length) return;
    // The countdown has to stop here too. Cancelling inside the send window
    // ended the batch but left the ticker running, so the screen went on
    // promising a send that had already been called off.
    if (DONE.includes(detail.batch.status) && !forceDetail) {
      clearTimeout(pollTimer); clearInterval(tickTimer); return doneView();
    }
    cursor = Math.max(0, Math.min(cursor, detail.messages.length - 1));
    const b = detail.batch, m = detail.messages[cursor], locked = !["editing", "invalid"].includes(b.status);
    const errors = m.validation && m.validation.errors || [], warnings = m.validation && m.validation.warnings || [];
    const attachments = m.attachments || [];
    ensureScheduleDefaults();
    const schedule = scheduleCheck(b);
    const minParts = easternParts(new Date()), maxParts = easternParts(new Date(Date.now() + 7 * 86400000));
    const scheduleMin = `${minParts.year}-${minParts.month}-${minParts.day}`;
    const scheduleMax = `${maxParts.year}-${maxParts.month}-${maxParts.day}`;
    const scheduleHtml = locked ? "" : `<fieldset class="email-schedule"><legend>When should sending begin?</legend>
      <label><input type="radio" name="emailTiming" value="now" ${sendTiming === "now" ? "checked" : ""}> Send now</label>
      <label><input type="radio" name="emailTiming" value="later" ${sendTiming === "later" ? "checked" : ""}> Schedule for later</label>
      <div class="email-schedule-fields" ${sendTiming === "later" ? "" : "hidden"}>
        <label>Date <input id="emailScheduleDate" type="date" min="${scheduleMin}" max="${scheduleMax}" aria-describedby="emailScheduleHelp emailScheduleError" value="${esc(scheduleDate)}"></label>
        <label>Time <input id="emailScheduleTime" type="time" step="900" aria-describedby="emailScheduleHelp emailScheduleError" value="${esc(scheduleTime)}"></label>
        <small id="emailScheduleHelp">Eastern Time. Sending may continue after this start time.</small>
        <p id="emailScheduleError" class="${schedule.error || schedule.quarter ? "bad" : "good"}" role="alert">${esc(schedule.error || schedule.quarter || (schedule.date ? `Starts ${easternLabel(schedule.date)}` : ""))}</p>
      </div></fieldset>`;
    // The poll below re-renders every three seconds while a batch is working.
    // Replacing innerHTML resets scroll, which threw the rep back to the top of
    // a long recipient list mid-read. Keep where they were.
    const sendBlocked = locked ? "" : sendBlockedReason(b);
    const overridden = detail.messages.filter((x) => x.subjectOverridden || x.bodyOverridden).length;
    const takers = detail.messages.length - overridden;
    const lockedNotice = ["held", "needs_review", "schedule_held"].includes(b.status)
      ? "This scheduled batch is held. Review it before choosing a new send time."
      : b.status === "paused" ? "This batch is paused. It will not continue until you resume it."
      : ["drafting", "sending", "scheduled"].includes(b.status)
        ? (b.mode === "send"
          ? "Queued on the server - safe to close this window; sending continues in Microsoft 365."
          : "Draft creation is queued on the server - safe to close this window.")
        : "";
    /* Two different elements scroll, depending on the layout.
     *
     * On a phone .email-grid is display:block and #emailBody is the scroller.
     * On the desktop .email-editor carries overflow:auto and does the
     * scrolling, so restoring #emailBody's scrollTop restored a zero -- which
     * is why stepping to the next recipient jumped to the top of the page on a
     * desktop and behaved correctly on a phone.
     */
    const keepScroll = {
      body: (document.getElementById("emailBody") || {}).scrollTop || 0,
      editor: (document.querySelector(".email-editor") || {}).scrollTop || 0,
    };
    document.getElementById("emailTitle").textContent = b.name || "Email batch";
    document.getElementById("emailBody").innerHTML = `<div class="email-grid">
      <aside class="email-list">${b.suppressedNote
        ? `<p class="email-suppressed">${esc(b.suppressedNote)}</p>` : ""}${b.copiedInsteadNote
        ? `<p class="email-suppressed">${esc(b.copiedInsteadNote)}</p>` : ""}${tallyRow()}${pagerRow()}
        <details class="email-jump"${jumpOpen() ? " open" : ""}><summary>Jump to someone</summary>
        <ol>${detail.messages.map((x, i) => {
          const over = x.subjectOverridden || x.bodyOverridden;
          return `<li class="${i === cursor ? "on" : ""}">
          <button type="button" data-email="pick" data-index="${i}">
            <span>${esc(x.recipientName || x.recipientEmail)}</span>
            <small>${esc(x.recipientEmail)} &middot; ${esc(identityLabel(x))}</small>
            <em class="state-${esc(x.state)}">${esc(stateLabel(x.state))}${
              over ? ` &middot; own wording` : ""}</em></button>
          ${!locked && detail.messages.length > 1
            ? `<button type="button" class="email-row-drop" data-email="drop-recipient"
                 data-id="${esc(x.id)}" data-name="${esc(x.recipientName || x.recipientEmail)}"
                 title="Remove ${esc(x.recipientName || x.recipientEmail)} from this batch"
                 aria-label="Remove ${esc(x.recipientName || x.recipientEmail)}">&times;</button>` : ""}
          </li>`;
        }).join("")}</ol></details></aside>
      <main class="email-editor">
        <section class="email-common"><div class="email-section-head">
          <div><p class="eyebrow">Step 1 &middot; edit all</p><h3>Common template</h3></div>
          ${locked ? "" : `<div class="email-head-acts">
            <button type="button" class="email-small ghost" data-email="restore-common"
              title="Replace the common text with the approved template wording">Restore approved</button>
            <button type="button" class="email-small" id="emailApply" data-email="save-common">Apply to ${
              takers} of ${detail.messages.length}</button></div>`}</div>
          <label class="email-label">Subject template<input id="emailCommonSubject" maxlength="500" value="${esc(b.commonSubject)}" ${locked ? "disabled" : ""}></label>
          <label class="email-label email-grow">Body template<textarea id="emailCommonBody" rows="10" maxlength="50000" ${locked ? "disabled" : ""}>${esc(b.commonBodyText)}</textarea></label>
          <div id="emailCommonLint" class="email-lint"></div>
          <p id="emailApplyState" class="email-applystate" hidden></p>
          <p class="email-applyall" id="emailApplyAll" hidden>
            <button type="button" class="email-small grave" data-email="apply-all"
              >Apply to all ${detail.messages.length}, replacing individual edits</button></p>
          <p class="email-fine">Safe fields: {{first_name}}, {{last_name}}, {{company_name}}, {{sender_name}}, {{sender_title}}.
            ${(detail.templateImages || []).length ? `Charts: ${detail.templateImages.map((i) =>
              `<code>{{image:${esc(i.id)}}}</code>`).join(" ")}.` : ""}
            ${overridden ? `${overridden} email${overridden === 1 ? " keeps its" : "s keep their"} own wording and will not take this change.` : ""}</p></section>
        <section class="email-individual${m.subjectOverridden || m.bodyOverridden ? " overridden" : ""}">
          <div class="email-section-head">
            <div><p class="eyebrow">Step 2 &middot; edit one &middot; ${cursor + 1} of ${detail.messages.length}</p>
            <h3>${esc(m.recipientName || m.recipientEmail)}</h3>
            <p class="email-fine">${esc(identityLabel(m))}</p></div>
            ${locked ? "" : `<span id="emailSaveState" class="email-savestate good">Saved</span>`}</div>
          ${m.subjectOverridden || m.bodyOverridden ? `<p class="email-override">
            <b>This email keeps its own wording.</b> Edits to the common template above are
            not applied to it.${locked ? "" : ` <button type="button" class="email-small ghost"
              data-email="reset-one">Use the common wording instead</button>`}</p>` : ""}
          <label class="email-label">To<input value="${esc(m.recipientEmail)}" disabled></label>
          ${teammatePicker(m, b, locked)}
          <label class="email-label">Final subject<input id="emailOneSubject" maxlength="500" value="${esc(m.subject)}" ${locked ? "disabled" : ""}></label>
          <label class="email-label email-grow">Final body<textarea id="emailOneBody" rows="12" maxlength="50000" ${locked ? "disabled" : ""}>${esc(m.bodyText)}</textarea></label>
          <div class="email-checks">${errors.map((v) => `<p class="bad">&#9888; ${esc(v.message)}</p>`).join("")}${warnings.map((v) => `<p>&#9432; ${esc(v.message)}</p>`).join("")}</div>
          ${m.bounceKind === "hard" ? `<div class="email-failure">
            <p class="email-failure-head">&#9888; This address bounced</p>
            <p class="email-failure-why">${esc(m.recipientEmail)} was permanently rejected by the
              recipient's mail server${m.bounceReason ? ` (${esc(m.bounceReason)})` : ""}.
              It is now suppressed and will be excluded from future batches.</p>
          </div>` : ""}
          ${m.failureMessage ? `<div class="email-failure">
            <p class="email-failure-head">&#9888; This message did not go out</p>
            <p class="email-failure-why">${esc(m.failureMessage)}</p>
            ${m.failureCode ? `<p class="email-fine">Code: <code>${esc(m.failureCode)}</code>${
              m.graphRequestId ? ` &middot; Microsoft request id <code>${esc(m.graphRequestId)}</code>` : ""}</p>` : ""}
          </div>` : ""}
        </section>
        <section class="email-preview"><div class="email-section-head">
          <div><p class="eyebrow">Step 3 &middot; check</p><h3>Exactly what they receive</h3></div></div><div class="email-envelope"><p><b>From:</b> ${esc(b.graphMailbox)}</p><p><b>To:</b> ${esc(m.recipientEmail)}</p>${(m.cc || []).length
            ? `<p><b>Cc:</b> ${(m.cc || []).map((a) => `${esc(a)}<span class="env-note">${
                esc(ccReason(a, m, b))}</span>`).join(", ")}</p>` : ""}${(m.bcc || []).length
            ? `<p><b>Bcc:</b> ${(m.bcc || []).map((a) => `${esc(a)}<span class="env-note">${
                esc(bccReason(a, b))}</span>`).join(", ")}</p>` : ""}
          <p><b>Subject:</b> ${esc(m.subject)}</p><p><b>Attachments:</b> ${attachments.length ? attachments.map((a) => `${esc(a.name)} (${bytes(a.size)})`).join(", ") : "None"}</p></div>
          <div class="email-rendered">${previewWithImages(m.bodyHtml, m.inlineImages, b.templateId)}${m.signatureHtml || ""}</div></section>
      </main></div>
      <footer class="email-footer">${scheduleHtml}<p id="emailNotice" class="email-notice${
        sendBlocked ? " bad" : ""}">${esc((!locked && sendBlocked) ? sendBlocked
          : (b.warningMessage || (locked ? lockedNotice : "")))}</p><div>
        ${b.status === "completed" && b.mode === "send" && !b.parentBatchId && !b.followUpSentUtc
          ? `<button type="button" class="ask-btn" data-email="follow-up-open" data-id="${esc(b.id)}">Follow up on no reply</button>` : ""}
        ${b.status === "action_required" ? `<button type="button" class="ask-btn" data-email="connect">Reconnect Microsoft 365</button><button type="button" class="ask-btn" data-email="retry">Retry remaining</button>` : ""}
        ${["held", "needs_review", "schedule_held"].includes(b.status) ? `<button type="button" class="ask-btn primary" data-email="review-reschedule">Review &amp; reschedule</button>` : ""}
        ${locked && b.mode === "send" && !["completed", "canceled", "action_required", "held", "needs_review", "schedule_held"].includes(b.status) ? `<button type="button" class="ask-btn" data-email="pause">${b.status === "paused" ? "Resume remaining" : "Pause remaining"}</button>` : ""}
        ${locked && !["completed", "canceled", "drafts_ready"].includes(b.status) ? `<button type="button" class="ask-btn ghost" data-email="cancel">Cancel remaining</button>` : ""}
        ${locked ? "" : `<button type="button" class="ask-btn primary" data-email="approve-drafts">Create drafts</button>
          ${sendBlocked
            ? `<span class="email-sendoff" title="${esc(sendBlocked)}">Cannot send directly &#9432;</span>`
            : `<button type="button" class="ask-btn grave" data-email="approve-send">${sendTiming === "later" ? "Approve &amp; Schedule" : "Approve &amp; Send"}</button>`}`}
      </div></footer>`;
    // requestAnimationFrame, not a straight assignment: immediately after
    // innerHTML the new content has no layout yet, so scrollTop is clamped to
    // whatever height exists at that instant -- usually zero. That is why
    // stepping to the next recipient jumped back to the top.
    requestAnimationFrame(() => {
      const b2 = document.getElementById("emailBody");
      const e2 = document.querySelector(".email-editor");
      if (b2 && keepScroll.body) b2.scrollTop = keepScroll.body;
      if (e2 && keepScroll.editor) e2.scrollTop = keepScroll.editor;
    });
    wireAutoSave();
    markOpened();
    wireCommonApply();
    wireCommonLint();
    wireSwipe();
    announceSwipe();
    startCountdown();
    schedulePoll();
  }

  // Which side the last swipe came from, consumed once by announceSwipe(). Null
  // for every other kind of navigation -- tapping the arrows or picking from the
  // jump list is already an explicit act and needs no confirmation of itself.
  let swipeFrom = null;

  function announceSwipe() {
    if (!swipeFrom) return;
    const side = swipeFrom; swipeFrom = null;
    const host = document.getElementById("emailBody");
    const m = detail.messages[cursor];
    if (!host) return;
    host.classList.remove("email-swipe-left", "email-swipe-right");
    // Forces a reflow so the animation restarts on a repeat swipe in the same
    // direction; without it the class is already present and nothing replays.
    void host.offsetWidth;
    host.classList.add(`email-swipe-${side}`);
    const toast = document.createElement("div");
    toast.className = "email-swipe-toast";
    toast.setAttribute("role", "status");          // announced by VoiceOver too
    toast.innerHTML = `${esc(m.recipientName || m.recipientEmail)}`
      + `<small>${cursor + 1} of ${detail.messages.length}</small>`;
    host.appendChild(toast);
    setTimeout(() => toast.remove(), 1000);
  }

  // Desktop opens the list; a phone does not. On a wide screen it is a sidebar
  // that costs nothing and saves a click. On a phone it is a full-height list
  // pushing the actual editor below the fold, which is what it was collapsed
  // for in the first place.
  function jumpOpen() {
    return global.matchMedia && global.matchMedia("(min-width: 761px)").matches;
  }

  // While a send is inside its cancellation window the batch is simply "working"
  // on screen, which is the moment a rep most needs to know they can still stop
  // it. Count it down visibly, once a second, updating only the banner text so
  // the rest of the view does not flicker.
  function startCountdown() {
    clearInterval(tickTimer);
    const until = detail && detail.batch && detail.batch.sendNotBeforeUtc;
    if (!until) return;
    const target = new Date(until).getTime();
    if (!target || target <= Date.now()) return;
    if (!document.getElementById("emailNotice")) return;
    const paint = () => {
      // Re-read the element each tick. Holding the original reference kept the
      // ticker writing into a node that had been replaced, so a cancelled batch
      // still looked like it was counting down to a send.
      const host = document.getElementById("emailNotice");
      if (!host || !detail || !detail.batch.sendNotBeforeUtc) { clearInterval(tickTimer); return; }
      const left = Math.ceil((target - Date.now()) / 1000);
      if (left <= 0) {
        clearInterval(tickTimer);
        host.innerHTML = `<span class="email-countdown gone">Sending now — the window has closed.</span>`;
        return;
      }
      host.innerHTML = `<span class="email-countdown"><b>${left}s</b> to cancel before
        ${detail.batch.recipientCount} email${detail.batch.recipientCount === 1 ? "" : "s"} send.
        Use <b>Cancel remaining</b> below to stop. The batch is stored on the server;
        you may close this window and sending will continue.</span>`;
    };
    paint();
    tickTimer = setInterval(paint, 1000);
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    if (!detail || ["editing", "invalid", "drafts_ready", "completed", "partial_failure", "canceled", "action_required"].includes(detail.batch.status)) return;
    pollTimer = setTimeout(async () => {
      try { detail = await api(`batch&id=${encodeURIComponent(detail.batch.id)}`, null, "GET"); composerView(); }
      catch { schedulePoll(); }
    }, 3000);
  }

  async function open(selected) {
    excluded = new Set(); openDomain = null;
    /* A WHITELIST, and it silently ate a feature.
     *
     * This rebuilds each recipient into a known shape rather than forwarding
     * whatever the caller handed over -- right instinct, since the map's row
     * objects carry plenty the emailer has no business posting. But teammates
     * and teammatesFull were never added to it, so the per-message "copy
     * someone on their team" picker had nothing to offer: the map computed all
     * eleven of Regina Stuzin's teammates, and three lines later they were
     * gone.
     *
     * The picker hides itself when the list is empty, so the feature simply
     * never appeared and nothing anywhere reported a problem.
     *
     * Same failure as the Graph field whitelist in email-store.js: a hand-kept
     * list of names, in a language with no compiler to notice the omission.
     * Anything added to AdvisorEmailData has to be added HERE too.
     */
    // Existing saved queues can predate the UI-level review guard. Refuse those
    // snapshots here; the server independently resolves every remaining CRD.
    const selectionSummary = selected && selected.eligibilitySummary;
    const identityBlocked = (selected || []).filter((r) => r && r.unconfirmed).length;
    recipients = (selected || []).filter((r) => r && !r.unconfirmed).map((r) => ({ contactId: r.contactId || r.crd || "", name: r.name || "",
      email: r.email || "", firm: r.firm || r.companyName || "", firstName: r.firstName || "", lastName: r.lastName || "",
      unconfirmed: !!r.unconfirmed,
      contactTier: r.contactTier || "",
      contactSource: r.contactSource || "",
      identityLabel: r.identityLabel || (global.Dial
        ? global.Dial.identityTierLabel(r.contactTier, r.contactSource) : ""),
      teammates: Array.isArray(r.teammates) ? r.teammates : [],
      teammatesFull: Array.isArray(r.teammatesFull) ? r.teammatesFull : [] }));
    if (selectionSummary && selectionSummary.excluded)
      global.alert(`${selectionSummary.included} of ${selectionSummary.selected} saved contacts can use the controlled composer. `
        + `${selectionSummary.excluded} were excluded by identity, source, or stale-data checks.`);
    else if (identityBlocked)
      global.alert(`${identityBlocked} unconfirmed contact${identityBlocked === 1 ? "" : "s"} `
        + `cannot use the controlled composer until the CRD link is resolved.`);
    if (!recipients.length) return;
    detail = null; cursor = 0; forceDetail = false; clearTimeout(pollTimer); clearInterval(tickTimer);
    const back = shell(); back.hidden = false;
    document.getElementById("emailTitle").textContent = "Prepare email";
    document.getElementById("emailBody").innerHTML = `<p class="email-loading">Loading Microsoft 365 email tools…</p>`;
    try { await loadCatalog(); if (!catalog.connection.connected) connectView(); else setupView(); }
    catch (e) { connectView(e.message); }
  }


  /* THE FOLLOW-UP SCREEN.
   *
   * Shows the arithmetic before it shows the compose box, because the number
   * that matters is "22 of 25 never answered" and the rep should see how it was
   * arrived at -- including who came OFF the list and why. A screen that just
   * said "22 recipients" would be asking them to trust a filter they cannot
   * inspect.
   */
  let followUp = null;

  async function openFollowUp(batchId){
    const back = shell(); back.hidden = false;
    document.getElementById("emailTitle").textContent = "Follow up";
    document.getElementById("emailBody").innerHTML = `<p class="email-loading">Working out who never replied…</p>`;
    try {
      followUp = await api(`follow_up_candidates&id=${encodeURIComponent(batchId)}`, null, "GET");
      followUpView();
    } catch (e) { document.getElementById("emailBody").innerHTML =
      `<p class="email-notice bad">${esc(e.message)}</p>`; }
  }

  function followUpView(){
    const f = followUp, c = f.counts;
    const off = [
      c.replied ? `${c.replied} replied` : "",
      c.bounced ? `${c.bounced} bounced` : "",
      c.suppressed ? `${c.suppressed} opted out since` : "",
      c.notSent ? `${c.notSent} never sent` : "",
    ].filter(Boolean);
    document.getElementById("emailBody").innerHTML = `<div class="email-setup">
      <p class="email-summary"><b>${c.remaining}</b> of ${c.sent} never replied${
        off.length ? ` &middot; ${esc(off.join(", "))}` : ""}</p>
      ${c.remaining ? "" : `<p class="email-notice">There is nobody to follow up.</p>`}
      <label class="email-label">What to say
        <textarea id="followUpText" rows="3" maxlength="2000">${esc(FOLLOW_UP_DEFAULT)}</textarea></label>
      <p class="email-fine">Sent as a reply on the original thread, so it arrives under
        the email they already have, with it quoted below. It carries its own
        unsubscribe link, because it is outreach we started.</p>
      <label class="email-check">
        <input type="checkbox" id="followUpAttach">
        <span>Attach the original documents again</span></label>
      <p class="email-fine">Off by default: re-sending a document people already
        have is a common way into a spam folder. Turn it on when the document is
        the reason you are writing.</p>
      ${off.length ? `<details class="email-mates"><summary>Who came off the list</summary>
        <div class="email-mates-list">${
          [["replied", "Replied"], ["bounced", "Bounced"], ["suppressed", "Opted out"]]
            .map(([k, label]) => (f[k] || []).length
              ? `<p class="email-fine"><b>${label}:</b> ${(f[k] || []).map((x) => esc(x.name || x.email)).join(", ")}</p>`
              : "").join("")}</div></details>` : ""}
      <footer class="email-footer">${scheduleHtml}<p id="emailNotice" class="email-notice"></p><div>
        <button type="button" class="ask-btn ghost" data-email="close">Close</button>
        <button type="button" class="ask-btn primary" data-email="follow-up-create"
          ${c.remaining ? "" : "disabled"}>Prepare ${c.remaining} follow-up${c.remaining === 1 ? "" : "s"}</button>
      </div></footer>
      </div>`;
  }

  const FOLLOW_UP_DEFAULT = "Just following up on the note below in case it reached you at a busy moment.";

  function batchScheduleText(batch) {
    const raw = batch.scheduledForUtc || (["held", "needs_review"].includes(batch.status) ? batch.sendNotBeforeUtc : "");
    const date = raw && new Date(raw);
    return date && !Number.isNaN(date.getTime()) ? `Scheduled for ${easternLabel(date)}` : "";
  }

  function emailUrl(batchId, connected) {
    const url = new URL(location.href);
    for (const key of ["email", "message"]) url.searchParams.delete(key);
    if (batchId) url.searchParams.set("emailBatch", batchId); else url.searchParams.delete("emailBatch");
    if (connected) url.searchParams.set("email", "connected");
    return `${url.pathname}${url.search}${url.hash}`;
  }

  function cleanEmailUrl() { history.replaceState(null, "", emailUrl("", false)); }

  async function openBatchById(id, pushUrl) {
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(String(id || ""))) {
      const back = shell(); back.hidden = false;
      document.getElementById("emailTitle").textContent = "Email batch unavailable";
      document.getElementById("emailBody").innerHTML = `<div class="email-history-error"><p>This email batch is unavailable.</p><button type="button" class="ask-btn primary" data-email="history">Open email activity</button></div>`;
      return;
    }
    const back = shell(); back.hidden = false;
    document.getElementById("emailTitle").textContent = "Loading email batch…";
    document.getElementById("emailBody").innerHTML = "";
    try {
      await loadCatalog(); detail = await api(`batch&id=${encodeURIComponent(id)}`, null, "GET");
      cursor = 0; forceDetail = false;
      if (pushUrl && new URL(location.href).searchParams.get("emailBatch") !== id)
        history.pushState({ emailBatch: id }, "", emailUrl(id, false));
      composerView();
    } catch (_) {
      document.getElementById("emailTitle").textContent = "Email batch unavailable";
      document.getElementById("emailBody").innerHTML = `<div class="email-history-error"><p>This email batch is unavailable.</p><button type="button" class="ask-btn primary" data-email="history">Open email activity</button></div>`;
    }
  }

  async function openHistory() {
    const back = shell(); back.hidden = false;
    document.getElementById("emailTitle").textContent = "Email activity";
    try {
      const data = await api("batches", null, "GET");
      document.getElementById("emailBody").innerHTML = `<div class="email-history">${data.batches.length ? data.batches.map((b) =>
        `<button type="button" data-email="open-batch" data-id="${esc(b.id)}"><b>${esc(b.name)}</b><span>${b.recipientCount} recipients · ${esc(stateLabel(b.status))}</span><small>${esc(batchScheduleText(b) || b.createdUtc || "")}</small></button>`).join("") : "<p>No email batches yet.</p>"}</div>`;
    } catch (e) { document.getElementById("emailBody").innerHTML = `<p class="email-error">${esc(e.message)}</p>`; }
  }

  async function act(button) {
    const action = button.dataset.email;
    if (action === "close") { shell().hidden = true; clearTimeout(pollTimer); clearInterval(tickTimer); forceDetail = false; cleanEmailUrl(); return; }
    if (action === "details") { forceDetail = true; composerView(); return; }
    // ---- recipient domain grouping ----
    // Every one of these re-renders the setup screen, which would otherwise
    // discard the template and attachment choices made above it. Capture and
    // restore them rather than moving the picker somewhere it fits worse.
    if (["dom-toggle", "dom-open", "dom-all", "dom-none", "person-toggle"].includes(action)) {
      const tplEl = document.getElementById("emailTemplate");
      const keepTemplate = tplEl ? tplEl.value : "";
      const keepDocs = [...document.querySelectorAll("#emailDocs input:checked")].map((x) => x.value);
      const keepFamilies = [...document.querySelectorAll(".email-family:checked")].map((x) => x.value);
      const addrIn = (domain) => recipients.filter((r) => domainOf(r) === domain)
        .map((r) => String(r.email || "").toLowerCase());

      if (action === "dom-open") {
        openDomain = openDomain === button.dataset.domain ? null : button.dataset.domain;
      } else if (action === "dom-all") {
        excluded = new Set();
      } else if (action === "dom-none") {
        excluded = new Set(recipients.map((r) => String(r.email || "").toLowerCase()));
      } else if (action === "dom-toggle") {
        const list = addrIn(button.dataset.domain);
        // Mixed goes to fully-included on the next click, not fully-excluded --
        // the rep is reaching for "all of these", and losing the ones already
        // ticked would be a surprising way to answer that.
        const anyKept = list.some((a) => !excluded.has(a));
        const allKept = list.every((a) => !excluded.has(a));
        if (allKept) list.forEach((a) => excluded.add(a));
        else if (anyKept) list.forEach((a) => excluded.delete(a));
        else list.forEach((a) => excluded.delete(a));
      } else {
        const addr = button.dataset.address;
        if (excluded.has(addr)) excluded.delete(addr); else excluded.add(addr);
      }

      /* Ticking a domain re-renders the whole setup view, which puts the
       * scroll position back to the top -- so a rep working down a list of
       * fifteen domains loses their place on every click and has to find it
       * again. Captured before the re-render and restored after, on whichever
       * ancestor is actually doing the scrolling. */
      const scroller = scrollParent(button);
      const wasAt = scroller ? scroller.scrollTop : 0;
      setupView();
      if (scroller) scroller.scrollTop = wasAt;
      const tplBack = document.getElementById("emailTemplate");
      if (tplBack && keepTemplate) tplBack.value = keepTemplate;
      for (const box of document.querySelectorAll("#emailDocs input"))
        if (!box.disabled) box.checked = keepDocs.includes(box.value);
      for (const box of document.querySelectorAll(".email-family")) box.checked = keepFamilies.includes(box.value);
      const notes = document.getElementById("emailTemplate");
      if (notes) notes.dispatchEvent(new Event("change"));
      return;
    }
    if (action === "mates-open") {
      // `open` still holds the PRE-click value here; the browser flips it after.
      const details = button.parentElement;
      if (details && details.open) matesOpen.delete(button.dataset.id);
      else matesOpen.add(button.dataset.id);
      return;
    }
    if (action === "mate-toggle") {
      const m = detail.messages[cursor];
      // They are picking, so it stays open through the re-render.
      matesOpen.add(m.id);
      const on = new Set((m.teammateCc || []).map((a) => String(a).toLowerCase()));
      const a = button.dataset.address;
      if (button.checked) on.add(a); else on.delete(a);
      button.disabled = true;
      try {
        const r = await api("update_message_cc",
          { batchId: detail.batch.id, messageId: m.id, teammates: [...on] });
        detail = r;
        composerView();
        if (r.ccRemovedRecipients && r.ccRemovedRecipients.length)
          notice(`${r.ccRemovedRecipients.join(", ")} ${
            r.ccRemovedRecipients.length === 1 ? "is" : "are"} now copied here `
            + `instead of receiving their own email.`);
        else if (r.ccSuppressed && r.ccSuppressed.length)
          notice(`${r.ccSuppressed.join(", ")} could not be copied — unsubscribed.`, true);
      } catch (e) { button.disabled = false; notice(e.message, true); }
      return;
    }
    if (action === "docs") { clearTimeout(pollTimer); return docsView(); }
    if (action === "health") { clearTimeout(pollTimer); return openHealth(); }
    if (action === "health-days") { healthDays = Number(button.dataset.days) || 90; return openHealth(); }
    if (action === "templates") { clearTimeout(pollTimer); return templatesView(); }
    if (action === "tpl-new") {
      editing = { id: "", name: "", subject: "", bodyText: "", requiredDocumentIds: [], images: [] };
      savedSnapshot = null;          // a new template is unsaved by definition
      return templateEditView();
    }
    if (action === "tpl-edit") {
      editing = (catalog.templates || []).find((x) => x.id === button.dataset.id);
      savedSnapshot = editing ? templateFingerprint(editing) : null;
      return editing ? templateEditView() : templatesView("That template could not be loaded.", true);
    }
    /* Send this template to yourself.
     *
     * The people who manage templates and approved PDFs are not advisors, so
     * they have no CRD and cannot be picked as recipients anywhere in the app.
     * Without this the only way for a template author to see their own work
     * arrive was to borrow a real advisor from a list and mail them -- which is
     * exactly the accident every other control here exists to prevent.
     *
     * The recipient is the connected mailbox itself, taken from the server's
     * catalog rather than typed, so this cannot be aimed at anyone else. It is
     * an ordinary batch of one: same rendering, same attachments, same
     * signature, same guardrails.
     */
    if (action === "tpl-test") {
      const mailbox = catalog.connection && catalog.connection.mailbox;
      if (!mailbox) return notice("Connect your Microsoft 365 mailbox first.", true);
      const name = button.dataset.name || "this template";
      if (!confirm(`Send "${name}" to ${mailbox}?

`
        + `A one-person batch addressed to you, with the template's required `
        + `attachments and charts exactly as a rep would send it.`)) return;
      button.disabled = true;
      try {
        const me = (catalog.connection.profile || {});
        detail = await api("create_batch", {
          name: `Test — ${name}`,
          templateId: button.dataset.id,
          attachmentIds: [],
          recipients: [{
            contactId: "",
            email: mailbox,
            name: me.displayName || mailbox,
            firstName: (me.givenName || String(me.displayName || "").split(" ")[0] || "there"),
            lastName: me.surname || "",
            companyName: "Equity Investment Corporation",
          }],
        });
        editing = null;
        cursor = 0;
        composerView();
      } catch (e) { button.disabled = false; notice(e.message, true); }
      return;
    }
    if (action === "tpl-back") { editing = null; return templatesView(); }
    if (action === "tpl-publish") {
      // The checkbox has already flipped by the time a click reaches here, so
      // .checked is the state being asked for, not the one being left.
      const wanted = button.checked === true;
      const name = button.dataset.name || button.dataset.id;
      try {
        const r = await api("publish_template", { id: button.dataset.id, published: wanted });
        catalog.templates = r.templates || catalog.templates || [];
        templatesView(wanted
          ? `"${name}" is live. The sales team can now choose it.`
          : `"${name}" is held. It stays in this library and the team cannot see it.`);
      } catch (e) {
        // Put the checkbox back: leaving it showing a state the server refused
        // is how an administrator comes to believe a template is live when it
        // is not.
        button.checked = !wanted;
        templatesView(e.message, true);
      }
      return;
    }
    if (action === "tpl-delete") {
      const name = button.dataset.name || button.dataset.id;
      if (!confirm(`Remove the template "${name}"?\n\nReps will no longer be able to choose it. Batches already created are unaffected.`)) return;
      try { const r = await api("delete_template", { id: button.dataset.id }); catalog.templates = r.templates; templatesView(`Removed ${name}.`); }
      catch (e) { templatesView(e.message, true); }
      return;
    }
    if (action === "tpl-save") {
      button.disabled = true;
      const draft = collectTemplate();
      try {
        const r = await api("put_template", draft);
        catalog.templates = r.templates || catalog.templates || [];
        editing = (r.templates || []).find((x) => x.id === (r.saved || {}).id) || draft;
        savedSnapshot = templateFingerprint(editing);
        templateEditView(`Saved as version ${r.saved.version}.`);
      } catch (e) {
        button.disabled = false;
        // Redraw from the DRAFT, not from `editing`. templateEditView renders
        // whatever `editing` holds, and for a template being created that is an
        // empty object -- so any rejection, a lint error included, silently threw
        // away everything the administrator had typed. Their work survives the
        // error now, and the message tells them what to change.
        editing = draft;
        templateEditView(e.message, true);
      }
      return;
    }
    if (action === "tpl-img-insert") {
      // Put the placeholder where the cursor is, rather than making an
      // administrator hand-type a token that must match exactly.
      const box = document.getElementById("tplBody");
      const token = `{{image:${button.dataset.id}}}`;
      const at = box.selectionStart == null ? box.value.length : box.selectionStart;
      box.value = `${box.value.slice(0, at)}${token}${box.value.slice(at)}`;
      box.focus();
      box.selectionStart = box.selectionEnd = at + token.length;
      runLint();
      return;
    }
    if (action === "tpl-img-delete") {
      try {
        const r = await api("delete_template_image", { templateId: editing.id, imageId: button.dataset.id });
        catalog.templates = r.templates;
        editing = r.templates.find((x) => x.id === editing.id) || editing;
        templateEditView("Chart removed.");
      } catch (e) { templateEditView(e.message, true); }
      return;
    }
    if (action === "tpl-img-upload") {
      const input = document.getElementById("tplImgFile");
      const file = input && input.files && input.files[0];
      const name = (document.getElementById("tplImgName").value || "").trim();
      if (!file) return templateEditView("Choose an image file.", true);
      if (!name) return templateEditView("Give the chart a name — it becomes its placeholder id.", true);
      button.disabled = true;
      try {
        const dataBase64 = await readAsBase64(file);
        const r = await api("put_template_image", { templateId: editing.id, imageId: name, name, dataBase64 });
        catalog.templates = r.templates;
        editing = r.templates.find((x) => x.id === editing.id) || editing;
        templateEditView(`Added ${name}. Use ${r.saved.placeholder} in the body.`);
      } catch (e) { button.disabled = false; templateEditView(e.message, true); }
      return;
    }
    // Both admin list views use docs-back. Opened from Settings there is no
    // batch behind them, so falling through to setupView() would show a
    // recipient list that does not exist -- go back where we came from instead.
    if (action === "docs-back" && !detail) {
      shell().hidden = true;
      if (adminReturn) { const back = adminReturn; adminReturn = null; back(); }
      return;
    }
    if (action === "docs-back") { return detail ? composerView() : setupView(); }

    if (action === "material-queue-remove") {
      materialQueue.splice(Number(button.dataset.index), 1); return docsView();
    }
    if (action === "materials-upload") {
      const candidates = materialQueue.filter((row) => !["done", "invalid"].includes(row.status));
      const existing = (catalog && catalog.documents) || [], seenIds = new Set(), seenTuples = new Set(), seenNames = new Set();
      for (const row of candidates) {
        row.logicalId = uploadLogicalId(row);
        const tuple = uploadTuple(row), fileName = String(row.file.name || "").toLowerCase();
        const conflict = existing.some((d) => d.id === row.logicalId || uploadTuple(d) === tuple
          || String(d.fileName || "").toLowerCase() === fileName)
          || seenIds.has(row.logicalId) || seenTuples.has(tuple) || seenNames.has(fileName);
        if (conflict) {
          row.status = "duplicate"; row.duplicate = "Already published or queued for this family, version, and period. Remove it and use Replace PDF when it supersedes an existing version.";
        } else {
          row.status = "ready"; row.error = ""; row.duplicate = "";
          seenIds.add(row.logicalId); seenTuples.add(tuple); seenNames.add(fileName);
        }
      }
      const pending = candidates.filter((row) => row.status === "ready");
      if (!pending.length) return docsView("Nothing was published. Remove duplicate rows or use Replace PDF on the existing material.", true);
      button.disabled = true;
      let next = 0;
      const worker = async () => {
        while (next < pending.length) {
          const row = pending[next++];
          if (!row.name || !row.familyId) { row.status = "error"; row.error = "Display name and family are required."; continue; }
          row.status = "uploading"; docsView();
          try {
            const dataBase64 = await readAsBase64(row.file);
            const result = await api("put_document", { id: row.logicalId,
              name: row.name, fileName: row.file.name, dataBase64, familyId: row.familyId,
              category: row.category, channel: row.channel, periodKind: row.periodKind,
              periodKey: row.periodKey, asOfDate: row.asOfDate, freshness: "current" });
            catalog.documents = result.documents || catalog.documents;
            row.status = "done";
          } catch (error) { row.status = "error"; row.error = error.message; }
          docsView();
        }
      };
      await Promise.all([worker(), worker()]);
      try { await loadCatalog(); }
      catch (error) { return docsView("Publishing finished, but the refreshed library could not be loaded: " + error.message, true); }
      return docsView(pending.some((row) => row.status === "error")
        ? "Some PDFs could not be published. Correct the marked rows and try those again."
        : "Published " + pending.length + " approved PDF" + (pending.length === 1 ? "." : "s."),
        pending.some((row) => row.status === "error"));
    }
    if (action === "material-save") {
      const box = button.closest(".email-material-version");
      const value = (name) => ((box.querySelector('[data-material-field="' + name + '"]') || {}).value || "").trim();
      button.disabled = true;
      try {
        const result = await api("update_document", { id: button.dataset.id, name: value("name"),
          familyId: value("familyId"), category: value("category"), channel: value("channel"),
          periodKind: value("periodKind"), periodKey: value("periodKey"), asOfDate: value("asOfDate"),
          freshness: value("freshness") });
        catalog.documents = result.documents || catalog.documents;
        return docsView("Material details saved.");
      } catch (error) { button.disabled = false; return docsView(error.message, true); }
    }
    if (action === "route-add") {
      const set = catalog.materialRoutes || (catalog.materialRoutes = { rules: [] });
      set.rules.push({ domain: "", channel: "ubs", source: "manual",
        evidenceCount: 0, status: "active", disabled: false }); return docsView();
    }
    if (action === "route-remove") {
      const rules = (((catalog || {}).materialRoutes || {}).rules || []);
      const row = rules.find((r) => r.domain === button.dataset.domain);
      if (row) { row.disabled = true; row.status = "disabled"; }
      return docsView("Route removed locally. Save routing rules to publish the change.");
    }
    if (action === "route-restore") {
      const rules = (((catalog || {}).materialRoutes || {}).rules || []);
      const row = rules.find((r) => r.domain === button.dataset.domain);
      if (row) { row.disabled = false; row.status = "active"; }
      return docsView("Route restored. Save routing rules to publish it.");
    }
    if (action === "routes-save") {
      const routePolicy = (catalog && catalog.materialRoutes) || {};
      const routeRows = (routePolicy.rules || []).map((row) => ({
        domain: String(row.domain || "").trim().toLowerCase(),
        channel: row.channel || "ubs",
        source: row.source,
        evidenceCount: row.evidenceCount,
        status: row.status,
        disabled: row.disabled === true,
      })).filter((row) => row.domain);
      button.disabled = true;
      try {
        const result = await api("put_material_routes", { rules: routeRows,
          seedVersion: routePolicy.seedVersion,
          etag: routePolicy.etag || "" });
        catalog.materialRoutes = result.materialRoutes;

        return docsView("Recipient routing rules saved.");
      } catch (error) { button.disabled = false; return docsView(error.message, true); }
    }
    if (action === "doc-delete") {
      const name = button.dataset.name || button.dataset.id;
      if (!confirm(`Remove "${name}" from the approved catalog?\n\nReps will no longer be able to attach it. Batches already carrying it will fail validation.`)) return;
      button.disabled = true;
      try { const r = await api("delete_document", { id: button.dataset.id }); catalog.documents = r.documents; docsView(`Removed ${name}.`); }
      catch (e) { docsView(e.message, true); }
      return;
    }
    if (action === "doc-replace") {
      replacing = { id: button.dataset.id, name: button.dataset.name };
      docsView();
      const nameBox = document.getElementById("docName");
      if (nameBox) nameBox.value = replacing.name;
      const file = document.getElementById("docFile");
      if (file) { file.focus(); file.scrollIntoView({ block: "center" }); }
      return;
    }
    if (action === "doc-replace-cancel") { replacing = null; return docsView(); }
    if (action === "doc-upload") {
      const input = document.getElementById("docFile");
      const file = input && input.files && input.files[0];
      const name = (document.getElementById("docName").value || "").trim();
      if (!file) return docsView("Choose a PDF to publish.", true);
      if (!name) return docsView("Give the document a display name.", true);
      // Checked here for a fast, clear message; the server checks the actual
      // bytes, which is the check that counts -- a renamed file passes this one.
      if (!/\.pdf$/i.test(file.name) && file.type !== "application/pdf")
        return docsView("Only PDF files can be approved as attachments.", true);
      button.disabled = true;
      try {
        const dataBase64 = await readAsBase64(file);
        // The ID is what keeps a replacement in place. Taken from the row being
        // replaced, NOT from the display name -- an administrator who edits the
        // name while replacing would otherwise publish a second document and
        // leave the template pointing at the old one.
        const target = replacing ? replacing.id : name;
        // The name on disk, sent as well as the display name. The advisor gets
        // the file called what it is actually called; the display name stays a
        // label for the picker in this app and nothing more.
        const r = await api("put_document", { id: target, name, fileName: file.name, dataBase64 });
        catalog.documents = r.documents;
        const saved = r.saved;
        const was = replacing;
        replacing = null;
        docsView(was
          ? `Replaced ${esc(was.name)} — now version ${saved.version}. Templates requiring it are unchanged.`
          : `Published ${saved.name} as version ${saved.version}.`);
      } catch (e) { docsView(e.message, true); }
      return;
    }
    /* Awaited, because the FIELD app resolves teammates from a practice file it
     * may still have to fetch. The desk returns a plain array and awaiting that
     * costs nothing -- one contract, both apps. */
    if (action === "open-list") {
      return open(global.AdvisorEmailData ? await global.AdvisorEmailData.list() : []);
    }
    if (action === "history") { cleanEmailUrl(); return openHistory(); }
    if (action === "follow-up-open") { return openFollowUp(button.dataset.id); }
    if (action === "follow-up-create") {
      button.disabled = true; notice("Preparing the follow-up…");
      try {
        detail = await api("create_follow_up", {
          batchId: followUp.batchId,
          text: (document.getElementById("followUpText") || {}).value || "",
          includeAttachments: !!(document.getElementById("followUpAttach") || {}).checked,
        });
        cursor = 0; composerView();
      } catch (e) { button.disabled = false; notice(e.message, true); }
      return;
    }
    if (action === "connect") {
      button.disabled = true;
      try { const batchId = new URL(location.href).searchParams.get("emailBatch") || (detail && detail.batch && detail.batch.id) || ""; const r = await api("connect", { returnTo: emailUrl(batchId, true) }); location.assign(r.authorizeUrl); }
      catch (e) { connectView(e.message); }
      return;
    }
    if (action === "create") {
      const kept = keptRecipients();
      if (!kept.length) return notice("Every recipient is excluded. Include at least one.", true);
      // Confirmed by domain, not by count. "Send to 52 people?" is answered yes
      // without reading; "morganstanley.com 52, rjf.com 8" is the moment the
      // stray firm gets caught, which is the entire point of the grouping.
      const groups = domainGroups().filter((g) => g.kept);
      if (groups.length > 1 && !confirm(`This batch spans ${groups.length} email domains:

`
        + groups.map((g) => `  ${g.domain} — ${g.kept}`).join("\n")
        + `

Generate emails for all of them?`)) return;
      button.disabled = true; notice("Generating one email per recipient…");
      try {
        // Includes the disabled ones: :checked is independent of :disabled, and
        // the required documents are checked-and-disabled by design.
        const attachmentIds = [...document.querySelectorAll("#emailDocs input:checked")].map((x) => x.value);
        const materialFamilyIds = [...document.querySelectorAll(".email-family:checked")].map((x) => x.value);

        const ccColleague = ((document.getElementById("ccColleague") || {}).value || "").trim();
        const followUpDays = Number((document.getElementById("followUpDays") || {}).value || 0);
        sendTiming = "now"; scheduleDate = ""; scheduleTime = "09:00";
        detail = await api("create_batch", { recipients: kept,
          templateId: (document.getElementById("emailTemplate") || {}).value || "",
          attachmentIds, materialFamilyIds, ccColleague, followUpDays });
        composerView();
      } catch (e) { button.disabled = false; notice(e.message, true); }
      return;
    }
    if (action === "pick") {
      if (!await saveOne()) return;
      cursor = Number(button.dataset.index) || 0; composerView(); return;
    }
    if (action === "step") {
      if (!await saveOne()) return;
      const next = cursor + (Number(button.dataset.by) || 0);
      if (next >= 0 && next < detail.messages.length) { cursor = next; composerView(); }
      return;
    }
    if (action === "drop-recipient") {
      const name = button.dataset.name;
      if (!confirm(`Remove ${name} from this batch?

They stay on your call list and keep their history — this only takes them out of this email.`)) return;
      if (!await saveOne()) return;
      try {
        detail = await api("remove_recipient", { batchId: detail.batch.id, messageId: button.dataset.id });
        cursor = Math.min(cursor, detail.messages.length - 1);
        composerView();
      } catch (e) { notice(e.message, true); }
      return;
    }
    if (action === "next-failed") {
      // Walks the messages that actually failed to send, wrapping, so pressing
      // it repeatedly steps through them instead of sticking on the first.
      forceDetail = true;
      const n = detail.messages.length;
      for (let step = 1; step <= n; step++) {
        const i = (cursor + step) % n;
        if (detail.messages[i].failureMessage) { cursor = i; composerView(); return; }
      }
      composerView();
      return;
    }
    if (action === "next-behind") {
      if (!await saveOne()) return;
      const n = detail.messages.length;
      for (let step = 1; step <= n; step++) {
        const i = (cursor + step) % n;
        if ((detail.messages[i].validation && detail.messages[i].validation.warnings || [])
            .some((w) => w.code === "behind_common_text")) { cursor = i; composerView(); return; }
      }
      return;
    }
    if (action === "next-problem") {
      if (!await saveOne()) return;
      // Wrap from wherever they are, so pressing it repeatedly walks the
      // blocked messages rather than sticking on the first one.
      const n = detail.messages.length;
      for (let step = 1; step <= n; step++) {
        const i = (cursor + step) % n;
        if ((detail.messages[i].validation && detail.messages[i].validation.errors || []).length) {
          cursor = i; composerView(); return;
        }
      }
      return;
    }
    /* Back to the wording compliance approved.
     *
     * The safety net that makes the rest of this bearable. A rep who is nervous
     * about touching the common text is a rep who works around the tool; one
     * click back to the published template makes experimenting cheap, which is
     * usually better than making it hard.
     *
     * The text comes from the SERVER's copy of the template, not from anything
     * the page is holding, so "approved" means approved.
     */
    if (action === "restore-common") {
      const approved = detail.approvedText;
      if (!approved) return notice("The approved wording for this template could not be loaded.", true);
      if (!confirm("Replace the common subject and body with the approved template wording?"
        + String.fromCharCode(10, 10)
        + "Emails you edited individually keep their own text.")) return;
      try {
        notice("Restoring…");
        detail = await api("update_common", { batchId: detail.batch.id,
          subject: approved.subject, bodyText: approved.bodyText });
        composerView();
        notice("Restored to the approved wording.");
      } catch (e) { notice(e.message, true); }
      return;
    }
    /* Force the common wording onto everybody, individual edits included.
     *
     * Destructive and irreversible, so it is confirmed by COUNT and by name --
     * "overwrite 2 emails" is answered yes reflexively, while seeing whose
     * wording is about to be discarded is not.
     */
    if (action === "apply-all") {
      const own = detail.messages.filter((x) => x.subjectOverridden || x.bodyOverridden);
      const names = own.map((x) => x.recipientName || x.recipientEmail);
      if (!own.length) return;
      if (!confirm(`Replace the wording on all ${detail.messages.length} emails?`
        + String.fromCharCode(10, 10)
        + `${own.length} email${own.length === 1 ? " was" : "s were"} edited individually and `
        + `will LOSE that wording: ${names.slice(0, 6).join(", ")}`
        + `${names.length > 6 ? `, and ${names.length - 6} more` : ""}.`
        + String.fromCharCode(10, 10) + `This cannot be undone.`)) return;
      try {
        notice("Applying to everybody…");
        detail = await api("update_common", { batchId: detail.batch.id, overwriteAll: true,
          subject: document.getElementById("emailCommonSubject").value,
          bodyText: document.getElementById("emailCommonBody").value });
        composerView();
        notice(`Applied to all ${detail.messages.length}. ${own.length} individual `
          + `edit${own.length === 1 ? " was" : "s were"} replaced.`);
      } catch (e) { notice(e.message, true); }
      return;
    }
    if (action === "save-common") {
      try {
        notice("Applying common edits…");
        detail = await api("update_common", { batchId: detail.batch.id,
          subject: document.getElementById("emailCommonSubject").value,
          bodyText: document.getElementById("emailCommonBody").value });
        composerView();
        // Naming them is the point. "Applies to non-overridden emails" is true
        // and unreadable; "Chen, Whitfield and Okafor kept their own wording"
        // is the same fact in a form a rep will act on.
        // A deletion is the mistake a spell-checker cannot see, and the likelier
        // one: tidy up the greeting, lose {{first_name}}, and every email in the
        // batch now opens "Hi ,".
        const gone = detail.removedTokens || [];
        if (gone.length) {
          notice(`Heads up — your edit removed ${gone.map((t) => `{{${t}}}`).join(", ")}`
            + ` from the approved wording. That is fine if you meant it.`, true);
        }
        const kept = detail.keptOwnText || [];
        if (kept.length) notice(`${kept.length} kept their own wording and did NOT get this change: `
          + kept.slice(0, 3).map((k) => k.name).join(", ") + (kept.length > 3 ? `, +${kept.length - 3} more` : ""), true);
      } catch (e) { notice(e.message, true); } return;
    }
    if (action === "reset-one") {
      const m = detail.messages[cursor];
      try { detail = await api("update_message", { batchId: detail.batch.id, messageId: m.id,
        resetSubject: true, resetBody: true, reviewed: false }); composerView(); }
      catch (e) { notice(e.message, true); } return;
    }
    // "Check batch" was removed. It re-ran exactly the validation that approval
    // runs -- approval refuses on any error -- so it could not catch anything,
    // and every edit already re-validates on its way back from the server. Two
    // buttons in this footer also fit a phone; three did not.
    //
    // The server op remains, reachable as ?op=validate, for diagnosing a batch
    // that looks stuck without having to approve it to find out why.
    if (action === "approve-drafts" || action === "approve-send") {
      // Unapplied common edits are the one way to send text nobody meant to
      // send: the box shows the new wording, the messages carry the old, and
      // the preview agrees with the messages. Refused rather than warned about,
      // because a warning at this point is one more thing to click past.
      if (commonDirty()) {
        return notice("You have unapplied changes to the common template. "
          + "Press Apply first, or restore the approved wording.", true);
      }
      const mode = action === "approve-send" ? "send" : "drafts", b = detail.batch;
      const timing = mode === "send" ? scheduleCheck(b) : { date: null, error: "", quarter: "" };
      if (timing.error || timing.quarter) return notice(timing.error || timing.quarter, true);
      const attachmentText = b.attachmentSummary.length ? b.attachmentSummary.map((a) => `${a.name} (${bytes(a.size)})`).join("\n") : "No attachments";
      const verb = mode === "send" ? (timing.date
        ? `approve and schedule ${b.recipientCount} emails to begin ${easternLabel(timing.date)}`
        : `approve ${b.recipientCount} emails for sending after the ${catalog.limits.cancellationSeconds}-second cancellation window`)
        : `create ${b.recipientCount} Outlook drafts`;
      // The unreviewed count belongs in this dialog, not only in the tally row
      // above it. This is the last moment anyone can stop the send, and "8 not
      // yet opened" is exactly the fact a rep needs at that moment.
      const c = tally();
      if (!confirm(`Review confirmation\n\n${b.recipientCount} recipients\n`
        + `${c.unopened ? `${c.unopened} never opened by you\n` : ""}`
        + `${attachmentText}\n\n${verb}?`)) return;

      // Server-enforced too; this only decides whether to ask. A rep who
      // dismisses the prompt gets told the approval stopped rather than
      // silently sending nothing.
      const needsCode = catalog.limits.passcodeOver != null
        && b.recipientCount > catalog.limits.passcodeOver;
      let passcode = "";
      if (needsCode) {
        passcode = prompt(`This batch has ${b.recipientCount} recipients.\n\n`
          + `Enter the approval passcode to continue.`) || "";
        if (!passcode) return notice("Approval cancelled — no passcode entered.", true);
      }
      button.disabled = true;
      approvalPending = true;
      try { detail = await api("approve", { batchId: b.id, mode, reviewed: true, passcode,
        scheduledForUtc: timing.date ? timing.date.toISOString() : "",
        confirmation: { recipientCount: b.recipientCount, attachmentIds: b.attachmentIds,
          scheduledForUtc: timing.date ? timing.date.toISOString() : "" } }); composerView(); }
      catch (e) { button.disabled = false; notice(e.message, true); }
      finally { approvalPending = false; }
      return;
    }
    if (action === "review-reschedule") {
      try {
        const previousTime = detail.batch.scheduledForUtc || detail.batch.sendNotBeforeUtc;
        detail = await api("review_schedule", { batchId: detail.batch.id });
        sendTiming = "later";
        if (previousTime) {
          const p = easternParts(new Date(previousTime));
          scheduleDate = `${p.year}-${p.month}-${p.day}`; scheduleTime = `${p.hour}:${p.minute}`;
        }
        composerView();
      } catch (e) { notice(e.message, true); }
      return;
    }
    if (["pause", "cancel", "retry"].includes(action)) {
      const actual = action === "pause" && detail.batch.status === "paused" ? "resume" : action;
      if (actual === "cancel" && !confirm("Cancel every email that has not already been submitted? Sent emails will not be recalled.")) return;
      // Stop the ticker before the round trip, not after. Cancelling inside the
      // send window used to leave it counting down for another second or two,
      // which is precisely the moment a rep needs to be told it stopped.
      if (actual === "cancel") { clearInterval(tickTimer); notice("Cancelling…"); }
      try { detail = await api(actual, { batchId: detail.batch.id }); composerView(); }
      catch (e) { notice(e.message, true); } return;
    }
    if (action === "open-batch") {
      await openBatchById(button.dataset.id, true);
    }
  }


  document.addEventListener("change", (event) => {
    if (event.target.name === "emailTiming") {
      sendTiming = event.target.value === "later" ? "later" : "now";
      composerView();
      const first = document.getElementById("emailScheduleDate");
      if (sendTiming === "later" && first) first.focus();
      return;
    }
    if (["emailScheduleDate", "emailScheduleTime"].includes(event.target.id)) {
      scheduleDate = (document.getElementById("emailScheduleDate") || {}).value || "";
      scheduleTime = (document.getElementById("emailScheduleTime") || {}).value || "";
      composerView(); return;
    }
    if (event.target.id === "docFiles") {
      const existingNames = new Set(((catalog && catalog.documents) || []).map((d) => String(d.fileName || "").toLowerCase()));
      const queuedNames = new Set(materialQueue.map((r) => String(r.file.name).toLowerCase()));
      for (const file of [...(event.target.files || [])]) {
        const row = suggestMaterial(file), key = file.name.toLowerCase();
        if (!/\.pdf$/i.test(file.name) && file.type !== "application/pdf") {
          row.status = "invalid"; row.error = "Only PDF files can be published.";
          materialQueue.push(row); continue;
        }
        row.logicalId = uploadLogicalId(row);
        if (existingNames.has(key)) row.duplicate = "A published file has this name";
        else if (queuedNames.has(key)) row.duplicate = "This filename is already queued";
        else if (((catalog && catalog.documents) || []).some((d) =>
          d.id === row.logicalId || uploadTuple(d) === uploadTuple(row)))
          row.duplicate = "This logical material or family/version/period already exists; use Replace PDF if this supersedes it";
        else if (materialQueue.some((d) =>
          uploadLogicalId(d) === row.logicalId || uploadTuple(d) === uploadTuple(row)))
          row.duplicate = "Another queued PDF has the same logical ID or family/version/period";
        if (row.duplicate) row.status = "duplicate";
        materialQueue.push(row); queuedNames.add(key);
      }
      docsView();
      const first = document.querySelector(".email-material-upload input");
      if (first) first.focus();
      return;
    }
    const routeRow = event.target.closest(".email-route-row");
    if (routeRow && event.target.dataset.routeField) {
      const rules = (((catalog || {}).materialRoutes || {}).rules || []);
      const rule = rules.find((item) => String(item.domain || "") === String(routeRow.dataset.routeKey || ""));
      if (rule) {
        if (event.target.dataset.routeField === "domain") {
          rule.domain = event.target.value.trim().toLowerCase(); routeRow.dataset.routeKey = rule.domain;
          const remove = routeRow.querySelector('[data-email="route-remove"]');
          if (remove) remove.dataset.domain = rule.domain;
        } else rule.channel = event.target.value;
      }
      return;
    }
    const row = event.target.closest("[data-upload-row]");
    if (row && event.target.dataset.uploadField) {
      const item = materialQueue[Number(row.dataset.uploadRow)];
      if (item) { item[event.target.dataset.uploadField] = event.target.value; item.status = "ready"; item.error = ""; item.duplicate = ""; }
    }
  }, true);

  document.addEventListener("input", (event) => {
    if (event.target.id === "materialSearch") {
      materialSearch = event.target.value;
      const slot = document.querySelector(".email-material-library");
      if (slot) slot.innerHTML = libraryHtml((catalog && catalog.documents) || []);
    } else if (event.target.id === "routeSearch") {
      routeSearch = event.target.value;
      const slot = document.querySelector(".email-route-card");
      if (slot) {
        const cursorAt = event.target.selectionStart;
        slot.outerHTML = routesHtml();
        const next = document.getElementById("routeSearch");
        if (next) { next.focus(); next.setSelectionRange(cursorAt, cursorAt); }
      }
    }
  }, true);

  document.addEventListener("click", async (event) => {
    const direct = event.target.closest("[data-email]");
    if (direct) {
      /* preventDefault CANCELS A CHECKBOX.
       *
       * This dispatcher was written for buttons and links, where suppressing
       * the default is right. On an <input type=checkbox> it stops the browser
       * applying the tick -- so the handler then read `checked` and got the
       * value from BEFORE the click, concluded the box was being un-ticked, and
       * saved nothing. The teammate picker looked completely inert: the box
       * flickered and the list came back unchanged.
       */
      const toggle = (direct.tagName === "INPUT"
          && /^(checkbox|radio)$/i.test(direct.type || ""))
        // A <summary> needs its default too, or the disclosure never opens.
        || direct.tagName === "SUMMARY";
      if (!toggle) event.preventDefault();
      event.stopPropagation();
      act(direct);
      return;
    }
    const legacy = event.target.closest('[data-contact="email"], [data-dial="mailed"], [data-mail], [data-sess-mail]');
    if (!legacy || !global.AdvisorEmailData) return;
    event.preventDefault(); event.stopPropagation();
    const id = legacy.dataset.advisor || legacy.dataset.crd || legacy.dataset.mail || legacy.dataset.sessMail;
    // Awaited: see the note on open-list above.
    const recipient = await global.AdvisorEmailData.recipientFor(id);
    if (recipient) open([recipient]);
  }, true);

  const params = new URLSearchParams(location.search);
  const linkedBatch = params.get("emailBatch") || "";
  if (linkedBatch) setTimeout(() => {
    history.replaceState({ emailBatch: linkedBatch }, "", emailUrl(linkedBatch, false));
    openBatchById(linkedBatch, false);
  }, 0);
  else if (params.get("email") === "connected") setTimeout(() => {
    history.replaceState(null, "", emailUrl("", false)); openHistory();
  }, 0);
  else if (params.get("email") === "error") setTimeout(() => { shell().hidden = false; connectView(params.get("message") || "Microsoft connection failed."); }, 0);
  global.addEventListener("popstate", () => {
    const id = new URL(location.href).searchParams.get("emailBatch");
    if (id) openBatchById(id, false);
    else { shell().hidden = true; clearTimeout(pollTimer); clearInterval(tickTimer); }
  });

  // Opens a management screen directly, with no batch and no recipients. The
  // admin screens were previously reachable only from the compose setup view,
  // which needs a selected list -- so an administrator whose job is publishing
  // templates and PDFs had to first pretend to email somebody.
  //
  // isAdmin is asked of the server; the boolean the catalog returns is only
  // what decides whether to DRAW the entry, and every write is checked again
  // server-side regardless of what the page believes.
  /* Where "Back" goes from an admin list.
   *
   * templatesView and docsView are reachable two ways: from a composer (there
   * is a batch behind them) and from Settings (there is not). The second case
   * used to close the sheet outright, which made Back and Close the same
   * button and lost the panel the administrator came from.
   */
  let adminReturn = null;

  async function openAdmin(which, onBack) {
    adminReturn = typeof onBack === "function" ? onBack : null;
    shell().hidden = false;
    document.getElementById("emailTitle").textContent = "Loading…";
    document.getElementById("emailBody").innerHTML = "";
    try {
      await loadCatalog();
      if (!catalog.isAdmin) {
        document.getElementById("emailTitle").textContent = "Not available";
        document.getElementById("emailBody").innerHTML =
          `<p class="email-error">This area is limited to email administrators.</p>`;
        return;
      }
      detail = null; cursor = 0;
      if (which === "health") return openHealth();
      return which === "templates" ? templatesView() : docsView();
    } catch (e) {
      document.getElementById("emailTitle").textContent = "Not available";
      document.getElementById("emailBody").innerHTML = `<p class="email-error">${esc(e.message)}</p>`;
    }
  }

  // Cached so the Settings panel can decide whether to draw the admin rows
  // without a round trip every time it opens.
  let adminKnown = null;
  async function isAdmin() {
    if (adminKnown !== null) return adminKnown;
    try { adminKnown = !!(await loadCatalog()).isAdmin; } catch { adminKnown = false; }
    return adminKnown;
  }

  // Exposed for the Settings panel's "copy a colleague" picker: the catalog is
  // fetched here, and the list is the server's, not the client's.
  const internalRecipients = () => (catalog && catalog.internalRecipients) || [];
  /* The approved document list, for the one-to-one reply and follow-up
   * composers on the advisor profile.
   *
   * Exposed rather than re-fetched: this catalog is already the server's
   * answer, and a second copy could come to disagree with the bulk composer's
   * picker about what a rep may attach. Empty until the catalog loads, which is
   * harmless -- the file picker beside it still works, and the server validates
   * every document id regardless of what the client offered. */
  const documents = () => (catalog && catalog.documents) || [];
  global.EmailComposer = { open, openHistory, openAdmin, isAdmin,
                           internalRecipients, documents };
  global.DirectSendOps = { accept: acceptDirect, watch: watchDirect,
                           pending: pendingDirect, resume: resumeDirect };
  setTimeout(resumeDirect, 0);
})(window);
