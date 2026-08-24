/* Field view — "who is worth seeing near me, and can I reach them".
 *
 * Loads geographic tiles rather than the national contacts file. A rep in
 * Washington DC pulls ~430 KB of the cells around them instead of 7.6 MB of the
 * whole country, and gets everyone within range regardless of which side of a
 * state line they sit on -- which state sharding would have got wrong for 77%
 * of them.
 *
 * Deliberately not a responsive version of index.html. Same data, different
 * question: that page is for exploring a universe, this one is for the next
 * ninety minutes.
 */

const CELL = 0.25;                 // degrees; a cell is ~17 miles across
const RADII = [25, 50, 100];       // miles, in the order "Search wider" walks
const THIN = 20;                   // below this, widen without being asked
const PAGE = 200;                  // rows added per "Show more"

// Stamped by src/web_assets.py from metadata time plus every deployed JSON
// byte. Field data still revalidates, but the shared build ID prevents stale
// same-day rebuilds and keeps every first-party data request explicit.
const DATA_VERSION = "20260822T034641Z-20e295e9d9c755f6";
const dataUrl = file => {
  const path = file.startsWith("data/") ? file : `data/${file}`;
  return `${path}${path.includes("?") ? "&" : "?"}v=${encodeURIComponent(DATA_VERSION)}`;
};


let INDEX = null;
let COL = {};
const TILE_CACHE = new Map();

/* ---- TEMPORARY: field-view timing ----------------------------------------
 * The same instrument the desktop carries, so the two can be compared on the
 * same axes. Remove once the performance questions are settled.
 *
 * Measures the three things that are separately fixable and that file sizes
 * cannot tell apart: NETWORK (tile and shard fetches), CPU (the TILE_CACHE
 * flatten every sheet open does, and the distance sort), and the dialer's boot.
 *
 * `PERF.report()` in the console prints the table.
 */
const PERF = window.PERF = {
  t0: performance.now(),
  spans: [],
  usableAt: null,
  mark(name){ performance.mark(name); },
  add(name, ms){ this.spans.push([name, ms]); },
  async time(name, fn){
    const start = performance.now();
    try { return await fn(); }
    finally {
      const ms = performance.now() - start;
      this.spans.push([name, ms]);
      performance.measure(name, { start, duration: ms });
    }
  },
  report(){
    const rows = this.spans.map(([n, ms]) => ({ phase: n, ms: +ms.toFixed(1) }));
    if (this.usableAt != null)
      rows.push({ phase: "→ list usable after", ms: +(this.usableAt - this.t0).toFixed(1) });
    rows.push({ phase: "tiles cached", ms: TILE_CACHE.size });
    rows.push({ phase: "rows cached",
                ms: [...TILE_CACHE.values()].reduce((n, t) => n + t.length, 0) });
    if (performance.memory)
      rows.push({ phase: "JS heap MB", ms: +(performance.memory.usedJSHeapSize / 1e6).toFixed(1) });
    console.table(rows);
    return rows;
  },
};
let ROWS = [];                     // the current result set
let NOTE = "";                     // status headline, kept apart from the
                                   // suffixes render() appends
let limit = PAGE;
let HERE = null;                   // {lat, lon}
let radiusIdx = 0;                 // index into RADII
let ME = null;
let NAMES = null;                  // {shards:Set, split:Set, unplaced:n}
const NAME_CACHE = new Map();      // shard key -> rows
let searchSeq = 0;                 // guards against out-of-order responses

const CHIPS = { direct: false, crm: false, assets: false, ranked: false };

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function money(v){
  if (!(v > 0)) return "";
  if (v >= 1e9) return "$" + (v / 1e9).toFixed(1) + "B";
  if (v >= 1e6) return "$" + (v / 1e6).toFixed(v >= 1e7 ? 0 : 1) + "M";
  if (v >= 1e3) return "$" + Math.round(v / 1e3) + "K";
  return "$" + Math.round(v);
}

/* ---------- identity --------------------------------------------------- */
// Static Web Apps resolves this at the edge from the session cookie, so it is
// the signed-in user rather than anything this page asserts.
async function whoAmI(){
  const p = await Dial.whoAmI();
  if (p) $("who").textContent = p.userDetails || "";
  return p;
}

/* ---------- tiles ------------------------------------------------------- */
// EIC's book by product, and the territory map. Both are small, whole-file
// lookups rather than tile columns -- act_assets is 70 KB gzipped and
// territories under 4 KB, against a tile rebuild and a bigger payload on every
// pan. Neither blocks the map: a rep who cannot reach them still gets names,
// numbers and the call list, which is what the field view is for.
let BOOK = null;      // {accounts:[[acv,lcv,mf]], advisors:{crd:{t,n,ix,...}}}
let TERR = null;      // {states:{XX:{c,n,e}}, national:{...}}

async function loadExtras(){
  const grab = async (url) => {
    try { const r = await fetch(url); return r.ok ? await r.json() : null; }
    catch { return null; }
  };
  [BOOK, TERR] = await Promise.all([grab(dataUrl("act_assets.json")),
                                    grab(dataUrl("territories.json"))]);
}

const bookFor = (crd) => (BOOK && BOOK.advisors[String(crd)]) || null;
// Territory is a function of STATE, not of any CRM field -- see
// src/build_territories.py for why that is more accurate than the field it
// replaces, not merely a substitute for it.
const territoryFor = (state) => (TERR && TERR.states[String(state || "").toUpperCase()]) || null;

async function loadIndex(){
  if (INDEX) return INDEX;
  const start = performance.now();
  INDEX = await (await fetch(dataUrl("tile_index.json"))).json();
  PERF.add("tile_index:fetch", performance.now() - start);
  INDEX.columns.forEach((c, i) => { COL[c] = i; });
  // Built here rather than by the caller: neighbourhood() depends on it, and a
  // Set assembled later in an unrelated async block is exactly the ordering
  // that works on a fast connection and fails on a slow one.
  INDEX.cellSet = new Set(INDEX.cells);
  return INDEX;
}

// Every populated cell that could hold an advisor within `miles`.
function neighbourhood(lat, lon, miles){
  const span = Math.ceil(miles / 17) + 1;
  const ci = Math.round(lat / CELL), cj = Math.round(lon / CELL);
  const keys = [];
  for (let di = -span; di <= span; di++)
    for (let dj = -span; dj <= span; dj++)
      keys.push(`${ci + di}_${cj + dj}`);
  // Only cells the builder wrote. In sparse country most of the square is
  // empty, and a screenful of 404s is a slow answer for no reason.
  return keys.filter((k) => INDEX.cellSet.has(k));
}

async function loadTiles(keys){
  const out = [];
  const fresh = keys.filter((k) => !TILE_CACHE.has(k));
  const start = performance.now();
  await Promise.all(keys.map(async (k) => {
    if (!TILE_CACHE.has(k)) {
      const r = await fetch(dataUrl(`tiles/${k}.json`));
      TILE_CACHE.set(k, r.ok ? (await r.json()).rows : []);
    }
    out.push(...TILE_CACHE.get(k));
  }));
  // Cached calls cost nothing and would drown the useful ones in noise.
  if (fresh.length)
    PERF.add(`tiles:fetch (${fresh.length} new of ${keys.length}, `
             + `${out.length.toLocaleString()} rows)`, performance.now() - start);
  return out;
}

function milesBetween(lat, lon, r){
  const dy = (r[COL.lat] - lat) * 69.0;
  const dx = (r[COL.lon] - lon) * 69.0 * Math.cos(lat * Math.PI / 180);
  return Math.hypot(dx, dy);
}

/* ---------- filtering --------------------------------------------------- */
const REACHES_PERSON = new Set(["direct", "extension"]);
const isDirect = (r) => REACHES_PERSON.has(r[COL.phone_kind]);
// Two questions, not one. "EIC relationship" used to mean owner-recorded OR
// assets>0, which sounded like "they are a client" and overwhelmingly was not:
// across the map 25,775 advisors have an owner in Act! and only 2,926 have any
// assets -- and all but TWO of those already had an owner. So the union was
// 89% CRM entries wearing a label that implied money.
//
// A rep looking for existing clients and a rep avoiding a colleague's contact
// are doing different jobs, and the second list is nine times the size of the
// first. Splitting them makes the 2,926 reachable at all.
const isCRM = (r) => !!r[COL.owner];
const hasAssets = (r) => (r[COL.assets] || 0) > 0;
const isRanked = (r) => r[COL.ranked] === 1;

function passes(r){
  if (CHIPS.direct && !isDirect(r)) return false;
  if (CHIPS.crm && !isCRM(r)) return false;
  if (CHIPS.assets && !hasAssets(r)) return false;
  if (CHIPS.ranked && !isRanked(r)) return false;
  return true;
}

/* ---------- rendering --------------------------------------------------- */
function render(rows, note){
  ROWS = rows;
  if (note !== undefined) { NOTE = note; limit = PAGE; }

  const matched = rows.filter(passes);
  const shown = matched.slice(0, limit);

  $("status").innerHTML = NOTE
    + (matched.length !== rows.length
        ? ` &middot; ${matched.length.toLocaleString()} after filters` : "")
    + (shown.length < matched.length
        ? ` &middot; showing ${shown.length.toLocaleString()}` : "");

  $("list").innerHTML = shown.map((r, i) => {
    const dist = HERE ? `${milesBetween(HERE.lat, HERE.lon, r).toFixed(1)} mi` : "";
    // Badges sit on the NAME line, not in the detail line. The owner used to be
    // appended to "title · firm · city, state", a single truncating line -- so
    // on exactly the records with a long title AND a long firm, the initials
    // were the first thing ellipsed away. The one signal that should stop a rep
    // dialling was the one most likely to be invisible.
    const badges = [
      r[COL.owner] ? `<span class="badge owned" title="EIC relationship owner">${esc(r[COL.owner])}</span>` : "",
      r[COL.assets] > 0 ? `<span class="badge money" title="assets with EIC">${esc(money(r[COL.assets]))}</span>` : "",
      isRanked(r) ? `<span class="badge rank" title="Barron's or Forbes ranked">&#9733;</span>` : "",
      // Swiping a row queues it, so the row has to be able to say it is queued.
      Dial.inQueue(r[COL.crd]) ? `<span class="badge queued" title="on the call list">&#10003;</span>` : "",
    ].join("");
    const sub = [r[COL.title], r[COL.firm], `${r[COL.city]}, ${r[COL.state]}`]
      .filter(Boolean).map(esc).join(" &middot; ");
    const telUrl = Dial.telHref(r[COL.crd], r[COL.phone]);
    const tel = !r[COL.phone] ? ""
      : !telUrl
        ? `<span class="blocked" title="Do not call" aria-label="${esc(r[COL.name])} — do not call">&#9940;</span>`
        : `<a class="${isDirect(r) ? "" : "office"}" href="${esc(telUrl)}"
           data-call="${esc(r[COL.crd])}" aria-label="Call ${esc(r[COL.name])}">&#9742;</a>`;
    const mail = r[COL.email]
      ? `<a class="mail" href="#"
           data-mail="${esc(r[COL.crd])}" aria-label="Email ${esc(r[COL.name])}">&#9993;</a>` : "";
    return `<li>
      <div class="who" data-open="${i}">
        <div class="nm-line"><span class="nm">${esc(r[COL.name])}</span>${badges}</div>
        <div class="sub">${sub}</div>
      </div>
      <div class="dist">${dist}</div>
      <div class="acts">${tel}${mail}</div>
    </li>`;
  }).join("");
  $("list")._shown = shown;

  $("more").hidden = shown.length >= matched.length;
  if (!$("more").hidden) {
    const left = matched.length - shown.length;
    $("more").textContent = `Show ${Math.min(PAGE, left).toLocaleString()} more `
      + `(${left.toLocaleString()} left)`;
  }
  // Widening is how a filtered list gets refilled rather than abandoned: with
  // "EIC relationship" on, Memphis returns 7 people inside 25 miles.
  const next = RADII[radiusIdx + 1];
  $("wider").hidden = !HERE || !next;
  if (!$("wider").hidden) $("wider").textContent = `Search wider (${next} miles)`;
}

/* ---------- detail sheet ------------------------------------------------- */
function sameBuilding(r){
  const key = r[COL.office];
  // "||" is an address we never resolved -- every unplaced advisor would
  // otherwise appear to share one enormous building.
  if (!key || key.replace(/\|/g, "").trim() === "") return [];
  return [...TILE_CACHE.values()].flat()
    .filter((o) => o[COL.office] === key && o[COL.crd] !== r[COL.crd]);
}

/* THE REST OF THE TEAM.
 *
 * Loaded per state on demand rather than shipped with the app: the whole set is
 * 668 KB gzipped, and a rep opening one sheet should not pay for the other 52
 * states. Cached after the first fetch, so the second sheet in the same state
 * is free.
 *
 * A teammate already in a loaded tile is CLICKABLE -- the sheet can switch to
 * them. One who is not, because they work out of a different city, is shown as
 * plain text with their state. Saying the name and admitting we cannot jump to
 * it is more useful than hiding the person.
 */
const PRACTICE_CACHE = new Map();

async function practicesFor(state){
  const key = String(state || "").toUpperCase();
  if (!key) return {};
  if (!PRACTICE_CACHE.has(key)) {
    const start = performance.now();
    PRACTICE_CACHE.set(key, fetch(dataUrl(`practices/${key}.json`))
      .then((r) => { PERF.add(`practices:${key} fetch`, performance.now() - start); return r; })
      .then((r) => (r.ok ? r.json() : {}))
      .catch(() => ({})));
  }
  return PRACTICE_CACHE.get(key);
}

function teammateHtml(rec, selfCrd){
  const others = (rec.m || []).filter((m) => String(m[0]) !== String(selfCrd));
  if (!others.length) return "";
  // The fifth place in this file that flattens TILE_CACHE. Timed because the
  // aggregate of those five, not any one of them, is what would eventually
  // make opening a sheet feel slow.
  const start = performance.now();
  const loaded = new Set([...TILE_CACHE.values()].flat().map((o) => String(o[COL.crd])));
  PERF.add(`sheet:teammate scan (${TILE_CACHE.size} tiles)`, performance.now() - start);
  const items = others.map(([crd, name, st]) => {
    const label = esc(name || crd);
    return loaded.has(String(crd))
      ? `<li><button type="button" class="mate" data-mate="${esc(crd)}">${label}</button></li>`
      : `<li><span class="mate-far">${label}</span>
           <span class="mate-sub">${esc(st || "")}</span></li>`;
  }).join("");
  return `<details class="mates"><summary>${others.length} on ${esc(rec.n)}</summary>
      <ul>${items}</ul></details>`;
}

// "With EIC  $5.9M  (ACV $5.9M · LCV — · Fund —)"
//
// The total leads because it is what a rep reads first; the split follows
// because it is what they act on. A ZERO PRINTS AS AN EM DASH rather than being
// dropped -- "LCV —" is the sales fact, and an omitted line reads as missing
// data instead of an opening.
//
// Falls back to the tile's blended figure when the book is unavailable, so a
// failed fetch degrades to what the sheet showed before rather than to nothing.
function withEic(r){
  const b = bookFor(r[COL.crd]);
  if (!b || !(b.acv > 0 || b.lcv > 0 || b.mf > 0)){
    return r[COL.assets] > 0
      ? `<p class="sheet-row"><span class="sheet-lab">With EIC</span> ${esc(money(r[COL.assets]))}</p>`
      : "";
  }
  const total = (b.acv || 0) + (b.lcv || 0) + (b.mf || 0);
  const part = (lab, v) => `${lab} ${v > 0 ? esc(money(v)) : "&mdash;"}`;
  // A review-tier CRD match means the money is attributed on a name similarity
  // rather than a confirmed identity. Marked, because real dollars against
  // possibly the wrong advisor is the quiet kind of wrong.
  const warn = b.t === "review"
    ? ` <span class="warn-inline" title="Matched on name, firm and location — not a confirmed identifier">&#9888;</span>`
    : "";
  const shared = b.sh ? ` &middot; ${b.sh} shared with team-mates` : "";
  return `<p class="sheet-row"><span class="sheet-lab">With EIC</span>
      <b>${esc(money(total))}</b>${warn}
      <span class="eic-split">(${part("ACV", b.acv || 0)} &middot; ${part("LCV", b.lcv || 0)}
      &middot; ${part("Fund", b.mf || 0)})</span>
      <small class="eic-note">${b.n} account${b.n === 1 ? "" : "s"}${shared}</small></p>`;
}

// Whose patch this advisor sits in. NOT a claim that a relationship exists --
// that is what the "With EIC" row above says. This one exists to tell a rep
// they are looking outside their own territory.
function territoryRow(r){
  // Silent where the CRM already names somebody: that record is a stated fact
  // and this is a derived one, so showing both would say the same thing twice
  // and invite the reader to wonder which is right.
  if (r[COL.owner]) return "";
  const t = territoryFor(r[COL.state]);
  if (!t) return "";
  const mine = ME && String(ME.userDetails || "").toLowerCase() === String(t.e).toLowerCase();
  if (mine) return "";        // silent in your own patch, which is most of what you look at
  return `<p class="sheet-row"><span class="sheet-lab">Territory</span>
      ${esc(r[COL.state])} &mdash; ${esc(t.n)}</p>`;
}

function openSheet(r){
  const dist = HERE ? `${milesBetween(HERE.lat, HERE.lon, r).toFixed(1)} miles away` : "";
  const row = (lab, val) => val
    ? `<p class="sheet-row"><span class="sheet-lab">${lab}</span> ${esc(val)}</p>` : "";
  const owned = r[COL.owner] ? `<p class="warn-box">
      &#9679; <b>${esc(r[COL.owner])}</b> owns this relationship at EIC.
      Coordinate before you call.</p>` : "";
  // Firm-wide suppression, surfaced before the buttons rather than after: a
  // rep should see it in the same glance as the phone number.
  const dnc = Dial.state.dnc.get(String(r[COL.crd]));

  // Suppression used to hide the "+ Call list" button and leave the tel: links
  // working, which is the wrong half: the list is a convenience, the call is
  // the thing we promised not to make. A blocked number still shows -- so an
  // inbound call can be recognised -- but it is not a link.
  const blocked = (label, ghost) =>
    `<span class="${ghost ? "ghost " : ""}blocked">&#9742; ${label}</span>`;
  const work = Dial.telHref(r[COL.crd], r[COL.phone]);
  const mob = Dial.telHref(r[COL.crd], r[COL.mobile]);
  const tel = !r[COL.phone] ? ""
    : !work ? blocked(isDirect(r) ? "Direct" : "Office")
            : `<a href="${esc(work)}"
      data-call="${esc(r[COL.crd])}">&#9742; ${isDirect(r) ? "Direct" : "Office"}</a>`;
  const cell = !r[COL.mobile] ? ""
    : !mob ? blocked("Mobile", true)
           : `<a class="ghost" href="${esc(mob)}"
      data-call="${esc(r[COL.crd])}">&#9742; Mobile</a>`;
  // Same draft machinery as the session card, driven by the purpose chip in
  // the log block below. Rebuilt whenever the sheet re-renders, which is what
  // the chip handler triggers here.
  const mail = r[COL.email] ? `<a class="ghost"
      href="#"
      data-mail="${esc(r[COL.crd])}">&#9993; Email${
        adhocPurpose ? " (" + esc(Dial.purposeLabel(adhocPurpose)) + ")" : ""}</a>` : "";
  const queued = Dial.inQueue(r[COL.crd]);
  const queueBtn = dnc ? "" : `<button class="ghost" data-queue="${esc(r[COL.crd])}">`
      + `${queued ? "&#10003; On call list" : "&#43; Call list"}</button>`;

  // The cheapest meetings a rep will ever get: they are already going to the
  // building. Only lists people we can name -- an office with nobody else on
  // file says nothing worth a disclosure.
  const mates = sameBuilding(r);
  const building = mates.length ? `
    <details class="mates"><summary>${mates.length} other${mates.length === 1 ? "" : "s"} in this building</summary>
      <ul>${mates.slice(0, 25).map((o) => `<li>
        <button type="button" class="mate" data-mate="${esc(o[COL.crd])}">${esc(o[COL.name])}</button>
        <span class="mate-sub">${esc(o[COL.title] || o[COL.firm] || "")}</span>
        ${o[COL.owner] ? `<span class="badge owned">${esc(o[COL.owner])}</span>` : ""}
      </li>`).join("")}</ul>
    </details>` : "";

  $("sheetInner").innerHTML = `
    <button id="sheetClose" aria-label="Close">&times;</button>
    <h2>${esc(r[COL.name])}${flagMarksField(r[COL.crd])}</h2>
    <div class="sub">${esc(r[COL.title] || "")}</div>
    ${owned}
    ${row("Firm", r[COL.firm])}
    <div id="sheetTeam"></div>
    ${row("Team", r[COL.team] && Number(r[COL.team_size]) > 1
        ? `${r[COL.team]} — ${Number(r[COL.team_size]) - 1} teammate${
             Number(r[COL.team_size]) === 2 ? "" : "s"}`
        : r[COL.team])}
    ${row("Office", `${r[COL.city]}, ${r[COL.state]}${dist ? " — " + dist : ""}`)}
    ${row("Phone", r[COL.phone_pretty]
        ? `${r[COL.phone_pretty]} (${isDirect(r) ? "Direct" : "Office"})` : "")}
    ${r[COL.email] ? `<p class="sheet-row"><span class="sheet-lab">Email</span>
        ${Dial.isDnc(r[COL.crd])
          ? `<span title="Firm-wide do-not-call, so this address is not one-click"
               >${esc(r[COL.email])}</span>`
          : `<a class="sheet-mailto" href="mailto:${esc(r[COL.email])}"
               title="Opens a blank email outside the app — nothing is logged"
               >${esc(r[COL.email])}</a>`}</p>` : ""}
    ${withEic(r)}
    ${territoryRow(r)}
    ${isRanked(r) ? `<p class="sheet-row"><span class="sheet-lab">Ranked</span>
        &#9733; Barron's or Forbes</p>` : ""}
    ${r[COL.tier] === "review" ? `<p class="warn-box">&#9888; Unconfirmed match —
       we believe this is them, but it is not certain.</p>` : ""}
    ${dnc ? `<p class="warn-box">&#9940; <b>Do not call.</b> Added by
        ${esc(dnc.by || "a colleague")}${dnc.at ? " on " + esc(String(dnc.at).slice(0, 10)) : ""}.
        ${dnc.reason ? esc(dnc.reason) : ""}</p>` : ""}
    <div class="sheet-acts">${tel}${cell}${mail}${queueBtn}</div>
    ${building}
    <!-- ALWAYS PRESENT, not gated on having tapped Call. A rep who takes an
         inbound call, or dials from a desk handset, still has to be able to
         record it -- and "Call received" would be unreachable in a design that
         waits for an outbound tap. Collapsed so the card is not carrying eight
         buttons for the majority of views where nobody is logging anything;
         opened automatically once they tap a number, which is the moment they
         are most likely to want it. -->
    <details class="log-out"${outcomeOpen.has(String(r[COL.crd])) ? " open" : ""}>
      <summary>Log an outcome</summary>
      ${purposeRow("adhoc", adhocPurpose)}
      <textarea class="log-note" id="adhocNote" rows="2"
        placeholder="Note — saved with the outcome">${esc(adhocNote)}</textarea>
      <div class="outcome" data-crd="${esc(r[COL.crd])}">
        ${Dial.OUTCOMES.map((o) =>
          `<button data-outcome="${o.key}"${o.grave ? ' class="grave"' : ""}>${esc(o.label)}</button>`).join("")}
      </div>
    </details>
    <!-- OUTSIDE the <details>, so the acknowledgement survives the collapse.
         Inside it, logging would close the block and take its own confirmation
         with it -- leaving the rep with a card that looks untouched, which is
         the "did that take?" doubt the collapse exists to answer. -->
    <p class="log-out-note"></p>
    <p class="sheet-hist" data-hist="${esc(r[COL.crd])}"></p>
    <!-- Everything anyone has recorded about this person, ours and the CRM's.
         Collapsed: most views of a card want the phone number, and this costs
         a round trip to Act!, so it is fetched only when opened. -->
    <details class="hist-full" data-histfull="${esc(r[COL.crd])}">
      <summary>Full history</summary>
      <div class="hist-body"><p class="lists-none">Loading…</p></div>
    </details>
    <!-- Email, both directions. Collapsed and fetched on first open for the
         same reason as the block above: a rep opening this card is usually
         about to dial, and nothing may come between them and the number.

         Worth carrying on a phone at all because "they replied yesterday" is
         the most useful thing anyone can know in the ten seconds before a call,
         and it is precisely what a rep standing in a car park cannot go and
         look up in Outlook. -->
    <details class="mail-full" data-mailfull="${esc(r[COL.crd])}">
      <summary>Email activity</summary>
      <div class="mail-body"><p class="lists-none">Loading…</p></div>
    </details>`;
  // Stamped so an in-flight fetch can tell whether the sheet it was started
  // for is still the one on screen.
  $("sheet").dataset.crd = String(r[COL.crd]);
  $("sheet").hidden = false;
  $("sheet").scrollTop = 0;

  // After paint, like the history block below: a team roster is worth waiting
  // a moment for and is not worth delaying the phone number by.
  const teamKey = r[COL.team_key];
  if (teamKey && Number(r[COL.team_size]) > 1) {
    practicesFor(r[COL.state]).then((all) => {
      const rec = all && all[teamKey];
      const host = $("sheetTeam");
      // The sheet may have moved on to somebody else while this was in flight.
      if (rec && host && $("sheet").dataset.crd === String(r[COL.crd])) {
        host.innerHTML = teammateHtml(rec, r[COL.crd]);
      }
    });
  }

  // "Has anyone here already spoken to them?" -- fetched after paint, because
  // it must never delay the phone number. Across all users, which is the one
  // question a per-browser log could never answer.
  Dial.history(r[COL.crd], 5).then((events) => {
    const el = $("sheetInner").querySelector("[data-hist]");
    if (!el || el.dataset.hist !== String(r[COL.crd]) || !events.length) return;
    const last = events[0];
    el.innerHTML = `<b>${events.length}</b> previous `
      + `${events.length === 1 ? "contact" : "contacts"} logged &middot; last `
      + `${esc(String(last.at || "").slice(0, 10))} by ${esc(last.who || "someone")}`
      + (last.disposition ? ` (${esc(Dial.outcomeLabel(last.disposition))})` : "");
  }).catch(() => { /* history is a nicety; its absence is not an error */ });
}

/* ---------- the log ------------------------------------------------------
 * Was localStorage, which meant a rep with a phone and a laptop had two half
 * logs and no way to reconcile them -- and a cache clear threw both away. Now
 * /api/log, attributed server-side from the Entra session.
 *
 * Tapping a number is INCIDENTAL: it records intent, not an outcome, and a
 * failure to record it must not interrupt the call the rep is making. Failing
 * loudly is reserved for dispositions, which are the part that cannot be
 * reconstructed from anything. */
function logTouch(crd, kind, row, purpose){
  // Identify the person from whatever source actually knows them. A tile row is
  // richest, but a national search hit has no tile loaded and a session works
  // from queue snapshots -- and the first version passed null in that case, so
  // every call placed from a session logged a bare CRD. The log read back as an
  // anonymous number where it should have read as a name.
  const q = (Dial.state.items || []).find((i) => String(i.crd) === String(crd));
  const who = row
    ? { name: row[COL.name], firm: row[COL.firm], phone: row[COL.phone],
        phoneKind: row[COL.phone_kind] }
    : q
      ? { name: q.name, firm: q.firm, phone: q.phone, phoneKind: q.phoneKind }
      : { name: "", firm: "", phone: "", phoneKind: "" };
  Dial.log({ crd, kind, ...who, purpose: purpose || "" }).catch(() => {});
}

const rowByCrd = (crd) =>
  [...TILE_CACHE.values()].flat().find((r) => r[COL.crd] === crd) || null;

// What travels to the server as a queue entry. Enough to dial the person on a
// device that has never loaded their tile -- which is the whole point of
// building a list at a desk and working it from a car.
function snapshotOf(r){
  return { crd: r[COL.crd], name: r[COL.name], firm: r[COL.firm],
           phone: r[COL.phone] || "", phoneKind: r[COL.phone_kind] || "",
           city: r[COL.city] || "", state: r[COL.state] || "",
           email: r[COL.email] || "",
           // Travels WITH the queue entry rather than being looked up later:
           // a phone working a list in a car has no tile to consult, and this
           // is what stops an unconfirmed match being used to silence somebody
           // firm-wide from a screen that shows only a name and a number.
           unconfirmed: r[COL.tier] === "review" };
}

/* An advisor's teammates, with addresses, for the emailer's per-message picker.
 *
 * The sheet already lists teammates by NAME -- a practice record carries
 * [crd, name, state] and nothing more. The address lives on the teammate's own
 * tile row, which is why the sheet only makes a teammate tappable when their
 * tile happens to be loaded.
 *
 * So this is deliberately PARTIAL, in exactly the way the sheet already is: a
 * teammate whose tile is not loaded has no address here, and offering their
 * name with nothing to send to would be worse than leaving them out. The
 * practice file is fetched if it is not already cached, which covers the common
 * case of working a list inside one area.
 */
async function teammatesWithEmail(crd, state, teamKey){
  if (!state) return [];
  let practices;
  try { practices = await practicesFor(state); } catch { return []; }
  if (!practices) return [];
  /* NO TEAM KEY IS NORMAL, and it used to mean no teammates.
   *
   * An advisor opened from the queue is a stored snapshot, and snapshotOf()
   * carries no team key -- there is no room in a queue entry for a field only
   * the emailer wants. The old guard returned [] the moment the key was
   * missing, so working a list built at the desk -- the most common way this
   * screen is reached -- could never offer a teammate.
   *
   * The practice file already knows which team somebody is on. Ask it. */
  let rec = teamKey ? practices[teamKey] : null;
  if (!rec) {
    for (const candidate of Object.values(practices)) {
      if ((candidate.m || []).some((m) => String(m[0]) === String(crd))) { rec = candidate; break; }
    }
  }
  if (!rec || !rec.m) return [];
  const out = [];
  for (const m of rec.m) {
    const mateCrd = String(m[0]);
    if (mateCrd === String(crd)) continue;
    /* The address now ships WITH the practice record, as a fourth column.
     *
     * It used to be looked up in TILE_CACHE, which holds only the tiles near
     * where the rep is STANDING -- so a teammate one city over had no address
     * and was silently dropped. The tile is still consulted as a fallback, for
     * a shard built before the column existed. */
    let email = String(m[3] || "");
    if (!email) {
      const row = rowByCrd(mateCrd);
      email = row ? String(row[COL.email] || "") : "";
    }
    if (email) out.push({ name: m[1] || mateCrd, email });
  }
  return out;
}

/* ASYNC, and email.js awaits both.
 *
 * The desk resolves teammates from contacts.json, which is already in memory.
 * Here the practice file may still need fetching, so the contract had to
 * become a promise -- awaiting a plain value is harmless, so the desk's
 * synchronous version keeps working unchanged.
 *
 * Without this the phone's emailer sent recipients carrying no teammates at
 * all, and the per-message "copy someone on their team" picker hid itself --
 * silently, because an empty list and no teammates look identical to it.
 */
window.AdvisorEmailData = {
  /* A LOADED TILE IS NOT REQUIRED, and demanding one broke this on the phone.
   *
   * Both of these used to skip the lookup entirely unless a tile row was in
   * hand, so an advisor whose tile was not in memory had no teammates -- and an
   * advisor reached from a queue never has one, because the queue is a stored
   * snapshot rather than a tile row. The queue is how a rep works a list in the
   * field, so the picker was empty in exactly the case it was built for.
   *
   * The snapshot already carries the state, which is all the practice file
   * needs. Where a row IS loaded its team key is passed as a shortcut, and the
   * lookup falls back to searching the state's practices when it is not. */
  recipientFor: async (crd) => {
    const row = rowByCrd(String(crd));
    const base = row ? snapshotOf(row)
      : Dial.state.items.find((it) => String(it.crd) === String(crd));
    if (!base) return base;
    const mates = await teammatesWithEmail(
      String(crd), row ? row[COL.state] : base.state, row ? row[COL.team_key] : "");
    return { ...base, teammates: mates.map((m) => m.email), teammatesFull: mates };
  },
  list: async () => {
    const out = [];
    for (const it of Dial.state.items) {
      const row = rowByCrd(String(it.crd));
      const mates = await teammatesWithEmail(
        String(it.crd), row ? row[COL.state] : it.state, row ? row[COL.team_key] : "");
      out.push({ ...it, teammates: mates.map((m) => m.email), teammatesFull: mates });
    }
    return out;
  },
};

// A national search hit is a PADDED row: name, city and state, and nothing
// else -- its phone and email live in a tile that has not been fetched. Queuing
// one straight off the list stored a snapshot with no number, and the session
// card then rendered a person with no Call button and no explanation. The queue
// looked full and was partly undialable.
//
// So the tile is fetched before queueing rather than on tap. It costs one
// request at the moment someone commits to calling this person, which is
// exactly when it is worth paying.
async function snapshotForQueue(r){
  if (r.remoteCell && !r[COL.phone]) {
    const full = await hydrate(r[COL.crd], r.remoteCell).catch(() => null);
    if (full) return snapshotOf(full);
  }
  return snapshotOf(r);
}

/* ---------- the dialer ---------------------------------------------------- */
let dialErr = "";                  // last save failure, shown until it clears

// The same net as the desktop view. Swipe-to-queue, the sheet buttons and the
// outcome grid all run inside async handlers, and a rejection in one of those
// is invisible: the swipe simply does not stick. A rep in a car will not open a
// console to find out why.
addEventListener("unhandledrejection", (e) => {
  const msg = (e.reason && e.reason.message) || String(e.reason || "");
  if (msg) { dialErr = msg; renderDial(); }
});
let sessCollapsed = false;         // session card shrunk while searching
// The note lives OUT here rather than in the textarea, because the session card
// re-renders on every Dial change -- including once a second while the
// auto-dial countdown runs. Read from the DOM instead and a rep typing during
// that countdown would watch their sentence disappear.
let sessNote = "";                 // note for the person currently in front of us
let sessNoteOpen = false;          // whether the rep opened the note box
// CRDs whose ad-hoc outcome grid is open. Keyed by person rather than a single
// boolean because the sheet is re-rendered for whoever is opened next, and a
// shared flag would leave the grid hanging open on the following advisor as
// though something were half-logged against them.
const outcomeOpen = new Set();
// The note being typed against an off-queue call. Held here for the same reason
// sessNote is: the sheet is rebuilt whenever it re-renders, and a value living
// only in the textarea disappears with it.
let adhocNote = "";
// Why the call was made, for the person in front of us and for an off-queue
// card. Held out here for the same reason the notes are -- the card re-renders
// once a second while the auto-dial counts down, and a selection living only in
// a CSS class would blink out from under the rep's thumb.
let sessPurpose = "";
let adhocPurpose = "";
let listMenuOpen = false;          // the ⋮ list menu
let listsOpen = false;             // the full list-management sheet
let listsMode = "all";             // "all" = every list, "edit" = who is on this one
let placesOpen = false;            // the "work from somewhere else" sheet
let placeTerm = "";                // what is typed in it
let settingsOpen = false;          // the preferences sheet

/* The purpose chips, one line, in both views.
 *
 * A ROW OF BUTTONS RATHER THAN A <select>. On a phone a native select opens a
 * modal wheel, which is two taps and a context switch for a four-item choice
 * the rep makes while the phone is still at their ear. Four chips are one tap.
 *
 * Nothing is selected by default and tapping the selected chip clears it: the
 * purpose is optional, and a rep who is not sure must be able to leave it
 * blank rather than pick the least wrong one. A guessed purpose in the CRM's
 * Title column is worse than no purpose, because it reads as a fact.
 */
function purposeRow(scope, chosen){
  return `<div class="purpose" data-purpose-scope="${scope}"
      role="group" aria-label="Why this call">
    ${Dial.PURPOSES.map((p) => `<button type="button" data-purpose="${p.key}"
      class="${chosen === p.key ? "on" : ""}"
      aria-pressed="${chosen === p.key}">${esc(p.label)}</button>`).join("")}
  </div>`;
}

// "4 minutes ago" — enough to judge whether an outcome is worth correcting
// without putting a clock on the card.
function sinceText(iso){
  const ms = Date.now() - new Date(iso).getTime();
  if (!(ms >= 0)) return "just now";
  const m = Math.round(ms / 60000);
  if (m < 1) return "moments ago";
  if (m < 60) return `${m} minute${m === 1 ? "" : "s"} ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} hour${h === 1 ? "" : "s"} ago`;
  return `on ${String(iso).slice(0, 10)}`;
}

/* ---------- list management ----------------------------------------------
 * The bar's <select> switches lists; that is all it does. Rename, empty and
 * delete lived on the ⋮ menu and applied only to whichever list was OPEN, so
 * tidying up three stale lists meant opening each one first -- which is also
 * how a rep empties the list they meant to keep.
 *
 * This shows every list at once and acts on the row under the thumb. "Call" is
 * the primary action on each, because picking up a saved list and working it
 * from wherever you are standing is the reason lists exist at all.
 */
// Editing membership was the gap: a rep could rename, empty or delete a list,
// but not see who was on it or take one person off. "Empty" was the only tool
// for "this one shouldn't be here", which is why lists got abandoned instead of
// corrected.
function renderListEdit(){
  const S = Dial.state;
  const items = S.items || [];
  $("listsInner").innerHTML = `
    <button id="listsClose" aria-label="Close">&times;</button>
    <h2>${esc(S.listName || "This list")}</h2>
    <p class="lm-note">${items.length} ${items.length === 1 ? "person" : "people"}.
      Removing someone here does not delete any call history, and does not add
      them to do-not-call.</p>
    ${items.length ? `<ul class="lists-ul">${items.map((it) => `
      <li class="lists-row">
        <span class="lists-main"><b>${esc(it.name || "Unnamed")}</b>
          <small>${esc(it.firm || it.companyName || "")}</small></span>
        <span class="lists-acts">
          <button class="grave" data-ledit="drop" data-crd="${esc(it.crd)}">Remove</button>
        </span>
      </li>`).join("")}</ul>` : `<p class="lists-none">Nobody on this list yet.</p>`}
    <p class="lm-note">To add people, use <b>Add</b> on a contact card, or the bulk
      add button on the map.</p>
    <div class="lists-new"><button data-ledit="back">Back to all lists</button></div>`;
}

function renderLists(){
  const el = $("lists");
  el.hidden = !listsOpen;
  if (!listsOpen) return;
  if (listsMode === "edit") return renderListEdit();
  const S = Dial.state;
  const ls = S.lists || [];
  $("listsInner").innerHTML = `
    <button id="listsClose" aria-label="Close">&times;</button>
    <h2>Call lists</h2>
    <ul class="lists-ul">${flagListRowsField()}</ul>
    ${ls.length ? `<ul class="lists-ul">${ls.map((l) => `
      <li class="lists-row${l.id === S.listId ? " on" : ""}">
        <span class="lists-main"><b>${esc(l.name)}</b>
          <small>${l.count} ${l.count === 1 ? "person" : "people"}`
          + `${l.cycle > 1 ? ` &middot; pass ${l.cycle}` : ""}`
          + `${l.id === S.listId ? " &middot; open" : ""}</small></span>
        <span class="lists-acts">
          <button class="primary" data-lists="call" data-id="${esc(l.id)}"
            ${l.count ? "" : "disabled"}>Call</button>
          <button data-lists="rename" data-id="${esc(l.id)}">Rename</button>
          <button data-lists="empty" data-id="${esc(l.id)}"
            ${l.count ? "" : "disabled"}>Empty</button>
          <button class="grave" data-lists="drop" data-id="${esc(l.id)}">Delete</button>
        </span>
      </li>`).join("")}</ul>` : `<p class="lists-none">No lists yet.</p>`}
    <div class="lists-new">
      <input id="listsName" type="text" placeholder="Name for a new list"
        value="${esc(defaultListName())}" aria-label="Name for a new list">
      <button data-lists="new">Create</button>
    </div>
    <p class="lm-note">Your call history is kept whatever you do here.</p>`;
}

/* KEY CONTACTS and DUE DILIGENCE, as two standing lists on the phone.
 *
 * The desk has had these since the flags shipped; the field view had neither
 * the lists nor any sign the flags exist, so a rep who starred somebody at
 * their desk could not find them again from a car. Both views read the same
 * Dial.state.flags, which dial.js already fetches on boot here -- the data was
 * present the whole time and nothing consumed it.
 *
 * Named exactly as the desk names them ("Key contacts", "Due diligence") so
 * calling from either device opens the SAME list rather than making a second
 * one that looks identical.
 */
/* THE STAR AND THE SHIELD, as controls rather than only as lists.
 *
 * Reading them shipped first; setting them was still desk-only, which is the
 * wrong half. The rep who learns that this is the person who runs manager due
 * diligence is the rep standing in their lobby, and making them remember it
 * until they are back at a desk means it does not get recorded.
 *
 * SAME GEOMETRY AS THE DESK, deliberately duplicated rather than shared: the
 * two views load different files and share only dial.js, and putting these in
 * dial.js would give a module with no DOM of its own an opinion about markup.
 * The paths are the thing that must not drift, so they are named constants in
 * both places and the audit compares them.
 *
 * SVG, not emoji, for the reason the desk found the hard way: the shield at
 * U+1F6E1 U+FE0F carries a variation selector that forces emoji presentation,
 * so the browser paints its own colour and CSS `color` does nothing. It looked
 * permanently set while the star changed colour correctly -- two controls, one
 * of them lying about its state.
 */
const STAR_PATH = "M12 2.6l2.9 6 6.6.9-4.8 4.6 1.2 6.5L12 17.6 6.1 20.6l1.2-6.5"
                + "L2.5 9.5l6.6-.9z";
const SHIELD_PATH = "M12 2.5l7.5 3v5.2c0 4.7-3.2 8.6-7.5 9.8-4.3-1.2-7.5-5.1"
                  + "-7.5-9.8V5.5z";
const CHECK_PATH = "M8.4 12.2l2.4 2.4 4.6-4.9";

function flagMarkField(crd, kind, on, label, path, extra){
  return `<button type="button" class="flag-mark${on ? " on" : ""}"
      data-flag="${kind}" data-advisor="${esc(crd)}"
      title="${esc(label)}${on ? " — tap to unmark" : " — tap to mark"}"
      aria-label="${esc(label)}" aria-pressed="${on}">
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="${path}" fill="${on ? "currentColor" : "none"}"
              stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>
        ${extra || ""}
      </svg></button>`;
}

function flagMarksField(crd){
  const dd = Dial.isDueDiligence(crd);
  return `<span class="contact-flags">`
    + flagMarkField(crd, "key", Dial.isKeyContact(crd), "Key contact", STAR_PATH)
    + flagMarkField(crd, "dd", dd, "Due diligence", SHIELD_PATH,
        `<path d="${CHECK_PATH}" fill="none" stroke="${dd ? "var(--panel, #fff)" : "currentColor"}"
          stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>`)
    + `</span>`;
}

function flaggedField(kind){
  const out = [];
  for (const [crd, f] of (Dial.state.flags || new Map())) {
    if (kind === "key" ? !f.key : !f.dd) continue;
    // The tile row when we happen to hold it, for a firm name and a number.
    // The flag entry always carries the name it was saved under, so a flagged
    // advisor is never listed as a bare CRD just because the rep is standing
    // somewhere else.
    const row = rowByCrd(String(crd));
    out.push({ crd: String(crd),
               name: (row && row[COL.name]) || f.name || `CRD ${crd}`,
               firm: (row && row[COL.firm]) || "",
               row });
  }
  return out.sort((a, b) => a.name.localeCompare(b.name));
}

/* Find a flagged advisor the rep is nowhere near.
 *
 * Same route the national search already uses: the name index is sharded on
 * every token of a name, so any word of the one stored with the flag reaches
 * the record, and the record carries the cell to hydrate from.
 *
 * This is why numbers are resolved when the rep taps CALL rather than while the
 * list is drawn -- it is one fetch per person who is not already in a loaded
 * tile, and drawing a list should not cost that.
 */
async function locateFlagged(crd, name){
  await loadNameIndex();
  const words = String(name || "").toLowerCase()
    .replace(/[^a-z ]/g, " ").split(/\s+/).filter((w) => w.length > 1);
  for (const word of words) {
    const key = shardFor(word);
    if (!key) continue;
    const rows = await loadShard(key);
    const hit = rows.find((r) => String(r[NAMES.C.crd]) === String(crd));
    if (hit) return await hydrate(String(crd), hit[NAMES.C.cell]);
  }
  return null;
}

async function dialableFlagged(kind){
  const out = [];
  for (const person of flaggedField(kind)) {
    if (person.row && person.row[COL.phone]) { out.push(snapshotOf(person.row)); continue; }
    const full = await locateFlagged(person.crd, person.name).catch(() => null);
    if (full && full[COL.phone]) out.push(snapshotOf(full));
  }
  return out;
}

const FLAG_SETS = [["key", "&#9733;", "Key contacts"],
                   ["dd", "&#128737;&#65039;", "Due diligence"]];

function flagListRowsField(){
  return FLAG_SETS.map(([kind, icon, label]) => {
    const people = flaggedField(kind);
    return `<li class="lists-row flag-list">
      <span class="lists-main"><b>${icon} ${label}</b>
        <small>${people.length} ${people.length === 1 ? "person" : "people"}`
      + `${people.length ? "" : " &middot; mark someone at your desk"}</small></span>
      <span class="lists-acts">
        <button class="primary" data-lists="flag-call" data-kind="${kind}"
          ${people.length ? "" : "disabled"}>Call</button>
        <button data-lists="flag-show" data-kind="${kind}"
          ${people.length ? "" : "disabled"}>Show</button>
      </span></li>`;
  }).join("");
}

function defaultListName(){
  return `List ${new Date().toLocaleDateString(undefined, { month: "short", day: "numeric" })}`;
}

async function openLists(){
  listsOpen = true;
  renderLists();
  // Refreshed rather than trusted. The counts in Dial.state.lists were last
  // fetched at some point in the past, and a screen whose whole job is to say
  // how big each list is has to be believable.
  try { await Dial.loadLists(); } catch { /* stale is still legible */ }
  renderLists();
}

// Rename, empty and delete all write to whichever list is OPEN, so acting on
// another one means opening it and putting the rep back afterwards. They did
// not ask to be moved off the list they were working.
async function onOtherList(id, fn){
  const was = Dial.state.listId;
  if (was !== id) await Dial.openList(id);
  await fn();
  if (was !== id) await Dial.openList(was);
  await Dial.loadLists();
}

/* ---------- the full history ----------------------------------------------
 * Ours plus the CRM's, de-duplicated server-side by actStatus. Fetched only
 * when the block is opened: it costs a round trip to Act! and most views of a
 * card want the phone number, not the file.
 */
function histRowHtml(r){
  return `<li class="${r.crm ? "from-crm" : "from-app"}">
    <span class="hist-when">${esc(r.at)}</span>
    <span class="hist-what">${esc(r.what)}</span>
    ${r.who ? `<span class="hist-who">${esc(r.who)}</span>` : ""}
    ${r.text ? `<span class="hist-text">${esc(r.text)}</span>` : ""}
  </li>`;
}

async function fillFullHistory(crd, body){
  try {
    const d = await Dial.fullHistory(crd, 40);
    const { rows, notice } = Dial.describeHistory(d.events, d.crm);
    if (!body.isConnected) return;
    body.innerHTML =
      (notice ? `<p class="hist-notice">${esc(notice)}</p>` : "")
      + (rows.length
          ? `<ul class="hist-list">${rows.map(histRowHtml).join("")}</ul>`
          : `<p class="lists-none">Nothing recorded for this advisor.</p>`)
      // Said plainly rather than shown as a badge. The rep is looking at two
      // merged sources and should know which is which without a legend.
      + (d.crm.ok ? `<p class="hist-foot">Includes ${d.crm.count}
          ${d.crm.count === 1 ? "entry" : "entries"} from Act!.</p>` : "");
  } catch (e) {
    if (body.isConnected)
      body.innerHTML = `<p class="hist-notice">History could not be loaded —
        ${esc(e.message || "")}</p>`;
  }
}

/* ---------- needs attention -----------------------------------------------
 * The desk's work queue on a phone, from the same endpoint. Who to pick up now
 * and why -- never a count of what has been sent, for the reason the server's
 * own module gives at length: a number that rewards sending pulls against the
 * 25-a-day limit, and the screen a rep reads every morning wins that argument.
 *
 * The reasons, their order and their wording all arrive from the server. This
 * file decides layout and nothing else.
 */
let workOpen = false;
let workData = null;

function workRowHtml(entry){
  const when = entry.lastReplyAt || entry.lastActivityAt;
  return `<li class="work-row">
      <button class="work-main" data-work="open" data-crd="${esc(entry.advisorCrd)}">
        <b>${esc(entry.advisorEmail || entry.advisorCrd)}</b>
        <small class="work-why work-${esc(entry.reason)}">${esc(entry.reasonLabel)}${
          when ? ` &middot; ${esc(mailWhen(when))}` : ""}</small>
      </button>
      <span class="work-acts">
        <button data-work="follow" data-crd="${esc(entry.advisorCrd)}"
          data-name="${esc(entry.advisorEmail || "")}">Follow up</button>
        ${entry.replyState === "new"
          ? `<button data-work="state" data-state="reviewed"
               data-crd="${esc(entry.advisorCrd)}">Reviewed</button>` : ""}
        ${entry.reason === "bounced"
          ? `<button data-work="state" data-op="queue_dismiss_bounce"
               data-crd="${esc(entry.advisorCrd)}">Address OK</button>`
          : entry.replyState !== "done"
            ? `<button data-work="state" data-state="done"
                 data-crd="${esc(entry.advisorCrd)}">Done</button>` : ""}
        <button data-work="state" data-op="queue_snooze" data-days="30"
          data-crd="${esc(entry.advisorCrd)}">Snooze</button>
      </span></li>`;
}

function renderWork(){
  const el = $("work");
  el.hidden = !workOpen;
  if (!workOpen) return;
  const body = !workData
    ? `<p class="lists-none">Loading…</p>`
    : workData.error
      ? `<p class="hist-notice">${esc(workData.error)}</p>`
      : !workData.count
        /* NOT "you are all caught up". This covers one mailbox, since reply
           tracking was switched on -- an empty queue means nothing has been
           OBSERVED, and a rep should not read it as an all-clear. */
        ? `<p class="lists-none">Nothing waiting. This covers what has been
             observed in your own mailbox since reply tracking was switched on.</p>`
        : `<div class="work-heads">${(workData.reasons || [])
              .filter((r) => (workData.counts || {})[r.key])
              .map((r) => `<span class="work-count"><b>${workData.counts[r.key]}</b>
                 ${esc(r.label)}</span>`).join("")}</div>
           <ul class="work-list">${workData.entries.map(workRowHtml).join("")}</ul>`;
  $("workInner").innerHTML =
    `<button id="workClose" aria-label="Close">&times;</button>
     <h2>Needs attention</h2>${body}`;
}

async function loadWork(){
  try {
    const r = await fetch("/api/email?op=queue_work", { headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    workData = await r.json();
  } catch (e) {
    workData = { error: `The queue could not be loaded — ${e.message || ""}`, count: 0 };
  }
  paintWorkBadge();
  if (workOpen) renderWork();
}

/* The count on the header button.
 *
 * Silent at zero and silent on failure -- a badge that shows a number when the
 * queue could not even be read would be worse than no badge, because a rep
 * would trust it. */
function paintWorkBadge(){
  const badge = $("workCount");
  if (!badge) return;
  const n = workData && !workData.error ? Number(workData.count || 0) : 0;
  badge.textContent = n > 99 ? "99+" : String(n);
  badge.hidden = !n;
}

/* ---------- email activity ------------------------------------------------
 * The same timeline the desk shows, on a phone.
 *
 * The WORDING is the server's, not this file's -- `label` and `basis` arrive
 * already decided. Whether something counts as a reply must not depend on which
 * device a rep happens to be holding, which is the same reason display_name.py
 * exists and the reason the desk and the phone are checked against each other
 * in the audit.
 *
 * Deliberately narrower than the desk: date, what happened, subject. A phone
 * screen has room for the answer to "should I mention their email?" and not
 * much else.
 */
function mailWhen(value){
  if (!value) return "";
  const d = new Date(value);
  if (isNaN(d)) return String(value);
  const today = new Date();
  const sameYear = d.getFullYear() === today.getFullYear();
  return d.toLocaleDateString(undefined,
    { month: "short", day: "numeric", ...(sameYear ? {} : { year: "numeric" }) });
}

function mailRowHtml(entry, crd){
  const arrow = entry.direction === "outbound" ? "&rarr;" : "&larr;";
  // A message in a colleague's mailbox cannot be opened by this rep -- their
  // token does not reach it. The row still shows, because knowing somebody
  // already made contact is most of the value of having this on a phone.
  const tap = entry.mine && entry.id
    ? `<button class="mail-view" data-mail-msg="${esc(entry.id)}"
         data-mail-crd="${esc(crd)}">Read</button>` : "";
  const flag = entry.ambiguous
    ? ` <span class="mail-warn">shared address</span>` : "";
  return `<li class="mail-row mail-${esc(entry.direction)}">
      <span class="mail-when">${esc(mailWhen(entry.occurredAt))}</span>
      <span class="mail-what">${arrow} ${esc(entry.label)}${flag}</span>
      <span class="mail-subj">${esc(entry.subject || "(no subject)")}</span>
      ${tap}</li>`;
}

async function fillMailActivity(crd, body){
  try {
    const r = await fetch(`/api/email?op=activity&crd=${encodeURIComponent(crd)}`,
                          { headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    if (!body.isConnected) return;
    if (data.internal){
      // See the desk's renderActivity(): a colleague, deliberately not tracked.
      body.innerHTML = `<p class="lists-none">A colleague, not a prospect &mdash;
        email is not tracked here.</p>`;
      return;
    }
    body.innerHTML = (data.entries && data.entries.length)
      ? `<ul class="mail-list">${data.entries.map((x) => mailRowHtml(x, crd)).join("")}</ul>`
      /* NOT "no email". The sweep covers connected mailboxes only, only since
         it was switched on, and only addresses we hold for this advisor. A rep
         about to dial would act on "none" -- and might well be wrong. */
      : `<p class="lists-none">Nothing observed. That is not the same as
           nothing having happened.</p>`;
  } catch (e) {
    if (body.isConnected)
      body.innerHTML = `<p class="hist-notice">Email activity could not be
        loaded — ${esc(e.message || "")}</p>`;
  }
}

/* Attaching, from a phone.
 *
 * The approved library and the device's own file picker, side by side. On a
 * phone the second one is the camera roll and the Files app, which is exactly
 * how a rep standing in a car park sends the fact sheet they were just asked
 * for.
 *
 * Whatever the source, the server blind-copies compliance on any attachment to
 * an advisor -- the same rule and the same code path as a campaign.
 */
function mailAttachHtml(){
  const docs = (window.EmailComposer && EmailComposer.documents
                && EmailComposer.documents()) || [];
  return `<details class="mail-attach">
      <summary>Attach</summary>
      ${docs.length ? `<select class="mail-doc" multiple size="${Math.min(docs.length, 4)}">
          ${docs.map((d) => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join("")}
        </select>` : ""}
      <input type="file" class="mail-file" multiple>
      <p class="mail-attach-note">Anything attached is blind-copied to compliance.</p>
    </details>`;
}

function readMailAttachments(box){
  const select = box.querySelector(".mail-doc");
  const documentIds = select ? [...select.selectedOptions].map((o) => o.value) : [];
  const input = box.querySelector(".mail-file");
  const files = input ? [...input.files] : [];
  return Promise.all(files.map((file) => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(`${file.name} could not be read.`));
    reader.onload = () => resolve({ name: file.name, contentType: file.type,
      data: String(reader.result).split(",")[1] || "" });
    reader.readAsDataURL(file);
  }))).then((read) => ({ documentIds, files: read }));
}

/* Start a NEW conversation with an advisor who has gone quiet.
 *
 * A blank sheet: no template. The signature still comes through, because that
 * is firm identity rather than content. The recipient is NOT sent from here --
 * the server takes it from the advisor's own timeline, so this cannot be used
 * to mail an arbitrary address from the rep's mailbox.
 */
async function showFollowUp(crd, name){
  const back = document.createElement("div");
  back.className = "ask-back";
  back.innerHTML = `<div class="ask mail-msg" role="dialog" aria-modal="true"
      aria-label="Follow up">
      <h3>Follow up${name ? ` &middot; ${esc(name)}` : ""}</h3>
      <p class="mail-meta">Starts a new conversation, to the address already on
        their timeline.</p>
      <input type="text" class="follow-subject" maxlength="200" placeholder="Subject">
      <textarea class="mail-reply" rows="5" maxlength="5000"
        placeholder="Your message — your signature is added automatically."></textarea>
      ${mailAttachHtml()}
      <button class="ask-btn primary" data-follow-send="${esc(crd)}">Send</button>
      <button class="ask-btn" data-mail-close="1">Cancel</button>
      <p class="mail-reply-note"></p></div>`;
  document.body.appendChild(back);
  const close = () => back.remove();
  back.addEventListener("click", async (e) => {
    if (e.target === back || e.target.closest("[data-mail-close]")) { close(); return; }
    const btn = e.target.closest("[data-follow-send]");
    if (!btn) return;
    const box = back.querySelector(".mail-msg");
    const subject = box.querySelector(".follow-subject").value.trim();
    const text = box.querySelector(".mail-reply").value.trim();
    const note = box.querySelector(".mail-reply-note");
    if (!subject){ note.textContent = "A subject is needed."; return; }
    if (!text){ note.textContent = "Nothing to send."; return; }
    // Locked for the round trip. On a phone connection a second tap is easy and
    // a sent email has no undo.
    btn.disabled = true;
    note.textContent = "Sending…";
    try {
      const attached = await readMailAttachments(box);
      const r = await fetch("/api/email", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ op: "follow_up", crd, subject, text,
                               operationId: (btn.dataset.op ||= crypto.randomUUID()),
                               ...attached }) });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
      note.textContent = `Sent to ${j.to}.`;
      setTimeout(close, 900);
    } catch (err) {
      note.textContent = err.message;
      btn.disabled = false;
    }
  });
}

/* One message, read on the phone.
 *
 * Plain text from Graph, escaped again here. This is mail written by people
 * outside the firm and rendered inside our own page; one layer is not enough,
 * and the field app has no sanitiser to fall back on.
 */
async function showMailMessage(crd, id){
  const back = document.createElement("div");
  back.className = "ask-back";
  back.innerHTML = `<div class="ask mail-msg" role="dialog" aria-modal="true"
      aria-label="Email"><p class="lists-none">Loading…</p></div>`;
  document.body.appendChild(back);
  const close = () => back.remove();
  back.addEventListener("click", (e) => { if (e.target === back) close(); });
  const box = back.querySelector(".mail-msg");
  try {
    const r = await fetch(`/api/email?op=activity_message&crd=${encodeURIComponent(crd)}`
                          + `&id=${encodeURIComponent(id)}`,
                          { headers: { Accept: "application/json" } });
    const j = await r.json();
    box.innerHTML = !r.ok
      ? `<h3>Email</h3><p class="hist-notice">${esc(j.error || `HTTP ${r.status}`)}</p>`
        + `<button class="ask-btn" data-mail-close="1">Close</button>`
      : `<h3>${esc(j.subject || "(no subject)")}</h3>`
        + `<p class="mail-meta">${esc(j.fromName || j.from)}`
        + (j.receivedAt || j.sentAt ? ` &middot; ${esc(mailWhen(j.receivedAt || j.sentAt))}` : "")
        + `</p><pre class="mail-body-text">${esc(j.text || "(no text in this message)")}</pre>`
        /* Reply, on the phone. Only on INBOUND mail -- replying to our own
         * send would mail ourselves.
         *
         * A short answer typed standing up is the whole use case: "yes, sending
         * it over now". Anything longer belongs in Outlook, and the button for
         * that is right beside it. */
        + (j.from && !j.isOwn ? `
            <textarea class="mail-reply" rows="3" maxlength="5000"
              placeholder="Reply — plain text"></textarea>
            ${mailAttachHtml()}
            <button class="ask-btn primary" data-mail-reply="${esc(id)}"
              data-crd="${esc(crd)}">Send reply</button>
            <p class="mail-reply-note"></p>` : "")
        // Outlook for anything beyond reading. It cannot be framed, so it opens
        // the app -- which on a phone is usually what a rep wants anyway.
        + (j.webLink ? `<a class="ask-btn ghost" href="${esc(j.webLink)}"
             target="_blank" rel="noopener">Open in Outlook</a>` : "")
        + `<button class="ask-btn" data-mail-close="1">Close</button>`;
  } catch (e) {
    box.innerHTML = `<h3>Email</h3><p class="hist-notice">${esc(e.message)}</p>`
      + `<button class="ask-btn" data-mail-close="1">Close</button>`;
  }
  box.addEventListener("click", async (e) => {
    if (e.target.closest("[data-mail-close]")) { close(); return; }
    const btn = e.target.closest("[data-mail-reply]");
    if (!btn) return;
    const area = box.querySelector(".mail-reply");
    const note = box.querySelector(".mail-reply-note");
    const text = (area.value || "").trim();
    if (!text){ note.textContent = "Nothing to send."; return; }
    // Locked for the round trip. A second tap on a slow connection -- which is
    // the normal connection for this app -- would send the advisor the same
    // reply twice, and a sent email has no undo.
    btn.disabled = true; area.disabled = true;
    note.textContent = "Sending…";
    try {
      const attached = await readMailAttachments(box);
      const r = await fetch("/api/email", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ op: "reply_send", crd: btn.dataset.crd,
                               id: btn.dataset.mailReply, text,
                               // Same id on a retry, so a lost response cannot
                               // become a second email to the advisor.
                               operationId: (btn.dataset.op ||= crypto.randomUUID()),
                               ...attached }) });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
      note.textContent = "Sent.";
      area.value = "";
    } catch (err) {
      note.textContent = err.message;
      btn.disabled = false; area.disabled = false;
    }
  });
}

/* ---------- preferences ---------------------------------------------------
 * Kept deliberately short. Every row here is a thing a rep sets ONCE and then
 * benefits from silently -- not a control panel for the app. Anything that
 * would need explaining does not belong on a phone screen.
 */
function renderSettings(){
  const el = $("settings");
  el.hidden = !settingsOpen;
  if (!settingsOpen) return;
  const S = Dial.state;
  const g = (k, d) => Dial.setting(k, d);
  $("settingsInner").innerHTML = `
    <button id="settingsClose" aria-label="Close">&times;</button>
    <h2>Settings</h2>
    <p class="set-note">Saved to your account, so the desk and the phone agree.</p>

    <label class="set-row">
      <span>List that opens first</span>
      <select id="setList">
        <option value="">Wherever I left off</option>
        ${(S.lists || []).map((l) => `<option value="${esc(l.id)}"`
          + `${g("defaultListId") === l.id ? " selected" : ""}>${esc(l.name)}</option>`).join("")}
      </select>
    </label>

    <label class="set-row">
      <span>Start "Near me" at</span>
      <select id="setRadius">
        ${RADII.map((m, i) => `<option value="${i}"`
          + `${String(g("fieldRadius", "0")) === String(i) ? " selected" : ""}>`
          + `${m} miles</option>`).join("")}
      </select>
    </label>

    <div class="set-row set-block">
      <span>Default starting point</span>
      <p class="set-sub">${g("homeLabel")
        ? esc(g("homeLabel")) + ` &middot; <button id="setHomeClear" class="linkish">clear</button>`
        : "Your device's location"}</p>
      <p class="set-sub">Set this from <b>Elsewhere</b> after choosing a place.</p>
    </div>

    <div class="set-row set-block">
      <span>Email signature</span>
      <p class="set-sub">Generated centrally from your Microsoft 365 profile and the approved corporate disclosure. The exact signature appears in every email preview.</p>
    </div>

    <label class="set-row set-check">
      <input type="checkbox" id="setAuto"${S.auto.on ? " checked" : ""}>
      <span>Auto-dial the next call</span>
    </label>

    <div class="set-row set-block" id="setAdmin" hidden>
      <span>Email administration</span>
      <p class="set-sub">Approved templates and the PDFs reps may attach.</p>
      <p class="set-actions">
        <button type="button" id="setTemplates" class="set-btn">Manage templates</button>
        <button type="button" id="setDocs" class="set-btn">Manage approved documents</button>
        <button type="button" id="setHealth" class="set-btn">Sender health</button>
      </p>
    </div>

    <div class="set-row set-block">
      <span>Signed in as</span>
      <p class="set-sub">${esc((ME && ME.userDetails) || "not signed in")}</p>
      <button type="button" id="setSignOut" class="set-signout">Log out</button>
    </div>

    <p class="set-saved" id="setSaved"></p>`;

  // Asked after the panel is painted so opening Settings never blocks on a
  // round trip. Every write is re-checked server-side; this only decides
  // whether the rows are drawn.
  if (global.EmailComposer && EmailComposer.isAdmin) {
    EmailComposer.isAdmin().then((yes) => {
      const row = $("setAdmin");
      if (row && yes) row.hidden = false;
    });
  }
}

async function saveSetting(patch){
  const el = $("setSaved");
  if (el) el.textContent = "Saving…";
  try {
    await Dial.saveSettings(patch);
    if (el) { el.textContent = "Saved."; el.className = "set-saved"; }
  } catch (e) {
    // Said out loud. A preference that silently failed to save is one the rep
    // sets again next week and blames themselves for.
    if (el) { el.textContent = e.message || "That could not be saved.";
              el.className = "set-saved bad"; }
  }
}

function renderDial(){
  const S = Dial.state;

  const problem = $("dialProblem");
  problem.hidden = !S.problem;
  if (S.problem) problem.textContent = `Call logging unavailable — ${S.problem}`;

  // The bar is the whole dialer when no session is running: how many are
  // queued, and the one button that starts working through them.
  const bar = $("queueBar");
  const n = S.items.length;
  const p = Dial.progress();
  const finished = n > 0 && p.left === 0;
  bar.hidden = (!n && S.lists.length < 2) || S.running;
  const lm = $("listMenu");
  lm.hidden = bar.hidden || !listMenuOpen;
  if (!lm.hidden) {
    lm.innerHTML =
      `<div class="lm-head">${esc(S.listName)} &middot; ${n} ${n === 1 ? "person" : "people"}`
      + `${S.cycle > 1 ? ` &middot; pass ${S.cycle}` : ""}</div>`
      + `<button id="lmAll">Manage all lists&hellip;</button>`
      + `<button id="lmEdit"${n ? "" : " disabled"}>Edit who&rsquo;s on it</button>`
      + `<button id="lmRename">Rename this list</button>`
      + `<button id="lmEmpty"${n ? "" : " disabled"}>Remove everyone from it</button>`
      + `<button id="lmDrop" class="grave">Delete this list</button>`
      + `<p class="lm-note">Your call history is kept either way.</p>`;
  }
  if (!bar.hidden) {
    // The list is named and switchable, because a rep working "Georgia ranked"
    // monthly and "New prospects" weekly has two different jobs, not one queue.
    const picker = S.lists.length > 1 || n
      ? `<select id="qList" aria-label="Call list">`
        + S.lists.map((l) => `<option value="${esc(l.id)}"${l.id === S.listId ? " selected" : ""}>`
            + `${esc(l.name)} (${l.count})</option>`).join("")
        + `<option value="__new">+ New list…</option></select>`
      : "";
    // Two deliberate rows. Top: which list, and the menu that acts on it.
    // Bottom: email, history, and the call action. One row could not hold the
    // list name and a primary button on a phone without one of them losing.
    bar.innerHTML = `<div class="q-row q-row-top">` + picker
      + `<span class="qn">${p.done} of ${n} done</span>`
      + `<button id="qMenu" class="ghost-btn" aria-label="List options">&#8942;</button>`
      + `</div><div class="q-row q-row-actions">`
      + (n ? `<button type="button" class="email-toolbar-btn" data-email="open-list">Email list</button>` : "")
      + `<button type="button" class="email-toolbar-btn" data-email="history" aria-label="Email history">&#9993; History</button>`
      // A finished list must NOT say "Resume". It used to, and pressing it
      // silently replayed the whole list from person one.
      + (finished
          ? `<button id="qCycle" class="primary">Start pass ${S.cycle + 1}</button>`
          : `<button id="qStart" class="primary"${S.problem ? " disabled" : ""}>`
            + `${p.done ? "Continue calling" : "Start calling"}</button>`)
      + `</div>`;
  }

  const sess = $("session");
  const cur = Dial.current();
  sess.hidden = !cur;
  if (!cur) return;

  const kindLabel = cur.phoneKind === "direct" ? "Direct"
    : cur.phoneKind === "extension" ? "Extension"
    : cur.phone ? "Office" : "";
  // Progress through the CYCLE, not position in the array. On a saved list
  // worked monthly, "31 of 148 this pass" is the number the rep cares about;
  // the array index is an implementation detail that reordering would change.
  const pos = `${Math.min(p.done + 1, S.items.length)} of ${S.items.length}`
            + (S.cycle > 1 ? ` &middot; pass ${S.cycle}` : "");
  const nxt = S.items[Dial.state.items.findIndex(
    (it, i) => i > S.cursor && !Dial.isDone(it.crd))];

  // Collapsed to a single line while the rep is searching. The card is sticky
  // and takes most of a phone screen, so leaving it open pushed every search
  // result below the fold -- the session did not need pausing, it needed to
  // get out of the way.
  sess.classList.toggle("collapsed", sessCollapsed);
  if (sessCollapsed) {
    sess.innerHTML = `<button class="sess-mini" id="sessExpand">
      <span class="sess-mini-pos">${pos}</span>
      <span class="sess-mini-name">${esc(cur.name)}</span>
      <span class="sess-mini-hint">tap to resume</span></button>`;
    return;
  }

  // What was recorded for this person on this pass, if anything. Only appears
  // when the rep has come back to them, and it is a statement rather than an
  // edit box: logging again appends a correction, it does not rewrite.
  const prior = Dial.lastOutcome(cur.crd);
  const priorLine = prior ? `<p class="sess-prior">You logged
      <b>${esc(Dial.outcomeLabel(prior.disposition))}</b>
      ${esc(sinceText(prior.at))}. Logging another records a correction.</p>` : "";

  sess.innerHTML = `
    <div class="sess-top">
      ${Dial.canBack() ? `<button id="qBack" class="ghost-btn">&lsaquo; Back</button>` : ""}
      <span class="sess-pos">${pos}</span>
      <button id="qPause" class="ghost-btn">Pause</button>
    </div>
    ${priorLine}
    <h2 class="sess-name">${esc(cur.name)}</h2>
    <div class="sess-sub">${esc([cur.firm, [cur.city, cur.state].filter(Boolean).join(", ")]
        .filter(Boolean).join(" &middot; ")).replace(/&amp;middot;/g, "&middot;")}</div>
    <div class="sess-acts">
      ${!cur.phone ? ""
        : !Dial.telHref(cur.crd, cur.phone)
          ? `<span class="sess-call blocked">&#9940; Do not call</span>`
          : `<a class="sess-call" href="${esc(Dial.telHref(cur.crd, cur.phone))}"
          data-sess-call="${esc(cur.crd)}">&#9742; Call${kindLabel ? " (" + kindLabel + ")" : ""}</a>`}
      <!-- The draft is built from the purpose chip below, so choosing
           "Materials" and tapping Email opens a compose window already
           written. No chip chosen means a blank compose window, which is the
           honest default: pre-filling a guess is how boilerplate reaches a
           prospect over the rep's own signature. -->
      ${cur.email ? `<a class="ghost" href="#"
          data-sess-mail="${esc(cur.crd)}">&#9993; Email${
            sessPurpose ? " (" + esc(Dial.purposeLabel(sessPurpose)) + ")" : ""}</a>` : ""}
      <button class="ghost" data-sess-open="${esc(cur.crd)}">Details</button>
    </div>
    ${cur.phone ? "" : `<p class="sess-nonum">&#9888; No number on file for this
       person &mdash; the card shows only what we hold. Log <b>Skip</b> to move on.</p>`}
    <p class="sess-hint">How did it go? Logging an outcome moves to the next call.</p>
    ${purposeRow("sess", sessPurpose)}
    <!-- Collapsed by default. A note is worth having and is not worth a
         permanently open textarea between the rep's thumb and the outcome grid;
         on a phone that box is the difference between one screen and two.
         Its text reaches the CRM, so it is the rep's own words, not a field. -->
    <details class="sess-note"${sessNoteOpen ? " open" : ""}>
      <summary>Add a note${sessNote ? " &bull;" : ""}</summary>
      <textarea id="sessNote" rows="3"
        placeholder="Note — saved with the outcome">${esc(sessNote)}</textarea>
    </details>
    <!-- A 3x3 grid rather than a wrapping row. Equal cells mean each outcome
         sits in the same place every time, which is what a thumb learns; a
         ragged flex row also left "Do not call" alone on its own line, giving
         the one irreversible button the most isolated and easiest-to-hit
         position on the card. Skip spans two cells so the grid closes. -->
    <div class="outcome sess-out">
      ${Dial.OUTCOMES.filter((o) => !o.grave).map((o) =>
        `<button data-sess-outcome="${o.key}">${esc(o.label)}</button>`).join("")}
      <button class="span2 ghost-btn" data-sess-outcome="skipped">Skip, no call made</button>
      ${Dial.OUTCOMES.filter((o) => o.grave).map((o) =>
        `<button class="grave" data-sess-outcome="${o.key}">${esc(o.label)}</button>`).join("")}
    </div>
    <div class="sess-foot">
      ${nxt ? `<span class="sess-next">Next: ${esc(nxt.name)}</span>` : `<span class="sess-next">Last one</span>`}
    </div>
    <!-- Two separate switches. The field view only ever stated the announce
         setting in passing -- "Auto-dial next, saying the name first" -- so a
         rep could read it but not change it without going to a desktop. -->
    <div class="sess-auto-row">
      <label class="sess-auto">
        <input type="checkbox" id="autoDial"${S.auto.on ? " checked" : ""}>
        <span>Auto-dial next</span>
      </label>
      ${S.auto.on ? `<label class="sess-auto">
        <input type="checkbox" id="autoSay"${S.auto.announce ? " checked" : ""}>
        <span>Say name</span>
      </label>` : ""}
    </div>
    ${S.pending ? `<p class="sess-count">Calling <b>${esc(S.pending.name)}</b>
        in ${S.pending.left}s <button class="ghost-btn" id="autoCancel">Cancel</button></p>` : ""}
    ${dialErr ? `<p class="warn-box">${esc(dialErr)}</p>` : ""}`;
}

// The tel: anchor lives in this view, so this view places the call. A blocked
// navigation leaves the Call button armed rather than pretending it dialled.
function armFieldAutoDial(){
  const nxt = Dial.current();
  if (!nxt) return;
  Dial.armAuto(nxt, () => {
    const a = $("session").querySelector("[data-sess-call]");
    if (a) a.click();
  });
}

// The disposition path, and the only place in the field view that refuses to
// carry on when a write fails. Nothing advances until the server has the record.
async function recordOutcome(disposition){
  const cur = Dial.current();
  if (!cur) return;
  if (!Dial.confirmGrave(disposition, cur)) return;
  const btns = $("session").querySelectorAll("[data-sess-outcome]");
  btns.forEach((b) => { b.disabled = true; });
  // Read once, here, rather than trusting sessNote alone: a rep who types and
  // taps an outcome in the same breath can beat the input event.
  const box = document.getElementById("sessNote");
  const note = (box ? box.value : sessNote).trim();
  try {
    const res = await Dial.log({ ...cur, disposition, note, purpose: sessPurpose,
                                 kind: "outcome" });
    // Not an error -- the outcome IS saved -- but the rep should not be left
    // believing the CRM has it when it does not.
    dialErr = Dial.actNotice(res.act);
    // Only after the server has it. Clearing on tap would throw away the note
    // on exactly the failure where the rep still needs it.
    sessNote = ""; sessNoteOpen = false; sessPurpose = "";
    // Three ways to leave this person, each already positioning the cursor:
    // do-not-call removed them, call back moved them to the end, and anything
    // else leaves them in place so the cursor has to move.
    if (Dial.REQUEUE.has(disposition)) await Dial.requeue(cur.crd);
    else if (!res.removedCurrent) await Dial.advance();
    armFieldAutoDial();
  } catch (e) {
    // Stay on this person. Losing our place is a far smaller harm than a rep
    // believing a call was recorded when it was not.
    // The server's message is authoritative; only add the reassurance it did
    // not already give, so the box never reads "Nothing was saved. Nothing was
    // saved."
    dialErr = /saved/i.test(e.message) ? e.message
                                       : `${e.message} Nothing was saved.`;
  }
  renderDial();
}

/* ---------- national name search -----------------------------------------
 * Prefix-sharded static files rather than a query endpoint. Managed Functions
 * cold-start in seconds, which is the wrong latency for search-as-you-type; a
 * file from a CDN edge is always warm. Worst shard in the country is 83 KB
 * gzipped, median 28 records. */
async function loadNameIndex(){
  if (NAMES) return NAMES;
  const d = await (await fetch(dataUrl("name_index.json"))).json();
  NAMES = { cols: d.columns, shards: new Set(d.shards), split: new Set(d.split),
            unplaced: d.unplaced, searchable: d.searchable };
  NAMES.C = {}; d.columns.forEach((c, i) => { NAMES.C[c] = i; });
  return NAMES;
}

// Longest available prefix wins. Heavy prefixes (ja, jo, ma...) were split to
// three letters at build time, so their two-letter file does not exist -- and
// `split` lets us say "keep typing" instead of returning a silent nothing.
function shardFor(term){
  if (NAMES.shards.has(term.slice(0, 3))) return term.slice(0, 3);
  if (NAMES.shards.has(term.slice(0, 2))) return term.slice(0, 2);
  return null;
}

async function loadShard(key){
  if (!NAME_CACHE.has(key)) {
    const r = await fetch(dataUrl(`names/${key}.json`));
    NAME_CACHE.set(key, r.ok ? (await r.json()).rows : []);
  }
  return NAME_CACHE.get(key);
}

// A search hit from the index carries only enough to list it. Opening the card
// needs the full record, which lives in that advisor's tile -- so the tile is
// fetched on tap rather than for every result.
async function hydrate(crd, cell){
  const rows = await loadTiles([cell]);
  return rows.find((r) => r[COL.crd] === crd) || null;
}

/* ---------- locating and searching --------------------------------------- */
function within(rows, miles){
  return rows
    .map((r) => [milesBetween(HERE.lat, HERE.lon, r), r])
    .filter(([d]) => d <= miles)
    .sort((a, b) => a[0] - b[0])
    .map(([, r]) => r);
}

async function searchRadius(auto){
  const miles = RADII[radiusIdx];
  $("useLoc").hidden = true;
  $("status").textContent = `Loading advisors within ${miles} miles…`;
  const rows = await loadTiles(neighbourhood(HERE.lat, HERE.lon, miles));
  const near = within(rows, miles);

  // A rep in Bozeman gets 71 advisors inside 25 miles and the same 71 inside
  // 50 -- the neighbouring cells are empty. They should not have to work out
  // that they are in a sparse area; the app already knows.
  if (near.length < THIN && radiusIdx < RADII.length - 1) {
    radiusIdx++;
    return searchRadius(true);
  }

  rememberWhere();
  const states = [...new Set(near.map((r) => r[COL.state]))].filter(Boolean).sort();
  render(near, `<b>${near.length.toLocaleString()}</b> within ${miles} miles`
    + (states.length > 1 ? ` across ${states.join(", ")}` : "")
    + (auto ? " &middot; widened automatically" : ""));

  // Locating while a search is on screen must REFINE it, not replace it. A rep
  // who typed a firm, was told the area was not loaded, and tapped the button
  // to load it, has just watched their search vanish into a list of everyone
  // nearby -- which looks exactly like the feature not working.
  const q = $("q").value.trim();
  if (q.length >= 2) await search(q);
}

/* ---------- where "here" is ------------------------------------------------
 * The radius search always ran from the GPS, which answers "who is near me"
 * and cannot answer "who is near where I am going on Thursday" -- the question
 * a rep asks while planning a trip, which is most of what this view is for
 * between appointments.
 *
 * Same search, different centre. The only new machinery is a way to name a
 * place, and that comes from geo_index.json, which the desktop already uses
 * for its location type-ahead. No external geocoder, no API key in the page:
 * suggestions are drawn entirely from our own geocoded data, so a place we
 * cannot offer is a place we have no advisors in anyway.
 */
let WHERE_LABEL = "";              // "Charlotte, NC" — or "" for the GPS
let GEO = null;                    // {zips, cities}, loaded on first use

/* The radius a fresh search starts at.
 *
 * ONE FUNCTION, because there are three callers -- "Near me", picking a place,
 * and the saved starting point at boot -- and every one of them used to write
 * `radiusIdx = 0` for itself. The rep's setting was then applied at boot and
 * immediately overwritten by the place lookup that followed it, so the
 * preference silently did nothing on exactly the path it was set for.
 *
 * Still only a STARTING point: searchRadius widens automatically when an area
 * turns out to be thin, which is the behaviour that matters in Montana.
 */
function startRadiusIdx(){
  const i = Number(Dial.setting("fieldRadius", "0"));
  return i >= 0 && i < RADII.length ? i : 0;
}

function renderWhere(){
  const el = $("whereNow");
  el.hidden = !HERE;
  if (!HERE) return;
  el.innerHTML = WHERE_LABEL
    ? `Showing advisors near <b>${esc(WHERE_LABEL)}</b> `
      + `<button id="whereClear" class="linkish">use my location</button>`
    : `Showing advisors near <b>your location</b>`;
}

// 305 KB gzipped, so NOT on the critical path -- fetched the first time the
// rep opens the picker, which is the first moment it can possibly be wanted.
async function loadGeo(){
  if (GEO) return GEO;
  GEO = await (await fetch(dataUrl("geo_index.json"))).json();
  return GEO;
}

// City, ZIP or "City, ST". Ranked by how many advisors are there, because a
// rep typing "SPRINGFIELD" means the one with 300 advisors far more often than
// the one with two.
function placeMatches(term){
  const q = String(term || "").trim().toUpperCase();
  if (q.length < 2 || !GEO) return [];
  const out = [];
  if (/^\d{3,5}$/.test(q)) {
    for (const z in GEO.zips) {
      if (!z.startsWith(q)) continue;
      const [st, lat, lon, n] = GEO.zips[z];
      out.push({ label: `ZIP ${z}, ${st}`, lat, lon, n });
      if (out.length > 40) break;
    }
  }
  // "CHARLOTTE, NC" — the state half narrows a name that repeats.
  const [namePart, statePart] = q.split(",").map((s) => s.trim());
  for (const c in GEO.cities) {
    if (!c.startsWith(namePart)) continue;
    for (const [st, lat, lon, n] of GEO.cities[c]) {
      if (statePart && st !== statePart) continue;
      out.push({ label: `${titleCase(c)}, ${st}`, lat, lon, n });
    }
    if (out.length > 200) break;
  }
  out.sort((a, b) => b.n - a.n);
  return out.slice(0, 25);
}

const titleCase = (s) => String(s).toLowerCase()
  .replace(/\b[a-z]/g, (c) => c.toUpperCase());

/* THE INPUT IS BUILT ONCE, AND NEVER REBUILT WHILE IT IS BEING TYPED IN.
 *
 * This used to replace the whole sheet's innerHTML on every keystroke -- the
 * heading, the search box and the results together -- and then put focus and
 * the caret back afterwards. It worked, in the sense that the letters arrived.
 * But destroying the focused input drops the sheet's scroll to zero, and the
 * focus() that follows scrolls it back: on every single keypress the screen
 * jumped to the top and returned. Restoring the caret hid the data loss and
 * left the movement, which is the part a rep actually sees.
 *
 * So the chrome is painted once and only the RESULTS are re-rendered. Nothing
 * the rep is touching is destroyed, so there is no focus to restore, no caret
 * to put back, and no scroll to lose.
 */
function placesResultsHtml(rows){
  return `${!GEO ? `<p class="lists-none">Loading places…</p>`
    : placeTerm.trim().length < 2
      ? `<p class="lists-none">Type a city or ZIP.</p>`
      : rows.length
        ? `<ul class="places-ul">${rows.map((r, i) => `
            <li><button data-place="${i}">
              <span class="place-name">${esc(r.label)}</span>
              <span class="place-n">${r.n.toLocaleString()} advisor${r.n === 1 ? "" : "s"}</span>
            </button></li>`).join("")}</ul>`
        : `<p class="lists-none">No place on file matches that. We can only
           offer places we hold advisors in.</p>`}
    ${HERE && WHERE_LABEL ? `<button id="placeDefault" class="place-default">
       Make ${esc(WHERE_LABEL)} my default starting point</button>` : ""}`;
}

function renderPlaces(term){
  const el = $("places");
  el.hidden = !placesOpen;
  if (!placesOpen) return;
  const rows = placeMatches(term === undefined ? placeTerm : term);
  // Painted only when it is not already there -- which is the first render of
  // each opening, and the recovery path after a load failure replaced it.
  if (!$("placeQ") || !$("placesResults")) {
    $("placesInner").innerHTML = `
      <button id="placesClose" aria-label="Close">&times;</button>
      <h2>Work from somewhere else</h2>
      <input id="placeQ" type="search" inputmode="search" autocomplete="off"
        placeholder="City or ZIP — e.g. Charlotte, NC"
        aria-label="City or ZIP" value="${esc(placeTerm)}">
      <div id="placesResults"></div>`;
  }
  $("placesResults").innerHTML = placesResultsHtml(rows);
  $("placesInner")._rows = rows;
}

async function goToPlace(p){
  HERE = { lat: p.lat, lon: p.lon };
  WHERE_LABEL = p.label;
  radiusIdx = startRadiusIdx();
  placesOpen = false;
  renderPlaces();
  await loadIndex();
  if (!neighbourhood(HERE.lat, HERE.lon, RADII[RADII.length - 1]).length) {
    renderWhere();
    render([], `No advisors on file near ${p.label}.`);
    return;
  }
  await searchRadius(false);
  renderWhere();
}

function nearMe(){
  if (!navigator.geolocation) {
    $("status").textContent = "This device will not share a location. Search by name instead.";
    return;
  }
  $("status").textContent = "Finding you…";
  navigator.geolocation.getCurrentPosition(async (p) => {
    HERE = { lat: p.coords.latitude, lon: p.coords.longitude };
    WHERE_LABEL = "";
    renderWhere();
    radiusIdx = startRadiusIdx();
    await loadIndex();
    if (!neighbourhood(HERE.lat, HERE.lon, RADII[RADII.length - 1]).length) {
      render([], "No advisors on file anywhere near here.");
      return;
    }
    await searchRadius(false);
    renderWhere();
  }, (err) => {
    $("status").textContent = err.code === 1
      ? "Location permission denied. Search by name instead."
      : "Could not get a location. Search by name instead.";
  }, { enableHighAccuracy: false, timeout: 10000, maximumAge: 300000 });
}

/* The field index only holds advisors we have contact detail for. When a search
   finds nobody, the honest next step is usually the full map -- it carries every
   registered advisor, so a firm name and a website is often there even when a
   phone number is not. Rather than making the rep retype the name on the other
   page, hand the term over in the URL. */
function setDeskLink(term){
  const link = $("deskLink");
  const q = (term || "").trim();
  if (q) {
    link.textContent = "Search in full map";
    link.href = `index.html#q=${encodeURIComponent(q)}`;
  } else {
    link.textContent = "Full map";
    link.href = "index.html";
  }
}

async function search(term){
  const q = term.trim().toLowerCase();
  const seq = ++searchSeq;
  setDeskLink("");
  // The session gets out of the way while searching and comes back when the
  // box is cleared. Not paused -- looking someone up mid-session is normal,
  // and making it cost a re-Start would stop reps doing it.
  const wasCollapsed = sessCollapsed;
  sessCollapsed = q.length >= 2;
  if (sessCollapsed !== wasCollapsed) renderDial();
  if (q.length < 2) {
    $("useLoc").hidden = true;
    if (ROWS.length) render(ROWS);
    return;
  }
  const terms = q.split(/\s+/).filter(Boolean);
  const all = (hay) => terms.every((t) => hay.includes(t));

  // Loaded tiles are the only place FIRM is searchable, and that is a property
  // of the index rather than an oversight: the national shards are keyed on
  // name prefixes, so putting firms in them would file every UBS advisor in the
  // country under "ub" -- one 20,000-record shard, which is the exact shape the
  // prefix design exists to avoid. Firm search is therefore geographic, and
  // answers the question a rep in a car park actually has: who from this firm
  // is near me.
  //
  // Matched against name, firm, city and state together rather than name-OR-
  // firm, so "ubs seal beach" and "raymond james atlanta" work. Each term has
  // to appear somewhere; which field it landed in does not matter.
  const firmOnly = (r) => all(String(r[COL.firm] || "").toLowerCase())
                          && !all(String(r[COL.name] || "").toLowerCase());
  const local = [...TILE_CACHE.values()].flat().filter((r) => all(
    `${r[COL.name]} ${r[COL.firm]} ${r[COL.city]} ${r[COL.state]}`.toLowerCase()));
  const localIds = new Set(local.map((r) => r[COL.crd]));
  const firmHits = local.filter(firmOnly).length;

  await loadNameIndex();
  const key = shardFor(terms[0]);
  let national = [];
  let note = "";
  if (!key && NAMES.split.has(terms[0].slice(0, 2)) && terms[0].length < 3) {
    note = " &middot; keep typing for a national search";
  } else if (key) {
    const rows = await loadShard(key);
    if (seq !== searchSeq) return;          // a later keystroke already won
    // Matches the displayed name OR one of the alternate tokens the builder
    // could not derive from it -- which is how "bill" reaches William Kaiser.
    national = rows.filter((r) => {
      if (localIds.has(r[NAMES.C.crd])) return false;
      const hay = String(r[NAMES.C.name]).toLowerCase();
      const alt = String(r[NAMES.C.alt] || "").split(" ").filter(Boolean);
      return terms.every((t) =>
        hay.includes(t) || alt.some((a) => a.startsWith(t)));
    });
  }

  if (HERE) local.sort((a, b) =>
    milesBetween(HERE.lat, HERE.lon, a) - milesBetween(HERE.lat, HERE.lon, b));

  // Index records are shorter than tile rows. Padded to the same width with a
  // marker in `crd`-adjacent slots so render() can treat them uniformly and
  // simply show no call buttons -- the number lives in the tile.
  const padded = national.map((r) => {
    const row = new Array(INDEX.columns.length).fill("");
    row[COL.crd]   = r[NAMES.C.crd];
    row[COL.name]  = r[NAMES.C.name];
    row[COL.city]  = r[NAMES.C.city];
    row[COL.state] = r[NAMES.C.state];
    row[COL.assets] = 0;
    row[COL.ranked] = 0;
    row.remoteCell = r[NAMES.C.cell];       // how to hydrate on tap
    return row;
  });

  const hits = local.concat(padded);
  if (!hits.length) setDeskLink(term);

  // Without a location there are no tiles, so firm matching has nothing to look
  // at and the box silently behaves as if it only took names. Say so, and offer
  // the one action that fixes it.
  const noArea = !TILE_CACHE.size;
  $("useLoc").hidden = !noArea;

  render(hits, `<b>${hits.length.toLocaleString()}</b> matching “${esc(term.trim())}”`
    // "nearby" would be a lie on a national miss -- with no local and no index
    // hit, nothing was scoped to a location in the first place.
    + (local.length && padded.length
        ? ` &middot; ${local.length.toLocaleString()} nearby, ${padded.length.toLocaleString()} elsewhere`
        : padded.length ? " nationally" : local.length ? " nearby" : "")
    + (firmHits ? ` &middot; ${firmHits.toLocaleString()} by firm` : "")
    + (noArea ? " &middot; searching names only — firm search needs your area"
              : "")
    + (hits.length ? "" : " &middot; no contact detail on file — try <b>Search in full map</b> above")
    + note);
}

/* ---------- wiring -------------------------------------------------------- */
document.addEventListener("click", (e) => {
  const chip = e.target.closest("[data-chip]");
  if (chip) {
    const k = chip.dataset.chip;
    CHIPS[k] = !CHIPS[k];
    chip.setAttribute("aria-pressed", String(CHIPS[k]));
    chip.classList.toggle("on", CHIPS[k]);
    limit = PAGE;
    render(ROWS);
    return;
  }

  const open = e.target.closest("[data-open]");
  if (open) {
    const r = $("list")._shown[+open.dataset.open];
    if (r.remoteCell && !r[COL.phone] && !r[COL.email]) {
      // A national search hit: the tile carrying their phone and email has not
      // been loaded yet.
      hydrate(r[COL.crd], r.remoteCell).then((full) => openSheet(full || r));
    } else {
      openSheet(r);
    }
    return;
  }

  const mate = e.target.closest("[data-mate]");
  if (mate) {
    const r = [...TILE_CACHE.values()].flat()
      .find((o) => o[COL.crd] === mate.dataset.mate);
    if (r) openSheet(r);
    return;
  }

  /* Setting a flag from the phone.
   *
   * Repainted IN PLACE rather than by reopening the sheet: openSheet() sets
   * scrollTop = 0, so a rep who scrolled down to the history and then tapped
   * the shield would be thrown back to the top of the card. Same class of
   * jump as the place search had, and the reason to avoid a full rebuild here
   * is the same -- do not destroy what the rep is looking at.
   *
   * Dial.setFlag is optimistic and rolls itself back, so the mark moves on tap
   * and reverts if the write fails. The button is disabled meanwhile: it is a
   * round trip to a Function App that may be cold, and a second tap would
   * enqueue the opposite write.
   */
  const flag = e.target.closest("[data-flag]");
  if (flag) {
    const crd = flag.dataset.advisor, kind = flag.dataset.flag;
    const on = flag.getAttribute("aria-pressed") !== "true";
    const row = rowByCrd(String(crd));
    const marks = flag.closest(".contact-flags");
    flag.disabled = true;
    Dial.setFlag(crd, kind, on, (row && row[COL.name]) || "", "")
      .then(() => {
        // Both marks, because they render as a pair and the other one has to
        // keep showing what it showed.
        if (marks) marks.outerHTML = flagMarksField(crd);
        // The two standing lists are built from these flags; if the sheet
        // above them is open it is now out of date.
        if (listsOpen) renderLists();
      })
      .catch((err) => {
        flag.disabled = false;
        dialErr = err.message || "That could not be saved.";
        renderDial();
      });
    return;
  }

  if (e.target.closest("#sheetClose")) { $("sheet").hidden = true; return; }

  const call = e.target.closest("[data-call]");
  if (call) {
    const crd = String(call.dataset.call);
    // Open the grid for when they come back. The tel: link hands off to the
    // phone's dialer, so the rep leaves this page mid-call and returns to it --
    // and returning to a card with the outcome buttons already showing is the
    // difference between logging the call and meaning to.
    outcomeOpen.add(crd);
    logTouch(crd, "call", rowByCrd(crd));

    const inSheet = !!e.target.closest("#sheetInner");
    if (inSheet) {
      const d = document.querySelector("#sheetInner .log-out");
      if (d) d.open = true;
      return;
    }

    // THE LIST ROW'S DIAL BUTTON, which had nowhere to log at all.
    //
    // The outcome grid lives on the detail sheet, and this button never opened
    // it -- so a rep who dialled straight from the list made a call the app had
    // no way to record. The sheet was one tap away and they had no reason to
    // know that; from where they were sitting the feature simply did not exist.
    //
    // So dialling from the list OPENS the sheet, with the grid already
    // expanded. Same destination as tapping the row, reached by the action that
    // means "I am calling this person".
    //
    // Resolved through the LIST INDEX rather than rowByCrd, which only searches
    // loaded tiles: a row found by name search comes from the name shards and
    // has no tile behind it, so the one path most likely to be used from a
    // standing start is exactly the one that would have silently found nothing.
    const li = call.closest("li");
    const slot = li && li.querySelector("[data-open]");
    const shown = $("list")._shown || [];
    const r = (slot && shown[Number(slot.dataset.open)]) || rowByCrd(crd);
    if (r) openSheet(r);
    return;
  }

  // Fetched on FIRST open only. `open` still holds the pre-click value here,
  // so a falsy value means the browser is about to expand it.
  const hf = e.target.closest(".hist-full > summary");
  if (hf) {
    const det = hf.parentElement;
    if (!det.open && !det._loaded) {
      det._loaded = true;
      fillFullHistory(det.dataset.histfull, det.querySelector(".hist-body"));
    }
    return;
  }

  const mf = e.target.closest(".mail-full > summary");
  if (mf) {
    const det = mf.parentElement;
    if (!det.open && !det._loaded) {
      det._loaded = true;
      fillMailActivity(det.dataset.mailfull, det.querySelector(".mail-body"));
    }
    return;
  }

  // Reading one message. Fetched only on the tap, never with the list: on a
  // phone, on a car park signal, a timeline that dragged twenty message bodies
  // down with it would be worse than no timeline.
  const mv = e.target.closest("[data-mail-msg]");
  if (mv) {
    e.preventDefault();
    showMailMessage(mv.dataset.mailCrd, mv.dataset.mailMsg);
    return;
  }

  // Remembered, so re-rendering the sheet does not collapse it under the rep.
  const toggle = e.target.closest(".log-out > summary");
  if (toggle) {
    const d = toggle.parentElement;
    const crd = String((d.querySelector(".outcome") || {}).dataset
                       ? d.querySelector(".outcome").dataset.crd : "");
    // `open` still holds the PRE-click value here; the browser flips it after.
    if (d.open) outcomeOpen.delete(crd); else outcomeOpen.add(crd);
    return;
  }

  const mail = e.target.closest("[data-mail]");
  if (mail) {
    logTouch(mail.dataset.mail, "email", rowByCrd(mail.dataset.mail), adhocPurpose);
    return;
  }

  /* ---- dialer ---- */
  const qAdd = e.target.closest("[data-queue]");
  if (qAdd) {
    const r = rowByCrd(qAdd.dataset.queue);
    if (!r) return;
    if (Dial.inQueue(r[COL.crd])) {
      Dial.remove(r[COL.crd]).then(() => openSheet(r));
    } else {
      snapshotForQueue(r).then((snap) => Dial.add(snap)).then((res) => {
        if (res.blocked) alert("That advisor is on the firm-wide do-not-call list.");
        openSheet(r);
      });
    }
    return;
  }
  if (e.target.closest("#sessExpand")) {
    sessCollapsed = false;
    $("q").value = "";
    render(ROWS);
    renderDial();
    return;
  }
  if (e.target.closest("#qBack")) {
    // Does not un-log. It returns to the person so the outcome can be
    // corrected, which appends rather than rewrites.
    Dial.cancelAuto();
    Dial.back().then(renderDial);
    return;
  }
  if (e.target.closest("#qStart")) { Dial.start(); renderDial(); return; }
  if (e.target.closest("#qPause")) { Dial.cancelAuto(); Dial.pause(); renderDial(); return; }
  if (e.target.closest("#autoCancel")) { Dial.cancelAuto(); return; }
  if (e.target.closest("#qCycle")) {
    // The list is untouched; only the clock moves. Everyone becomes pending
    // again without deleting the history that says they were called last month.
    Dial.startCycle().then(() => { Dial.start(); renderDial(); });
    return;
  }
  // A real menu, not a prompt asking the rep to TYPE the word "rename".
  if (e.target.closest("#qMenu")) { listMenuOpen = !listMenuOpen; renderDial(); return; }
  if (e.target.closest("#lmAll")) {
    listMenuOpen = false;
    listsMode = "all";
    renderDial();
    openLists();
    return;
  }
  if (e.target.closest("#lmEdit")) {
    listMenuOpen = false;
    listsMode = "edit";
    listsOpen = true;
    renderDial();
    renderLists();
    return;
  }
  if (e.target.closest("#listsClose")) { listsOpen = false; listsMode = "all"; renderLists(); return; }
  const ledit = e.target.closest("[data-ledit]");
  if (ledit) {
    if (ledit.dataset.ledit === "back") { listsMode = "all"; openLists(); return; }
    const crd = ledit.dataset.crd;
    // Dial.remove already anchors on the person rather than the index, so
    // taking someone out from above the cursor does not skip whoever is next.
    Dial.remove(crd)
      .then(() => { renderListEdit(); renderDial(); })
      .catch((err) => alert(err.message || "That person could not be removed."));
    return;
  }

  /* ---- where "here" is ---- */
  if (e.target.closest("#placeBtn")) {
    placesOpen = true;
    renderPlaces();
    // Fetched on open, not at load: 305 KB gzipped is not something to spend
    // on a rep who only ever taps "Near me".
    loadGeo().then(() => renderPlaces()).catch(() => {
      $("placesInner").innerHTML =
        `<button id="placesClose" aria-label="Close">&times;</button>
         <h2>Work from somewhere else</h2>
         <p class="lists-none">The place list could not be loaded.</p>`;
    });
    setTimeout(() => { const b = $("placeQ"); if (b) b.focus(); }, 50);
    return;
  }
  if (e.target.closest("#placesClose")) { placesOpen = false; renderPlaces(); return; }
  const pick = e.target.closest("[data-place]");
  if (pick) {
    const rows = $("placesInner")._rows || [];
    const p = rows[Number(pick.dataset.place)];
    if (p) goToPlace(p);
    return;
  }
  if (e.target.closest("#whereClear")) { nearMe(); return; }
  if (e.target.closest("#placeDefault")) {
    saveSetting({ homeLabel: WHERE_LABEL, homeLat: String(HERE.lat),
                  homeLon: String(HERE.lon) });
    placesOpen = false;
    renderPlaces();
    $("status").textContent = `${WHERE_LABEL} is now your default starting point.`;
    return;
  }

  /* ---- needs attention ---- */
  if (e.target.closest("#workBtn")) {
    workOpen = true;
    renderWork();
    // Re-read on every open. A queue on a phone is looked at between meetings,
    // and a cached one from an hour ago is exactly the wrong answer.
    loadWork();
    return;
  }
  if (e.target.closest("#workClose")) { workOpen = false; renderWork(); return; }
  const workAct = e.target.closest("[data-work]");
  if (workAct) {
    const crd = workAct.dataset.crd;
    if (workAct.dataset.work === "follow") {
      // The action the queue is asking for. Without it a quiet_warm row
      // tells a rep to re-engage somebody and gives them nowhere to do it.
      showFollowUp(crd, workAct.dataset.name);
      return;
    }
    if (workAct.dataset.work === "open") {
      // Straight to the card, so the queue is one tap from a call. The tile for
      // their area may not be loaded -- the field app holds a few areas, not
      // the country -- so this is best-effort and says so rather than doing
      // nothing, which would read as a broken queue.
      const r = rowByCrd(crd);
      if (r) { workOpen = false; renderWork(); openSheet(r); }
      else alert("Full details are in the tile for their area, which is not loaded here.");
      return;
    }
    workAct.disabled = true;
    fetch("/api/email", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ op: workAct.dataset.op || "reply_state", crd,
                             state: workAct.dataset.state,
                             days: Number(workAct.dataset.days || 0) || undefined }) })
      // Checked, because it was not: a failed write used to reload the list as
      // though it had worked, and the row came back looking untouched.
      .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); })
      // Reloaded rather than removing the row: the advisor may still be in the
      // queue for a DIFFERENT reason, and hiding them would say "done" when
      // they are not.
      .then(loadWork)
      .catch(() => {
        workAct.disabled = false;
        const note = document.querySelector("#workInner .hist-notice")
          || $("workInner").insertAdjacentElement("afterbegin",
               Object.assign(document.createElement("p"), { className: "hist-notice" }));
        note.textContent = "That did not save. Try again.";
      });
    return;
  }

  /* ---- preferences ---- */
  if (e.target.closest("#settingsBtn")) {
    settingsOpen = true;
    renderSettings();
    return;
  }
  if (e.target.closest("#settingsClose")) {
    settingsOpen = false; renderSettings(); return;
  }
  // Confirmed, because this signs out of Microsoft on the device, not just this
  // app -- a mis-tap costs a sign-in everywhere, and that is worth one question.
  if (e.target.closest("#setTemplates") || e.target.closest("#setDocs")
      || e.target.closest("#setHealth")) {
    const which = e.target.closest("#setTemplates") ? "templates"
      : e.target.closest("#setHealth") ? "health" : "docs";
    settingsOpen = false; renderSettings();
    EmailComposer.openAdmin(which);
    return;
  }
  if (e.target.closest("#setSignOut")) {
    if (confirm("Log out of the advisor map and of Microsoft on this device?")) Dial.signOut();
    return;
  }
  if (e.target.closest("#setHomeClear")) {
    saveSetting({ homeLabel: "", homeLat: "", homeLon: "" })
      .then(() => renderSettings());
    return;
  }

  const lb = e.target.closest("[data-lists]");
  if (lb) {
    const act = lb.dataset.lists;
    const id = lb.dataset.id;
    const l = (Dial.state.lists || []).find((x) => x.id === id);
    const fail = (err) => { dialErr = err.message || "That could not be saved.";
                            renderDial(); };
    if (act === "new") {
      const box = document.getElementById("listsName");
      const name = ((box && box.value) || "").trim() || defaultListName();
      Dial.createList(name)
        .then(() => Dial.loadLists())
        .then(() => { renderLists(); renderDial(); })
        .catch(fail);
      return;
    }
    /* BEFORE the `if (!l) return` below, deliberately.
     *
     * A flag list has no list id -- it is built from the flags as they stand
     * right now -- so it would fall straight through that guard and the tap
     * would do nothing at all, silently. That is the same shape as the bug
     * that hid the teammate picker.
     *
     * A SNAPSHOT into an ordinary list, matching the desk: a session that
     * reordered itself because somebody starred a contact elsewhere would lose
     * the rep's place mid-call.
     */
    if (act === "flag-call" || act === "flag-show") {
      const kind = lb.dataset.kind;
      const label = kind === "key" ? "Key contacts" : "Due diligence";
      if (act === "flag-show") {
        const names = flaggedField(kind).map((x) => x.name);
        dialErr = `${label}: ${names.join(", ")}`;
        renderDial();
        return;
      }
      lb.disabled = true;
      lb.textContent = "Finding…";
      dialableFlagged(kind).then(async (people) => {
        if (!people.length) {
          // Distinguishes "nobody is flagged" from "nobody flagged has a
          // number we can reach", which are different problems for the rep.
          dialErr = `Nobody on ${label} has a number on file.`;
          renderLists(); renderDial(); return;
        }
        listsOpen = false;
        await Dial.openList(label);
        await Dial.addMany(people, { phoneOnly: true });
        Dial.start();
        renderLists(); renderDial();
      }).catch((err) => { renderLists(); fail(err); });
      return;
    }
    if (!l) return;
    if (act === "call") {
      // Open it AND start. Opening alone would close this sheet onto the same
      // bar the rep was looking at before, with no sign the tap landed.
      listsOpen = false;
      Dial.openList(id).then(() => { Dial.start(); renderLists(); renderDial(); })
        .catch(fail);
      return;
    }
    if (act === "rename") {
      const name = prompt("Name for this list", l.name);
      if (!name || !name.trim() || name.trim() === l.name) return;
      onOtherList(id, () => Dial.renameList(name.trim()))
        .then(() => { renderLists(); renderDial(); }).catch(fail);
      return;
    }
    if (act === "empty") {
      if (!confirm(`Remove all ${l.count} people from "${l.name}"?\n\n`
                   + `The list stays; your call history is kept.`)) return;
      onOtherList(id, () => Dial.clear())
        .then(() => { renderLists(); renderDial(); }).catch(fail);
      return;
    }
    if (act === "drop") {
      if (!confirm(`Delete the list "${l.name}"?\n\nYour call history is kept.`)) return;
      Dial.deleteList(id).then(() => Dial.loadLists())
        .then(() => { renderLists(); renderDial(); }).catch(fail);
      return;
    }
    return;
  }
  if (e.target.closest("#lmRename")) {
    const cur = Dial.state.listName;
    const name = prompt("Name for this list", cur);
    listMenuOpen = false;
    if (name && name.trim() && name.trim() !== cur) Dial.renameList(name.trim()).then(renderDial);
    else renderDial();
    return;
  }
  if (e.target.closest("#lmEmpty")) {
    const S = Dial.state;
    listMenuOpen = false;
    if (confirm(`Remove all ${S.items.length} people from "${S.listName}"?\n\n`
                + `The list stays; your call history is kept.`)) Dial.clear().then(renderDial);
    else renderDial();
    return;
  }
  if (e.target.closest("#lmDrop")) {
    const S = Dial.state;
    listMenuOpen = false;
    if (confirm(`Delete the list "${S.listName}"?\n\nYour call history is kept.`))
      Dial.deleteList(S.listId).then(renderDial);
    else renderDial();
    return;
  }

  const sCall = e.target.closest("[data-sess-call]");
  if (sCall) { logTouch(sCall.dataset.sessCall, "call", null); return; }
  const sMail = e.target.closest("[data-sess-mail]");
  if (sMail) {
    // The shared email composer intercepts this in the capture phase. This is
    // retained only for an older cached client: opening a composer is intent,
    // never evidence that an email was submitted or accepted.
    logTouch(sMail.dataset.sessMail, "email", null, sessPurpose);
    return;
  }

  // Opening the full card from a session needs the tile, which on a queue built
  // elsewhere may not be loaded -- so this is best-effort and silent.
  const sOpen = e.target.closest("[data-sess-open]");
  if (sOpen) {
    const r = rowByCrd(sOpen.dataset.sessOpen);
    if (r) openSheet(r);
    else alert("Full details are in the tile for their area, which is not loaded here.");
    return;
  }

  // Remembered so the box is still open after the next re-render, which the
  // countdown fires every second.
  const sNote = e.target.closest(".sess-note > summary");
  if (sNote) { sessNoteOpen = !sessNoteOpen; return; }

  // Tapping the chosen chip clears it. Repainted in place rather than through a
  // re-render, because the session card rebuild would also rebuild the note
  // textarea and take the cursor out of it mid-sentence.
  const pchip = e.target.closest("[data-purpose]");
  if (pchip) {
    const scope = pchip.closest("[data-purpose-scope]").dataset.purposeScope;
    const key = pchip.dataset.purpose;
    const now = (scope === "sess" ? sessPurpose : adhocPurpose) === key ? "" : key;
    if (scope === "sess") sessPurpose = now; else adhocPurpose = now;
    [...pchip.parentElement.children].forEach((b) => {
      const on = b.dataset.purpose === now;
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", String(on));
    });
    // The Email button's draft is built from this choice, and the card is
    // deliberately NOT re-rendered here (that would take the cursor out of the
    // note the rep may be typing). So the one thing the choice changes is
    // updated by hand.
    const scopeEl = scope === "sess" ? $("session") : $("sheetInner");
    const mail = scopeEl && scopeEl.querySelector(
      scope === "sess" ? "[data-sess-mail]" : "[data-mail]");
    if (mail) {
      const item = scope === "sess" ? Dial.current()
        : (() => { const r = rowByCrd(mail.dataset.mail); return r ? snapshotOf(r) : null; })();
      if (item) {
        mail.href = "#";
        mail.innerHTML = "&#9993; Email"
          + (now ? " (" + esc(Dial.purposeLabel(now)) + ")" : "");
      }
    }
    return;
  }

  const sOut = e.target.closest("[data-sess-outcome]");
  if (sOut) { recordOutcome(sOut.dataset.sessOutcome); return; }

  // Ad-hoc outcome from the detail sheet, outside any session.
  const out = e.target.closest("[data-outcome]");
  if (out) {
    const crd = out.closest(".outcome").dataset.crd;
    const r = rowByCrd(crd);
    if (!Dial.confirmGrave(out.dataset.outcome,
                           r ? { name: r[COL.name] } : { crd })) return;
    [...out.parentElement.children].forEach((b) => b.classList.remove("on"));
    out.classList.add("on");
    // Read from the DOM as well as the mirror: a rep can finish typing and tap
    // an outcome faster than the input event fires.
    const box = document.getElementById("adhocNote");
    const note = (box ? box.value : adhocNote).trim();
    const det = out.closest(".log-out");
    const slot = det.parentElement.querySelector(".log-out-note");
    const label = Dial.outcomeLabel(out.dataset.outcome);

    // CLOSE FIRST, THEN WAIT FOR THE SERVER.
    //
    // This used to close only once /api/log resolved, and that response is not
    // fast: the request writes to Table Storage and then makes up to five
    // sequential round trips to Act! -- authorize, schedule, clear, read back,
    // sometimes delete -- before it answers. So the rep tapped "Attempted",
    // nothing moved for a second or more, and the natural reading of a button
    // that does nothing is that the tap missed. That is how a call gets logged
    // twice.
    //
    // The tap is acknowledged immediately and the outcome is stated in words,
    // because "closed" alone is not a claim about what was recorded. THE WRITE
    // IS STILL AUTHORITATIVE: a failure reopens the block, restores the note
    // and says so in the red slot below. Nothing here is optimistic about the
    // SAVE -- only about the animation.
    det.open = false;
    outcomeOpen.delete(String(crd));
    slot.className = "log-out-note";
    slot.textContent = `Logging ${label}…`;

    Dial.log({ ...(r ? snapshotOf(r) : { crd }), disposition: out.dataset.outcome,
               note, purpose: adhocPurpose, kind: "outcome" })
      .then((res) => {
        adhocNote = "";
        adhocPurpose = "";
        slot.textContent = Dial.actNotice(res && res.act) || `Logged: ${label}.`;
      })
      .catch((err) => {
        // Put the rep back exactly where they were, with their words intact.
        out.classList.remove("on");
        det.open = true;
        outcomeOpen.add(String(crd));
        const b2 = document.getElementById("adhocNote");
        if (b2) b2.value = note;
        adhocNote = note;
        slot.className = "log-out-note bad";
        slot.textContent = /saved/i.test(err.message)
          ? err.message : `${err.message} Nothing was saved.`;
      });
  }
});

/* ---------- swipe to queue ------------------------------------------------
 * RIGHT, not left. Left-swipe on a list row means "delete" everywhere else on
 * this phone, and borrowing it to mean "add" fights muscle memory built by
 * every other app the rep uses.
 *
 * The ＋ button stays. Swiping is undiscoverable on its own, so it is an
 * accelerator for people who find it, never the only way through.
 */
const SWIPE_OPEN = 64;         // px of travel that counts as a swipe
const SWIPE_SLOP = 12;         // px of vertical drift that means "scrolling"
let sw = null;

function rowOf(t){ return t.closest ? t.closest("#list li") : null; }

$("list").addEventListener("touchstart", (e) => {
  if (e.touches.length !== 1) return;
  const li = rowOf(e.target);
  if (!li) return;
  sw = { li, x0: e.touches[0].clientX, y0: e.touches[0].clientY, dx: 0, locked: null };
}, { passive: true });

$("list").addEventListener("touchmove", (e) => {
  if (!sw) return;
  const dx = e.touches[0].clientX - sw.x0;
  const dy = e.touches[0].clientY - sw.y0;
  // Decide once whether this gesture is a scroll or a swipe, and never revisit
  // it -- a row that starts following a vertical scroll is worse than one that
  // ignores an ambiguous drag.
  if (sw.locked === null) {
    if (Math.abs(dy) > SWIPE_SLOP && Math.abs(dy) > Math.abs(dx)) { sw.locked = "scroll"; return; }
    if (Math.abs(dx) > SWIPE_SLOP) sw.locked = "swipe";
    else return;
  }
  if (sw.locked !== "swipe") return;
  sw.dx = Math.max(0, Math.min(dx, SWIPE_OPEN + 20));   // right only, with a stop
  sw.li.style.transform = `translateX(${sw.dx}px)`;
  sw.li.classList.toggle("swiping", sw.dx > 8);
  sw.li.dataset.swipeArmed = sw.dx >= SWIPE_OPEN ? "1" : "";
}, { passive: true });

$("list").addEventListener("touchend", async () => {
  if (!sw) return;
  const { li, dx } = sw;
  const armed = dx >= SWIPE_OPEN;
  sw = null;
  li.style.transform = "";
  li.classList.remove("swiping");
  delete li.dataset.swipeArmed;
  if (!armed) return;

  const rows = $("list")._shown || [];
  const slot = +li.querySelector("[data-open]").dataset.open;
  const r = rows[slot];
  if (!r) return;
  const crd = r[COL.crd];
  if (Dial.isDnc(crd)) { flashRow(slot, "Do not call"); return; }

  let msg;
  if (Dial.inQueue(crd)) {
    await Dial.remove(crd);
    msg = "Removed";
  } else {
    const res = await Dial.add(await snapshotForQueue(r));
    msg = res.added ? "Added to call list" : "Already queued";
  }
  // Render BEFORE flashing. The other way round put the confirmation on a row
  // that render() then replaced, so the swipe silently worked and looked as
  // though nothing had happened.
  render(ROWS);
  flashRow(slot, msg);
});

function flashRow(slot, msg){
  const li = $("list").children[slot];
  if (!li) return;
  const tag = document.createElement("span");
  tag.className = "swipe-flash";
  tag.textContent = msg;
  li.appendChild(tag);
  setTimeout(() => tag.remove(), 1100);
}

$("nearBtn").addEventListener("click", nearMe);
// Same action as "Near me", offered at the point the rep discovers they need
// it. searchRadius() re-runs the live search afterwards, so the firm they typed
// is still on screen when the tiles land.
$("useLoc").addEventListener("click", nearMe);
$("more").addEventListener("click", () => { limit += PAGE; render(ROWS); });
$("wider").addEventListener("click", async () => {
  if (radiusIdx < RADII.length - 1) { radiusIdx++; await searchRadius(false); }
});
$("q").addEventListener("input", (e) => search(e.target.value));

// Delegated, because the textarea is destroyed and rebuilt on every re-render.
document.addEventListener("input", (e) => {
  if (!e.target) return;
  if (e.target.id === "sessNote") sessNote = e.target.value;
  if (e.target.id === "adhocNote") adhocNote = e.target.value;
  if (e.target.id === "placeQ") {
    placeTerm = e.target.value;
    // Only the results are re-rendered; the box the rep is typing in is left
    // alone. No focus to restore, no caret to put back, and no scroll jump --
    // see renderPlaces().
    renderPlaces();
  }
});

// Speaking once on the enabling tap primes iOS speech synthesis, which will not
// start later outside a user gesture.
document.addEventListener("change", (e) => {
  if (e.target.id === "autoDial") {
    Dial.setAuto({ on: e.target.checked });
    if (e.target.checked) Dial.say("Auto dial on");
    renderDial();
    return;
  }
  if (e.target.id === "autoSay") {
    Dial.setAuto({ announce: e.target.checked });
    renderDial();
    return;
  }
  if (e.target.id === "setList") {
    saveSetting({ defaultListId: e.target.value });
    return;
  }
  if (e.target.id === "setRadius") {
    saveSetting({ fieldRadius: e.target.value });
    return;
  }
  if (e.target.id === "setAuto") {
    // Through Dial.setAuto so the session card, localStorage and the account
    // all move together -- setting it here directly would leave three copies
    // of one preference disagreeing.
    Dial.setAuto({ on: e.target.checked });
    renderDial();
    return;
  }
  if (e.target.id === "qList") {
    const v = e.target.value;
    if (v === "__new") {
      const name = prompt("Name for the new list", "");
      // Reopening the current list resets the <select>, which would otherwise
      // sit on "+ New list…" after a cancel.
      if (!name) { renderDial(); return; }
      Dial.createList(name.trim()).then(renderDial);
    } else {
      Dial.openList(v).then(renderDial);
    }
  }
});

/* ---------- coming back after a while -------------------------------------
 * A phone put down for an hour is the normal case, not an edge case. Two
 * things go wrong on return and neither announced itself:
 *
 *   the Entra session lapses, so every request answers with a sign-in page.
 *     Nothing checked, so the app looked fine until the rep pressed something.
 *   iOS discards the page, so a reload loses HERE and the nearby list with it,
 *     leaving "Tap Near me" after the rep had already done exactly that.
 *
 * The first is now checked on resume and stated plainly with a way out. The
 * second is fixed by remembering where the rep was.
 */
const WHERE_KEY = "advisorField.where.v1";

function rememberWhere(){
  if (!HERE) return;
  try {
    sessionStorage.setItem(WHERE_KEY, JSON.stringify(
      { lat: HERE.lat, lon: HERE.lon, radiusIdx, at: Date.now() }));
  } catch {}
}

// Only within the same sitting. A location from yesterday is worse than none:
// the rep has moved, and a list of "nearby" advisors 300 miles away is wrong
// in a way that looks right.
const WHERE_MAX_AGE = 3 * 60 * 60 * 1000;

async function restoreWhere(){
  let w = null;
  try { w = JSON.parse(sessionStorage.getItem(WHERE_KEY)); } catch {}
  if (!w || !w.lat || Date.now() - (w.at || 0) > WHERE_MAX_AGE) return false;
  HERE = { lat: w.lat, lon: w.lon };
  radiusIdx = Math.min(w.radiusIdx || 0, RADII.length - 1);
  await loadIndex();
  await searchRadius(false);
  return true;
}

function showSessionLapsed(){
  const el = $("dialProblem");
  el.hidden = false;
  el.innerHTML = "Your sign-in expired while the app was in the background. "
    + '<button id="reloadNow" class="ghost-btn">Reload to sign in</button>';
}

// `visibilitychange` rather than `focus`: iOS fires it reliably when the app
// comes back from the background, which `focus` does not.
document.addEventListener("visibilitychange", async () => {
  if (document.visibilityState !== "visible") { Dial.cancelAuto(); return; }
  try {
    const r = await fetch("/api/health", { credentials: "same-origin" });
    const type = r.headers.get("Content-Type") || "";
    if (!r.ok || !type.includes("json")) { showSessionLapsed(); return; }
    // Alive: pick up anything that changed on another device while away.
    await Dial.refreshQueue();
    await Dial.refreshDnc();
    // The queue moves while the app is in the background -- a sweep runs
    // every 15 minutes. A badge from an hour ago is the wrong answer to
    // the only question this button exists to answer.
    loadWork().catch(() => {});
    renderDial();
  } catch {
    showSessionLapsed();
  }
});

document.addEventListener("click", (e) => {
  if (e.target.closest("#reloadNow")) location.reload();
});

(async () => {
  ME = await whoAmI();
  await loadIndex();
  // Not awaited into the critical path: the map and the call list must not wait
  // on the book, and both render correctly without it.
  loadExtras().catch(() => {});
  // Same treatment for the queue badge. It is worth knowing three people
  // replied, and not worth a millisecond of "Near me".
  loadWork().catch(() => {});
  // The dialer boots independently of the map data: a rep opening the app to
  // resume yesterday's list should not wait on tile_index.json, and a failure
  // here must not stop "Near me" from working.
  Dial.onChange(renderDial);
  // If the page was discarded and reloaded, put the rep back where they were
  // rather than making them tap "Near me" again for the same answer. Tried
  // FIRST, because it is the most recent thing the rep actually did -- a saved
  // default is what to do when there is no such thing, not something that
  // should override where they were ten minutes ago.
  // Both started before either is awaited. The dialer's round trips and the
  // tile fetch are independent, and serialising them would add the slower of
  // the two to every cold start on a phone.
  const dialer = PERF.time("dial.init", () => Dial.init()).then(renderDial);
  const resumed = await PERF.time("restoreWhere (tiles + sort)",
                                  () => restoreWhere().catch(() => false));
  // Stamped here rather than after the dialer: this is the moment a rep can
  // read the list of advisors near them, which is what the app is for. The
  // dialer arriving a second later does not change that.
  PERF.usableAt = performance.now();
  PERF.mark("list-usable");
  console.info("[perf] list usable — run PERF.report() for the breakdown");
  await dialer;
  renderWhere();

  // A saved starting point makes the app useful on open rather than after a
  // tap. Only when nothing was resumed and nothing has been searched.
  const hLat = Number(Dial.setting("homeLat"));
  const hLon = Number(Dial.setting("homeLon"));
  if (!resumed && !HERE && hLat && hLon) {
    goToPlace({ lat: hLat, lon: hLon, label: Dial.setting("homeLabel") })
      .catch(() => {});
  }
})();

/* Shell-only caching -- see sw.js. Registered after load so it never competes
   with the first paint, and failure is silent by design: no worker just means
   the app behaves exactly as it did before, over the network.

   This lives here rather than in an inline <script> in field.html so that the
   page's Content-Security-Policy can be script-src 'self' with no inline
   escape hatch -- an allowance that would apply to every injection site on the
   page, to protect eight lines of registration code. */
if ("serviceWorker" in navigator) {
  addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => {}));
}
