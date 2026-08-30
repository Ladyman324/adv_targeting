/* Advisor Map. Leaflet + marker clustering over OSM tiles.
   Panel counts follow the map viewport; firms can be multi-selected to compare.

   Two scopes, because 535k advisor pins will not fit in a browser:
     US     offices_national.json -- one record per legal-firm office.
            Shared addresses retain every CRD and motion; advisor-level filters
            are disabled because this layer intentionally carries firm facts.
     <ST>   pins_<ST>.geojson -- every advisor-branch pairing, all filters live.
   Switching scope swaps the data source; nothing else about the UI changes. */

// getComputedStyle is a forced style read. renderMarkers asks for the marker
// colour once per pin, so a West-territory draw performed ~82,000 of them and
// the cluster icon builder added more. Custom properties only change when the
// colour scheme flips, so cache them and drop the cache on that event.
const CSS_VAR_CACHE = new Map();
function cssVar(n){
  let v = CSS_VAR_CACHE.get(n);
  if (v === undefined){
    v = getComputedStyle(document.documentElement).getPropertyValue(n).trim();
    CSS_VAR_CACHE.set(n, v);
  }
  return v;
}
try {
  matchMedia("(prefers-color-scheme: dark)")
    .addEventListener("change", () => CSS_VAR_CACHE.clear());
} catch {}

// Map marks live on a permanently light basemap, so their colour must not follow
// the panel theme. The dark theme's --accent (#54d4bf) was being painted on it:
// 1.58:1 against OSM land, 1.39:1 over parkland, and white numerals on it at
// 1.82:1. --map-accent is fixed and reads 6.23 / 5.48 / 7.15 for the same three.
const mapAccent = () => cssVar("--map-accent");
const mapMuted = () => cssVar("--map-muted");

// selected-firm color; the array remains for restoring older saved state safely
const COMPARE = ["#12b39c", "#e0a53a", "#8079e0", "#e8615d", "#4aa3e0", "#9fc93c"];
// Stamped by src/web_assets.py from the normalized metadata time plus a digest
// of every deployed JSON path and byte. It changes for standalone shard
// rebuilds too, and its leading date keeps the stale-build warning readable.
// Do not edit it by hand.
const DATA_VERSION = "20260830T123258Z-21ddbb8031c2bde3";
const dataUrl = file => `data/${file}?v=${DATA_VERSION}`;
Dial.setContactRouteVersion(DATA_VERSION);
// ONE scale for every mark on the map. There used to be two, and they were not
// comparable: buildings grew as 20 + 5.2*sqrt(n) and saturated at 56px by just
// 48 advisors, while clusters used a cubed log that stayed near 20px well into
// the hundreds. The visible result was a 22-advisor building drawn 70% larger
// than a 99-advisor cluster.
//
// The anchor is measured rather than assumed. The old 15,000 was a guess that
// squashed everything real into the bottom of the curve; after de-duplication
// the largest building in the West territory holds 849 advisors and the largest
// cluster at territory zoom is about 2,800.
// The floor sits well below the old 20px so a lone advisor recedes. Single- and
// two-advisor offices are the most numerous marks on screen -- 34 of 126 in a
// metro view -- and at a 22px floor with a 2px ring each was competing for
// attention with a 32-advisor cluster. Concentration should draw the eye, not
// the sheer number of marks.
const MARK_MIN_PX = 14, MARK_MAX_PX = 78, MARK_ANCHOR = 3000, MARK_CURVE = 1.3;
function markDiameter(n){
  const t = Math.log(1 + Math.max(1, n)) / Math.log(1 + MARK_ANCHOR);
  return Math.round(MARK_MIN_PX +
    (MARK_MAX_PX - MARK_MIN_PX) * Math.pow(Math.min(1, t), MARK_CURVE));
}
const clusterDiameter = markDiameter;

// Type has to scale with the mark. The size was fixed at 11-12px while marks now
// span 17-78px, so a single advisor wore a 12px digit across 71% of a 17px
// circle while the same 12px covered 27% of a 45px one. Digit count matters as
// much as diameter -- "2,181" needs room a "1" does not.
function markFont(diameter, label){
  // Diameter drives it, so type grows monotonically with the mark. Letting digit
  // count drive it instead made a 6 render larger than a 12 -- the smaller
  // number looking bigger, which is worse than either being slightly off.
  let size = Math.max(8, Math.min(12, Math.round(diameter * 0.30)));
  // then shrink only if the digits genuinely will not fit inside the ring
  const inner = diameter - 6;
  const chars = String(label).length;
  while (size > 7 && chars * size * 0.62 > inner) size -= 1;
  return size;
}

const STATE_NAMES = {
  AK:"Alaska",AL:"Alabama",AR:"Arkansas",AZ:"Arizona",CA:"California",CO:"Colorado",
  CT:"Connecticut",DC:"District of Columbia",DE:"Delaware",FL:"Florida",GA:"Georgia",
  HI:"Hawaii",IA:"Iowa",ID:"Idaho",IL:"Illinois",IN:"Indiana",KS:"Kansas",KY:"Kentucky",
  LA:"Louisiana",MA:"Massachusetts",MD:"Maryland",ME:"Maine",MI:"Michigan",MN:"Minnesota",
  MO:"Missouri",MS:"Mississippi",MT:"Montana",NC:"North Carolina",ND:"North Dakota",
  NE:"Nebraska",NH:"New Hampshire",NJ:"New Jersey",NM:"New Mexico",NV:"Nevada",
  NY:"New York",OH:"Ohio",OK:"Oklahoma",OR:"Oregon",PA:"Pennsylvania",PR:"Puerto Rico",
  RI:"Rhode Island",SC:"South Carolina",SD:"South Dakota",TN:"Tennessee",TX:"Texas",
  UT:"Utah",VA:"Virginia",VI:"U.S. Virgin Islands",VT:"Vermont",WA:"Washington",
  WI:"Wisconsin",WV:"West Virginia",WY:"Wyoming",
};
const OUTSIDE_CONTINENTAL = new Set(["AK", "HI", "PR", "VI"]);
// drawing every one of 133k offices stalls the main thread, so the national
// layer renders the largest offices in view -- the ones a national map is for
const NAT_CAP = 4000;
const HEAT_HIT_CAP = 600;

// EIC sales territories -> the states each senior director covers, from
// eicatlanta.com/contact. VI folds into Florida/PR (Borland's Caribbean book).
// A territory is a scope like a state, but stitched from several state files.
const TERRITORIES = {
  "West":        ["CA","NV","OR","ID","WA","MT","WY","AK","HI"],
  "Southwest":   ["UT","AZ","CO","NM","KS","OK","TX","AR","LA"],
  "Midwest":     ["KY","OH","IN","IL","MO","IA","NE","SD","ND","MN","WI","MI"],
  "Southeast":   ["MS","TN","AL","GA","SC","NC"],
  "Mid-Atlantic":["DC","MD","DE","NJ","PA","WV","VA"],
  "Northeast":   ["CT","RI","MA","NY","VT","NH","ME"],
  "Florida/PR":  ["FL","PR","VI"],
};
const TERRITORY_LEAD = {
  "West":"Steve Halley", "Southwest":"Tate Lambeth", "Midwest":"Steve Zimmerman",
  "Southeast":"Matt Keeter", "Mid-Atlantic":"Keith Telesca",
  "Northeast":"Dennis McKinney", "Florida/PR":"Sam Borland",
};
const STATE_TO_TERRITORY = {};
Object.entries(TERRITORIES).forEach(([territory, states]) =>
  states.forEach(state => { STATE_TO_TERRITORY[state] = territory; }));
const terrKey = name => "T:" + name;           // scope value for a territory
let GEO = null;                                 // {zips, cities} location index
let OWNER_ROLES = null;                         // advisor CRD -> Schedule A roles
let BARRONS = null;                             // advisor CRD -> Barron's rankings
let FORBES = null;                              // advisor CRD -> Forbes rankings
let CONTACTS = null;                            // advisor CRD -> phone and email
const SUPPORT = {
  geo:"idle", meta:"idle", owner:"idle", barrons:"idle",
  forbes:"idle", act:"idle", territories:"idle",
};
const SUPPORT_ERROR = {};
let regionalSupportPromise = null;
function supportStart(key){ SUPPORT[key] = "loading"; delete SUPPORT_ERROR[key]; }
function supportReady(key){ SUPPORT[key] = "ready"; delete SUPPORT_ERROR[key]; }
function supportFailed(key, err){
  SUPPORT[key] = "failed";
  SUPPORT_ERROR[key] = (err && err.message) || String(err || "unavailable");
}

let scope = "US";                   // "US" or a two-letter state code
let NAT = null;                     // firms:[[name,crd,score,raum_m,selects,equity_m,funds_m,mapped_advisors]], offices:[[lon,lat,n,fi,m,nf,si,oi]]
let NAT_FIRM_BY_CRD = new Map();
let NAT_PLACEMENTS_BY_CRD = new Map();
let META = null;
let ME = null;                      // /.auth/me clientPrincipal, for the settings panel
let ADMIN = false;                  // EmailAdministrator, per the server's catalog
let ADV_INDEX = null;
let ADV_INDEX_PROMISE = null;
let FIRM_PROFILES = null;
let FIRM_PROFILE_PROMISE = null;
let openFirmCrd = null;
let ALL = [];
let reg = "all";                    // all | dual | ria
let expSel = new Set();             // empty = all; else subset of lt10|10to20|gt20
let reachSel = new Set();           // empty = all; else subset of one|few|many
let geoSel = new Set();             // empty = all; else rooftop|approximate|neighbour
let advQuery = "";                  // advisor name or CRD substring
let focusedAdvisorId = null;        // explicit advisor-result map focus
let focusedAdvisorLabel = "";
const BUILDING_MARKERS = new Map(); // building key -> its Leaflet marker
const natViz = "heat";              // national view is intentionally automatic
let selectedFirms = [];             // highlighted firm CRDs, in colour-assignment order
let firmColor = {};                 // selected firm CRD -> highlight colour
// 5.G(7) is ON by default: firms that hire outside managers are the whole point
// of the tool, so the unfiltered map is the unusual state, not the resting one.
// "Reset all" therefore restores this rather than clearing it -- clearing it
// left a rep looking at every adviser in the country and calling that a reset.
const SELECTS_DEFAULT = true;
let selectsOnly = SELECTS_DEFAULT;  // persists across scope changes
let aumSel = new Set();              // empty = Any; otherwise union of AUM bands
let firmSort = "advisors";          // firm-list order: advisors | relevant AUM
let ownerOnly = false;              // Advanced: only firm owners and officers
let rankedOnly = false;             // Advanced: only advisors on a published ranking
let contactableOnly = false;        // only advisors with an email or a phone on file
let assetsOnly = false;             // only advisors with a team or individual asset figure
// Firms struck off the map. Distinct from selectedFirms, which is a positive
// "show only these"; this is a negative "show everything except these", and a
// rep builds it up one firm at a time while working a territory -- the wirehouse
// they already cover, the broker-dealer that is not a prospect.
let excludedFirms = new Set();
let continentalOnly = true;
try { continentalOnly = sessionStorage.getItem("advisorMap.continentalOnly") !== "false"; }
catch {}

const AUM_BANDS = {
  all:     { label:"Any",          lo:0,      hi:Infinity },
  lt100m:  { label:"<$100M",       lo:0,      hi:100e6 },
  "100m1b": { label:"$100M–$1B",  lo:100e6,  hi:1e9 },
  "1b10b":  { label:"$1B–$10B",   lo:1e9,    hi:10e9 },
  "10b100b":{ label:"$10B–$100B", lo:10e9,   hi:100e9 },
  gt100b:  { label:">$100B",       lo:100e9,  hi:Infinity },
};
function fmtMoney(d){
  if (d >= 1e12) return "$" + (d / 1e12).toFixed(d < 1e13 ? 1 : 0) + "T";
  if (d >= 1e9)  return "$" + (d / 1e9).toFixed(d < 1e10 ? 1 : 0) + "B";
  if (d >= 1e6)  return "$" + Math.round(d / 1e6) + "M";
  // Thousands, and separators below that. The ADV figures this formatter was
  // written for are always billions, so the sub-million branch never showed --
  // until EIC's own book landed in the same bar, where a single county can be
  // a few hundred thousand and used to render as "$345917". money() has
  // handled K since it was written; this brings the two into line.
  if (d >= 1e3)  return "$" + Math.round(d / 1e3) + "K";
  return "$" + Math.round(d).toLocaleString("en-US");
}
function firmIapdUrl(crd){
  return `https://adviserinfo.sec.gov/firm/summary/${encodeURIComponent(crd)}`;
}
// The AUM predicate runs once per pin per pass, several passes per map move.
// Spreading the Set into an array inside that predicate allocated ~80k arrays
// per pass on a territory, so the selected bounds are flattened once here and
// every caller reads the flat list. syncTargetingUI() is the single writer.
let AUM_BOUNDS = [];
function syncAumBounds(){
  AUM_BOUNDS = [...aumSel].map(key => AUM_BANDS[key]).filter(Boolean)
    .map(band => [band.lo, band.hi]);
}
function raumActive(){ return AUM_BOUNDS.length > 0; }
function inAumBands(dollars){
  for (let i = 0; i < AUM_BOUNDS.length; i++)
    if (dollars >= AUM_BOUNDS[i][0] && dollars < AUM_BOUNDS[i][1]) return true;
  return false;
}
// Relevant AUM = equities + funds/ETFs. The firm rows show it allocated to what
// is actually on screen, by the same rule the AUM card uses: the firm's pool
// times its share of mapped advisors in view. Returning the whole national pool
// put a figure that never moved next to an advisor count that follows the
// viewport -- a wirehouse read the same number zoomed to one Atlanta office as
// it did nationally, which made the column useless for ranking a territory.
// Pass visible = null for the unallocated national pool.
function relevantAumForFirm(crd, visible, placements=false){
  const firm = NAT_FIRM_BY_CRD.get(String(crd));
  if (!firm) return null;
  if (!Number.isFinite(firm[5]) || !Number.isFinite(firm[6])) return null;
  const pool = (firm[5] + firm[6]) * 1e6;
  if (visible == null) return pool;
  const denominator = placements ? NAT_PLACEMENTS_BY_CRD.get(String(crd)) : firm[7];
  if (!denominator) return null;
  return pool * Math.min(1, visible / denominator);
}

// ---- map ----
// Start where the first useful view will be. The old Georgia camera caused a
// visible jump and fetched tiles discarded as soon as national data arrived.
const map = L.map("map", { preferCanvas:true, zoomAnimation:false });
if (continentalOnly) map.setView([39.5, -96.5], 4);
else map.fitBounds([[17, -178], [72, -64]], {
  padding:[12,12], maxZoom:3, animate:false,
});
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19, attribution: "© OpenStreetMap contributors",
}).addTo(map);

function createClusterLayer(){
  return L.markerClusterGroup({
    showCoverageOnHover: false, maxClusterRadius: 48,
    spiderfyOnMaxZoom: true, animate: false,
    iconCreateFunction: group => {
      // Advisor headcount, not marker count: children are buildings now, so
      // getChildCount() would label a cluster with how many BUILDINGS it holds.
      // An advisor filed at two buildings inside one cluster is counted twice --
      // 8.4% of Georgia advisors and 15.8% of West sit at more than one address,
      // less than that within any single cluster. The panel KPI stays the exact
      // distinct-advisor figure; this is a placement count.
      const count = group.getAllChildMarkers()
        .reduce((sum, m) => sum + (m.options.advisorCount || 1), 0);
      // Continuous log sizing makes 100, 1,000, and 8,000 meaningfully
      // different without allowing the largest metros to dominate the map.
      const diameter = clusterDiameter(count);
      // Cluster children are building divIcons now, not per-advisor
      // circleMarkers, so the old read of marker.options.fillColor returned
      // nothing and every cluster fell through to the default teal -- four
      // selected firms produced a uniformly green map. Roll the firm mix up
      // from the buildings instead. Skipped entirely when no firm is selected,
      // which is the common case and would otherwise walk every child on every
      // icon build.
      let background, breakdown = "";
      if (selectedFirms.length){
        const mix = firmMix(group.getAllChildMarkers()
          .map(marker => marker.options.building).filter(Boolean));
        background = mixBackground(mix, diameter);
        breakdown = mixTooltip(mix);
      } else {
        background = `color-mix(in srgb, ${mapAccent()} 90%, transparent)`;
      }
      const places = group.getChildCount();
      breakdown = [`${count.toLocaleString()} advisors across ${places.toLocaleString()} building${places === 1 ? "" : "s"}`,
                   breakdown].filter(Boolean).join(" — ");
      return L.divIcon({
        className:"advisor-cluster-wrap",
        html:`<div class="advisor-cluster" title="${esc(breakdown)}" style="width:${diameter}px;height:${diameter}px;background:${background};font-size:${markFont(diameter, count.toLocaleString())}px">${count.toLocaleString()}</div>`,
        iconSize:[diameter, diameter], iconAnchor:[diameter / 2, diameter / 2],
      });
    },
  });
}
let cluster = createClusterLayer();
let markerBatchToken = 0;
const MARKER_BATCH_SIZE = 750;
let selectedMapLayer = null;

function markMapSelection(layer){
  clearMapSelection();
  selectedMapLayer = layer;
  if (layer.setStyle){
    layer._detailStyle = { weight:layer.options.weight, opacity:layer.options.opacity,
                           fillOpacity:layer.options.fillOpacity };
    layer.setStyle({ weight:3.5, opacity:1, fillOpacity:.96 });
  }
  if (layer._icon) layer._icon.classList.add("map-object-selected");
  if (layer.bringToFront) layer.bringToFront();
}

// A ring drawn at a location opened from the details drawer. Clicking a marker
// already highlights it, but opening the same place from a drawer row gave no
// map feedback at all, so you were reading a roster with no idea which pin it
// was. Deliberately not spiderfied: 51.7% of advisors sit at addresses holding
// more than twelve people, and the largest holds 1,689 -- a starburst there is
// unreadable, and the roster list is the better instrument for picking a person.
let locationHalo = null;
function highlightLocation(rec){
  if (locationHalo){ map.removeLayer(locationHalo); locationHalo = null; }
  if (!rec || rec.lat == null) return;
  locationHalo = L.circleMarker([rec.lat, rec.lon], {
    radius: 17, weight: 3, color: mapAccent(), opacity: .95,
    fill: false, interactive: false,
  }).addTo(map);
}

function clearMapSelection(){
  if (!selectedMapLayer) return;
  if (selectedMapLayer.setStyle && selectedMapLayer._detailStyle)
    selectedMapLayer.setStyle(selectedMapLayer._detailStyle);
  if (selectedMapLayer._icon) selectedMapLayer._icon.classList.remove("map-object-selected");
  selectedMapLayer = null;
}

function afterNextPaint(fn){
  // One rAF runs before a paint. A second one cannot run until the browser has
  // had an opportunity to paint the work queued by the first frame.
  //
  // BUT rAF DOES NOT FIRE IN A HIDDEN TAB, and this gates the support loads --
  // geo_index.json among them, which IS city and ZIP search. A page opened in a
  // background tab, or a phone whose screen locks while it loads, never starts
  // them, so the box reads "City and ZIP search is still loading" indefinitely
  // while name, CRD and firm search work fine, because those do not depend on
  // it. That asymmetry is exactly how the bug presents.
  //
  // So a timer races the frames, and whichever arrives first wins, once. The
  // double rAF exists to yield to a paint, not to be the only route: where
  // there will never be a paint, waiting for one is waiting forever.
  let done = false;
  const once = () => { if (done) return; done = true; fn(); };
  requestAnimationFrame(() => requestAnimationFrame(once));
  setTimeout(once, 1200);
}

function activeTransitionBatch(target, token, transition){
  return transition && token === markerBatchToken && target === cluster &&
    map.hasLayer(target) && transition.request === scopeRequest;
}

function addMarkerBatches(target, markers, token, start=0, transition=null){
  if (token !== markerBatchToken || target !== cluster || !map.hasLayer(target)) return;
  const end = Math.min(start + MARKER_BATCH_SIZE, markers.length);
  target.addLayers(markers.slice(start, end));
  if (start === 0 && transition){
    // The first batch is now attached in the same task that removes the old
    // national layer. Clear the busy treatment before async callers can issue
    // a follow-up redraw that would supersede this batch's paint callback.
    clearScopePending(transition.request);
    afterNextPaint(() => {
      if (!activeTransitionBatch(target, token, transition)) return;
      const elapsedMs = performance.now() - transition.startedAt;
      PERF.add(`scope:${transition.scope}:to first regional batch visible`, elapsedMs);
      PERF.signal("regional:first-batch-visible", {
        scope: transition.scope, request: transition.request, elapsedMs,
        markerCount: end, totalMarkers: markers.length,
      });
    });
  }
  if (end < markers.length){
    requestAnimationFrame(() =>
      addMarkerBatches(target, markers, token, end, transition));
  } else if (transition){
    afterNextPaint(() => {
      if (!activeTransitionBatch(target, token, transition)) return;
      const elapsedMs = performance.now() - transition.startedAt;
      const detail = {
        scope: transition.scope, request: transition.request, elapsedMs,
        markerCount: markers.length,
        batchCount: Math.max(1, Math.ceil(markers.length / MARKER_BATCH_SIZE)),
      };
      PERF.add(`scope:${transition.scope}:to regional batches settled`, elapsedMs);
      PERF.signal("regional:batches-settled", detail);
      PERF.signal("scope:transition-settled", { ...detail, kind: "regional" });
    });
  }
}
map.addLayer(cluster);
const heatLayer = L.heatLayer([], {
  radius: 20, blur: 15, minOpacity: .12, max: 2.2, maxZoom: 4,
  gradient: { .12: "#2c7bb6", .34: "#28a9a1", .55: "#f3df78", .76: "#f39a3c", 1: "#d73027" },
});
const heatRedraw = heatLayer._redraw;
heatLayer._redraw = function(){
  if (!this._map){ this._frame = null; return; }
  return heatRedraw.call(this);
};

function removeHeatLayer(){
  // leaflet.heat schedules redraws with requestAnimationFrame. If scope changes
  // before that callback runs, onRemove clears _map and the queued callback
  // otherwise throws while trying to call _map.getSize().
  if (heatLayer._frame){
    L.Util.cancelAnimFrame(heatLayer._frame);
    heatLayer._frame = null;
  }
  if (map.hasLayer(heatLayer)) map.removeLayer(heatLayer);
}

// ---- load ----
const countEl = document.getElementById("count");
const scopeSel = document.getElementById("scope");
const continentalBox = document.getElementById("continentalOnly");
const noticeEl = document.getElementById("notice");
const noticeText = document.getElementById("noticeText");
const scopeLoadingEl = document.getElementById("scopeLoading");
let noticeTimer = null;
let pendingScope = null; // { request, target }

function paintScopePending(target, request){
  pendingScope = { request, target };
  scopeLoadingEl.textContent = `Loading ${scopeLabel(target)} — keeping the current map visible…`;
  scopeLoadingEl.hidden = false;
  document.body.classList.add("scope-pending");
  map.getContainer().setAttribute("aria-busy", "true");
  scopeSel.setAttribute("aria-busy", "true");
}

function clearScopePending(request){
  if (!pendingScope || pendingScope.request !== request) return;
  pendingScope = null;
  scopeLoadingEl.hidden = true;
  document.body.classList.remove("scope-pending");
  map.getContainer().removeAttribute("aria-busy");
  scopeSel.removeAttribute("aria-busy");
  scheduleSupportLoads();
}

// Sidebar tooltips are portalled to the viewport so the panel's scroll and
// overflow boundaries can never crop their content.
const floatingInfo = document.createElement("div");
floatingInfo.className = "floating-info";
floatingInfo.hidden = true;
floatingInfo.setAttribute("role", "tooltip");
floatingInfo.id = "floatingInfo";
document.body.appendChild(floatingInfo);
document.body.classList.add("floating-info-enabled");
let activeInfoWrap = null;

function positionFloatingInfo(){
  if (!activeInfoWrap || floatingInfo.hidden) return;
  const button = activeInfoWrap.querySelector(".info-button");
  if (!button) return;
  const anchor = button.getBoundingClientRect();
  const box = floatingInfo.getBoundingClientRect();
  const margin = 12, gap = 7;
  let left = Math.max(margin, Math.min(anchor.left, window.innerWidth - box.width - margin));
  let top = anchor.bottom + gap;
  if (top + box.height > window.innerHeight - margin)
    top = anchor.top - box.height - gap;
  top = Math.max(margin, Math.min(top, window.innerHeight - box.height - margin));
  floatingInfo.style.left = `${Math.round(left)}px`;
  floatingInfo.style.top = `${Math.round(top)}px`;
}

// Tooltip bodies that cost a full scan of the data are registered as thunks and
// evaluated the first time the tooltip is actually opened, not on every pan.
const DEFERRED_INFO = new Map();
let scopeNotice = "";               // e.g. missing jurisdiction files for a territory
function setScopeTotals(fn){
  const help = document.getElementById("countHelp");
  DEFERRED_INFO.set("countHelp", () => [fn(), scopeNotice].filter(Boolean).join(" "));
  help.textContent = "…";           // non-empty so the tooltip still opens
}
function resolveDeferredInfo(source){
  const fn = source && DEFERRED_INFO.get(source.id);
  if (fn) source.textContent = fn();
}

function showFloatingInfo(wrap){
  const source = wrap.querySelector(".info-popover");
  if (!source) return;
  resolveDeferredInfo(source);
  if (!source.textContent.trim()) return;
  activeInfoWrap = wrap;
  floatingInfo.textContent = source.textContent.trim();
  floatingInfo.hidden = false;
  positionFloatingInfo();
}

function hideFloatingInfo(){
  floatingInfo.hidden = true;
  activeInfoWrap = null;
}

// Delegated rather than bound once at startup: the details drawer renders its
// own .info-wrap markup, and per-element listeners attached at load time would
// silently skip every tooltip created afterwards.
let infoSeq = 0;
function linkInfoAria(wrap){
  const source = wrap.querySelector(".info-popover");
  const button = wrap.querySelector(".info-button");
  if (!source || !button) return;
  if (!source.id) source.id = `infoPopover${++infoSeq}`;
  button.setAttribute("aria-describedby", source.id);
}
document.addEventListener("mouseover", e => {
  const wrap = e.target.closest?.(".info-wrap");
  if (!wrap || wrap === activeInfoWrap) return;
  linkInfoAria(wrap); showFloatingInfo(wrap);
});
document.addEventListener("mouseout", e => {
  const wrap = e.target.closest?.(".info-wrap");
  if (wrap && !wrap.contains(e.relatedTarget) && !wrap.contains(document.activeElement))
    hideFloatingInfo();
});
document.addEventListener("focusin", e => {
  const wrap = e.target.closest?.(".info-wrap");
  if (wrap){ linkInfoAria(wrap); showFloatingInfo(wrap); }
  else if (activeInfoWrap) hideFloatingInfo();
});
window.addEventListener("resize", positionFloatingInfo);
document.addEventListener("scroll", positionFloatingInfo, true);

function showNotice(message){
  clearTimeout(noticeTimer);
  noticeText.textContent = message;
  noticeEl.hidden = false;
  noticeTimer = setTimeout(() => { noticeEl.hidden = true; }, 9000);
}
document.getElementById("noticeClose").addEventListener("click", () => {
  clearTimeout(noticeTimer); noticeEl.hidden = true;
});

// THE NET UNDER THE ASYNC CLICK HANDLERS.
//
// Nearly every button on this page runs inside `async e => {...}`, and a throw
// in one of those rejects a promise nobody is holding: no dialog, no console
// stack the rep will ever see, just a button that did nothing. That has already
// happened once here -- the ⋮ menu read a variable out of scope and silently
// did nothing for a week.
//
// So an unhandled rejection is shown. It is a blunt instrument and the message
// will occasionally be technical, but "something went wrong, here is what it
// said" beats a UI that quietly declines to act.
addEventListener("unhandledrejection", (e) => {
  const msg = (e.reason && e.reason.message) || String(e.reason || "");
  if (msg) showNotice(msg);
});

function syncContinentalUI(){
  continentalBox.checked = continentalOnly;
  continentalBox.closest(".switch").classList.toggle("on", continentalOnly);
}
function setContinentalOnly(value, doRedraw=true){
  continentalOnly = !!value;
  try { sessionStorage.setItem("advisorMap.continentalOnly", String(continentalOnly)); }
  catch {}
  syncContinentalUI();
  if (doRedraw && NAT) renderAll(true);
}
syncContinentalUI();
continentalBox.addEventListener("change", () => {
  if (continentalBox.checked && OUTSIDE_CONTINENTAL.has(scope)){
    syncContinentalUI();
    showNotice(`${scopeLabel(scope)} is outside the Lower 48, so Continental U.S. only remains off.`);
    return;
  }
  setContinentalOnly(continentalBox.checked);
});

function setBusy(msg){ countEl.textContent = msg; countEl.hidden = false; }
function setLoading(){ countEl.hidden = true; }

/* THE NATIONAL VIEW LOADS IN TWO PARTS.
 *
 * national_view.json is a compact, state-aware grid with both all-placement
 * and selecting-manager counts. That is enough to draw an honest national
 * heat map and fill the scope picker, so the map is usable as soon as it lands.
 *
 * The larger firm/office detail payload follows in the background once the map
 * is on screen. The layer then upgrades from grid cells to buildings.
 *
 * The split is measured, not assumed. Fetching the office array cost 7,078ms
 * cold; PARSING it cost 12ms and building both index Maps from it cost 4.6ms.
 * It was transfer and nothing else, so the only lever is bytes at boot.
 *
 * At national zoom the grid is not a compromise anybody can see: two offices a
 * quarter-degree apart land on the same pixel at zoom 4. What the detail buys
 * is firm colouring, the per-building panel, and viewport counts -- none of
 * which a rep can ask for in the first second, and all of which arrive before
 * they can.
 */
let NAT_DETAIL_READY = false;
let natDetailPromise = null;
let NAT_DETAIL_ERROR = "";
let NATIONAL_DETAIL_REASON = "";

function loadNational(){
  setLoading();
  /* NO `cache: "no-store"` on either fetch, deliberately.
   *
   * The office file used to carry one, and it was the only fetch in the app
   * that did. The effect was not freshness, it was a full re-download on EVERY
   * load: with the browser cache in play its sibling datasets revalidate and
   * come back 304 in about 100 ms each, while this one took 2,453 ms because
   * no-store forbids keeping it at all.
   *
   * staticwebapp.config.json permits five-minute browser-private cache hits
   * only for national_view, offices_national and pins_*. Every contact, ACT and
   * field payload keeps `no-cache` and therefore revalidates. dataUrl() appends
   * a timestamp plus an all-data digest, so any rebuilt JSON gets a new URL.
   * The deliberately short, public-data-only window avoids repeat roundtrips
   * during map transitions without delaying revocation for direct lines,
   * emails or relationship data. A blanket one-year immutable policy was
   * rejected for that reason.
   */
  return fetch(dataUrl("national_view.json")).then(r => {
    if (!r.ok) throw new Error(`national view ${r.status}`);
    return r.json();
  }).then(j => {
    if (!Array.isArray(j.states) || !Array.isArray(j.grid))
      throw new Error("National view data is incomplete; refresh the generated artifact.");
    // `offices` starts EMPTY and is filled by loadNationalDetail(). Every
    // consumer of NAT.offices must check NAT_DETAIL_READY before reporting a
    // count, or it will state "0 offices in view" while the file is in flight.
    NAT = { firms: [], states: j.states, grid: j.grid, offices: [] };
    NAT_FIRM_BY_CRD = new Map();
    NAT_PLACEMENTS_BY_CRD = new Map();
    const gT = document.createElement("optgroup"); gT.label = "Sales territories";
    Object.keys(TERRITORIES).sort().forEach(name => {
      const o = document.createElement("option");
      o.value = terrKey(name);
      o.textContent = `${name} — ${TERRITORY_LEAD[name]}`;
      gT.appendChild(o);
    });
    scopeSel.appendChild(gT);
    const gS = document.createElement("optgroup"); gS.label = "States";
    j.states.forEach(s => {
      const o = document.createElement("option");
      o.value = s;
      o.textContent = STATE_NAMES[s] ? `${STATE_NAMES[s]} (${s})` : s;
      gS.appendChild(o);
    });
    scopeSel.appendChild(gS);
  });
}

// Schedule A seen from the advisor side. Loaded with the base data rather than
// lazily, because the Advanced "Owner or officer" filter runs per pin and must
// not silently pass everything while a fetch is still in flight.
/* The full office array, fetched after the map is usable.
 *
 * Idempotent: a rep who selects a firm during the wait triggers the same
 * promise rather than a second 1.45 MB download. */
function loadNationalDetail(signal=null, quiet=false){
  if (NAT_DETAIL_READY) return Promise.resolve(NAT);
  if (natDetailPromise) return natDetailPromise;
  NAT_DETAIL_ERROR = "";
  natDetailPromise = fetch(dataUrl("offices_national.json"), { signal })
    .then(r => {
      if (!r.ok) throw new Error(`national office detail ${r.status}`);
      return r.json();
    })
    .then(j => {
      if (!NAT || !Array.isArray(j.firms) || !Array.isArray(j.states) ||
          !Array.isArray(j.offices) || j.states.length !== NAT.states.length ||
          j.states.some((state, i) => state !== NAT.states[i]) ||
          !j.firms.every(firm => firm.length >= 8))
        throw new Error("National office detail does not match the compact national view.");
      NAT.firms = j.firms;
      NAT.offices = j.offices;
      NAT_FIRM_BY_CRD = new Map(j.firms.map(firm => [String(firm[1]), firm]));
      // Per-firm placement totals need every office, so they are built here
      // rather than at boot. 4.6ms for all 125,183 of them.
      NAT_PLACEMENTS_BY_CRD = new Map();
      j.offices.forEach(office => {
        const crd = String(NAT.firms[office[3]][1]);
        NAT_PLACEMENTS_BY_CRD.set(crd, (NAT_PLACEMENTS_BY_CRD.get(crd) || 0) + office[2]);
      });
      NAT_DETAIL_READY = true;
      NAT_DETAIL_ERROR = "";
      NATIONAL_DETAIL_REASON = "";
      // Redraw only if the national layer is what is on screen; a rep who has
      // already moved into a state should not have their view rebuilt.
      if (scope === "US" && !pendingScope) renderAll(false);
      else if (scope !== "US") refreshPanel();
      return NAT;
    })
    .catch(err => {
      if (err && err.name === "AbortError") throw err;
      NAT_DETAIL_ERROR = err.message || String(err);
      if (!quiet)
        showNotice(`National office detail could not be loaded (${NAT_DETAIL_ERROR}).`);
      if (scope === "US" && !pendingScope) refreshPanel();
      return null;
    })
    .finally(() => { natDetailPromise = null; });
  return natDetailPromise;
}

function loadOwnerRoles(){
  supportStart("owner");
  return fetch(dataUrl("owner_roles.json"))
    .then(r => { if (!r.ok) throw new Error(`owner roles ${r.status}`); return r.json(); })
    .then(j => { OWNER_ROLES = j; supportReady("owner"); })
    .catch(err => { OWNER_ROLES = null; supportFailed("owner", err); });
}

// roles this advisor holds, decoded; [] when they hold none
function ownerRolesFor(advisorId){
  const rows = OWNER_ROLES && OWNER_ROLES.roles[String(advisorId)];
  if (!rows) return [];
  return rows.map(([firmCrd, titleIdx, code, ctrl]) => ({
    firmCrd, title: OWNER_ROLES.titles[titleIdx] || "", code, ctrl: !!ctrl,
  }));
}

// ---- Barron's rankings ----
// Loaded eagerly for the same reason as owner roles: the Advanced toggle runs
// per pin and must not pass everything while a fetch is in flight.
function loadBarrons(){
  supportStart("barrons");
  return fetch(dataUrl("barrons.json"))
    .then(r => { if (!r.ok) throw new Error(`Barron's rankings ${r.status}`); return r.json(); })
    .then(j => { BARRONS = j; supportReady("barrons"); })
    .catch(err => { BARRONS = null; supportFailed("barrons", err); });
}

// Rankings for this advisor, already sorted by scarcity -- entry 0 is the one
// worth badging. [] when unranked, which is 99.6% of the universe.
function barronsFor(advisorId){
  const rows = BARRONS && BARRONS.advisors[String(advisorId)];
  if (!rows) return [];
  return rows.map(([list, rank, state, year, url]) => ({ list, rank, state, year, url }));
}

// The four lists are NOT comparable. top1500 ranks within a state, so "#1"
// there means "#1 in Georgia" and recurs 51 times; the other three are
// national over different universes, where a bare "#1" would be ambiguous
// between Top 100 and Top Women. Every label therefore carries its scope.
function barronsRankText(entry){
  if (!entry || entry.rank == null) return "RANKED";
  if (entry.list === "top1500")
    return entry.state ? `#${entry.rank} IN ${entry.state}` : `#${entry.rank}`;
  if (entry.list === "top100") return `TOP 100 #${entry.rank}`;
  if (entry.list === "women") return `TOP WOMEN #${entry.rank}`;
  if (entry.list === "independent") return `INDEPENDENT #${entry.rank}`;
  return `#${entry.rank}`;
}

function barronsTitle(entry){
  const label = (BARRONS && BARRONS.labels[entry.list]) || entry.list;
  return `Barron's ${label}${entry.year ? ` (${entry.year})` : ""}`;
}

// The compact badge used on rosters and lists, where vertical space is scarce
// and the point is only that this person is worth a call.
function barronsTag(advisorId){
  const hits = barronsFor(advisorId);
  if (!hits.length) return "";
  const best = hits[0];
  const more = hits.length > 1 ? ` +${hits.length - 1}` : "";
  const tip = hits.map(barronsTitle).join(" · ");
  return `<span class="rbar" title="${esc(tip)}">BARRON'S ${esc(barronsRankText(best))}${more}</span>`;
}

// ---- Forbes rankings ----
// Unlike Barron's, Forbes publishes no CRD, so most of these advisors were
// identified by matching name, firm and location. Precision measured against
// the 1,303 advisors whose CRD we DO know is 99.6%, which is high but not
// exact -- roughly one badge in 250 is the wrong person. Every entry therefore
// carries how it was established, and the UI must keep the two apart.
function loadForbes(){
  supportStart("forbes");
  return fetch(dataUrl("forbes.json"))
    .then(r => { if (!r.ok) throw new Error(`Forbes rankings ${r.status}`); return r.json(); })
    .then(j => { FORBES = j; supportReady("forbes"); })
    .catch(err => { FORBES = null; supportFailed("forbes", err); });
}

function forbesFor(advisorId){
  const rows = FORBES && FORBES.advisors[String(advisorId)];
  if (!rows) return [];
  return rows.map(([label, full, rank, tier, url]) => ({
    label, full, rank, tier, url, confirmed: tier === "c",
  }));
}

// Team assets describe the advisor's TEAM, not the individual, and teammates
// repeat the same figure. Labelled explicitly wherever it appears -- a rep
// reading it as one person's book would badly overstate the opportunity.
function forbesTeamAssets(advisorId){
  const value = FORBES && FORBES.team_assets[String(advisorId)];
  return value == null ? null : value;
}

// Confirmed entries lead, so the badge shows the strongest provenance first.
function forbesBest(hits){
  return hits.slice().sort((a, b) => (b.confirmed - a.confirmed) || (a.rank - b.rank))[0];
}

function forbesTag(advisorId){
  const hits = forbesFor(advisorId);
  if (!hits.length) return "";
  const best = forbesBest(hits);
  const more = hits.length > 1 ? ` +${hits.length - 1}` : "";
  // "≈" is not decoration: it marks an inferred match, and it carries the
  // distinction without relying on colour alone.
  const mark = best.confirmed ? "" : "≈";
  const tip = hits.map(h => `${h.full}${h.confirmed ? "" : " (matched on name, firm and location)"}`).join(" · ");
  return `<span class="rfor${best.confirmed ? "" : " rfor-inferred"}" title="${esc(tip)}">` +
         `${mark}FORBES ${esc(best.label)}${more}</span>`;
}

/* ---- contact details ------------------------------------------------------
 *
 * NOT on the boot critical path. contacts.json is 8.95 MB compressed, and on a
 * cold load it cost 25.5 of the 25.6 seconds the map took to become usable --
 * while also starving the other eight datasets on the same HTTP/2 connection:
 * offices_national took 9.1s cold and 151ms warm, the difference being almost
 * entirely contention with this file.
 *
 * The national view does not need it. It draws firm offices; contact records
 * are read when a CARD is opened, a teammate list is unfolded, a queue button
 * is drawn, or one of two filters is applied -- all of which happen after the
 * map is on screen.
 *
 * So it starts after first paint and the UI states what it knows. What it must
 * NEVER do is answer a question about contact data before the data is there:
 * "no advisors are reachable in this state" is a wrong answer, not a slow one.
 */
let CONTACTS_READY = false;
/* THREE states, not two.
 *
 * There used to be `ready` and everything else, and "everything else" rendered
 * as "Loading contact details…". So a permanent failure looked identical to a
 * slow one -- forever. The first version of this fix stopped the loader
 * claiming nobody had contact data, and simply moved the wrong answer: the card
 * then said it was still loading something that had already failed.
 *
 * loading -> the fetch is in flight, "Loading…" is true
 * failed  -> it is not coming, and the card must say so
 * ready   -> render
 */
let CONTACTS_ERROR = "";
let contactsPromise = null;

/* SHARDED, because Azure Static Web Apps will not serve a file over 25 MB.
 *
 * contacts.json reached 40 MB and SWA answered with its own 500 page -- no
 * Content-Length, nothing in any log naming the file. The map still drew,
 * because the failure fell through to an empty advisor set, so every card
 * showed no phone and no email and the two contact filters found nobody. A
 * wrong answer delivered confidently, which is exactly what the comment above
 * says this must never do.
 *
 * contacts_base.json carries the teams, the practices and a manifest of shard
 * names. The shards carry advisor records and are merged back into one object,
 * so nothing downstream can tell the difference.
 *
 * Blob storage was the alternative and would have needed `connect-src 'self'`
 * relaxed in the CSP -- a weaker security posture on the page holding the
 * contact file and the call log, to solve a hosting problem. Sharding keeps it
 * same-origin.
 */
function loadContacts(signal=null){
  if (CONTACTS_READY) return Promise.resolve(CONTACTS);
  if (contactsPromise) return contactsPromise;
  contactsPromise = fetch(dataUrl("contacts_base.json"), { signal })
    .then(r => { if (!r.ok) throw new Error(`contacts_base ${r.status}`); return r.json(); })
    .then(base => Promise.all((base.shards || []).map(name =>
        fetch(dataUrl(name), { signal })
          .then(r => { if (!r.ok) throw new Error(`${name} ${r.status}`); return r.json(); })))
      .then(parts => {
        const advisors = {};
        for (const part of parts) Object.assign(advisors, part.advisors || {});
        return { ...base, advisors };
      }))
    .catch(err => {
      if (err && err.name === "AbortError"){
        contactsPromise = null;
        throw err;
      }
      /* NOT an empty advisor set.
       *
       * Falling back to {advisors:{}} set CONTACTS_READY and let the UI answer
       * "nobody here has contact data" -- which is a wrong answer, not a slow
       * one, and indistinguishable from the truth. Leaving it unready keeps the
       * contact filters disabled and the cards honest about not knowing yet.
       */
      showNotice(`Contact details could not be loaded (${err.message}). `
        + `Phone numbers and email addresses are missing until this is fixed.`);
      CONTACTS_ERROR = err.message;
      contactsPromise = null;
      // Redraw so every card showing "Loading…" switches to saying it failed.
      syncContactSwitches();
      redraw();
      if (detailsCurrent) renderDetailEntry(detailsCurrent, false);
      return null;
    })
    .then(j => {
      if (!j) return;
      CONTACTS = j;
      CONTACTS_READY = true;
      CONTACTS_ERROR = "";
      // Pins can win the race against the deferred contact shards. Enrich any
      // names already on screen now; later scopes do this in rehydrate().
      for (const feature of ALL){
        const p = feature.properties;
        p.n = advisorDisplayName(p.id, p.n);
      }
      // Anything already on screen that was drawn without contact detail:
      // the panel's cards, the queue buttons in list rows, and the two
      // filter switches that were held disabled.
      syncContactSwitches();
      redraw();
      // A panel opened during the wait was drawn without contact detail. Redraw
      // it in place rather than leaving a card that says a person has no phone
      // when the file listing it has just arrived.
      if (detailsCurrent) renderDetailEntry(detailsCurrent, false);
      if (advQuery && advQuery.length >= 2 && !advOut.hidden)
        renderNationalSearch();
      reconcileDesktopDialRoutes().catch(() => {});
    });
  return contactsPromise;
}

function contactFor(advisorId){
  return (CONTACTS && CONTACTS.advisors[String(advisorId)]) || null;
}

function preferredDisplayName(formalName, preferredFirst){
  const formal = String(formalName || "").trim().replace(/\s+/g, " ");
  const preferred = String(preferredFirst || "").trim();
  if (!formal || !/^[\p{L}][\p{L}'’\-]{0,39}$/u.test(preferred)) return formal;
  const parts = formal.split(" ");
  const token = value => String(value || "").normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/[^a-z0-9]/g, "");
  if (token(parts[0]) === token(preferred)) return formal;
  if (parts[1] && /^\(.+\)$/.test(parts[1])) return formal;
  return parts[0] + " (" + preferred + ")" +
    (parts.length > 1 ? " " + parts.slice(1).join(" ") : "");
}

function advisorDisplayName(advisorId, fallback=""){
  const c = contactFor(advisorId);
  const formal = fallback || (c && c.n) || "";
  // Preserve the map/search/field view's canonical formal name when supplied;
  // contacts.json contributes a preference only when the producer explicitly
  // emitted a preferred presentation. This prevents Chris (Christopher).
  const preferred = c && c.pn ? c.sal : "";
  return preferredDisplayName(formal, preferred || "")
    || (c && c.pn) || formal;
}

// ---- EIC's book, split by product ----
// The card used to show one blended figure, which hides the thing a rep is
// standing there to decide: which product this relationship holds, and so
// which it does NOT. Merrill Lynch holds $1.06B of All-Cap and exactly zero
// Large-Cap; Raymond James is the mirror image. The gaps are per-advisor too.
//
// Values are FULL account values. An account shared by four advisors shows at
// full value on each, which is right for a card -- the rep needs the size of
// the relationship, not a quarter of it -- and wrong for any sum. NEVER total
// this across advisors; act_assets.json carries de-duplicated firm figures in
// its `totals` block for that.
let ACT_ASSETS = null;
let ACT_ASSETS_ERROR = "";

function loadActAssets(){
  supportStart("act");
  ACT_ASSETS_ERROR = "";
  return fetch(dataUrl("act_assets.json"))
    .then(r => { if (!r.ok) throw new Error(`EIC assets ${r.status}`); return r.json(); })
    .then(j => { ACT_ASSETS = j; supportReady("act"); })
    .catch(err => {
      ACT_ASSETS = null;
      ACT_ASSETS_ERROR = err.message || String(err);
      supportFailed("act", err);
    });
}

function bookFor(advisorId){
  return (ACT_ASSETS && ACT_ASSETS.advisors[String(advisorId)]) || null;
}

/* WHAT A TEAM HOLDS WITH US, counted once.
 *
 * The same method the summary bar uses, and for the same reason. A book value
 * on an advisor is the FULL account value, which is right on a card -- the rep
 * needs the size of the relationship, not their share of it -- and wrong the
 * moment two advisors on one team are added together, because the account they
 * share would be counted twice.
 *
 * So the accounts are unioned by index first and valued afterwards. An account
 * held jointly by four members of a practice contributes once.
 *
 * Returns null when we hold nothing, so the caller can say nothing rather than
 * print a confident $0.
 */
function teamBook(crds){
  if (!ACT_ASSETS || !ACT_ASSETS.accounts) return null;
  const seen = new Set();
  let holders = 0;
  for (const crd of crds){
    const b = ACT_ASSETS.advisors[String(crd)];
    if (!b) continue;
    holders++;
    for (const i of b.ix) seen.add(i);
  }
  if (!seen.size) return null;
  let acv = 0, lcv = 0, mf = 0;
  for (const i of seen){
    const a = ACT_ASSETS.accounts[i];
    if (!a) continue;
    acv += a[0]; lcv += a[1]; mf += a[2];
  }
  return { total: acv + lcv + mf, acv, lcv, mf, accounts: seen.size, holders };
}

// ---- who covers a state ----
// NOT to be confused with TERRITORIES above, which maps a REGION NAME to its
// states for the scope selector. This maps a STATE to the PERSON responsible
// for it. The two encode the same seven groupings -- independently, one written
// by hand here and one recovered from the CRM -- and audit.py asserts they
// still agree, because two copies of a business fact drift.
//
// Derived from state rather than read from the CRM: the "EIC Contact" field
// cannot be reached through the Act! Web API and is blank on 9,223 LPL contacts
// nobody tagged, so the derivation is the better source rather than a fallback.
// Validated out of sample against 6,141 hand-assigned records: 6,140 matched.
let SALES_TERRITORY = null;

function loadTerritories(){
  supportStart("territories");
  return fetch(dataUrl("territories.json"))
    .then(r => { if (!r.ok) throw new Error(`sales territories ${r.status}`); return r.json(); })
    .then(j => { SALES_TERRITORY = j; supportReady("territories"); })
    .catch(err => { SALES_TERRITORY = null; supportFailed("territories", err); });
}

function territoryFor(state){
  const s = String(state || "").toUpperCase();
  return (SALES_TERRITORY && SALES_TERRITORY.states[s]) || null;
}

// What a work number actually reaches, and what the button is allowed to
// promise. A rep who dials expecting a desk and gets a receptionist has burned
// the opening of the call, so "Direct" is only ever printed over a number the
// source verified as one person's -- everything else says "Office" or hedges.
//
// `unverified` exists because most CRM numbers cannot be classified: one row
// at a branch is not evidence the line is a desk line. It reads "Work", the
// same neutral word used before this distinction existed.
const PHONE_KIND = {
  // Either the scraper saw the firm publish it as a direct dial, or this
  // person's number is used once in an office where we hold three or more
  // colleagues who do NOT all share one line -- a staffed branch does not
  // issue a different main number to each of its advisors.
  direct:      { label:"Direct",  cls:"",              tip:"direct line — reaches this person" },
  extension:   { label:"Direct",  cls:"",              tip:"shared line plus a personal extension — reaches this person" },
  switchboard: { label:"Office",  cls:" contact-btn-office", tip:"main office line — you will need to ask for them" },
  shared:      { label:"Office",  cls:" contact-btn-office", tip:"line shared by a few colleagues — you may need to ask for them" },
  "toll-free": { label:"Office",  cls:" contact-btn-office", tip:"toll-free routing line — you will need to ask for them" },
  // Nobody else at the firm has this number, but whether the advisor answers
  // it is unknown. Edward Jones is the reason this is not called Direct: its
  // ~19,000 one-advisor branches each have a unique number that a branch
  // administrator picks up. "Listed" promises what we actually know.
  "sole-use":  { label:"Listed",  cls:" contact-btn-listed",
                 tip:"listed for this advisor alone — may ring their desk or their branch" },
  unverified:  { label:"Work",    cls:" contact-btn-office", tip:"work number — we have not confirmed whether it is a direct line" },
  // Captrust and EP Wealth label a number used by one person "single-occupant".
  // That sounds like a desk line and is NOT one: spot-checking 15 of them
  // against Captrust's own /locations/ pages showed every one is the whole
  // office's published number at an office that happens to hold one advisor.
  // It reaches a front desk, so it says Office.
  "single-occupant": { label:"Office", cls:" contact-btn-office",
                       tip:"office line at a one-advisor office — published for the office, not the desk" },
  _:           { label:"Work",    cls:" contact-btn-office", tip:"work number — line type unknown" },
};

function teamFor(c){
  return (c && c.tm && CONTACTS && CONTACTS.teams && CONTACTS.teams[c.tm]) || null;
}
/* Raw mailto is separate from the controlled composer. By explicit business
 * decision every displayed address stays clickable, including review-tier and
 * do-not-call records. Outlook receives none of the app's suppression,
 * identity, rate-limit, or activity-log controls, so the tooltip keeps that
 * discretion visible at the point of use.
 */
function mailtoLink(address, emailConfirmed, crd){
  const a = esc(address), cautions = [];
  if (!emailConfirmed) cautions.push("identity is not approved for the controlled composer");
  if (crd && Dial.isDnc(crd)) cautions.push("a do-not-call record is present");
  const tip = `${a} - opens a blank email outside the app; nothing is logged and app suppression controls do not apply`
    + (cautions.length ? `; caution: ${cautions.join("; ")}` : "");
  return `<a class="contact-mailto"
      href="mailto:${encodeURIComponent(address).replace(/%40/g, "@")}"
      title="${esc(tip)}">${a}</a>`;
}

function practiceFor(c){
  return (c && c.pk && CONTACTS && CONTACTS.practices && CONTACTS.practices[c.pk]) || null;
}

// The other people on a team, as a disclosure the rep opens rather than a
// wall of names under every card. An SMA conversation is usually with a
// PRACTICE, so the rest of the buying unit is the useful thing to see -- but
// only on demand, because most of the time the rep is looking at one person.
function teammateList(rec, selfId){
  if (!rec || !rec.m || rec.m.length < 2) return "";
  // Each member is [advisor CRD, state]. The state is carried because at
  // national scope the map holds no per-state features, so a click has to know
  // which scope to switch into before it can find anybody.
  const others = rec.m.filter(m => String(m[0]) !== String(selfId));
  if (!others.length) return "";
  const rows = others.map(([id, st]) => {
    const t = contactFor(id);
    const name = (t && t.n) || ("CRD " + id);
    // Kept OUT of `name`: it is markup, and name goes through esc() below and
    // into a title attribute where a tag would be nonsense.
    const role = t && t.ti ? ` <span class="teammate-title">${esc(t.ti)}</span>` : "";
    const reach = t && REACHES_PERSON_KINDS.has(t.wk) ? ' <span class="teammate-dot" title="direct line on file">&#9679;</span>' : "";
    // Same jump the "elsewhere" control uses, so a teammate behaves like any
    // other advisor on the map rather than being a dead end.
    return `<li><button type="button" class="teammate" data-teammate="${esc(id)}"
        data-teammate-state="${esc(st || "")}"
        title="Show ${esc(name)} on the map">${esc(name)}</button>
        ${personActionButton(id, name)}${role}${reach}</li>`;
  });
  // The team's stated size and the number we can NAME often differ, because a
  // teammate whose contact row matched no advisor is not in this file. Saying
  // "4 teammates" beside a team headed "6 people" reads as a contradiction, so
  // the shortfall is stated rather than left for the rep to notice.
  const missing = rec.sz ? rec.sz - 1 - others.length : 0;
  const label = `${others.length} teammate${others.length === 1 ? "" : "s"}`
    + (missing > 0 ? ` on file &middot; ${missing} not matched` : "");
  return `<details class="teammates"><summary>${label}</summary>`
    + `<ul>${rows.join("")}</ul></details>`;
}
const REACHES_PERSON_KINDS = new Set(["direct", "extension"]);

// $840K / $2.4M / $1.2B. An asset figure is read at a glance, so the unit
// matters more than the digits; the exact number goes in the title attribute.
function money(v){
  if (!(v > 0)) return "";
  if (v >= 1e9) return "$" + (v / 1e9).toFixed(v >= 1e10 ? 0 : 1) + "B";
  if (v >= 1e6) return "$" + (v / 1e6).toFixed(v >= 1e7 ? 0 : 1) + "M";
  if (v >= 1e3) return "$" + Math.round(v / 1e3) + "K";
  return "$" + Math.round(v);
}
function exactMoney(v){
  return "$" + Math.round(v).toLocaleString("en-US");
}

// A click is INTENT, not a completed call -- the rep may not have dialled, and
// nobody may have answered. Logged locally so a rep can see who they have
// already worked today; never to be written into a CRM as call history.
const CONTACT_LOG_KEY = "advisorMap.contactLog.v1";
// Tapping a number or an address records INTENT, not an outcome. It goes to the
// server so the firm can see it, but a failure here must never interrupt the
// call the rep is placing -- failing loudly is reserved for dispositions, which
// are the part that cannot be reconstructed from anything else.
//
// The localStorage copy is kept ONLY to drive today's counter in the panel,
// which is a per-person convenience rather than a record. The record is the
// server's.
function logContact(advisorId, kind, name){
  let log = [];
  try { log = JSON.parse(localStorage.getItem(CONTACT_LOG_KEY)) || []; } catch { log = []; }
  log.push({ id:String(advisorId), name, kind, at:new Date().toISOString(), scope });
  try { localStorage.setItem(CONTACT_LOG_KEY, JSON.stringify(log.slice(-2000))); } catch {}
  renderContactCount();

  const c = contactFor(advisorId);
  Dial.log({
    crd: advisorId,
    name: name || "",
    firm: (c && c.cn) || "",
    phone: (c && c.w) || "",
    phoneKind: (c && c.wk) || "",
    kind: kind === "email" ? "email" : "call",
    // WHICH draft was opened, not that anything was sent. The browser gets no
    // completion callback from a tel: action, so a log line saying
    // an email went out would assert something this click cannot know.
    purpose: kind === "email" ? cardPurpose : "",
  }).catch(() => {});
}

function renderContactCount(){
  const el = document.getElementById("contactLogCount");
  if (!el) return;
  let log = [];
  try { log = JSON.parse(localStorage.getItem(CONTACT_LOG_KEY)) || []; } catch {}
  const today = new Date().toISOString().slice(0, 10);
  const n = log.filter(r => r.at.slice(0, 10) === today).length;
  el.textContent = n ? `${n} attempt${n === 1 ? "" : "s"} today` : "";
  el.hidden = !n;
}

// Email clicks are intercepted by email.js and open the server-backed composer; the link
// itself intentionally has no external-mail fallback.
/* KEY CONTACT and DUE DILIGENCE, as marks beside a name.
 *
 * SVG, not emoji. The shield was U+1F6E1 followed by U+FE0F -- a variation
 * selector that forces emoji presentation, so the browser painted its own
 * colour and CSS `color` did nothing. It looked permanently selected while the
 * star, plain text at U+2605, changed colour correctly. Two controls, one of
 * them lying about its state.
 *
 * Filled when set, outline when not, so the two states are unmistakable and
 * both marks behave the same way. No text labels -- they sit beside the name at
 * reading size and the title says what they are.
 */
const STAR_PATH = "M12 2.6l2.9 6 6.6.9-4.8 4.6 1.2 6.5L12 17.6 6.1 20.6l1.2-6.5"
                + "L2.5 9.5l6.6-.9z";
const SHIELD_PATH = "M12 2.5l7.5 3v5.2c0 4.7-3.2 8.6-7.5 9.8-4.3-1.2-7.5-5.1"
                  + "-7.5-9.8V5.5z";
const CHECK_PATH = "M8.4 12.2l2.4 2.4 4.6-4.9";
const CALENDAR_PATH = "M5 5.5h14v14H5z M5 9h14 M8 3v5 M16 3v5";
const CLOCK_PATH = "M16.5 13.2a4.2 4.2 0 1 1 0 8.4 4.2 4.2 0 0 1 0-8.4z M16.5 15.2v2.4l1.7 1";

/* PRESSED MEANS MINE.
 *
 * It used to mean "somebody marked this", so a rep looking at a colleague's
 * key contact saw a lit star -- and pressing it CLEARED the colleague's mark
 * instead of adding their own. One rep could silently delete another's, and
 * there was no way to join a flag that was already set.
 *
 * The button now carries only this rep's membership. That colleagues also hold
 * it is shown beside the control rather than folded into its state: they are
 * two different facts and both are worth having -- whose list this is on, and
 * that somebody else is already working this person.
 */
function flagMark(crd, kind, on, label, path, extra, others = []){
  const also = others.length ? ` — also marked by ${others.join(", ")}` : "";
  return `<button type="button" class="flag-mark${on ? " on" : ""}${others.length ? " shared" : ""}"
      data-flag="${kind}" data-advisor="${esc(crd)}"
      title="${esc(label)}${on ? " — click to unmark" : " — click to mark"}${esc(also)}"
      aria-label="${esc(label)}${esc(also)}" aria-pressed="${on}">
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="${path}" fill="${on ? "currentColor" : "none"}"
              stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>
        ${extra || ""}
      </svg>${others.length ? `<i class="flag-also" aria-hidden="true"></i>` : ""}</button>`;
}

function flagMarks(crd){
  const mine = { key: Dial.flaggedByMe(crd, "key"), dd: Dial.flaggedByMe(crd, "dd"),
                 scheduler: Dial.flaggedByMe(crd, "scheduler") };
  const others = { key: Dial.flaggedByOthers(crd, "key"), dd: Dial.flaggedByOthers(crd, "dd"),
                   scheduler: Dial.flaggedByOthers(crd, "scheduler") };
  return `<span class="contact-flags">`
    + flagMark(crd, "key", mine.key, "Key person", STAR_PATH, "", others.key)
    + flagMark(crd, "dd", mine.dd, "Analyst", SHIELD_PATH,
        `<path d="${CHECK_PATH}" fill="none" stroke="${mine.dd ? "var(--panel, #fff)" : "currentColor"}"
          stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>`, others.dd)
    + flagMark(crd, "scheduler", mine.scheduler, "Scheduler", CALENDAR_PATH,
        `<path d="${CLOCK_PATH}" fill="var(--panel, #fff)" stroke="currentColor"
          stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>`, others.scheduler)
    + `</span>`;
}

/* A read-only pair, for lists where the marks are information rather than a
 * control -- team rosters, the call queue, search results. Same shapes, no
 * button, nothing to click by accident while working a list. */
function flagGlyphs(crd){
  const key = Dial.isKeyContact(crd), dd = Dial.isDueDiligence(crd), scheduler = Dial.isScheduler(crd);
  if (!key && !dd && !scheduler) return "";
  const one = (label, path, extra, kind) => `<svg class="flag-glyph flag-glyph-${kind}" viewBox="0 0 24 24"
      role="img" aria-label="${esc(label)}"><title>${esc(label)}</title>
      <path d="${path}" fill="currentColor"/>${extra || ""}</svg>`;
  return `<span class="flag-glyphs">`
    + (key ? one("Key person", STAR_PATH, "", "key") : "")
    + (dd ? one("Analyst", SHIELD_PATH,
        `<path d="${CHECK_PATH}" fill="none" stroke="var(--panel, #fff)"
          stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>`, "dd") : "")
    + (scheduler ? one("Scheduler", CALENDAR_PATH,
        `<path d="${CLOCK_PATH}" fill="var(--panel, #fff)" stroke="currentColor"
          stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>`, "scheduler") : "")
    + `</span>`;
}

/* Dense rows get one sibling action button rather than three nested buttons.
 * The active roles remain visible at a glance; the button opens the same direct
 * toggles used on a full contact card plus an explicit destination-list picker. */
function personActionButton(crd, name){
  return `<span class="person-quick">${flagGlyphs(crd)}
    <button type="button" class="person-action-trigger" data-person-actions="${esc(crd)}"
      data-person-name="${esc(name || "")}" aria-haspopup="dialog" aria-expanded="false"
      title="Label or add ${esc(name || "this person")} to a list"
      aria-label="Actions for ${esc(name || "this person")}">&hellip;</button></span>`;
}

let personActionBack = null;
function closePersonActions(){
  if (!personActionBack) return;
  const trigger = personActionBack._trigger;
  personActionBack.remove();
  personActionBack = null;
  if (trigger && trigger.isConnected) {
    trigger.setAttribute("aria-expanded", "false");
    trigger.focus();
  }
}

function openPersonActions(trigger){
  closePersonActions();
  const crd = String(trigger.dataset.personActions || "");
  const c = contactFor(crd) || {};
  const name = trigger.dataset.personName || advisorDisplayName(crd) || `CRD ${crd}`;
  // Standing role lists are projections of the toggles above, not ordinary
  // destinations. Manually adding here would be silently undone on rebuild.
  const lists = (Dial.state.lists || []).filter((list) => !standingKindOf(list.id));
  const back = document.createElement("div");
  back.className = "ask-back person-action-back";
  back._trigger = trigger;
  back.innerHTML = `<div class="ask person-action-dialog" role="dialog" aria-modal="true"
      aria-labelledby="personActionTitle">
    <h3 id="personActionTitle">${esc(name)}</h3>
    <p class="person-action-label">Sales roles</p>
    <div class="person-action-roles">${flagMarks(crd)}</div>
    <p class="person-action-label">Add to a call list</p>
    <div class="person-action-lists">${lists.length ? lists.map((list) =>
      `<button type="button" class="ask-btn" data-person-list="${esc(list.id)}"
        data-person-crd="${esc(crd)}">${esc(list.name)} <small>${Number(list.count) || 0}</small></button>`).join("")
      : `<p class="hint">Create a call list first.</p>`}</div>
    <p class="person-action-status" role="status" aria-live="polite"></p>
    <button type="button" class="ask-btn ghost" data-person-close>Close</button></div>`;
  personActionBack = back;
  trigger.setAttribute("aria-expanded", "true");
  document.body.appendChild(back);
  back.querySelector(".flag-mark, [data-person-list], [data-person-close]")?.focus();
}

document.addEventListener("keydown", (event) => {
  if (!personActionBack) return;
  if (event.key === "Escape") return closePersonActions();
  if (event.key !== "Tab") return;
  const focusable = [...personActionBack.querySelectorAll(
    'button:not([disabled]),[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled])')];
  if (!focusable.length) return;
  const first = focusable[0], last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault(); last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault(); first.focus();
  }
});
document.addEventListener("click", async (event) => {
  const trigger = event.target.closest("[data-person-actions]");
  if (trigger) {
    event.preventDefault();
    event.stopImmediatePropagation();
    openPersonActions(trigger);
    return;
  }
  if (!personActionBack) return;
  if (event.target === personActionBack || event.target.closest("[data-person-close]")) {
    event.preventDefault();
    event.stopImmediatePropagation();
    closePersonActions();
    return;
  }
  const listButton = event.target.closest("[data-person-list]");
  if (!listButton) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  const status = personActionBack.querySelector(".person-action-status");
  listButton.disabled = true;
  if (status) status.textContent = "Adding...";
  try {
    await loadContacts();
    const snapshot = dialSnapshot(listButton.dataset.personCrd);
    if (!snapshot.identityApproved)
      throw new Error("Confirmed contact details are not available for this person yet.");
    const result = await Dial.addToList(listButton.dataset.personList, snapshot);
    if (status) status.textContent = result && result.added === false
      ? "Already on that list." : "Added.";
    await Dial.loadLists();
    const current = (Dial.state.lists || []).find((list) => list.id === listButton.dataset.personList);
    const count = listButton.querySelector("small");
    if (count && current) count.textContent = Number(current.count) || 0;
    renderDialer();
  } catch (err) {
    if (status) status.textContent = err.message || "That could not be saved.";
  } finally { listButton.disabled = false; }
}, true);

function contactBlock(p){
  const c = contactFor(p.id);
  // "" means we HAVE the file and this person is not in it. Before the file
  // arrives the honest answer is "not yet", not "none" -- a card opened in the
  // first few hundred milliseconds would otherwise state that a reachable
  // advisor has no phone and no email. The panel redraws itself when
  // loadContacts() resolves.
  if (!c) {
    if (CONTACTS_READY) return "";
    // Failed is not the same as pending, and saying "Loading" about something
    // that already failed is how a rep waits for a file that is never coming.
    return CONTACTS_ERROR
      ? `<p class="contact-pending contact-failed">Contact details unavailable &mdash;
           ${esc(CONTACTS_ERROR)}. Reload to try again.</p>`
      : `<p class="contact-pending">Loading contact details…</p>`;
  }
  const rows = [];
  // Do-not-call suppressed the ADD button and left the tel: links live, which
  // made the guard decorative: the fastest way to call someone was the button
  // that was still there. A suppressed record shows the numbers -- a rep needs
  // to recognise an inbound call -- but they are not dialable.
  const dnc = Dial.state.dnc.get(String(p.id));
  // NO SUBJECT, DELIBERATELY.
  //
  // This used to pre-fill `${their firm} — Equity Investment Corporation`,
  // which led with the RECIPIENT'S OWN firm -- the least informative thing
  // available and the clearest tell of a mail-merge -- read as a letterhead
  // rather than a reason to open, and ran past the ~40 characters a phone
  // shows, truncating the half that identified EIC.
  //
  // A fixed line is the worst of the options on offer: usually deleted, so it
  // bought nothing, and occasionally NOT deleted, so it went out looking
  // automated. Blank makes the rep write a subject for the person they are
  // actually writing to, and Outlook prompts before sending an empty one, so
  // it cannot go out bare by accident.
  //
  // The `?subject=` parameter is omitted entirely rather than sent empty --
  // some clients treat a present-but-empty value as an intentional blank and
  // skip that prompt.
  //
  // THAT IS STILL THE DEFAULT, and the purpose chips do not overrule it. The
  // objection above was to a subject the app writes on the rep's behalf
  // whether or not they wanted one. A chip is the opposite: the rep picked it,
  // in the same breath as saying what this contact is for, and picking nothing
  // still opens a blank compose window. So the template applies only where
  // there is an explicit choice, and never by default.
  /* An UNCONFIRMED match gets no one-click anything.
   *
   * These contacts were matched to the SEC record on a name similarity, not on
   * an identifier, and 42% of them carry a CRM state that contradicts where the
   * SEC says the advisor sits. Jennifer Friberg's card was showing Scott
   * Friberg's Virginia number under an Atlanta address.
   *
   * The details still show, clearly marked, because a rep who can read them
   * decides better than one shown nothing. What is withheld is the ability to
   * act on them by reflex, because every downstream consequence lands on the
   * WRONG PERSON: an outcome logged against her CRD, a history entry saying she
   * was called, and -- worst -- a do-not-call that silences her firm-wide,
   * permanently, with no undo, because somebody else asked to be left alone.
   */
  const unconfirmed = c.t === "review";
  // Dial.tierCanEmail, not a local rule: this decided the same question as
  // field.js and the server registry, in three places, and widening the
  // server alone left both views still refusing.
  const emailConfirmed = Dial.tierCanEmail(c.t, c.src);

  if (c.e) rows.push(!emailConfirmed
    ? `<span class="contact-btn blocked" title="${esc(c.e)} — research-only address; confirm the identity before emailing.">&#9993; Email unavailable</span>`
    : `<a class="contact-btn"
      href="#"
      data-contact="email" data-advisor="${esc(p.id)}" title="${esc(c.e)}">&#9993; Email${
        cardPurpose ? " (" + esc(Dial.purposeLabel(cardPurpose)) + ")" : ""}</a>`);
  if (c.w){
    const k = PHONE_KIND[c.wk] || PHONE_KIND._;
    // The commas are dial pauses the handset honours, so the extension is
    // actually reached. Without them the rep lands on the switchboard while
    // the button promises a person.
    const dial = c.w + (c.wx ? "," + c.wx : "");
    const shown = c.wd + (c.wx ? ` ext. ${c.wx}` : "");
    const href = unconfirmed ? "" : Dial.telHref(p.id, dial);
    rows.push(href
      ? `<a class="contact-btn${k.cls}" href="${esc(href)}"
      data-contact="work" data-advisor="${esc(p.id)}"
      title="${esc(shown)} — ${esc(k.tip)}">&#9742; ${k.label}</a>`
      : `<span class="contact-btn blocked" title="${esc(shown)} — ${
          unconfirmed ? "unconfirmed match, so this number may reach somebody else" : "do not call"}">`
        + `&#9742; ${k.label}</span>`);
  }
  if (c.c) {
    const href = unconfirmed ? "" : Dial.telHref(p.id, c.c);
    rows.push(href
      ? `<a class="contact-btn" href="${esc(href)}"
      data-contact="cell" data-advisor="${esc(p.id)}" title="${esc(c.cd)}">&#9742; Mobile</a>`
      : `<span class="contact-btn blocked" title="${esc(c.cd)} — do not call">&#9742; Mobile</span>`);
  }
  // The firm's own page for this person, and their LinkedIn. Both open in a
  // new tab -- a rep researching before a call should not lose the map.
  if (c.pu) rows.push(`<a class="contact-btn contact-btn-link" href="${esc(c.pu)}" target="_blank"
      rel="noopener noreferrer" data-contact="profile" data-advisor="${esc(p.id)}"
      title="${esc(c.pu)}">&#128279; Profile</a>`);
  if (c.li) rows.push(`<a class="contact-btn contact-btn-link" href="${esc(c.li)}" target="_blank"
      rel="noopener noreferrer" data-contact="linkedin" data-advisor="${esc(p.id)}"
      title="${esc(c.li)}">&#128100; LinkedIn</a>`);
  // Bail only when there is genuinely nothing to say. Testing the buttons,
  // title and owner alone hid the panel for anyone whose only fact was a team
  // and its assets -- which is exactly the record a rep wants before a call.
  const hasAssets = !!(teamFor(c) || c.ia > 0);
  if (!rows.length && !c.ti && !c.tn && !c.o && !hasAssets) return "";

  // OWNERSHIP FIRST, and before the buttons. If this contact is already
  // somebody's relationship, that changes whether the rep should dial at all,
  // so it cannot sit below the fold as a footnote.
  //
  // Where the CRM names nobody, the TERRITORY is shown instead -- a different
  // and weaker claim, worded as such. "Assigned to" is not "has a relationship
  // with", and conflating them would put a warning on every card in the
  // country, which is the same as putting one on none of them.
  const terr = !c.o && territoryFor(p._state);
  const owner = c.o
    ? `<p class="contact-owner" title="EIC relationship owner from the CRM">
         &#9679; EIC relationship &mdash; owned by ${esc(c.o)}</p>`
    : terr
      ? `<p class="contact-owner contact-territory"
           title="Territory assignment derived from the advisor's state, not a recorded relationship">
           &#9673; ${esc(p._state)} &mdash; assigned to ${esc(terr.n)}</p>`
      : "";

  // Title is seniority. A Managing Director and a Financial Advisor at the
  // same firm are different conversations, so it reads at full size next to
  // the name rather than hiding in a tooltip.
  const title = c.ti ? `<p class="contact-title">${esc(c.ti)}</p>` : "";

  /* KEY CONTACT and DUE DILIGENCE.
   *
   * A star and a shield, both toggles rather than badges: the rep who learns
   * that this is the person who runs manager due diligence is the rep looking
   * at the card, and making them go elsewhere to record it means it does not
   * get recorded.
   *
   * Two separate marks because they are two separate facts. Often the same
   * person, not always -- so "both" has to show as both, which one combined
   * control could not do.
   */

  const team = teamFor(c);
  const practice = practiceFor(c);
  let assets = "";
  if (team){
    const per = team.sz > 1 ? ` &middot; ${team.sz} people` : "";
    assets = `<p class="contact-team">
        <span class="contact-team-label">Team</span>
        ${esc(team.n)}${team.c ? ` &middot; ${esc(team.c)}` : ""}${per}
        ${team.a > 0 ? `<b title="${esc(exactMoney(team.a))} — team total, not this person's book">
          ${esc(money(team.a))}</b>` : ""}
      </p>` + teammateList(team, p.id);
  } else if (c.tn){
    // A team the FIRM names, with no asset figure attached. 31,014 advisors
    // have one and none of them reached this panel before: the CRM block above
    // only fires for a team that carries a dollar amount, so every
    // roster-sourced practice was invisible. Deliberately shows no money --
    // inventing a figure to fill the slot is the error the whole team model
    // exists to avoid.
    const per = practice && practice.sz > 1 ? ` &middot; ${practice.sz} people` : "";
    // The team's own page, where the firm publishes one. On the NAME, because
    // that is what a rep is looking at when they want to see the practice --
    // and the practice site is usually far more informative about the buying
    // unit than any individual profile page.
    const site = practice && practice.u ? esc(practice.u) : "";
    const named = site
      ? `<a class="contact-team-link" href="${site}" target="_blank" rel="noopener"
           title="${site} — the practice's own site">${esc(c.tn)}</a>`
      : esc(c.tn);
    /* WHAT THE PRACTICE HOLDS, beside its name.
     *
     * De-duplicated across the members, so an account four of them share counts
     * once -- the individual figure on the card is deliberately the FULL account
     * value and must never be summed. Labelled "team" so it is never read as
     * this person's own book, which appears separately below.
     *
     * Silent when we hold nothing: a practice with no book gets no figure
     * rather than a confident $0.
     */
    const book = practice && practice.m ? teamBook(practice.m.map(m => m[0])) : null;
    const teamMoney = book
      ? ` <b class="contact-team-book" title="${esc(exactMoney(book.total))} across ${
          book.accounts} account${book.accounts === 1 ? "" : "s"} held by ${
          book.holders} of the team — each account counted once, so shared accounts are not double counted"
        >${esc(money(book.total))} with EIC</b>`
      : "";
    assets = `<p class="contact-team">
        <span class="contact-team-label">Team</span> ${named}${per}${teamMoney}</p>`
      + teammateList(practice, p.id);
  }

  // THE BOOK IS A SEPARATE FACT FROM THE TEAM, and used to be chained to it
  // with `else if`. That made a team NAME and an asset FIGURE mutually
  // exclusive on the card, which they are not: Christopher Tolman has a UBS
  // practice (Advocate Partners) and his own $42.8M in the CRM, and the team
  // name won the branch. 726 advisors lost $4.42B of book value that way --
  // present in contacts.json the whole time, never rendered.
  //
  // Worse than a plain omission, because passesHasAssets() counts c.ia: the
  // "has assets" filter kept these people on the map and then their card
  // showed no money.
  //
  // Safe to add unconditionally alongside the CRM team block: build_contacts
  // sets `ia` ONLY where no team-mate shares the amount, so no advisor carries
  // both, and nothing can be double counted. The !team guard states that
  // invariant rather than relying on it.
  if (!team && c.ia > 0){
    // An amount with no team-mate is this individual's own book. Labelled as
    // such: showing it as "team assets" would repeat the CRM's own error.
    assets += `<p class="contact-team">
        <span class="contact-team-label">Book</span>
        <b title="${esc(exactMoney(c.ia))} — this individual, no team recorded">
          ${esc(money(c.ia))}</b></p>`;
  }

  // WHICH PRODUCT, from the CRM's per-account records. Added ALONGSIDE the
  // figures above rather than replacing them: those come from the CRM's single
  // blended "Total Assets" column, this comes from the account-level b* fields,
  // and where they disagree that is worth seeing rather than hiding.
  //
  // A zero is printed, not omitted. "Large-Cap —" is the sales fact; a missing
  // line just looks like missing data, and the gap is the reason to show any of
  // this. Mid-Cap is deliberately absent: $20.6M across 20 accounts firm-wide,
  // and giving it a slot would overstate it.
  const book = bookFor(p.id);
  if (book && (book.acv > 0 || book.lcv > 0 || book.mf > 0)){
    const line = (label, v, tip) =>
      `<span class="prod" title="${esc(tip)}"><i>${label}</i>`
      + (v > 0 ? `<b>${esc(money(v))}</b>` : `<b class="none">&mdash;</b>`) + `</span>`;
    const shared = book.sh
      ? ` &middot; ${book.sh} of ${book.n} shared with team-mates`
      : "";
    // A review-tier CRD match means this money is attributed to this person on
    // a name similarity, not a confirmed identity. Showing real dollars against
    // possibly the wrong advisor is exactly the quiet wrongness worth marking,
    // the same way unconfirmed contact matches are marked elsewhere.
    const hedge = book.t === "review"
      ? `<span class="prod-warn" title="This advisor was matched to the CRM record on name, firm and location rather than a confirmed identifier — check before quoting these figures.">&#9888;</span>`
      : "";
    assets += `<div class="contact-products">
        <span class="contact-team-label">With EIC${hedge}</span>
        ${line("All-Cap", book.acv, "All-Cap Value SMA")}
        ${line("Large-Cap", book.lcv, "Large-Cap Value SMA")}
        ${line("EICIX", book.mf, "EICIX mutual fund")}
        <small>${book.n} account${book.n === 1 ? "" : "s"}${shared}</small>
      </div>`;
  }

  // A contact record that names a DIFFERENT firm than the SEC currently files
  // this person under is almost always a move. That is a prospecting signal,
  // not a data error, so it is surfaced rather than reconciled away. Only
  // shown when both firm CRDs are known -- an unknown firm is not a mismatch.
  const moved = c.fc && p.fc && String(c.fc) !== String(p.fc);
  // "contact record says CRD 6413" read like an ADVISOR crd. 6413 is LPL
  // Financial's FIRM crd, which is what c.fc holds -- so the line named a
  // number that has nothing to do with the person and invited the reader to
  // treat it as their identifier. Name the firm where we can, and say "firm
  // CRD" where we cannot.
  const firmName = (crd, fallbackName) => {
    if (fallbackName) return esc(fallbackName);
    const known = NAT_FIRM_BY_CRD && NAT_FIRM_BY_CRD.get(String(crd));
    return known && known[0] ? esc(known[0]) : `firm CRD ${esc(crd)}`;
  };
  const mover = moved
    ? `<p class="contact-moved" title="The contact record and the current SEC filing disagree on the firm">
         &#8644; Possible move &mdash; our record has them at
         <b>${firmName(c.fc, c.cn)}</b>, the SEC now files them at
         <b>${firmName(p.fc, p.f)}</b></p>`
    : "";

  /* The state the two sources disagree about.
   *
   * On an unconfirmed match this contradicts 42% of the time, and it is the
   * clearest evidence available that the record belongs to someone else. It was
   * invisible: the card showed an Atlanta address and a Virginia phone number
   * with nothing to connect the two facts.
   */
  const stateClash = c.cs && p._state && String(c.cs) !== String(p._state)
    ? `<p class="contact-clash">&#9888; Our contact record puts this person in
         <b>${esc(c.cs)}</b>, the SEC files them in <b>${esc(p._state)}</b>.
         ${unconfirmed ? "On an unconfirmed match that usually means the record belongs to a different person."
                       : "Worth checking which is current."}</p>`
    : "";

  // How we know this is them. `review` is SHOWN rather than hidden: a rep who
  // can see "we think this is them" decides better than one shown nothing,
  // and hiding it would quietly drop a third of the matched contacts.
  const unsure = c.t === "review";
  const prov = `<p class="contact-src${unsure ? " contact-src-review" : ""}">`
    + (unsure ? "&#9888; Unconfirmed match &mdash; " : "")
    + `Source: ${esc(c.src)}`
    + (c.also && c.also.length ? ` (also ${esc(c.also.join(", "))})` : "")
    + `</p>`;

  // Queueing sits with the other actions rather than in a menu: deciding to
  // call someone happens at the same moment as reading their number, and a rep
  // building a list of thirty should not leave the card to do it.
  if (Dial.isDnc(p.id)) {
    const d = Dial.state.dnc.get(String(p.id)) || {};
    rows.push(`<span class="contact-btn contact-btn-dnc"
      title="Firm-wide do-not-call${d.by ? " — added by " + esc(d.by) : ""}">&#9940; Do not call</span>`);
  } else {
    const on = Dial.inQueue(p.id);
    // A queued contact loses its provenance: the dialer shows a name and a
    // number, not how confidently they were matched. Keeping unconfirmed
    // records off the list is what stops the warning being left behind on the
    // card it was written for.
    rows.push(unconfirmed
      ? `<span class="contact-btn blocked" title="Unconfirmed match — confirm who this is before adding them to a call list.">&#43; Call list</span>`
      : `<button type="button" class="contact-btn contact-btn-queue${on ? " on" : ""}"
      data-queue-toggle="${esc(p.id)}"
      title="${on ? "Remove from the call list" : "Add to the call list"}"
      >${on ? "&#10003; On call list" : "&#43; Call list"}</button>`);
  }
  rows.push(`<button type="button" class="contact-btn" data-person-actions="${esc(p.id)}"
    data-person-name="${esc(p.n || "")}" aria-haspopup="dialog" aria-expanded="false"
    aria-label="Choose a list for ${esc(p.n || "this person")}">&#43; Choose list&hellip;</button>`);

  return `<div class="contact-panel${unsure ? " contact-panel-review" : ""}">
      ${owner}${title}
      <div class="contact-actions">${rows.join("")}</div>
      <p class="contact-meta">${c.e ? mailtoLink(c.e, emailConfirmed, p.id) : ""}${c.wd
          ? ` &middot; ${esc(c.wd)}${c.wx ? esc(" ext. " + c.wx) : ""} <span class="phone-kind">${esc((PHONE_KIND[c.wk] || PHONE_KIND._).tip)}</span>`
          : ""}${c.cd ? ` &middot; ${esc(c.cd)} mobile` : ""}</p>
      ${assets}${stateClash}${mover}
      <p class="contact-hist" data-card-hist="${esc(p.id)}"></p>
      <!-- The CRM's own record as well as ours. Collapsed and fetched only on
           open: it costs a round trip to Act!, and most views of a card want
           the phone number rather than the file. -->
      <details class="hist-full" data-histfull="${esc(p.id)}">
        <summary>Full history</summary>
        <div class="hist-body"><p class="hist-foot">Loading…</p></div>
      </details>
      ${outcomeBlock(p.id)}
      ${prov}
    </div>`;
}

/* Record a call that was not made from the queue.
 *
 * WHY THIS EXISTS AT ALL. The desktop could only log an outcome from inside a
 * dialer session, so the same rep doing the same thing got a CRM entry from
 * their phone and silence from their desk. That reads as an unreliable sync
 * rather than a missing feature, and it is the more damaging of the two.
 *
 * WHY IT IS NOT GATED ON HAVING TAPPED CALL. "Call received" would be
 * unreachable -- an inbound call has no preceding tap -- and so would a call
 * placed from a desk handset. Gating on the tap would make the CRM a record of
 * HOW a call was made rather than THAT it was made.
 *
 * COLLAPSED, because most views of a card are research rather than logging.
 * Opened automatically when the rep taps a number: the tel: link hands off to
 * the dialer, so they leave and come back, and coming back to a card with the
 * buttons already showing is the difference between logging the call and
 * meaning to.
 */
function outcomeBlock(advisorId){
  if (Dial.isDnc(advisorId)) return "";
  const id = String(advisorId);
  return `<details class="log-out"${cardOutcomeOpen.has(id) ? " open" : ""}>
      <summary>Log an outcome</summary>
      ${purposeRow("card", cardPurpose)}
      <textarea class="card-note" id="cardNote" rows="2"
        placeholder="Note — saved with the outcome">${esc(cardNote)}</textarea>
      <div class="dial-outcomes card-outcomes" data-card-outcome="${esc(id)}">
        ${Dial.OUTCOMES.map(o => `<button type="button" data-card-out="${o.key}"
          class="${o.grave ? "grave" : ""}">${esc(o.label)}</button>`).join("")}
      </div>
    </details>
    <!-- OUTSIDE the <details>. Logging collapses the block, and a confirmation
         that collapses with it would leave the rep with no evidence anything
         happened -- which is the same "did that take?" doubt the collapse is
         meant to answer. -->
    <p class="card-out-note"></p>`;
}

// Per-advisor rather than a single flag: the panel re-renders for whoever is
// opened next, and a shared boolean would leave the grid hanging open on the
// following advisor as though something were half-logged against them.
const cardOutcomeOpen = new Set();
// The note being typed against an off-queue call. Out here rather than read from
// the textarea because the contact panel is rebuilt on re-render.
let cardNote = "";
// Why the call was made, for the card and for the dialer session. Same reason
// as the note: the panel rebuilds and a selection held only as a CSS class
// would vanish with it.
let cardPurpose = "";
let dialPurpose = "";

/* The purpose chips, one line, shared by the card and the dialer session.
 *
 * Chips rather than a <select>: this is a four-item choice made in the same
 * beat as the outcome, and a dropdown is two interactions and a closed list
 * the rep has to open before they can see what the options even are.
 *
 * Nothing is selected by default, and clicking the selected chip clears it.
 * The purpose becomes the Act! history subject -- firm-visible text in the
 * CRM's Title column -- so a rep who is unsure must be able to leave it blank
 * rather than pick the least wrong one. A guess there reads as a fact.
 */
function purposeRow(scope, chosen){
  return `<div class="purpose" data-purpose-scope="${scope}"
      role="group" aria-label="Why this call">
    ${Dial.PURPOSES.map(p => `<button type="button" data-purpose="${p.key}"
      class="${chosen === p.key ? "on" : ""}"
      aria-pressed="${chosen === p.key}">${esc(p.label)}</button>`).join("")}
  </div>`;
}

// Repainted in place rather than via a re-render: rebuilding the panel would
// also rebuild the note textarea and take the cursor out of it mid-sentence.
document.addEventListener("click", e => {
  const chip = e.target.closest("[data-purpose]");
  if (!chip) return;
  const scope = chip.closest("[data-purpose-scope]").dataset.purposeScope;
  const key = chip.dataset.purpose;
  const now = (scope === "card" ? cardPurpose : dialPurpose) === key ? "" : key;
  if (scope === "card") cardPurpose = now; else dialPurpose = now;
  [...chip.parentElement.children].forEach(b => {
    const on = b.dataset.purpose === now;
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", String(on));
  });
  // The Email button's draft is built from this choice. The panel is
  // deliberately NOT re-rendered here -- that would take the cursor out of the
  // note the rep may be mid-sentence in -- so the one thing the choice changes
  // is updated by hand.
  const root = scope === "card"
    ? chip.closest(".contact-panel") : document.getElementById("dialerSession");
  const mail = root && root.querySelector('[data-contact="email"], [data-dial="mailed"]');
  if (mail) {
    const id = mail.dataset.advisor || mail.dataset.crd;
    const item = scope === "dial" ? Dial.current() : (id ? dialSnapshot(id) : null);
    if (item) {
      mail.href = "#";
      mail.innerHTML = "&#9993; Email"
        + (now ? " (" + esc(Dial.purposeLabel(now)) + ")" : "");
    }
  }
});

// "Has anyone here already spoken to them?" — filled in after paint, because it
// must never delay the phone number. Across every user: a colleague's call three
// weeks ago is exactly what a per-browser log could never tell you, and exactly
// what stops the second cold call to the same person.
function fillCardHistory(advisorId){
  const slot = document.querySelector(`[data-card-hist="${CSS.escape(String(advisorId))}"]`);
  if (!slot) return;
  Dial.history(advisorId, 5).then(events => {
    // The panel may have moved on to someone else while this was in flight.
    if (!slot.isConnected || !events.length) return;
    const last = events[0];
    slot.innerHTML = `&#9990; <b>${events.length}</b> previous `
      + `${events.length === 1 ? "contact" : "contacts"} logged &middot; last `
      + `${esc(String(last.at || "").slice(0, 10))} by ${esc(last.who || "someone")}`
      + (last.disposition ? ` (${esc(Dial.outcomeLabel(last.disposition))})` : "")
      + (last.note ? ` &mdash; &ldquo;${esc(last.note.slice(0, 120))}&rdquo;` : "");
  }).catch(() => { /* history is a nicety; its absence is not an error */ });
}

// ---- pin badges ----
// A rep planning a territory should not have to click a pin to learn whether
// it is actionable. These answer three questions at a glance: can I reach this
// person, do we already own the relationship, and is there an asset figure.
function contactMarks(advisorId){
  const c = contactFor(advisorId);
  if (!c) return { reach:0, owned:false, assets:false };
  return {
    reach: (c.e ? 1 : 0) + (c.w || c.c ? 1 : 0),   // 0, 1 or 2 ways in
    owned: !!c.o,
    assets: !!(c.tm || c.ia > 0),
  };
}
function contactDots(advisorId){
  const m = contactMarks(advisorId);
  if (!m.reach && !m.owned && !m.assets) return "";
  const bits = [];
  // Glyphs, not colour alone -- these have to survive a colourblind reader and
  // a 6px dot on a busy basemap.
  if (m.reach) bits.push(`<span class="pin-dot pin-dot-reach" title="${m.reach === 2
      ? "Email and phone on file" : "One contact method on file"}">${m.reach === 2 ? "●" : "◐"}</span>`);
  if (m.owned) bits.push(`<span class="pin-dot pin-dot-owned" title="Existing EIC relationship">◆</span>`);
  if (m.assets) bits.push(`<span class="pin-dot pin-dot-assets" title="Asset figure on file">$</span>`);
  return `<span class="pin-dots">${bits.join("")}</span>`;
}

document.addEventListener("click", e => {
  const btn = e.target.closest("[data-contact]");
  if (!btn) return;
  // A tapped number opens the outcome grid for when they come back from the
  // handoff. Only for the phone buttons -- opening it because someone followed
  // a LinkedIn link would be noise.
  if (btn.dataset.contact === "work" || btn.dataset.contact === "cell") {
    cardOutcomeOpen.add(String(btn.dataset.advisor));
    const d = btn.closest(".contact-panel")?.querySelector(".log-out");
    if (d) d.open = true;
  }
  logContact(btn.dataset.advisor, btn.dataset.contact,
             document.getElementById("firmOverviewName")?.textContent || "");
});

/* ---- the full history: ours and the CRM's, in one date-ordered list -------
 * De-duplicated server-side using our own actStatus column, so a local row is
 * hidden only where we recorded that Act! took a copy. A missed match can
 * therefore only ever show a row twice, never hide one -- the opposite of what
 * hunting our own writes inside Act!'s payload would risk.
 */
function histRowHtml(r){
  return `<li class="${r.crm ? "from-crm" : r.email ? "from-mail" : "from-app"}">
    <span class="hist-when">${esc(r.at)}</span>
    <span class="hist-what">${esc(r.what)}</span>
    ${r.who ? `<span class="hist-who">${esc(r.who)}</span>` : ""}
    ${r.text ? `<span class="hist-text">${esc(r.text)}</span>` : ""}
  </li>`;
}

async function fillFullHistory(crd, body){
  try {
    const d = await Dial.fullHistory(crd, 40);
    const { rows, notice } = Dial.describeHistory(d.events, d.crm, d.mail);
    if (!body.isConnected) return;
    body.innerHTML =
      (notice ? `<p class="hist-notice">${esc(notice)}</p>` : "")
      + (rows.length
          ? `<ul class="hist-list">${rows.map(histRowHtml).join("")}</ul>`
          : `<p class="hist-foot">Nothing recorded for this advisor.</p>`)
      + (d.crm.ok ? `<p class="hist-foot">Includes ${d.crm.count}
          ${d.crm.count === 1 ? "entry" : "entries"} from Act!.</p>` : "");
  } catch (e) {
    if (body.isConnected)
      body.innerHTML = `<p class="hist-notice">History could not be loaded —
        ${esc(e.message || "")}</p>`;
  }
}

// Fetched on FIRST open only. `open` still holds the pre-click value here, so
// a falsy value means the browser is about to expand it.
document.addEventListener("click", e => {
  const s = e.target.closest(".hist-full > summary");
  if (!s) return;
  const det = s.parentElement;
  if (!det.open && !det._loaded) {
    det._loaded = true;
    fillFullHistory(det.dataset.histfull, det.querySelector(".hist-body"));
  }
});

// Remembered across re-renders, which otherwise collapse it under the rep.
document.addEventListener("click", e => {
  const s = e.target.closest(".log-out > summary");
  if (!s) return;
  const grid = s.parentElement.querySelector("[data-card-outcome]");
  if (!grid) return;
  const id = String(grid.dataset.cardOutcome);
  // `open` still holds the PRE-click value; the browser flips it afterwards.
  if (s.parentElement.open) cardOutcomeOpen.delete(id); else cardOutcomeOpen.add(id);
});

/* An outcome logged from the card rather than from a session.
 *
 * DELIBERATELY DOES NOT TOUCH THE QUEUE. No advance, no requeue: there is no
 * cursor here to move, and a "Call back" from a card the rep opened by
 * searching should not silently reorder a list they are not working.
 *
 * It DOES fail loudly. This is a disposition -- the one thing in the whole
 * application a human authored and nothing can regenerate -- so a failed write
 * says so on the card instead of leaving a pressed button looking successful.
 */
document.addEventListener("click", async e => {
  const b = e.target.closest("[data-card-out]");
  if (!b) return;
  const grid = b.closest("[data-card-outcome]");
  const crd = String(grid.dataset.cardOutcome);
  const disposition = b.dataset.cardOut;
  const c = contactFor(crd);
  const name = advisorDisplayName(crd)
    || document.getElementById("firmOverviewName")?.textContent || "";
  if (!Dial.confirmGrave(disposition, { name, unconfirmed: !!(c && c.t === "review") })) return;

  const det = b.closest(".log-out");
  const slot = det.parentElement.querySelector(".card-out-note");
  // Read from the DOM too: a rep can finish typing and hit an outcome before the
  // input event lands.
  const box = det.querySelector("#cardNote");
  const note = (box ? box.value : cardNote).trim();
  [...grid.children].forEach(x => { x.disabled = true; });
  try {
    const res = await Dial.log({
      crd, name, firm: (c && c.cn) || "", phone: (c && c.w) || "",
      phoneKind: (c && c.wk) || "", disposition, note, purpose: cardPurpose,
      kind: "outcome",
    });
    [...grid.children].forEach(x => x.classList.toggle("on", x === b));
    slot.className = "card-out-note";
    slot.textContent = Dial.actNotice(res.act)
      || `Logged: ${Dial.outcomeLabel(disposition)}.`;
    // COLLAPSE, on success only. Off-queue there is no next person to move to,
    // so nothing else changes on screen -- and an outcome grid left standing
    // open reads as "that did not take", which is how the same call gets logged
    // twice. The confirmation line above stays visible under the closed
    // summary -- it is a sibling of the block, not a child -- so the
    // acknowledgement survives the collapse.
    cardNote = "";
    cardPurpose = "";
    cardOutcomeOpen.delete(crd);
    if (box) box.value = "";
    det.open = false;
  } catch (err) {
    slot.className = "card-out-note bad";
    slot.textContent = /saved/i.test(err.message) ? err.message
                                                  : `${err.message} Nothing was saved.`;
  } finally {
    [...grid.children].forEach(x => { x.disabled = false; });
  }
});

/* ============================ the dialer ================================= *
 * Desktop half of the call queue. Shares dial.js with the field view, so the
 * outcome vocabulary, the do-not-call suppression and the queue itself are one
 * definition rather than two that drift.
 *
 * The difference from the phone is the NOTE FIELD, and it is deliberate: this
 * is where a rep sits down for ninety minutes and can type. The field view
 * offers six buttons and no free text, because the alternative is someone
 * typing at a wheel.
 *
 * There is no call timer and no "in progress" state, for the same reason as on
 * the phone: the browser is never told whether a call connected, so any such
 * display would be invented.
 * ========================================================================= */
const DIAL_NOTE_KEY = "advisorMap.dialNote.v1";
let dialMenuOpen = false;          // the ⋮ list menu

// A queue entry is a snapshot, not a reference: the field view resolves people
// from geographic tiles and cannot open a record it has no tile for, so a list
// built here has to carry enough to dial from a phone in a car park.
function dialSnapshot(id){
  const c = contactFor(id);
  const f = ALL.find(x => String(x.properties.id) === String(id));
  const p = f ? f.properties : null;
  return {
    crd: String(id),
    name: advisorDisplayName(id, p && p.n) || `CRD ${id}`,
    firm: (c && c.cn) || (p && p.f) || "",
    phone: (c && c.w) ? c.w + (c.wx ? "," + c.wx : "") : "",
    phoneKind: (c && c.wk) || "",
    city: (p && p.c) || "",
    /* The OFFICE state, not the first state they are registered in.
     *
     * This read p.rs.split("|")[0]. `rs` is the pipe-delimited list of states an
     * advisor is licensed in -- "GA|LA|TX" -- which has nothing to do with where
     * they sit. Pairing its first entry with p.c, the office city, produced
     * combinations that do not exist: Mitchell Gentry, whose filed address is
     * 1776 Peachtree Street NW, showed as "Atlanta, TX" because Texas is the
     * only state he is registered in.
     *
     * It was wrong for 89,991 of 418,916 advisors -- 21.5% -- and wrong in the
     * most misleading way available, since a plausible city/state pair invites
     * no suspicion. _state is the state file the pin was loaded from, which is
     * the office location by construction.
     */
    /* STILL ONLY _state, and deliberately.
     *
     * I briefly added `|| c.cs` here so an advisor queued from outside the
     * loaded scope carried somewhere to switch the map to. That is the wrong
     * place for it: c.cs is the CRM's state, not the office location, and
     * pairing it with the office city stored above is precisely the
     * "Atlanta, TX" failure this comment already warns about. The audit check
     * caught it.
     *
     * The scope fallback lives at the point of USE instead -- see
     * openAdvisorAnywhere() -- where it decides which map to load and never
     * becomes part of the stored record. */
    state: (p && p._state) || "",
    email: (c && c.e) || "",
    // Carried on the queue entry so the dialer, which shows only a name and a
    // number, still knows the match was never confirmed.
    unconfirmed: !!(c && c.t === "review"),
    identityApproved: !!(c && (c.t === "confirmed" || c.t === "high")),
    contactTier: (c && c.t) || "",
    contactSource: (c && c.src) || "",
    emailConfirmed: !!(c && Dial.tierCanEmail(c.t, c.src)),
    emailEligibilityKnown: true,
    emailTierKey: Dial.emailTierKey(),
    contactRouteVersion: DATA_VERSION,
  };
}

let desktopRouteReconcile = null;
function reconcileDesktopDialRoutes(){
  if (!CONTACTS_READY || !Dial.state.ready) return Promise.resolve(false);
  if (!Dial.state.items.some((item) =>
      item.contactRouteVersion !== DATA_VERSION
      // The rule that decided this proof, not just the data behind it. Without
      // it, widening who may be emailed reached new lists only: every saved
      // item still matched the current data build, so nothing looked stale and
      // a rep's existing lists kept an answer from a rule we no longer apply.
      || item.emailTierKey !== Dial.emailTierKey()
      || item.emailEligibilityKnown !== true
      || (item.identityApproved !== true && !item.routeIssue)))
    return Promise.resolve(false);
  if (desktopRouteReconcile) return desktopRouteReconcile;
  desktopRouteReconcile = Dial.reconcileRoutes((saved) =>
      contactFor(saved.crd) ? dialSnapshot(saved.crd) : null)
    .finally(() => { desktopRouteReconcile = null; });
  return desktopRouteReconcile;
}

// The email composer receives the same server-synced contact snapshots as the dialer.
// It never accepts a client-supplied mailbox identity; this only supplies recipients.
/* The advisor's practice-mates, with addresses, for "CC the teammates".
 *
 * Supplied by the CLIENT because contacts.json is a static asset the API never
 * loads -- so the server treats this as a hint and re-checks every address for
 * shape, suppression and duplication before it reaches a draft.
 *
 * Only people with an address on file, and never the advisor themselves.
 */
function teammatesOf(crd){
  const c = contactFor(crd);
  const practice = c && practiceFor(c);
  if (!practice || !practice.m) return [];
  const out = [];
  for (const [id] of practice.m) {
    if (String(id) === String(crd)) continue;
    const mate = contactFor(id);
    // Review-tier practice members are evidence for a human, not authorized
    // recipients. The server enforces this too; filtering here prevents the UI
    // from offering a choice it will correctly refuse.
    if (mate && mate.e && Dial.tierCanEmail(mate.t, mate.src))
      out.push({ crd: String(id), name: advisorDisplayName(id), email: mate.e });
  }
  return out;
}
const teammateEmails = (crd) => teammatesOf(crd).map(t => t.email);

/* THE GREETING, from the CRM's own Dear field.
 *
 * {{first_name}} was rendered by splitting the display name, so Christopher
 * Tolman -- "Chris" in the Dear field of his own Act! record, and "Chris" to
 * everybody at UBS -- was greeted as Christopher on every email.
 *
 * Sent as firstName because email-core.mergeValues() already prefers
 * recipient.firstName over splitting the name; nothing on the server changes.
 * Absent for roster-sourced advisors, where the old behaviour is still right.
 */
const greetingFor = (crd) => {
  const c = contactFor(crd);
  return (c && c.sal) || "";
};

window.AdvisorEmailData = {
  recipientFor: (id) => {
    const snap = dialSnapshot(id);
    return !snap.emailConfirmed ? null : { ...snap, firstName: greetingFor(id),
      teammates: teammateEmails(id), teammatesFull: teammatesOf(id) };
  },
  list: () => {
    const out = Dial.state.items.filter((it) => Dial.emailRouteStatus(it).ok)
      .map((it) => ({ ...it, firstName: greetingFor(it.crd),
        identityLabel: Dial.identityTierLabel(it.contactTier, it.contactSource),
        teammates: teammateEmails(it.crd), teammatesFull: teammatesOf(it.crd) }));
    out.eligibilitySummary = { selected: Dial.state.items.length, included: out.length,
      excluded: Dial.state.items.length - out.length };
    return out;
  },
};


/* The office state, preferring live data over the stored snapshot.
 *
 * Queue entries are snapshots taken when somebody was added to a list, and the
 * snapshots written before the registration-state bug was fixed still carry the
 * wrong value -- Mitchell Gentry sits at 1776 Peachtree Street and his saved
 * entry says TX, because Texas is the only state he is licensed in.
 *
 * Correcting the writer fixed new entries and left every existing list wrong.
 * Reading through to the pin data repairs them on sight, without asking anybody
 * to rebuild a list they have already made.
 */
function liveState(it){
  // Same lookup dialSnapshot uses. ALL only holds the loaded scope, so a queue
  // entry from another state simply keeps its stored value rather than losing
  // one -- which is the right failure: stale beats blank.
  const f = it && it.crd != null
    ? ALL.find((x) => String(x.properties.id) === String(it.crd)) : null;
  const live = f && f.properties && f.properties._state;
  return live || (it && it.state) || "";
}

function dialKindLabel(kind){
  return kind === "direct" ? "Direct" : kind === "extension" ? "Extension"
       : kind ? "Office" : "";
}

let dialError = "";
let dialHistory = { crd: "", text: "" };
let standingCleanup = null;

// Which standing flag a REAL list corresponds to, by name, or "" for an
// ordinary list the rep made themselves.
const ROLE_META = {
  key: { label: "Key people", singular: "Key person", symbol: "&#9733;" },
  dd: { label: "Analysts", singular: "Analyst", symbol: "&#128737;" },
  scheduler: { label: "Schedulers", singular: "Scheduler", symbol: "&#128197;" },
};
const ROLE_LIST_IDS = { key: "role-key", dd: "role-analyst", scheduler: "role-scheduler" };
const LEGACY_ROLE_IDS = {
  keypeople: "key", keycontacts: "key", analyst: "dd", analysts: "dd",
  duediligence: "dd", scheduler: "scheduler", schedulers: "scheduler",
};
const STANDING_NAMES = {
  "key contacts": "key", "key people": "key",
  "due diligence": "dd", "analyst": "dd", "analysts": "dd",
  "scheduler": "scheduler", "schedulers": "scheduler",
};
function standingKindOf(listId){
  const id = String(listId || "").toLowerCase();
  const canonical = Object.entries(ROLE_LIST_IDS).find(([, roleId]) => roleId === id);
  if (canonical) return canonical[0];
  const legacy = LEGACY_ROLE_IDS[id];
  if (!legacy) return "";
  const l = (Dial.state.lists || []).find((x) => String(x.id).toLowerCase() === id);
  return l && STANDING_NAMES[String(l.name || "").trim().toLowerCase()] === legacy ? legacy : "";
}

/* Unmarking must also take them OFF the standing list.
 *
 * The list is materialised: openFlagList() writes the flagged people into a
 * real call list, so it is a snapshot, not a live view. Clearing the star
 * changed the flag and left the person sitting in the open queue -- with a Call
 * button beside them -- until something happened to rebuild it. A rep who
 * unstars somebody has just said "not this person", and the next number the
 * dialer serves must not be theirs.
 *
 * Only when the OPEN list is the matching standing list. Removing them from an
 * ordinary list the rep built by hand would be destroying work they did not ask
 * to undo -- their own list is theirs, whatever the flag says.
 */
async function dropFromStandingList(crd, kind, stillMine){
  if (stillMine) return false;
  if (standingKindOf(Dial.state.listId) !== kind) return false;
  if (!flaggedAdvisors(kind).length) {
    await Dial.deleteList(Dial.state.listId);
    return true;
  }
  if (!Dial.inQueue(crd)) return false;
  await Dial.remove(String(crd));
  return true;
}

function standingOption(kind, icon, label){
  const active = standingKindOf(Dial.state.listId) === kind;
  const n = active ? Dial.state.items.length : flaggedAdvisors(kind).length;
  if (!n && !active) return "";
  const selected = active ? " selected" : "";
  return `<option value="__${kind}"${selected}>${icon} ${label}${n ? ` (${n})` : ""}</option>`;
}

function renderDialer(){
  const S = Dial.state;
  const dock = document.getElementById("dialer");
  if (!dock) return;
  const n = S.items.length;
  const ordinaryLists = S.lists.filter((l) => !standingKindOf(l.id));
  const activeStanding = standingKindOf(S.listId);
  if (activeStanding && !flaggedAdvisors(activeStanding).length && !standingCleanup) {
    standingCleanup = Dial.deleteList(S.listId)
      .catch((err) => { dialError = err.message || "The empty role list could not be retired."; })
      .finally(() => { standingCleanup = null; renderDialer(); });
  }
  // The dock survives an EMPTY list as long as another list exists. It used to
  // hide whenever the open list had nobody in it, which meant creating a new
  // list made the whole dock vanish -- taking the list picker with it, so there
  // was no way back to the list you had just spent ten minutes filling.
  /* The dock also survives on FLAGS ALONE.
   *
   * It hid whenever the open list was empty and there was no second list --
   * and the dock carries the only route to the list manager and, now, to the
   * two standing lists in the picker. So a rep who starred somebody and was
   * not mid-session had no way to reach their own Key contacts at all: the
   * list existed, was correct, and was unreachable. That is how this feature
   * came to look like it had never been built. */
  const standing = Object.keys(ROLE_META).reduce((n, kind) => n + flaggedAdvisors(kind).length, 0);
  dock.hidden = !n && S.lists.length < 2 && !S.problem && !standing;
  if (dock.hidden) { dialMenuOpen = false; return; }

  const bar = document.getElementById("dialerBar");
  const cur = Dial.current();
  const p = Dial.progress();
  const finished = n > 0 && p.left === 0;
  bar.innerHTML = S.problem
    ? `<span class="dial-problem" title="${esc(S.problem)}">&#9888; Call logging unavailable</span>`
    // Two deliberate rows rather than one row that wraps when it feels like it.
    // Top: which list you are in, and the controls that act on the list itself.
    // Bottom: the things you do -- email, history, and the call action. The
    // one-row version put the list name and the primary button in competition
    // for the same horizontal space, and on a narrow dock the primary lost.
    : `<div class="dial-row dial-row-top">`
      + `<button type="button" class="dial-collapse" data-dial="toggle-queue"
           aria-label="Show or hide the call list">&#9776;</button>`
      + `<select class="dial-list" data-dial="pick" aria-label="Call list">`
        + ordinaryLists.map(l => `<option value="${esc(l.id)}"${l.id === S.listId ? " selected" : ""}>`
            + `${esc(l.name)} (${l.count})</option>`).join("")
        /* The standing entries BUILD the list; they are not a second copy of it.
         *
         * openFlagList() materialises a real list called "Key contacts", so
         * after the first use that list is in S.lists and the picker showed
         * BOTH -- and selecting the synthetic one made the <select> jump to the
         * real one, which reads as landing on a different list entirely. The
         * queue was right the whole time; the control was lying about it.
         *
         * So the synthetic entry appears only until the real list exists. From
         * then on there is exactly one row for it, and the list manager's ★ row
         * is what rebuilds it from the flags. */
        + Object.entries(ROLE_META).map(([kind, meta]) =>
            standingOption(kind, meta.symbol, meta.label)).join("")
        + `<option value="__new">+ New list…</option></select>`
      + `<span class="dial-count">${p.done} of ${n}</span>`
      + `<button type="button" class="dial-btn ghost" data-dial="menu"
           aria-expanded="${dialMenuOpen}" title="List options">&#8942;</button>`
      + `</div><div class="dial-row dial-row-actions">`
      + (n ? `<button type="button" class="email-toolbar-btn" data-email="open-list">Email list</button>` : "")
      + `<button type="button" class="email-toolbar-btn" data-email="history" title="Emails you have sent from here">&#9993; History</button>`
      // A finished list must NOT offer "Resume". It used to, and pressing it
      // silently replayed the list from person one.
      + (cur
          ? `<button type="button" class="dial-btn ghost" data-dial="pause">Pause</button>`
          : finished
            ? `<button type="button" class="dial-btn primary" data-dial="cycle">Start pass ${S.cycle + 1}</button>`
            : `<button type="button" class="dial-btn primary" data-dial="start"${n ? "" : " disabled"}>`
              + `${p.done ? "Continue calling" : "Start calling"}</button>`)
      + `</div>`;

  // A real menu, not a prompt asking the rep to TYPE the word "rename". That
  // was unusable even when it worked -- and it did not work, because it read a
  // variable that only exists inside this function.
  const menu = document.getElementById("dialerMenu");
  menu.hidden = !dialMenuOpen || !!S.problem;
  if (!menu.hidden) {
    menu.innerHTML =
      `<div class="dial-menu-head">${esc(S.listName)} &middot; ${n} ${n === 1 ? "person" : "people"}`
      + `${S.cycle > 1 ? ` &middot; pass ${S.cycle}` : ""}</div>`
      + `<button type="button" data-dial="lists">Manage all lists&hellip;</button>`
      + (activeStanding
        ? `<p class="dial-menu-note">Membership is controlled by the ${esc(ROLE_META[activeStanding].singular)} label.</p>`
        : `<button type="button" data-dial="edit"${n ? "" : " disabled"}>Edit who&rsquo;s on it</button>`
          + `<button type="button" data-dial="rename">Rename this list</button>`
          + `<button type="button" data-dial="empty"${n ? "" : " disabled"}>Remove everyone from it</button>`
          + `<button type="button" class="grave" data-dial="drop">Delete this list</button>`
          + `<p class="dial-menu-note">Your call history is kept either way.</p>`);
  }

  // The list, for reordering and removing. Collapsed by default -- the point of
  // a queue is not having to look at it.
  const qWrap = document.getElementById("dialerQueue");
  if (!qWrap.hidden) {
    qWrap.innerHTML = n ? `<ol class="dial-queue">${S.items.map((it, i) => `
      <li class="${Dial.isDone(it.crd) ? "done" : ""}${i === S.cursor && cur ? " current" : ""}">
        <span class="dial-li-main"><b>${esc(it.name)}${flagGlyphs(it.crd)}</b>
          <small>${esc([it.firm, [it.city, liveState(it)].filter(Boolean).join(", ")]
            .filter(Boolean).join(" · "))}</small></span>
        ${personActionButton(it.crd, it.name)}
        <span class="dial-li-acts">
          <button type="button" data-dial="up" data-crd="${esc(it.crd)}" title="Move up"${i ? "" : " disabled"}>&#9650;</button>
          <button type="button" data-dial="down" data-crd="${esc(it.crd)}" title="Move down"${i === n - 1 ? " disabled" : ""}>&#9660;</button>
          <button type="button" data-dial="remove" data-crd="${esc(it.crd)}" title="Remove">&times;</button>
        </span>
      </li>`).join("")}</ol>`
      : `<p class="dial-empty">Nothing queued. Use <b>+ Call list</b> on an advisor.</p>`;
  }

  const sess = document.getElementById("dialerSession");
  sess.hidden = !cur;
  if (!cur) return;

  const nxt = S.items[S.items.findIndex((it, i) => i > S.cursor && !Dial.isDone(it.crd))];
  const kind = dialKindLabel(cur.phoneKind);
  // Preserved across re-renders: a rep typing a note while the panel repaints
  // for an unrelated reason must not lose it.
  const note = sessionStorage.getItem(DIAL_NOTE_KEY + ":" + cur.crd) || "";
  // Only set once the rep has come back to somebody, so it reads as context
  // rather than as a running commentary on their own last click.
  const priorOutcome = Dial.lastOutcome(cur.crd);

  sess.innerHTML = `
    <div class="dial-sess-head">
      ${Dial.canBack() ? `<button type="button" class="dial-open" data-dial="back"
        title="Back to the previous call — corrects, never un-logs">&lsaquo; Back</button>` : ""}
      <span class="dial-pos">${Math.min(p.done + 1, S.items.length)} of ${S.items.length}${
        S.cycle > 1 ? ` &middot; pass ${S.cycle}` : ""}</span>
      <button type="button" class="dial-open" data-dial="open" data-crd="${esc(cur.crd)}"
        title="Open this advisor on the map">Show on map</button>
    </div>
    ${priorOutcome ? `<p class="dial-prior">You logged
      <b>${esc(Dial.outcomeLabel(priorOutcome.disposition))}</b> here.
      Logging another records a correction.</p>` : ""}
    <h3 class="dial-name">${esc(cur.name)}${personActionButton(cur.crd, cur.name)}</h3>
    <p class="dial-sub">${esc([cur.firm, [cur.city, cur.state].filter(Boolean).join(", ")]
        .filter(Boolean).join(" · "))}</p>
    ${cur.email ? `<p class="dial-mail">${mailtoLink(cur.email,
      Dial.emailRouteStatus(cur).ok, cur.crd)}</p>` : ""}
    <p class="dial-hist" id="dialHist">${dialHistory.crd === cur.crd ? dialHistory.text : ""}</p>
    <div class="dial-acts">
      ${!cur.phone ? `<span class="dial-nonum">No number on file</span>`
        : Dial.isDnc(cur.crd)
          ? `<span class="dial-nonum">&#9940; Do not call — log Skip to move on</span>`
          : !Dial.routeStatus(cur).ok
            ? `<span class="dial-nonum">Current contact details must be verified; log Skip to move on</span>`
            : `<a class="dial-btn primary" href="${esc(Dial.telHref(cur.crd, cur.phone, cur))}"
          data-dial="dialled" data-crd="${esc(cur.crd)}">&#9742; Call${kind ? " (" + kind + ")" : ""}</a>`}
      ${cur.email && Dial.emailRouteStatus(cur).ok ? `<a class="dial-btn ghost"
          href="#"
          data-dial="mailed" data-crd="${esc(cur.crd)}">&#9993; Email${
            dialPurpose ? " (" + esc(Dial.purposeLabel(dialPurpose)) + ")" : ""}</a>` : ""}
    </div>
    ${purposeRow("dial", dialPurpose)}
    <textarea id="dialNote" class="dial-note" rows="2" maxlength="4000"
      placeholder="Notes — saved with the outcome">${esc(note)}</textarea>
    <!-- A 3x3 grid, matching the field view. Equal cells mean each outcome sits
         in the same place every time, which is what a hand learns; the wrapping
         flex row it replaced put buttons in different positions depending on
         the panel width, and left "Do not call" wherever there happened to be
         room. Skip spans two cells so the grid closes. -->
    <div class="dial-outcomes">
      ${Dial.OUTCOMES.filter(o => !o.grave).map(o => `<button type="button" data-dial="outcome"
        data-outcome="${o.key}">${esc(o.label)}</button>`).join("")}
      <button type="button" data-dial="outcome" data-outcome="skipped"
        class="dial-skip span2">Skip, no call made</button>
      ${Dial.OUTCOMES.filter(o => o.grave).map(o => `<button type="button" data-dial="outcome"
        data-outcome="${o.key}" class="grave">${esc(o.label)}</button>`).join("")}
    </div>
    <div class="dial-foot">
      <span class="dial-next">${nxt ? "Next: " + esc(nxt.name) : "Last one"}</span>
    </div>
    <div class="dial-auto">
      <label class="dial-auto-row">
        <input type="checkbox" data-dial="auto-on"${S.auto.on ? " checked" : ""}>
        <span>Auto-dial the next call</span>
      </label>
      ${S.auto.on ? `<div class="dial-auto-opts">
        <label>after
          <input type="number" data-dial="auto-delay" min="${Dial.AUTO_MIN}"
                 max="${Dial.AUTO_MAX}" value="${S.auto.delay}"> s</label>
        <label><input type="checkbox" data-dial="auto-announce"${S.auto.announce ? " checked" : ""}>
          say the name first</label>
      </div>` : ""}
    </div>
    ${S.pending ? `<p class="dial-countdown">
        Calling <b>${esc(S.pending.name)}</b> in ${S.pending.left}s
        <button type="button" data-dial="auto-cancel">Cancel</button></p>` : ""}
    ${dialError ? `<p class="dial-err">${esc(dialError)}</p>` : ""}`;

  if (dialHistory.crd !== cur.crd) loadDialHistory(cur.crd);
}

// "Has anyone here already spoken to them?" -- across every user, which is the
// question a per-browser log could never answer. Fetched after paint; its
// absence is not an error.
function loadDialHistory(crd){
  dialHistory = { crd, text: "" };
  Dial.history(crd, 5).then(events => {
    if (dialHistory.crd !== crd || !events.length) return;
    const last = events[0];
    dialHistory.text = `<b>${events.length}</b> previous `
      + `${events.length === 1 ? "contact" : "contacts"} logged · last `
      + `${esc(String(last.at || "").slice(0, 10))} by ${esc(last.who || "someone")}`
      + (last.disposition ? ` (${esc(Dial.outcomeLabel(last.disposition))})` : "");
    const el = document.getElementById("dialHist");
    if (el) el.innerHTML = dialHistory.text;
  }).catch(() => {});
}

// Hands the placing of the call back to this view, because the tel: anchor
// lives here. A blocked navigation simply leaves the button for the rep.
function armAutoDial(){
  const nxt = Dial.current();
  if (!nxt) return;
  Dial.armAuto(nxt, () => {
    const a = document.querySelector('#dialerSession [data-dial="dialled"]');
    if (a) a.click();
  });
}

// The one place on this page that refuses to carry on when a write fails.
// Nothing advances until the server has the record.
async function recordDialOutcome(disposition){
  const cur = Dial.current();
  if (!cur) return;
  if (!Dial.confirmGrave(disposition, cur)) return;
  const noteEl = document.getElementById("dialNote");
  const note = noteEl ? noteEl.value.trim() : "";
  document.querySelectorAll('#dialerSession [data-dial="outcome"]')
    .forEach(b => { b.disabled = true; });
  try {
    const res = await Dial.log({ ...cur, disposition, note, purpose: dialPurpose,
                                 kind: "outcome" });
    // Not an error -- the outcome IS saved -- but the rep must not be left
    // believing the CRM has it when it does not. Empty in the ordinary case.
    dialError = Dial.actNotice(res.act);
    sessionStorage.removeItem(DIAL_NOTE_KEY + ":" + cur.crd);
    dialPurpose = "";
    // Three different ways to leave this person, and each already positions the
    // cursor differently:
    //   do-not-call  removed them, so the cursor is on the next one already
    //   call back    moved them to the end, same effect
    //   anything else  they stay put, so the cursor has to move
    if (Dial.REQUEUE.has(disposition)) await Dial.requeue(cur.crd);
    else if (!res.removedCurrent) await Dial.advance();
    dialHistory = { crd: "", text: "" };
    armAutoDial();
  } catch (err) {
    dialError = /saved/i.test(err.message) ? err.message
                                           : `${err.message} Nothing was saved.`;
  }
  renderDialer();
}

/* ---- bulk add ------------------------------------------------------------
 * One predicate for "what am I looking at", shared with the CSV export that
 * used to sit in this slot: advisors passing the current filters, inside the
 * viewport, deduped by CRD. Two definitions of "current selection" would
 * eventually disagree, and the disagreement would be silent.
 */
function visibleAdvisorIds(){
  const bounds = map.getBounds();
  const seen = new Set();
  for (const f of ALL){
    const p = f.properties;
    if (!passesFilters(p)) continue;
    const [lon, lat] = f.geometry.coordinates;
    if (!bounds.contains([lat, lon])) continue;
    seen.add(String(p.id));
  }
  return [...seen];
}

/* Where should a bulk add land?
 *
 * THE TEST IS "DOES THIS LIST ALREADY HAVE PEOPLE IN IT", not "is it a saved
 * list". It used to be the latter: the default scratch list called "current"
 * was exempt on the reasoning that it exists to be filled and emptied. But a
 * rep does not work in a list called "current" for ten minutes and then think
 * of it as scratch -- by then it is forty people they chose, and the next
 * +&#9742; tap silently poured another two hundred on top of them with no undo
 * and no way to tell the two groups apart afterwards.
 *
 * An EMPTY list still adds without asking, whatever it is called. There is
 * nothing there to damage, and a prompt on every single bulk add is how a rep
 * learns to dismiss the prompt without reading it -- which would cost us the
 * one case it exists for.
 */
function askDestination(count, listName, existing){
  return new Promise(resolve => {
    const back = document.createElement("div");
    back.className = "ask-back";
    back.innerHTML =
      `<div class="ask" role="dialog" aria-modal="true" aria-label="Where should these advisors go?">`
      + `<h3>Add ${count.toLocaleString()} ${count === 1 ? "advisor" : "advisors"} where?</h3>`
      + `<p><b>${esc(listName)}</b> already has ${existing.toLocaleString()} `
      + `${existing === 1 ? "person" : "people"} in it. Adding to it cannot be undone `
      + `in one step &mdash; afterwards there is no way to tell the two groups apart.</p>`
      + `<button type="button" class="ask-btn" data-ask="current">`
      + `Add to &ldquo;${esc(listName)}&rdquo;</button>`
      + `<button type="button" class="ask-btn primary" data-ask="new">Put them in a new list</button>`
      + `<input class="ask-name" type="text" placeholder="Name for the new list" `
      + `value="${esc(defaultListName())}" aria-label="Name for the new list">`
      + `<button type="button" class="ask-btn ghost" data-ask="cancel">Cancel</button></div>`;
    const done = (v) => { back.remove(); document.removeEventListener("keydown", onKey); resolve(v); };
    const onKey = (e) => { if (e.key === "Escape") done(null); };
    back.addEventListener("click", (e) => {
      if (e.target === back) return done(null);
      const b = e.target.closest("[data-ask]");
      if (!b) return;
      if (b.dataset.ask === "cancel") return done(null);
      if (b.dataset.ask === "current") return done({ mode: "current" });
      const name = back.querySelector(".ask-name").value.trim();
      done({ mode: "new", name: name || defaultListName() });
    });
    document.addEventListener("keydown", onKey);
    document.body.appendChild(back);
    back.querySelector(".ask-name").focus();
  });
}

/* ---- list management -----------------------------------------------------
 * The dialer bar has a <select> of lists, which is enough to SWITCH and not
 * enough to manage. Everything else -- rename, empty, delete -- was on the ⋮
 * menu and applied only to whichever list happened to be open, so tidying up
 * three stale lists meant opening each one first and doing the work from
 * inside it. That is also how a rep empties the wrong list.
 *
 * So: one place that shows every list at once, with its size and how many
 * passes it has had, and acts on the list you are pointing at rather than on
 * the one that is open. "Call" is the primary action on each row, because
 * picking up a saved list and working it is the reason lists exist -- and it
 * is the one thing the ⋮ menu could not do at all.
 */
const AUDIENCE_API = "/api/audiences";
let dynamicAudiences = [];
let audiencePreview = null;
let audienceProblem = "";

async function audienceCall(url, options){
  const response = await fetch(url, options);
  let data = {};
  try { data = await response.json(); } catch {}
  if (!response.ok) throw new Error(data.error || data.message || `Saved audiences returned ${response.status}.`);
  return data;
}
async function loadDynamicAudiences(){
  const data = await audienceCall(AUDIENCE_API);
  dynamicAudiences = data.audiences || data.items || [];
  return dynamicAudiences;
}
function audienceScopeDefinition(){
  const territory = scope.startsWith("T:") ? scope.slice(2) : "";
  return { kind:territory ? "territory" : "state", value:scope, label:scopeLabel(scope),
    states:territory ? [...(TERRITORIES[territory] || [])] : [scope] };
}
function captureAudienceDefinition(){
  return { version:1, scope:audienceScopeDefinition(), filters:{
    selectedFirms:[...selectedFirms], selectsOnly, aum:[...aumSel], reg,
    exp:[...expSel], reach:[...reachSel], geo:[...geoSel], ownerOnly,
    rankedOnly, excluded:[...excludedFirms], continentalOnly,
    contactableOnly, assetsOnly,
  }};
}
async function saveCurrentAudience(){
  if (scope === "US") return showNotice("Choose a state or sales territory before saving. The national layer contains offices, not advisor identities.");
  if (lassoPolygon) return showNotice("Clear the map lasso before saving. A movable map shape is not a stable audience rule.");
  const name = prompt("Name this dynamic audience", `${scopeLabel(scope)} prospects`);
  if (!name || !name.trim()) return;
  const description = prompt("Optional description", "") || "";
  try {
    await audienceCall(AUDIENCE_API, { method:"PUT", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({ name:name.trim(), description:description.trim(), definition:captureAudienceDefinition() }) });
    await loadDynamicAudiences();
    showNotice(`Saved dynamic audience "${name.trim()}".`);
    if (listsBack) paintListManager();
  } catch (error) { showNotice(error.message || "The audience could not be saved."); }
}
async function applyAudienceDefinition(audience){
  const definition = audience && audience.definition;
  if (!definition || definition.version !== 1) throw new Error("This audience uses an unsupported rule version.");
  const target = definition.scope && definition.scope.value;
  if (!target || target === "US") throw new Error("This audience does not identify an advisor-level state or territory.");
  if (target !== scope) {
    const result = await switchScope(target);
    if (!result || result.status !== "applied") throw new Error(`Could not load ${definition.scope.label || target}.`);
  }
  const f = definition.filters || {};
  selectedFirms = Array.isArray(f.selectedFirms) ? f.selectedFirms.map(String) : [];
  selectsOnly = !!f.selectsOnly;
  selectsBox.checked = selectsOnly;
  selectsBox.closest(".switch").classList.toggle("on", selectsOnly);
  refill(aumSel, f.aum); reg = f.reg || "all";
  refill(expSel, f.exp); refill(reachSel, f.reach); refill(geoSel, f.geo);
  ownerOnly = !!f.ownerOnly; rankedOnly = !!f.rankedOnly;
  excludedFirms = new Set((f.excluded || []).map(String));
  setContinentalOnly(f.continentalOnly !== false, false);
  contactableOnly = !!f.contactableOnly; assetsOnly = !!f.assetsOnly;
  focusedAdvisorId = null; focusedAdvisorLabel = ""; clearLasso(false);
  syncFirmColors(); document.getElementById("clearFirms").hidden = !selectedFirms.length;
  syncFilterButtons(); redraw();
}
function currentAudiencePreview(audience){
  const byCrd = new Map();
  for (const feature of ALL) {
    const p = feature.properties;
    if (passesFilters(p) && !byCrd.has(String(p.id))) byCrd.set(String(p.id), dialSnapshot(p.id));
  }
  const rows = [...byCrd.values()].map(item => {
    const dnc = Dial.isDnc(item.crd);
    const phoneStatus = item.phone ? Dial.routeStatus(item) : { ok:false };
    const emailStatus = item.email ? Dial.emailRouteStatus(item) : { ok:false };
    const callable = !dnc && !!item.phone && phoneStatus.ok;
    const emailable = !dnc && !!item.email && emailStatus.ok;
    const identityIssue = (!!item.phone && !phoneStatus.ok) || (!!item.email && !emailStatus.ok);
    return { item, callable, emailable, dnc, identityIssue, owner:territoryFor(item.state) };
  });
  return { audience, rows, matches:rows.length, callable:rows.filter(x => x.callable).length,
    emailable:rows.filter(x => x.emailable).length,
    excluded:rows.filter(x => !x.callable && !x.emailable).length,
    dnc:rows.filter(x => x.dnc).length, identity:rows.filter(x => x.identityIssue).length };
}
async function ensureAudienceIdentity(){
  if (ME) return ME;
  try { ME = await Dial.whoAmI(); } catch { ME = null; }
  return ME;
}
async function prepareAudiencePreviewData(audience){
  const f = audience && audience.definition && audience.definition.filters || {};
  const support = f.ownerOnly || f.rankedOnly
    ? loadRegionalSupport()
    : (SUPPORT.territories === "ready" ? Promise.resolve() : loadTerritories());
  await Promise.all([loadContacts(), ensureAudienceIdentity(), support]);
  if (!CONTACTS_READY)
    throw new Error("Contact eligibility is unavailable, so this audience cannot be previewed safely. Try again after contact data loads.");
  if (SUPPORT.territories !== "ready")
    throw new Error("Territory assignments are unavailable, so ownership cannot be reviewed safely.");
  if (f.ownerOnly && SUPPORT.owner !== "ready")
    throw new Error("Owner and officer data is unavailable for this saved audience.");
  if (f.rankedOnly && (SUPPORT.barrons !== "ready" || SUPPORT.forbes !== "ready"))
    throw new Error("Advisor ranking data is unavailable for this saved audience.");
}
function audienceTerritoryPolicy(preview){
  const email = String(ME && ME.userDetails || "").trim().toLowerCase();
  const national = Object.values((SALES_TERRITORY && SALES_TERRITORY.national) || {});
  const nationalRep = national.find(rep => String(rep && rep.e || "").trim().toLowerCase() === email);
  if (email && nationalRep)
    return { kind:"national", rows:preview.rows, outside:0,
      text:`National coverage account (${esc(nationalRep.n || email)}): all ${preview.matches.toLocaleString()} matches are within national coverage.` };
  const assigned = Object.values((SALES_TERRITORY && SALES_TERRITORY.states) || {})
    .find(rep => String(rep && rep.e || "").trim().toLowerCase() === email);
  if (email && assigned) {
    const rows = preview.rows.filter(row => String(row.owner && row.owner.e || "").trim().toLowerCase() === email);
    return { kind:"territory", rows, outside:preview.matches - rows.length,
      text:`${esc(assigned.n || email)}: ${rows.length.toLocaleString()} in my assigned territory; ${(preview.matches - rows.length).toLocaleString()} outside and excluded from new snapshots.` };
  }
  return { kind:"unassigned", rows:preview.rows, outside:0,
    text:`No sales territory is assigned to this account${email ? ` (${esc(email)})` : ""}. Review the owner distribution before preparing a contact list.` };
}
function dynamicAudienceRows(){
  if (audienceProblem) return `<p class="lists-none">${esc(audienceProblem)}</p>`;
  if (!dynamicAudiences.length) return `<p class="lists-none">No dynamic audiences saved yet. Use <b>save as audience</b> beside Active filters.</p>`;
  return `<ul class="lists-ul">${dynamicAudiences.map(a => `<li class="lists-row audience-row">
    <span class="lists-main"><b>${esc(a.name || "Untitled audience")}</b><small>Dynamic &middot; ${esc(a.scopeLabel || (a.definition && a.definition.scope && a.definition.scope.label) || "saved scope")}${a.description ? ` &middot; ${esc(a.description)}` : ""}</small></span>
    <span class="lists-acts"><button type="button" class="ask-btn primary" data-lists="audience-open" data-id="${esc(a.id)}">Preview</button><button type="button" class="grave" data-lists="audience-drop" data-id="${esc(a.id)}">Delete</button></span>
  </li>`).join("")}</ul>`;
}
function paintAudiencePreview(){
  const p = audiencePreview;
  if (!listsBack || !p) return;
  const owners = new Map();
  p.rows.forEach(r => { if (r.owner && r.owner.n) owners.set(r.owner.n, (owners.get(r.owner.n) || 0) + 1); });
  const policy = audienceTerritoryPolicy(p);
  const readyCall = policy.rows.filter(row => row.callable).length;
  const readyEmail = policy.rows.filter(row => row.emailable).length;
  listsBack.innerHTML = `<div class="ask lists lists-workspace" role="dialog" aria-modal="true" aria-label="Dynamic audience preview">
    <div class="lists-title"><div><span class="list-type dynamic">Dynamic audience</span><h3>${esc(p.audience.name)}</h3><p>${esc(p.audience.description || "Updates when source data or saved rules change.")}</p></div><button type="button" class="ask-btn ghost" data-lists="close">Close</button></div>
    <div class="audience-counts"><span><b>${p.matches.toLocaleString()}</b> matches</span><span><b>${p.callable.toLocaleString()}</b> callable</span><span><b>${p.emailable.toLocaleString()}</b> emailable</span><span class="excluded"><b>${p.excluded.toLocaleString()}</b> no usable route</span></div>
    <p class="audience-owner"><b>${policy.text}</b><br>Owner distribution: ${owners.size ? [...owners].map(([n,c]) => `${esc(n)} (${c})`).join("; ") : "assignment information unavailable"}. Raw channel counts above describe the full audience; snapshot actions below use ${policy.kind === "territory" ? "only this account's assigned territory" : policy.kind === "national" ? "national coverage" : "all matches after an explicit ownership review"}.</p>
    <p class="dial-menu-note">Ready for a new snapshot: ${readyCall.toLocaleString()} callable; ${readyEmail.toLocaleString()} emailable${policy.outside ? `; ${policy.outside.toLocaleString()} outside-territory matches excluded before channel checks` : ""}.</p>
    ${p.excluded ? `<p class="dial-menu-note">Review: ${p.dnc.toLocaleString()} do-not-call; ${p.identity.toLocaleString()} with non-actionable identity evidence; ${p.excluded.toLocaleString()} with neither a callable nor emailable route. Excluded contacts remain in the match count and never enter the call snapshot.</p>` : ""}
    <div class="lists-preview-actions"><button type="button" class="ask-btn" data-lists="audience-back">Back to lists</button><button type="button" class="ask-btn" data-lists="audience-queue"${readyCall ? "" : " disabled"}>Create call list</button><button type="button" class="ask-btn primary" data-lists="audience-email"${readyEmail ? "" : " disabled"}>Prepare email batch</button></div>
  </div>`;
  focusListDialog('[data-lists="audience-back"]');
}

function listRowActions(l){
  const cur = l.id === Dial.state.listId;
  return `<li class="lists-row${cur ? " on" : ""}">
    <span class="lists-main">
      <b>${esc(l.name)}</b>
      <small>Static &middot; ${l.count} ${l.count === 1 ? "person" : "people"}`
      + `${l.cycle > 1 ? ` &middot; pass ${l.cycle}` : ""}`
      + `${cur ? " &middot; open" : ""}</small>
    </span>
    <span class="lists-acts">
      <button type="button" class="ask-btn primary" data-lists="call" data-id="${esc(l.id)}"
        ${l.count ? "" : "disabled"}>Call</button>
      <button type="button" data-lists="email" data-id="${esc(l.id)}"
        ${l.count ? "" : "disabled"}>Email</button>
      <button type="button" data-lists="edit-open" data-id="${esc(l.id)}"
        ${l.count ? "" : "disabled"}>Edit</button>
      <button type="button" data-lists="rename" data-id="${esc(l.id)}">Rename</button>
      <button type="button" data-lists="empty" data-id="${esc(l.id)}"
        ${l.count ? "" : "disabled"}>Empty</button>
      <button type="button" class="grave" data-lists="drop" data-id="${esc(l.id)}">Delete</button>
    </span>
  </li>`;
}

let listsBack = null;
let listsEditMode = false;   // showing who is on the open list, not every list
let listsReturnFocus = null;

function focusListDialog(selector){
  if (!listsBack) return;
  const target = listsBack.querySelector(selector ||
    '[data-lists="audience-open"], [data-lists="flag-call"]:not([disabled]), .ask-name, [data-lists="close"]');
  if (target) target.focus();
}

async function openListManager(){
  if (listsBack) return;
  listsReturnFocus = document.activeElement;
  listsBack = document.createElement("div");
  listsBack.className = "ask-back";
  document.body.appendChild(listsBack);
  const onKey = (e) => {
    if (e.key === "Escape") { e.preventDefault(); closeListManager(); return; }
    if (e.key !== "Tab" || !listsBack) return;
    const focusable = [...listsBack.querySelectorAll(
      'button:not([disabled]),[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled])')];
    if (!focusable.length) return;
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  };
  document.addEventListener("keydown", onKey);
  listsBack._onKey = onKey;
  listsBack.addEventListener("click", (e) => {
    if (e.target === listsBack) closeListManager();
  });
  paintListManager();
  // Refreshed rather than trusted: the counts in Dial.state.lists are summaries
  // last fetched who-knows-when, and this screen exists to be believed.
  const results = await Promise.allSettled([Dial.loadLists(), loadDynamicAudiences()]);
  audienceProblem = results[1].status === "rejected" ? results[1].reason.message : "";
  paintListManager();
}

async function openAudienceFromLink(id){
  if (!id) return;
  await openListManager();
  try {
    const data = await audienceCall(`${AUDIENCE_API}?id=${encodeURIComponent(id)}`);
    const audience = data.audience || data;
    await applyAudienceDefinition(audience);
    await prepareAudiencePreviewData(audience);
    audiencePreview = currentAudiencePreview(audience);
    paintAudiencePreview();
  } catch (error) { closeListManager(); showNotice(error.message || "That saved audience could not be opened."); }
}

function closeListManager(){
  if (!listsBack) return;
  document.removeEventListener("keydown", listsBack._onKey);
  listsBack.remove();
  listsBack = null;
  const restore = listsReturnFocus;
  listsReturnFocus = null;
  if (restore && restore.isConnected && typeof restore.focus === "function") restore.focus();
}

// Membership editing was missing on both sides: "Empty" was the only answer to
// "this one person shouldn't be here", so a list with one bad entry got
// abandoned rather than corrected.
function paintListEdit(){
  if (!listsBack) return;
  const S = Dial.state, items = S.items || [];
  listsBack.innerHTML =
    `<div class="ask lists" role="dialog" aria-modal="true" aria-label="Edit list">`
    + `<h3>${esc(S.listName || "This list")}</h3>`
    + `<p class="dial-menu-note">${items.length} ${items.length === 1 ? "person" : "people"}. `
    + `Removing someone keeps their call history and does not add them to do-not-call.</p>`
    + (items.length
        ? `<ul class="lists-ul">${items.map((it) =>
            `<li class="lists-row"><span class="lists-main"><b>${esc(it.name || "Unnamed")}</b>`
            + `<small>${esc(it.firm || it.companyName || "")}</small></span>`
            + `<span class="lists-acts"><button type="button" class="ask-btn grave" `
            + `data-lists="edit-drop" data-id="${esc(it.crd)}">Remove</button></span></li>`).join("")}</ul>`
        : `<p>Nobody on this list yet.</p>`)
    + `<p class="dial-menu-note">To add people, use the lasso or bulk add on the map.</p>`
    + `<button type="button" class="ask-btn" data-lists="edit-back">Back to all lists</button>`
    + `<button type="button" class="ask-btn ghost" data-lists="close">Close</button></div>`;
  focusListDialog('[data-lists="edit-drop"], [data-lists="edit-back"]');
}

/* Key contacts and due-diligence contacts, as two standing lists.
 *
 * Only the ones we can place: a flagged advisor whose pins are not loaded has
 * no coordinates to call from, so the count says how many are in view rather
 * than implying the flag set is smaller than it is.
 */
function flaggedAdvisors(kind){
  const out = [];
  for (const [crd, f] of Dial.state.flags) {
    if (!f[kind]) continue;
    // Mine only. dial.js owns the membership test, so the two views and
    // the card control cannot disagree about whose flag this is.
    if (!Dial.flaggedByMe(crd, kind)) continue;
    const c = contactFor(crd);
    out.push({ crd, name: advisorDisplayName(crd, f.name) || `CRD ${crd}`,
               firm: (c && c.cn) || "", callable: !!(c && (c.w || c.c)) });
  }
  return out.sort((a, b) => a.name.localeCompare(b.name));
}

/* Turn a standing flag list into a real, open call list.
 *
 * One function because there are now two ways in -- the Call button in the
 * list manager and the entry in the dialer's picker -- and two code paths
 * building "the same" list differently is how they end up not being the same
 * list. `start` is the only difference: picking from the dropdown switches to
 * a list, it does not begin dialling.
 */
async function openFlagList(kind, { start = false } = {}){
  const label = (ROLE_META[kind] || ROLE_META.key).label;
  const people = flaggedAdvisors(kind).filter(p => p.callable);
  if (!people.length) { showNotice(`Nobody on ${label} has a number on file.`); return false; }
  const existing = (Dial.state.lists || []).find((list) => standingKindOf(list.id) === kind);
  // dialSnapshot builds the same item shape every other queue add uses --
  // name, firm, phone, city, state, email -- so a flag list behaves like any
  // other list once it is open.
  const result = await Dial.replaceList(existing ? existing.id : ROLE_LIST_IDS[kind], label,
    people.map(p => dialSnapshot(p.crd)), { phoneOnly: true, deleteIfEmpty: true });
  if (!result.added) {
    showNotice(`Nobody on ${label} is currently eligible for the calling queue.`);
    renderDialer();
    return false;
  }
  if (start) Dial.start();
  renderDialer();
  return true;
}

function flagListRows(){
  const sets = Object.entries(ROLE_META).map(([kind, meta]) => [kind, meta.symbol, meta.label]);
  return `<ul class="lists-ul">` + sets.map(([kind, icon, label]) => {
    const people = flaggedAdvisors(kind);
    const callable = people.filter(p => p.callable).length;
    return `<li class="lists-row flag-list">
      <span class="lists-main"><b>${icon} ${label}</b>
        <small>Smart view &middot; ${people.length} ${people.length === 1 ? "person" : "people"}`
      + `${people.length && callable !== people.length
            ? ` &middot; ${callable} with a number` : ""}`
      + `${people.length ? "" : " &middot; mark someone on their card"}</small></span>
      <span class="lists-acts">
        <button type="button" class="ask-btn primary" data-lists="flag-call"
          data-kind="${kind}"${callable ? "" : " disabled"}>Call</button>
        <button type="button" data-lists="flag-show" data-kind="${kind}"
          ${people.length ? "" : "disabled"}>Show</button>
      </span></li>`;
  }).join("") + `</ul>`;
}

function paintListManager(){
  if (!listsBack) return;
  if (listsEditMode) return paintListEdit();
  const ls = (Dial.state.lists || []).filter((list) => !standingKindOf(list.id));
  listsBack.innerHTML =
    `<div class="ask lists lists-workspace" role="dialog" aria-modal="true" aria-label="Lists and saved audiences">`
    + `<div class="lists-title"><div><h3>Lists</h3><p>Save reusable rules, then create a frozen call list or prepare an email from reviewed contacts.</p></div><button type="button" class="ask-btn ghost" data-lists="close">Close</button></div>`
    + `<section class="lists-section"><h4>Smart views</h4><p class="lists-section-note">Live views from labels you applied. Membership updates automatically.</p>`
    /* The two standing lists.
     *
     * DERIVED, not stored: they are whoever currently carries the flag, so
     * marking somebody a key contact puts them here and unmarking takes them
     * out, with no list to maintain and nothing to fall out of step. That is
     * also why they cannot be renamed, emptied or deleted -- there is no list
     * object underneath, only the flags.
     */
    + flagListRows() + `</section>`
    + `<section class="lists-section"><h4>Dynamic audiences</h4><p class="lists-section-note">Saved map rules. Preview current matches before preparing a channel-specific list.</p>${dynamicAudienceRows()}</section>`
    + `<section class="lists-section"><h4>Static contact lists</h4><p class="lists-section-note">Frozen working lists used for calls or email, limited to 250 people.</p>`
    + (ls.length
        ? `<ul class="lists-ul">${ls.map(listRowActions).join("")}</ul>`
        : `<p class="lists-none">No static contact lists of your own yet.</p>`)
    + `<div class="lists-new">`
    + `<input class="ask-name" type="text" placeholder="Name for a new list" `
    + `value="${esc(defaultListName())}" aria-label="Name for a new list">`
    + `<button type="button" class="ask-btn" data-lists="new">Create</button></div>`
    + `<p class="dial-menu-note">Your call history is kept whatever you do here.</p></section></div>`;
  focusListDialog();
}

document.addEventListener("click", async e => {
  const b = e.target.closest("[data-lists]");
  if (!b || !listsBack) return;
  const act = b.dataset.lists;
  const id = b.dataset.id;
  const l = (Dial.state.lists || []).find(x => x.id === id);
  try {
    if (act === "close") { listsEditMode = false; return closeListManager(); }
    if (act === "edit-back") { listsEditMode = false; return paintListManager(); }
    if (act === "audience-back") { audiencePreview = null; return paintListManager(); }
    if (act === "audience-open") {
      const data = await audienceCall(`${AUDIENCE_API}?id=${encodeURIComponent(id)}`);
      const audience = data.audience || data;
      await applyAudienceDefinition(audience);
      await prepareAudiencePreviewData(audience);
      audiencePreview = currentAudiencePreview(audience);
      return paintAudiencePreview();
    }
    if (act === "audience-drop") {
      const audience = dynamicAudiences.find(x => x.id === id);
      if (!audience || !confirm(`Delete the dynamic audience "${audience.name}"?\n\nExisting call lists are not affected.`)) return;
      await audienceCall(`${AUDIENCE_API}?id=${encodeURIComponent(id)}`, { method:"DELETE" });
      await loadDynamicAudiences(); return paintListManager();
    }
    if (act === "audience-queue" || act === "audience-email") {
      if (!audiencePreview) return;
      const channel = act === "audience-email" ? "email" : "call";
      const policy = audienceTerritoryPolicy(audiencePreview);
      if (policy.kind === "unassigned" && !confirm("No sales territory is assigned to this account. Review the owner distribution above before continuing.\n\nHave you reviewed ownership and want to prepare this list?")) return;
      const eligible = policy.rows.filter(x => channel === "email" ? x.emailable : x.callable).map(x => x.item);
      if (eligible.length > 250) {
        showNotice(`This audience has ${eligible.length.toLocaleString()} ${channel === "email" ? "emailable" : "callable"} people in the permitted snapshot scope; a static list holds 250. Refine the saved filters so contacts are not selected arbitrarily.`);
        return;
      }
      const name = prompt(`Name this static ${channel === "email" ? "email" : "call"} list`, audiencePreview.audience.name) || "";
      if (!name.trim()) return;
      await Dial.createList(name.trim());
      const result = await Dial.addMany(eligible, { phoneOnly:channel === "call" });
      closeListManager(); renderDialer();
      if (channel === "email") {
        const openEmail = document.querySelector('[data-email="open-list"]');
        if (!openEmail) return showNotice(`Created "${name.trim()}" with ${result.added.toLocaleString()} emailable people, but the email composer could not be opened.`);
        openEmail.click();
      } else showNotice(`Created "${name.trim()}" with ${result.added.toLocaleString()} callable people.`);
      return;
    }
    /* "Edit who's on it" for a list that is NOT open.
     *
     * The list manager could rename, empty and delete any list but could only
     * EDIT the open one -- that action lived solely in the dialer's own menu.
     * So tidying a list you were not working meant opening it first, which
     * moves the rep off the list they were actually calling.
     *
     * Opens it, edits, and the existing edit-back returns here.
     */
    if (act === "edit-open") {
      if (!l) return;
      if (l.id !== Dial.state.listId) await Dial.openList(l.id);
      listsEditMode = true;
      paintListEdit();
      renderDialer();
      return;
    }
    if (act === "edit-drop") {
      // Dial.remove anchors on the person rather than the index, so taking
      // someone out from above the cursor does not skip whoever is next.
      await Dial.remove(id);
      paintListEdit();
      renderDialer();
      return;
    }
    if (act === "new"){
      const name = listsBack.querySelector(".ask-name").value.trim() || defaultListName();
      await Dial.createList(name);
      await Dial.loadLists();
      paintListManager();
      renderDialer();
      return;
    }
    /* ABOVE the `if (!l) return` guard, and it must stay there.
     *
     * A flag row carries data-kind and NO data-id, so `l` is undefined and the
     * guard swallowed the click: Call and Show did nothing at all, silently.
     * The field view has the same handler and the same guard, and the comment
     * there says exactly this -- it was written while fixing it, and this copy
     * was never checked.
     */
    /* "Call" on a flag list builds a queue from the flags as they are RIGHT NOW.
     *
     * Deliberately a snapshot into an ordinary list rather than a live view: a
     * call session that reordered itself because somebody starred a contact in
     * another tab would lose the rep's place. The list is named for the flag, so
     * it is obvious where it came from and safe to delete afterwards.
     */
    if (act === "flag-call" || act === "flag-show"){
      const kind = b.dataset.kind;
      const label = (ROLE_META[kind] || ROLE_META.key).label;
      const everyone = flaggedAdvisors(kind);
      /* SHOW does not need a phone number.
       *
       * The "nobody has a number" check used to run before this branch, so
       * asking who is on the list was refused whenever none of them were
       * callable -- which is the moment a rep most wants to look, because the
       * list is not behaving as expected. Only CALL needs numbers. */
      if (act === "flag-show"){
        closeListManager();
        showNotice(everyone.length
          ? `${label}: ${everyone.map(p => p.name).join(", ")}`
          : `You have not marked anybody as ${label.toLowerCase()} yet.`);
        return;
      }
      if (!everyone.some(p => p.callable)) {
        showNotice(`Nobody on ${label} has a number on file.`); return;
      }
      closeListManager();
      try { await openFlagList(kind, { start: true }); }
      catch (err) { showNotice(err.message || "That list could not be built."); }
      return;
    }
    if (!l) return;
    if (act === "call"){
      // Open it AND start, which is the whole reason a rep comes here. Opening
      // without starting would leave them looking at the same dock they were
      // looking at before, wondering whether the click landed.
      closeListManager();
      await Dial.openList(id);
      Dial.start();
      renderDialer();
      return;
    }
    if (act === "email") {
      try {
        closeListManager();
        if (id !== Dial.state.listId) await Dial.openList(id);
        renderDialer();
        const openEmail = document.querySelector('[data-email="open-list"]');
        if (!openEmail) {
          showNotice(`Opened "${l.name}", but the email review could not be opened.`);
          return;
        }
        openEmail.click();
      } catch (error) {
        showNotice(error.message || `The list "${l.name}" could not be opened for email review.`);
      }
      return;
    }
    if (act === "rename"){
      const name = prompt("Name for this list", l.name);
      if (!name || !name.trim() || name.trim() === l.name) return;
      // renameList writes to whichever list is OPEN, so a rename of some other
      // list has to open it first. Restored afterwards: a rep tidying up their
      // lists has not asked to be moved off the one they were working.
      const was = Dial.state.listId;
      await Dial.openList(id);
      await Dial.renameList(name.trim());
      if (was !== id) await Dial.openList(was);
      await Dial.loadLists();
      paintListManager();
      renderDialer();
      return;
    }
    if (act === "empty"){
      if (!confirm(`Remove all ${l.count} people from "${l.name}"?\n\n`
                   + `The list stays; your call history is kept.`)) return;
      const was = Dial.state.listId;
      await Dial.openList(id);
      await Dial.clear();
      if (was !== id) await Dial.openList(was);
      await Dial.loadLists();
      paintListManager();
      renderDialer();
      return;
    }
    if (act === "drop"){
      if (!confirm(`Delete the list "${l.name}"?\n\nYour call history is kept.`)) return;
      await Dial.deleteList(id);
      await Dial.loadLists();
      paintListManager();
      renderDialer();
      return;
    }
  } catch (err) {
    showNotice(err.message || "That could not be saved.");
  }
});

/* ---- preferences ---------------------------------------------------------
 * Short on purpose. Every row is something a rep sets ONCE and then benefits
 * from silently; this is not a control panel for the application, and anything
 * that would need a paragraph of explanation does not belong in it.
 *
 * Stored on the account rather than in this browser. A "default territory"
 * that is Georgia on the desk and national on the phone is not a default -- it
 * is two settings sharing a name, which is the exact trap the active-list
 * preference already fell into.
 */
let setBack = null;


/* ---- the work queue ------------------------------------------------------
 *
 * "Who should I pick up now, and why." Per rep, unlike the shared timeline:
 * that is a question about one person's day, and a shared queue would put the
 * same advisor at the top of five screens at once.
 *
 * WHAT IS DELIBERATELY ABSENT
 * ---------------------------
 * Any count of what we sent. "117 emails this week" is the number that makes a
 * screen look busy and a rep behave worse -- it rewards sending, which is the
 * behaviour the 25-a-day limit exists to restrain, and a dashboard a rep reads
 * every morning will always beat a limit they meet once a day. Every row here
 * is a relationship that MOVED: somebody answered, somebody went quiet, a
 * follow-up came due, an address broke.
 *
 * The reasons and their order come from the server, so the desk, the phone and
 * any later report cannot disagree about what matters.
 */
const QUEUE_ACTIONS = {
  reply_new: ["mark_reviewed", "snooze"],
  reply_followup: ["follow_up", "done", "snooze"],
  due: ["follow_up", "snooze"],
  bounced: ["dismiss_bounce", "snooze"],
  quiet_warm: ["follow_up", "snooze"],
};

function queueActionKey(value){
  const raw = typeof value === "string" ? value
    : String((value && (value.key || value.action || value.op)) || "");
  return ({ reviewed: "mark_reviewed", queue_snooze: "snooze",
    snooze_30d: "snooze", queue_dismiss_bounce: "dismiss_bounce",
    address_ok: "dismiss_bounce", follow: "follow_up", followup: "follow_up" })[raw] || raw;
}

/* Newer APIs name the actions explicitly. During a staggered static/API
 * deployment the older response has no `actions`, so keep the same safe table
 * here. Server additions are intersected with that table: an unknown action
 * must not turn into a destructive button merely because one client is old. */
function queueActions(entry){
  const allowed = QUEUE_ACTIONS[entry.reason] || [];
  if (!Array.isArray(entry.actions)) return allowed;
  return [...new Set(entry.actions.map(queueActionKey).filter(a => allowed.includes(a)))];
}

function queueActionHtml(action, entry, label){
  const crd = esc(entry.advisorCrd);
  const who = esc(label);
  if (action === "follow_up") return `<button type="button" class="wq-act"
      data-wq-action="follow_up" data-wq-crd="${crd}" data-wq-name="${who}"
      aria-label="Follow up with ${who}">Follow up</button>`;
  if (action === "mark_reviewed") return `<button type="button" class="wq-act"
      data-wq-action="mark_reviewed" data-wq-crd="${crd}"
      aria-label="Mark ${who} reviewed">Mark reviewed</button>`;
  if (action === "done") return `<button type="button" class="wq-act ghost"
      data-wq-action="done" data-wq-crd="${crd}"
      aria-label="Mark work for ${who} done">Done</button>`;
  if (action === "dismiss_bounce") return `<button type="button" class="wq-act ghost"
      data-wq-action="dismiss_bounce" data-wq-crd="${crd}"
      aria-label="Confirm the address for ${who} is fine"
      title="The address is as good as it is going to get">Address is fine</button>`;
  if (action === "snooze") return `<button type="button" class="wq-act ghost"
      data-wq-action="snooze" data-wq-days="30" data-wq-crd="${crd}"
      aria-label="Snooze ${who} for 30 days" title="Put aside for 30 days">Snooze</button>`;
  return "";
}

function queueRowHtml(entry){
  const when = entry.lastReplyAt || entry.lastActivityAt;
  const name = advisorRow(entry.advisorCrd);
  const label = name && name[1] ? name[1] : (entry.advisorEmail || entry.advisorCrd);
  return `<li class="wq-row" data-wq-crd="${esc(entry.advisorCrd)}">
      <button type="button" class="wq-name" data-wq-action="open"
        data-wq-crd="${esc(entry.advisorCrd)}" aria-label="Open ${esc(label)}">${esc(label)}</button>
      ${personActionButton(entry.advisorCrd, label)}
      <span class="wq-why wq-${esc(entry.reason)}">${esc(entry.reasonLabel)}</span>
      <span class="wq-when">${esc(when ? fmtDate(when) : "")}</span>
      <span class="wq-acts">${queueActions(entry)
        .map(action => queueActionHtml(action, entry, label)).join("")}</span></li>`;
}

function queueSyncState(payload, me){
  const reps = payload && Array.isArray(payload.reps) ? payload.reps : [];
  const myId = String((me && me.userId) || "");
  const row = reps.find(r => myId && String(r.userId) === myId)
    || (reps.length === 1 ? reps[0] : null);
  if (!row) return { kind: "not-connected", trusted: false,
    text: "Microsoft 365 is not connected, so mailbox activity may be incomplete." };
  /* Prefer the server's explicit state. The field did not exist on the first
   * deployment, so the checks below remain as a rollout-safe inference for an
   * older API or a row written before the status contract was introduced. */
  const ingestion = String(row.ingestionStatus || "");
  const at = value => value ? fmtDate(value) : "";
  if (ingestion === "reconnect_required") return { kind: "reconnect", trusted: false,
    text: `${row.mailbox || "Microsoft 365"} must be reconnected before new replies can be observed.` };
  if (ingestion === "failed") return { kind: "stale", trusted: false,
    text: row.lastOkUtc
      ? `Mailbox activity may be incomplete; the last successful check was ${at(row.lastOkUtc)}.`
      : "Mailbox activity could not be checked, so this queue may be incomplete." };
  if (ingestion === "stale") return { kind: "stale", trusted: false,
    text: row.lastOkUtc
      ? `Mailbox activity may be incomplete; the last successful check was ${at(row.lastOkUtc)}.`
      : "Mailbox activity is stale, so this queue may be incomplete." };
  if (ingestion === "never_run") return { kind: "starting", trusted: false,
    text: "Mailbox activity tracking is starting; this list is not complete yet." };
  if (ingestion === "catching_up") return { kind: "catching-up", trusted: false,
    text: row.watermarkUtc
      ? `Mailbox activity is catching up and has been processed through ${at(row.watermarkUtc)}.`
      : "Mailbox activity is catching up; this list is not complete yet." };
  if (ingestion === "healthy") return { kind: "current", trusted: true,
    text: row.watermarkUtc
      ? `Mailbox activity is current through ${at(row.watermarkUtc)}.`
      : "Mailbox activity is current." };
  if (row.needsReconnect) return { kind: "reconnect", trusted: false,
    text: `${row.mailbox || "Microsoft 365"} must be reconnected before new replies can be observed.` };
  const watermark = row.watermarkUtc ? new Date(row.watermarkUtc) : null;
  const lastOk = row.lastOkUtc ? new Date(row.lastOkUtc) : null;
  const validWatermark = watermark && !isNaN(watermark);
  const validLastOk = lastOk && !isNaN(lastOk);
  if (!validWatermark || !validLastOk) return { kind: "starting", trusted: false,
    text: "Mailbox activity tracking is starting; this list is not complete yet." };
  if (row.backfill || Number(row.truncatedRuns || 0) > 0 || row.lastError === "more waiting") {
    return { kind: "catching-up", trusted: false,
      text: `Mailbox activity is catching up and has been processed through ${fmtDate(row.watermarkUtc)}.` };
  }
  const stale = Date.now() - lastOk.getTime() > 60 * 60 * 1000
    || Number(row.consecutiveFailures || 0) > 0 || Number(row.behindHours || 0) >= 2;
  if (stale) return { kind: "stale", trusted: false,
    text: `Mailbox activity may be incomplete; the last successful check was ${fmtDate(row.lastOkUtc)}.` };
  return { kind: "current", trusted: true,
    text: `Mailbox activity is current through ${fmtDate(row.watermarkUtc)}.` };
}

function queueSyncHtml(status){
  return `<p class="wq-sync wq-sync-${esc(status.kind)}" role="status">${esc(status.text)}</p>`;
}

function renderWorkQueue(box, data, status){
  const sync = queueSyncHtml(status);
  if (data.error){
    box.innerHTML = sync + `<p class="wq-error" role="status">${esc(data.error)}</p>`;
    return;
  }
  if (!data.count){
    box.innerHTML = sync + `<p class="profile-empty">${status.trusted
      ? "Nothing waiting in the mailbox activity processed so far."
      : "Nothing waiting in the mail processed so far; this is not an all-clear while sync is incomplete."}</p>`;
    return;
  }
  // The headline is what needs doing, not what has been done.
  const heads = (data.reasons || [])
    .filter(r => (data.counts || {})[r.key])
    .map(r => `<span class="wq-count"><b>${data.counts[r.key]}</b> ${esc(r.label)}</span>`)
    .join("");
  box.innerHTML = sync + `<div class="wq-heads">${heads}</div>`
    + `<ul class="wq-list">${data.entries.map(queueRowHtml).join("")}</ul>`;
}

async function openWorkQueue(){
  const opener = document.activeElement;
  const back = document.createElement("div");
  back.className = "ask-back";
  back.innerHTML = `<div class="ask work-queue" role="dialog" aria-modal="true" tabindex="-1"
      aria-labelledby="workQueueTitle"><h3 id="workQueueTitle">Needs attention</h3>
      <div class="wq-body"><p class="profile-empty">Loading…</p></div>
      <div class="profile-actions">
        <button type="button" class="ask-btn ghost" data-wq-close="1">Close</button>
      </div></div>`;
  document.body.appendChild(back);
  const dialog = back.querySelector(".work-queue");
  const close = (restoreFocus = true) => {
    if (!back.isConnected) return;
    back.remove();
    if (restoreFocus && opener && opener.isConnected && opener.focus) opener.focus();
  };
  back.addEventListener("click", e => { if (e.target === back) close(); });
  back.addEventListener("keydown", e => {
    if (e.key === "Escape") { e.preventDefault(); close(); }
  });
  dialog.focus();
  const box = back.querySelector(".wq-body");

  const getJson = async url => {
    const r = await fetch(url, { headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  };
  const load = async focusCrd => {
    dialog.setAttribute("aria-busy", "true");
    const mePromise = ME ? Promise.resolve(ME) : Dial.whoAmI().then(p => (ME = p));
    const [queueResult, statusResult] = await Promise.allSettled([
      getJson("/api/email?op=queue_work"),
      Promise.all([getJson("/api/email?op=sweep_status"), mePromise]),
    ]);
    if (!box.isConnected) return;
    const data = queueResult.status === "fulfilled" ? queueResult.value
      : { error: `The queue could not be loaded — ${queueResult.reason.message || "request failed"}`, count: 0 };
    const status = statusResult.status === "fulfilled"
      ? queueSyncState(statusResult.value[0], statusResult.value[1])
      : { kind: "unavailable", trusted: false,
          text: "Mailbox sync status is unavailable; the queue may be incomplete." };
    renderWorkQueue(box, data, status);
    dialog.removeAttribute("aria-busy");
    if (focusCrd) {
      const row = [...box.querySelectorAll(".wq-row")]
        .find(el => String(el.dataset.wqCrd) === String(focusCrd));
      const target = row && row.querySelector("[data-wq-action]");
      (target || box.querySelector("[data-wq-action]") || dialog).focus();
    }
  };

  back.addEventListener("click", async e => {
    if (e.target.closest("[data-wq-close]")) { close(); return; }
    const act = e.target.closest("[data-wq-action]");
    if (!act) return;
    const action = act.dataset.wqAction;
    if (action === "open"){
      // Same rule the dialer uses: the map holds one scope at a time, and an
      // advisor outside it has no feature to open. Said plainly rather than
      // failing silently, because a queue row that does nothing when tapped
      // reads as a broken queue.
      const crd = act.dataset.wqCrd;
      const c = contactFor(crd) || {};
      close();
      // c.cs is the CRM's state for this contact. The pin's own state is
      // unavailable here by definition -- the pin is not loaded, which is the
      // whole reason we are switching.
      await openAdvisorAnywhere(crd, c.cs || "");
      return;
    }
    if (action === "follow_up"){
      const crd = act.dataset.wqCrd;
      const name = act.dataset.wqName;
      // A modal must never open on top of another modal. The queue is only a
      // launcher here; closing it also prevents a successfully handled row from
      // remaining visibly stale behind the composer.
      close(false);
      openFollowUp(crd, name);
      return;
    }
    const payload = action === "snooze"
      ? { op: "queue_snooze", crd: act.dataset.wqCrd, days: Number(act.dataset.wqDays || 30) }
      : action === "dismiss_bounce"
        ? { op: "queue_dismiss_bounce", crd: act.dataset.wqCrd }
        : action === "mark_reviewed" || action === "done"
          ? { op: "reply_state", crd: act.dataset.wqCrd,
              state: action === "mark_reviewed" ? "reviewed" : "done" }
          : null;
    if (payload){
      const rowButtons = act.closest(".wq-row").querySelectorAll("button");
      rowButtons.forEach(button => { button.disabled = true; });
      try {
        const r = await fetch("/api/email", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload) });
        // Checked, because it was not: a 500 used to reload the list as though
        // the action had worked, and the row simply came back looking unchanged.
        if (!r.ok) {
          const j = await r.json().catch(() => ({}));
          throw new Error(j.error || `HTTP ${r.status}`);
        }
        // Reloaded rather than patched in place: the row may now have a
        // DIFFERENT reason for still being here -- a follow-up that is due, say
        // -- and hiding it would tell the rep they were finished when they are
        // not.
        await load(act.dataset.wqCrd);
      } catch (err) {
        rowButtons.forEach(button => { button.disabled = false; });
        const note = box.querySelector(".wq-error")
          || box.insertAdjacentElement("afterbegin",
               Object.assign(document.createElement("p"), { className: "wq-error" }));
        note.setAttribute("role", "status");
        note.textContent = err.message;
      }
    }
  });
  load();
}

/* ---- advisor email activity --------------------------------------------- */

/* The relationship timeline: what has passed between this firm and this
 * advisor, whoever sent it and wherever they sent it from.
 *
 * Filled in AFTER the panel draws, like the registration history above it. A
 * rep opening a card wants the card; making every profile wait on a round trip
 * to serve a section most of them will not read is the wrong trade.
 *
 * The WORDING comes from the server, not from this file. Whether something
 * counts as a reply is a judgement the desk and the phone must not make
 * differently -- the same reason display_name.py exists.
 */
function activityWhen(value){
  if (!value) return "";
  const d = new Date(value);
  if (isNaN(d)) return String(value);
  return `${fmtDate(value)} · ${d.toLocaleTimeString(undefined,
    { hour: "numeric", minute: "2-digit" })}`;
}

function activityRow(entry, crd){
  const arrow = entry.direction === "outbound" ? "→" : "←";
  // A message this rep did not send cannot be opened by them: it lives in a
  // colleague's mailbox and their delegated token does not reach it. The ROW is
  // shown regardless, because "Kate already emailed them on Tuesday" is exactly
  // what stops two reps working the same advisor in the same week.
  const view = entry.mine && entry.id
    ? `<button type="button" class="activity-view" data-activity-msg="${esc(entry.id)}"
         data-activity-crd="${esc(crd)}">View</button>`
    : `<span class="activity-elsewhere" title="This message is in another rep's mailbox.">—</span>`;
  const flag = entry.ambiguous
    ? ` <span class="activity-warn" title="Several advisors share this address, so this is not attributed to one person.">unattributed</span>`
    : "";
  return `<li class="activity-row activity-${esc(entry.direction)}">
      <span class="activity-when">${esc(activityWhen(entry.occurredAt))}</span>
      <span class="activity-what" title="${esc(entry.basis || "")}">${arrow} ${esc(entry.label)}${flag}</span>
      <span class="activity-subject">${esc(entry.subject || "(no subject)")}</span>
      ${view}</li>`;
}

function renderActivity(slot, data, crd){
  /* One of our own people. Said plainly rather than shown as an empty list.
   *
   * These 18 are EIC's own registered reps, on the map because the SEC feed
   * lists them. The timeline is firm-wide, so tracking them would let every rep
   * read when their colleagues emailed each other -- so their addresses are
   * excluded from the lookup entirely and nothing about their mail is recorded.
   *
   * An unexplained empty section looks like a bug and somebody would eventually
   * try to fix it, which is why this says what it is. */
  if (data.internal){
    slot.innerHTML = `<p class="profile-empty">This is a colleague, not a prospect &mdash;
      their email is not tracked here. Use Outlook.</p>`;
    const act = slot.closest(".profile-section");
    const btn = act && act.querySelector("[data-follow-open]");
    if (btn) btn.closest(".profile-actions").hidden = true;
    return;
  }
  if (!data.entries || !data.entries.length){
    /* NOT "no activity". The sweep sees only connected mailboxes, only since it
     * was switched on, and only addresses we hold for this advisor. Stating
     * "none" would assert something we have not established, and a rep would
     * reasonably act on it. */
    slot.innerHTML = `<p class="profile-empty">No email activity recorded &mdash;
      this is what has been observed, which is not the same as nothing having happened.</p>`;
    return;
  }
  slot.innerHTML = `<ul class="activity-list">`
    + data.entries.map(entry => activityRow(entry, crd)).join("")
    + `</ul>`;
}

function loadActivity(crd){
  const slot = document.getElementById("advisorActivity");
  if (!slot || slot.dataset.for !== String(crd)) return;
  const settle = (html) => {
    // The rep may have opened another card while this was in flight. Drawing
    // one advisor's correspondence into another advisor's panel would be a
    // quiet, plausible, entirely wrong answer.
    const now = document.getElementById("advisorActivity");
    if (!now || now.dataset.for !== String(crd)) return null;
    if (html) now.innerHTML = html;
    return now;
  };
  fetch(`/api/email?op=activity&crd=${encodeURIComponent(crd)}`,
        { headers: { Accept: "application/json" } })
    .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
    .then(data => { const slotNow = settle(null); if (slotNow) renderActivity(slotNow, data, crd); })
    .catch(() => settle(`<p class="profile-empty">Email activity could not be loaded.</p>`));
}

// A confirmation resumed by email.js after a reload still refreshes the
// visible relationship card. The event carries identifiers and status only.
window.addEventListener("directsendstatus", (event) => {
  const detail = (event && event.detail) || {};
  if (detail.state && detail.state.status === "sent" && detail.crd)
    loadActivity(detail.crd);
});

/* Start a NEW conversation with an advisor who has gone quiet.
 *
 * A blank sheet on purpose. This is the message a rep writes because the queue
 * said "warm, no contact in a while", and a templated re-engagement reads
 * exactly like what it is. The signature still comes through -- that is firm
 * identity, not content.
 *
 * The recipient is NOT sent from here. The server takes it from what has
 * already been observed for this advisor, so this composer cannot be used to
 * mail an arbitrary address from the rep's mailbox.
 */
async function openFollowUp(crd, name){
  const existing = window.DirectSendOps && DirectSendOps.pending("follow_up", crd);
  if (existing) {
    alert("A follow-up for this advisor is still being confirmed. Do not resend it; verify in Outlook if confirmation does not finish.");
    return;
  }
  const back = document.createElement("div");
  back.className = "ask-back";
  back.innerHTML = `<div class="ask follow-up" role="dialog" aria-modal="true"
      aria-label="Follow up">
      <h3>Follow up${name ? ` &middot; ${esc(name)}` : ""}</h3>
      <p class="follow-note-top">Starts a new conversation. Sent to the address
        already on this advisor's timeline.</p>
      <input type="text" class="follow-subject" maxlength="200" placeholder="Subject">
      <textarea class="follow-text" rows="7" maxlength="5000"
        placeholder="Your message — plain text. Your signature is added automatically."></textarea>
      ${attachRowHtml()}
      <div class="profile-actions">
        <button type="button" class="ask-btn" data-follow-send="${esc(crd)}">Send</button>
        <button type="button" class="ask-btn ghost" data-follow-close="1">Cancel</button>
      </div>
      <p class="reply-note"></p></div>`;
  document.body.appendChild(back);
  requestAnimationFrame(() => back.querySelector(".follow-subject")?.focus());
  const close = () => back.remove();
  back.addEventListener("click", async e => {
    if (e.target === back || e.target.closest("[data-follow-close]")) { close(); return; }
    const send = e.target.closest("[data-follow-send]");
    if (!send) return;
    const box = back.querySelector(".follow-up");
    const subject = box.querySelector(".follow-subject").value.trim();
    const text = box.querySelector(".follow-text").value.trim();
    const note = box.querySelector(".reply-note");
    if (!subject){ note.textContent = "A subject is needed."; return; }
    if (!text){ note.textContent = "Nothing to send."; return; }
    // Locked for the round trip: a second click sends the advisor a second
    // email, and there is no undo on a sent message.
    send.disabled = true;
    note.textContent = "Sending…";
    try {
      const attached = await readAttachments(box);
      const r = await fetch("/api/email", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ op: "follow_up", crd: send.dataset.followSend,
                               subject, text,
                               operationId: (send.dataset.op ||= crypto.randomUUID()),
                               ...attached }) });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
      const meta = { kind: "follow_up", crd: send.dataset.followSend, sourceId: "" };
      const update = (state) => {
        if (!note.isConnected) return;
        note.textContent = state.message || "Confirming with Outlook…";
        if (state.status === "sent") {
          loadActivity(send.dataset.followSend);
          setTimeout(close, 900);
        } else if (state.status === "failed") {
          send.disabled = false;
          delete send.dataset.op;
        }
      };
      if (window.DirectSendOps) DirectSendOps.accept(j, meta, update);
      else update(j);
    } catch (err) {
      note.textContent = err.message;
      send.disabled = false;
    }
  });
}

/* ---- attaching to a one-to-one message ----------------------------------
 *
 * Two sources, deliberately side by side: the approved library, and a file off
 * the rep's own machine. Whichever they use, the server blind-copies compliance
 * on any attachment to an advisor -- the same rule and the same code path the
 * bulk sender uses. That is what makes the second source acceptable without a
 * second approval workflow.
 */
function approvedDocs(){
  return (window.EmailComposer && EmailComposer.documents && EmailComposer.documents()) || [];
}

function attachRowHtml(){
  const docs = approvedDocs();
  return `<div class="attach-row">
      ${docs.length ? `<label class="attach-lab">Approved material
        <select class="attach-doc" multiple size="${Math.min(docs.length, 4)}">
          ${docs.map(d => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join("")}
        </select></label>` : ""}
      <label class="attach-lab">Or a file
        <input type="file" class="attach-file" multiple></label>
      <p class="attach-note">Anything attached is blind-copied to compliance,
        exactly as a campaign is.</p>
    </div>`;
}

/* Read the picked files as base64.
 *
 * Nothing is stored server-side: the bytes go onto the Outlook draft and are
 * gone. Read here rather than streamed because a one-to-one attachment is a
 * presentation or a fact sheet, not a video.
 */
function readAttachments(box){
  const select = box.querySelector(".attach-doc");
  const documentIds = select ? [...select.selectedOptions].map(o => o.value) : [];
  const input = box.querySelector(".attach-file");
  const files = input ? [...input.files] : [];
  return Promise.all(files.map(file => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(`${file.name} could not be read.`));
    reader.onload = () => resolve({ name: file.name, contentType: file.type,
      // strip the "data:...;base64," prefix the reader prepends
      data: String(reader.result).split(",")[1] || "" });
    reader.readAsDataURL(file);
  }))).then(read => ({ documentIds, files: read }));
}

/* One message, read on demand.
 *
 * The timeline holds metadata only; the text stays in Exchange until somebody
 * asks for it. Graph returns it as PLAIN TEXT and it is escaped again on the
 * way into the DOM -- this is mail written by people outside the firm being
 * rendered inside our own page, and one layer of that is not enough.
 */
async function showActivityMessage(crd, id){
  const back = document.createElement("div");
  back.className = "ask-back";
  back.innerHTML = `<div class="ask activity-msg" role="dialog" aria-modal="true" aria-label="Email">
      <p>Loading…</p></div>`;
  document.body.appendChild(back);
  const close = () => back.remove();
  back.addEventListener("click", e => { if (e.target === back) close(); });
  const box = back.querySelector(".activity-msg");
  try {
    const r = await fetch(`/api/email?op=activity_message&crd=${encodeURIComponent(crd)}`
                          + `&id=${encodeURIComponent(id)}`,
                          { headers: { Accept: "application/json" } });
    const j = await r.json();
    if (!r.ok){
      box.innerHTML = `<h3>Email</h3><p>${esc(j.error || `HTTP ${r.status}`)}</p>`
        + `<button type="button" class="ask-btn" data-activity-close="1">Close</button>`;
    } else {
      const when = j.receivedAt || j.sentAt;
      box.innerHTML = `<h3>${esc(j.subject || "(no subject)")}</h3>`
        + `<p class="activity-meta">${esc(j.fromName || j.from)}`
        + (j.fromName ? ` &lt;${esc(j.from)}&gt;` : "")
        + (when ? ` &middot; ${esc(activityWhen(when))}` : "") + `</p>`
        + `<pre class="activity-body">${esc(j.text || "(no text in this message)")}</pre>`
        /* Reply, right here.
         *
         * Only on INBOUND mail: replying to our own sent message would mail
         * ourselves. The composer is deliberately plain -- no formatting, no
         * attachments -- and `Open in Outlook` is one click away for anything
         * that needs more than a few sentences.
         */
        + (j.from && !j.isOwn ? `
          <details class="reply-box">
            <summary>Reply</summary>
            <p class="reply-to">To ${esc(j.fromName || j.from)}</p>
            <textarea class="reply-text" rows="5" maxlength="5000"
              placeholder="Your reply — plain text. Use Outlook for anything longer."></textarea>
            ${attachRowHtml()}
            <div class="profile-actions">
              <button type="button" class="ask-btn" data-reply-send="${esc(id)}"
                data-reply-crd="${esc(crd)}">Send reply</button>
              <label class="reply-all"><input type="checkbox" data-reply-all
                data-audience-crd="${esc(crd)}" data-audience-id="${esc(id)}">
                Reply to all</label>
            </div>
            <p class="reply-audience"></p>
            <p class="reply-note"></p>
          </details>` : "")
        + `<div class="profile-actions">`
        // Outlook itself, for anything beyond reading: forward, an attachment,
        // the rest of the thread. Microsoft does not permit this link in an
        // iframe, so it opens Outlook rather than embedding it.
        + (j.webLink
            ? `<a href="${esc(j.webLink)}" target="_blank" rel="noopener">Open in Outlook ↗</a>` : "")
        + `<button type="button" class="ask-btn ghost" data-activity-close="1">Close</button></div>`;
    }
  } catch (e) {
    box.innerHTML = `<h3>Email</h3><p>${esc(e.message)}</p>`
      + `<button type="button" class="ask-btn" data-activity-close="1">Close</button>`;
  }
  /* WHO REPLY ALL ACTUALLY REACHES.
   *
   * Reply-all pulls in the original's To and Cc, and those are not necessarily
   * advisors -- an assistant, the advisor's own client, somebody internal to
   * their firm. In Outlook a rep would see that list; here they would not.
   *
   * So it is resolved and shown, with any suppressed address marked. Disclosure
   * rather than refusal: blocking would stop a legitimate answer on a thread
   * somebody is already part of, which is exactly what we decided replies are
   * allowed to do.
   */
  box.addEventListener("change", async e => {
    const toggle = e.target.closest("[data-reply-all]");
    if (!toggle) return;
    const slot = box.querySelector(".reply-audience");
    if (!toggle.checked) { slot.textContent = ""; return; }
    slot.textContent = "Checking who that reaches…";
    try {
      const r = await fetch(`/api/email?op=reply_audience`
        + `&crd=${encodeURIComponent(toggle.dataset.audienceCrd)}`
        + `&id=${encodeURIComponent(toggle.dataset.audienceId)}`,
        { headers: { Accept: "application/json" } });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
      slot.innerHTML = `Goes to ${j.replyAll.length}: `
        + j.replyAll.map(p => `<span class="reply-who${p.suppressed ? " off" : ""}"${
            p.suppressed ? ' title="On the suppression list — replying is still allowed"' : ""
          }>${esc(p.address)}${p.suppressed ? " (unsubscribed)" : ""}</span>`).join(", ");
    } catch (err) {
      slot.textContent = `Could not check the recipients — ${err.message}`;
    }
  });

  box.addEventListener("click", async e => {
    if (e.target.closest("[data-activity-close]")) { close(); return; }
    const send = e.target.closest("[data-reply-send]");
    if (!send) return;
    const area = box.querySelector(".reply-text");
    const note = box.querySelector(".reply-note");
    const all = box.querySelector("[data-reply-all]");
    const existing = window.DirectSendOps
      && DirectSendOps.pending("reply", send.dataset.replyCrd, send.dataset.replySend);
    if (existing && !send.dataset.op) {
      send.disabled = true; area.disabled = true;
      note.textContent = "A reply to this message is still being confirmed. Do not resend it; verify in Outlook if needed.";
      return;
    }
    const text = (area.value || "").trim();
    if (!text){ note.textContent = "Nothing to send."; return; }
    // Disabled for the whole round trip. Without this a second tap while the
    // first is in flight sends the advisor the same reply twice, and there is
    // no undo on a sent email.
    send.disabled = true; area.disabled = true;
    note.textContent = "Sending…";
    try {
      const attached = await readAttachments(box);
      const r = await fetch("/api/email", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ op: "reply_send", crd: send.dataset.replyCrd,
                               id: send.dataset.replySend, text,
                               replyAll: !!(all && all.checked),
                               // Generated ONCE per composer, so a retry after a
                               // lost response carries the same id and the server
                               // recognises the send rather than repeating it.
                               operationId: (send.dataset.op ||= crypto.randomUUID()),
                               ...attached }) });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
      const meta = { kind: "reply", crd: send.dataset.replyCrd,
        sourceId: send.dataset.replySend };
      const update = (state) => {
        if (!note.isConnected) return;
        note.textContent = state.message || "Confirming with Outlook…";
        if (state.status === "sent") {
          area.value = "";
          // The timeline behind the dialog is now out of date by exactly one row.
          loadActivity(send.dataset.replyCrd);
        } else if (state.status === "failed") {
          send.disabled = false; area.disabled = false;
          delete send.dataset.op;
        }
      };
      if (window.DirectSendOps) DirectSendOps.accept(j, meta, update);
      else update(j);
    } catch (err) {
      note.textContent = err.message;
      send.disabled = false; area.disabled = false;
    }
  });
}


/* Show what Act! holds for this advisor.
 *
 * Added while assigning CRDs to CRM contacts, where the recurring question is
 * "is this the same person" and the answer lives in a system nobody wants to
 * switch to for every row. Read-only, admin-only, and the record is shown
 * verbatim rather than summarised -- a summary would decide for the reader which
 * fields matter, and the whole point is that they do not know yet.
 */
async function showActJson(crd){
  const back = document.createElement("div");
  back.className = "ask-back";
  back.innerHTML = `<div class="ask act-json" role="dialog" aria-modal="true" aria-label="CRM record">
      <h3>Act! record &middot; CRD ${esc(crd)}</h3>
      <p>Loading…</p></div>`;
  document.body.appendChild(back);
  const close = () => back.remove();
  back.addEventListener("click", (e) => { if (e.target === back) close(); });
  const box = back.querySelector(".act-json");
  try {
    const r = await fetch(`/api/email?op=act_contact&crd=${encodeURIComponent(crd)}`,
                          { headers: { Accept: "application/json" } });
    const j = await r.json();
    if (!r.ok || !j.ok) {
      const why = { no_contact_for_crd: "This advisor is not linked to an Act! contact.",
                    act_not_configured: "Act! is not configured for this environment.",
                    act_read_failed: "Act! could not be read: " + (j.detail || "") }[j.reason]
                  || j.error || j.reason || "Unavailable.";
      box.innerHTML = `<h3>Act! record &middot; CRD ${esc(crd)}</h3>`
        + `<p>${esc(why)}</p>`
        + `<button type="button" class="ask-btn" data-act-close="1">Close</button>`;
      return;
    }
    const text = JSON.stringify(j.contact, null, 2);
    box.innerHTML = `<h3>Act! record &middot; CRD ${esc(crd)}</h3>`
      + `<p>Act! contact id <code>${esc(j.contactId)}</code></p>`
      + `<pre class="act-json-body">${esc(text)}</pre>`
      + `<div class="profile-actions">`
      + `<button type="button" class="ask-btn" data-act-copy="1">Copy JSON</button>`
      + `<button type="button" class="ask-btn ghost" data-act-close="1">Close</button></div>`;
    box.querySelector("[data-act-copy]").addEventListener("click", () => {
      navigator.clipboard.writeText(text).then(
        () => { box.querySelector("[data-act-copy]").textContent = "Copied"; },
        () => { box.querySelector("[data-act-copy]").textContent = "Copy failed"; });
    });
  } catch (e) {
    box.innerHTML = `<h3>Act! record</h3><p>${esc(e.message)}</p>`
      + `<button type="button" class="ask-btn" data-act-close="1">Close</button>`;
  }
  box.addEventListener("click", (e) => { if (e.target.closest("[data-act-close]")) close(); });
}

/* The approved internal recipients, as the server reports them.
 *
 * Never a list this client invents: the picker shows exactly what the Function
 * App's EMAIL_INTERNAL_RECIPIENTS setting allows, so an address removed there
 * disappears here on the next load and stops being copied on the next send.
 */
function catalogInternal(){
  return (window.EmailComposer && EmailComposer.internalRecipients()) || [];
}

function openSettings(){
  if (setBack) return;
  setBack = document.createElement("div");
  setBack.className = "ask-back";
  document.body.appendChild(setBack);
  const onKey = (e) => { if (e.key === "Escape") closeSettings(); };
  document.addEventListener("keydown", onKey);
  setBack._onKey = onKey;
  setBack.addEventListener("click", (e) => { if (e.target === setBack) closeSettings(); });
  paintSettings();
  if (!ME) Dial.whoAmI().then((p) => { ME = p; paintSettings(); });
  if (!ADMIN && global.EmailComposer && EmailComposer.isAdmin)
    EmailComposer.isAdmin().then((yes) => { if (yes) { ADMIN = true; paintSettings(); } });
}

function closeSettings(){
  if (!setBack) return;
  document.removeEventListener("keydown", setBack._onKey);
  setBack.remove();
  setBack = null;
}

function paintSettings(){
  if (!setBack) return;
  const g = (k, d) => Dial.setting(k, d);
  const scopeSel = document.getElementById("scope");
  const opts = scopeSel ? [...scopeSel.options] : [];
  setBack.innerHTML =
    `<div class="ask lists ask-scroll" role="dialog" aria-modal="true" aria-label="Settings">`
    + `<h3>Settings</h3>`
    + `<p>Saved to your account, so the desk and the phone agree.</p>`

    + `<label class="set-row"><span>Area this map opens on</span>`
    + `<select id="setScope"><option value="">Wherever I was last</option>`
    + opts.map(o => `<option value="${esc(o.value)}"`
        + `${g("defaultScope") === o.value ? " selected" : ""}>${esc(o.textContent)}</option>`).join("")
    + `</select></label>`

    + `<label class="set-row"><span>Call list that opens first</span>`
    + `<select id="setList"><option value="">Wherever I left off</option>`
    + (Dial.state.lists || []).map(l => `<option value="${esc(l.id)}"`
        + `${g("defaultListId") === l.id ? " selected" : ""}>${esc(l.name)}</option>`).join("")
    + `</select></label>`

    /* Copy me / copy a colleague.
     *
     * OFF by default, and deliberately two separate choices rather than one
     * "copy" toggle: cc is visible to the advisor and bcc is not, and which one
     * a rep wants is the whole question. A single switch would have to pick for
     * them.
     *
     * The colleague list comes from the Function App's EMAIL_INTERNAL_RECIPIENTS
     * setting, and the server re-checks it on every send -- what is saved here
     * is a preference, never a permission.
     */
    + `<div class="set-row set-block"><span>Copy me on outgoing email</span>`
    + `<p class="set-sub">Applies to every message you send from the app. `
    + `A cc is visible to the advisor; a bcc is not.</p>`
    + `<p class="set-actions">`
    + ["", "cc", "bcc"].map(v => `<label class="set-radio"><input type="radio" name="setCopySelf"`
        + ` value="${v}"${(g("copySelf") || "") === v ? " checked" : ""}> `
        + `${v === "" ? "Off" : v.toUpperCase()}</label>`).join("")
    + `</p></div>`

    + `<div class="set-row set-block"><span>Copy a colleague</span>`
    + ((catalogInternal() || []).length
      ? `<p class="set-sub">Chosen from the approved internal list.</p>`
        + `<p class="set-actions">`
        + ["", "cc", "bcc"].map(v => `<label class="set-radio"><input type="radio" name="setCopyInternal"`
            + ` value="${v}"${(g("copyInternal") || "") === v ? " checked" : ""}> `
            + `${v === "" ? "Off" : v.toUpperCase()}</label>`).join("")
        + `</p>`
        + `<select id="setCopyInternalTo"><option value="">Choose a colleague…</option>`
        + catalogInternal().map(r => `<option value="${esc(r.address)}"`
            + `${g("copyInternalTo") === r.address ? " selected" : ""}>${esc(r.name)}</option>`).join("")
        + `</select>`
      : `<p class="set-sub">No internal recipients are configured. An administrator sets `
        + `<code>EMAIL_INTERNAL_RECIPIENTS</code> on the Function App.</p>`)
    + `</div>`

    + `<div class="set-row set-block"><span>Email signature</span>`
    + `<p class="set-sub">Generated centrally from your Microsoft 365 profile and the approved corporate disclosure. The exact signature appears in every email preview.</p></div>`

    + `<div class="set-row set-block" id="setAdmin"${ADMIN ? "" : " hidden"}>`
    + `<span>Email administration</span>`
    + `<p class="set-sub">Approved templates and the PDFs reps may attach.</p>`
    + `<p class="set-actions">`
    + `<button type="button" class="set-btn" data-set="templates">Manage templates</button>`
    + `<button type="button" class="set-btn" data-set="docs">Manage approved documents</button>`
    + `<button type="button" class="set-btn" data-set="health">Sender health</button>`
    + `</p></div>`

    + `<div class="set-row set-block"><span>Signed in as</span>`
    + `<p class="set-sub">${esc((ME && ME.userDetails) || "not signed in")}</p>`
    + `<button type="button" class="set-signout" data-set="signout">Log out</button></div>`

    + `<p class="set-saved" id="setSaved"></p>`
    + `<button type="button" class="ask-btn ghost" data-set="close">Close</button></div>`;
}

async function saveSetting(patch){
  const el = document.getElementById("setSaved");
  if (el) { el.textContent = "Saving…"; el.className = "set-saved"; }
  try {
    await Dial.saveSettings(patch);
    if (el) el.textContent = "Saved.";
  } catch (err) {
    // Said out loud. A preference that silently failed to save is one the rep
    // sets again next week and blames themselves for.
    if (el) { el.textContent = err.message || "That could not be saved.";
              el.className = "set-saved bad"; }
  }
}

document.addEventListener("click", async e => {
  const flag = e.target.closest("[data-flag]");
  if (flag) {
    const id = flag.dataset.advisor, kind = flag.dataset.flag;
    const on = flag.getAttribute("aria-pressed") !== "true";
    const c = contactFor(id) || {};
    flag.disabled = true;
    try {
      await Dial.setFlag(id, kind, on, advisorDisplayName(id), c.fc || "");
      const dropped = await dropFromStandingList(id, kind, Dial.flaggedByMe(id, kind));
      // Redraw the card in place so both marks reflect the new state, and the
      // map so a star appears on the pin without a reload.
      const modalMarks = flag.closest(".person-action-roles .contact-flags");
      if (modalMarks) modalMarks.outerHTML = flagMarks(id);
      else if (detailsCurrent) renderDetailEntry(detailsCurrent, false);
      redraw();
      if (dropped) renderDialer();
    } catch (err) {
      showNotice(err.message || "That could not be saved.");
    } finally { flag.disabled = false; }
    return;
  }
  if (e.target.closest("#workQueueBtn")) { openWorkQueue(); return; }
  if (e.target.closest("#settingsBtn")) { openSettings(); return; }
  // Confirmed, because this signs out of Microsoft in this browser, not just the
  // app -- a mis-click costs a sign-in everywhere, and that is worth one question.
  const adm = e.target.closest('[data-set="templates"], [data-set="docs"], [data-set="health"]');
  // Back from an admin list returns to Settings, the panel it was opened from,
  // rather than closing outright -- otherwise Back and Close do the same thing.
  if (adm) { const which = adm.dataset.set; closeSettings();
             EmailComposer.openAdmin(which, openSettings); return; }
  if (e.target.closest('[data-set="signout"]')) {
    if (confirm("Log out of the advisor map and of Microsoft in this browser?")) Dial.signOut();
    return;
  }
  if (e.target.closest('[data-set="close"]')) { closeSettings(); return; }
});

document.addEventListener("change", e => {
  if (!setBack || !e.target) return;
  if (e.target.id === "setScope") saveSetting({ defaultScope: e.target.value });
  if (e.target.id === "setList") saveSetting({ defaultListId: e.target.value });
  if (e.target.name === "setCopySelf") saveSetting({ copySelf: e.target.value });
  if (e.target.name === "setCopyInternal") saveSetting({ copyInternal: e.target.value });
  if (e.target.id === "setCopyInternalTo") saveSetting({ copyInternalTo: e.target.value });
});

function defaultListName(){
  return `List ${new Date().toLocaleDateString(undefined, { month: "short", day: "numeric" })}`;
}

// Says what it did, including what it did NOT do. A bulk action that quietly
// adds fewer people than its label promised is the failure this whole project
// keeps running into.
async function bulkQueue(ids, what){
  if (!ids.length){ showNotice(`No advisors ${what} to add.`); return; }
  if (Dial.state.items.length){
    const where = await askDestination(ids.length, Dial.state.listName,
                                       Dial.state.items.length);
    if (!where) return;
    if (where.mode === "new"){
      await Dial.createList(where.name);
      renderDialer();
    }
  }
  // A rejected save has to be SAID. An async click handler that throws rejects
  // silently -- the same failure that made the ⋮ menu do nothing at all -- and
  // the newest way to reject is a 409 from another device holding a newer copy
  // of this list, which is precisely the case a rep must not have to guess at.
  let res;
  try {
    res = await Dial.addMany(ids.map(dialSnapshot), { phoneOnly: true });
  } catch (err) {
    showNotice(err.message || "The call list could not be saved.");
    renderDialer();
    return;
  }
  const skipped = [
    res.noPhone ? `${res.noPhone.toLocaleString()} with no number` : "",
    res.dupe ? `${res.dupe.toLocaleString()} already queued` : "",
    res.blocked ? `${res.blocked.toLocaleString()} on do-not-call` : "",
  ].filter(Boolean).join(", ");
  showNotice(
    (res.added
      ? `Added ${res.added.toLocaleString()} to “${Dial.state.listName}”.`
      : `Nothing added.`)
    + (skipped ? ` Skipped ${skipped}.` : "")
    + (res.overflow
        ? ` ${res.overflow.toLocaleString()} did not fit — the list holds ${res.max}. Narrow the filters and try again.`
        : ""));
  renderDialer();
}

document.addEventListener("click", async e => {
  if (e.target.closest("#queueVisible") || e.target.closest("#queueVisibleMobile")){
    await bulkQueue(visibleAdvisorIds(), "in view");
    return;
  }
  // Same list the drawer is showing, which is only correct because that list
  // now honours the advisor filters.
  if (e.target.closest("#queueFirmList")){
    await bulkQueue(FIRM_ADVISOR_ROWS.map(f => String(f.properties.id)), "for this firm in view");
    return;
  }
  const rq2 = e.target.closest("[data-queue-roster]");
  if (rq2){
    const rec = rq2.dataset.queueRoster === "bldg"
      ? BLDG_BY_I[+rq2.dataset.queueIdx] : ADDR_BY_I[+rq2.dataset.queueIdx];
    if (rec) await bulkQueue([...rec.ids].map(String), "at this location");
    return;
  }

  // Quick-add from a list row. Repaints only that button, so a rep working
  // down a list of 40 does not have it re-render and lose their place.
  const rq = e.target.closest("[data-row-queue]");
  if (rq) {
    const id = rq.dataset.rowQueue;
    if (Dial.inQueue(id)) await Dial.remove(id);
    else {
      const r = await Dial.add(dialSnapshot(id));
      if (r.blocked) showNotice("That advisor is on the firm-wide do-not-call list.");
    }
    const on = Dial.inQueue(id);
    rq.classList.toggle("on", on);
    rq.innerHTML = on ? "&#10003;" : "&#43;";
    rq.title = on ? "Remove from the call list" : "Add to the call list";
    renderDialer();
    return;
  }

  const q = e.target.closest("[data-queue-toggle]");
  if (q) {
    const id = q.dataset.queueToggle;
    if (Dial.inQueue(id)) await Dial.remove(id);
    else {
      const r = await Dial.add(dialSnapshot(id));
      if (r.blocked) showNotice("That advisor is on the firm-wide do-not-call list.");
    }
    // Redraw the open card so the button reflects the new state. Only when
    // THIS advisor is the one on screen -- re-opening details for someone the
    // rep is not looking at would yank the panel out from under them.
    const f = ALL.find(x => String(x.properties.id) === String(id));
    if (f && detailsCurrent && detailsCurrent.type === "advisor"
        && String(detailsCurrent.feature?.properties?.id) === String(id))
      openAdvisorDetails(f, false);
    renderDialer();
    return;
  }

  const d = e.target.closest("[data-dial]");
  if (!d) return;
  const act = d.dataset.dial;
  const crd = d.dataset.crd;

  if (act === "toggle-queue") {
    const w = document.getElementById("dialerQueue");
    w.hidden = !w.hidden;
    // One disclosure at a time. The dock is 340px of a bottom-anchored column,
    // and stacking the menu on top of the queue is what pushed its own header
    // off the screen. The height cap in style.css makes that survivable; this
    // keeps it from happening in the first place, and two panels open at once
    // in a dock this size was cluttered regardless.
    if (!w.hidden) dialMenuOpen = false;
    renderDialer();
  } else if (act === "start") { Dial.start(); renderDialer(); }
  else if (act === "pause") { Dial.pause(); renderDialer(); }
  else if (act === "cycle") {
    // The list is untouched; only the clock moves, so everyone becomes pending
    // again without deleting the history that says they were called last month.
    await Dial.startCycle(); Dial.start(); renderDialer();
  } else if (act === "menu") {
    dialMenuOpen = !dialMenuOpen;
    // The same rule in the other direction.
    if (dialMenuOpen) document.getElementById("dialerQueue").hidden = true;
    renderDialer();
  } else if (act === "lists") {
    dialMenuOpen = false;
    listsEditMode = false;
    renderDialer();
    await openListManager();
  } else if (act === "edit") {
    dialMenuOpen = false;
    listsEditMode = true;
    renderDialer();
    await openListManager();
  } else if (act === "rename") {
    // Dial.state, not S. `S` is a local of renderDialer(), and reading it from
    // here threw a ReferenceError inside an async handler -- which rejects
    // silently, so the button simply did nothing at all.
    const cur = Dial.state.listName;
    const name = prompt("Name for this list", cur);
    dialMenuOpen = false;
    if (name && name.trim() && name.trim() !== cur) await Dial.renameList(name.trim());
    renderDialer();
  } else if (act === "empty") {
    const st = Dial.state;
    dialMenuOpen = false;
    if (confirm(`Remove all ${st.items.length} people from "${st.listName}"?\n\n`
                + `The list stays; your call history is kept.`)) await Dial.clear();
    renderDialer();
  } else if (act === "drop") {
    const st = Dial.state;
    dialMenuOpen = false;
    if (confirm(`Delete the list "${st.listName}"?\n\nYour call history is kept.`))
      await Dial.deleteList(st.listId);
    renderDialer();
  } else if (act === "up") { await Dial.move(crd, -1); renderDialer(); }
  else if (act === "down") { await Dial.move(crd, 1); renderDialer(); }
  else if (act === "remove") { await Dial.remove(crd); renderDialer(); }
  else if (act === "outcome") { await recordDialOutcome(d.dataset.outcome); }
  else if (act === "dialled" || act === "mailed") {
    const cur = Dial.current();
    Dial.log({ ...(cur || { crd }), kind: act === "dialled" ? "call" : "email",
               disposition: "",
               purpose: act === "mailed" ? dialPurpose : "" }).catch(() => {});
  } else if (act === "back") {
    // Returns to the person so a mis-tapped outcome can be corrected. It does
    // not un-log: the correction appends and the original stays in the record.
    Dial.cancelAuto();
    await Dial.back();
    dialHistory = { crd: "", text: "" };
    renderDialer();
  } else if (act === "auto-cancel") { Dial.cancelAuto(); }
  else if (act === "open") {
    // The queue entry carries the state, which is all the switch needs.
    const q = (Dial.state.items || []).find(i => String(i.crd) === String(crd));
    const c = contactFor(crd) || {};
    await openAdvisorAnywhere(crd, (q && q.state) || c.cs || "");
  }
});

// Auto-dial settings. `change` rather than `click`, so the checkbox and the
// number field are read after the browser has applied the new value.
document.addEventListener("change", e => {
  const el = e.target.closest("[data-dial]");
  if (!el) return;
  const act = el.dataset.dial;
  if (act === "pick") {
    if (el.value === "__new") {
      const name = prompt("Name for the new list", "");
      // Re-render on cancel, or the <select> is left showing "+ New list…".
      if (!name) { renderDialer(); return; }
      Dial.createList(name.trim()).then(renderDialer);
    } else if (["__key", "__dd", "__scheduler"].includes(el.value)) {
      // Switches to the list without dialling, exactly like picking any other.
      openFlagList(el.value.slice(2))
        .catch(err => { showNotice(err.message || "That list could not be built."); renderDialer(); });
    } else if (standingKindOf(el.value)) {
      /* The REAL "Key contacts" list, picked after it has been built once.
       *
       * Rebuilt from the flags rather than merely opened, so it is a live
       * projection of the star and not a snapshot that quietly goes stale
       * every time somebody marks or unmarks a contact. This is the whole
       * reason it can carry the same name in two places without them
       * disagreeing. */
      openFlagList(standingKindOf(el.value))
        .catch(err => { showNotice(err.message || "That list could not be rebuilt."); renderDialer(); });
    } else {
      Dial.openList(el.value).then(renderDialer);
    }
    return;
  }
  if (act === "auto-on") {
    Dial.setAuto({ on: el.checked });
    // Speaking once on the enabling click primes the speech engine, which on
    // some browsers refuses to start outside a user gesture.
    if (el.checked && Dial.state.auto.announce) Dial.say("Auto dial on");
    renderDialer();
  } else if (act === "auto-delay") {
    Dial.setAuto({ delay: Number(el.value) });
  } else if (act === "auto-announce") {
    Dial.setAuto({ announce: el.checked });
    renderDialer();
  }
});

// Notes survive a repaint. sessionStorage rather than the server: an unsaved
// note is a draft, and drafts belong to the tab that is typing them.
document.addEventListener("input", e => {
  if (e.target.id !== "dialNote") return;
  const cur = Dial.current();
  if (cur) sessionStorage.setItem(DIAL_NOTE_KEY + ":" + cur.crd, e.target.value);
});

function loadGeo(){
  supportStart("geo");
  return fetch(dataUrl("geo_index.json"))
    .then(r => { if (!r.ok) throw new Error(`location index ${r.status}`); return r.json(); })
    .then(j => { GEO = j; supportReady("geo"); })
    .catch(err => { GEO = null; supportFailed("geo", err); });
}

function loadMeta(){
  supportStart("meta");
  return fetch(dataUrl("metadata.json"))
    .then(r => { if (!r.ok) throw new Error(`metadata ${r.status}`); return r.json(); })
    .then(j => { META = j; supportReady("meta"); renderMetadata(); })
    .catch(err => {
      META = null;
      supportFailed("meta", err);
      document.getElementById("dataMeta").textContent = "Metadata unavailable.";
    });
}

function loadRegionalSupport(){
  if (!regionalSupportPromise){
    regionalSupportPromise = Promise.all([
      PERF.time("data:geo", loadGeo), PERF.time("data:meta", loadMeta),
      PERF.time("data:ownerRoles", loadOwnerRoles), PERF.time("data:barrons", loadBarrons),
      PERF.time("data:forbes", loadForbes), PERF.time("data:actAssets", loadActAssets),
      PERF.time("data:territories", loadTerritories),
    ]).then(() => {
      syncOwnerUI();
      syncRankedUI();
      syncContactSwitches();
      if (searchBox.value.trim()) renderLocSuggest();
      if (scope === "US" && !pendingScope) renderEicNational();
      return SUPPORT;
    });
  }
  return regionalSupportPromise;
}

function failedSupportLabels(){
  const rows = [];
  if (SUPPORT.owner === "failed") rows.push("ownership roles");
  if (SUPPORT.barrons === "failed" || SUPPORT.forbes === "failed") rows.push("advisor rankings");
  if (SUPPORT.act === "failed") rows.push("EIC assets");
  if (SUPPORT.territories === "failed") rows.push("sales assignments");
  return rows;
}

// disclosure labels in the same bit order as export_geojson.py's DRP dict
const DRP_LABELS = ["Criminal", "Regulatory action", "Civil judicial", "Customer complaint",
  "Termination", "Judgment / lien", "Bankruptcy", "Bond", "Investigation"];

// Rehydrate the compact wire format into {geometry, properties} features so every
// consumer downstream is untouched. Firm-level fields (firm, motion, score, RAUM,
// 5.G(7), fit/size) come from the firm dictionary; the IAPD url is rebuilt from
// the CRD; the disclosure mask unpacks to labels.
/* What to say about outside managers, given WHICH signal fired.
 *
 * "No outside-manager selection reported" was literally true of LPL -- their
 * Form ADV answers Item 5.G(7) "N" -- and useless: they sponsor a wrap
 * programme carrying $598.6 billion across five programmes, which is precisely
 * the business of placing client money with managers who are not them. A rep
 * repeated that badge to LPL and was corrected by the client.
 *
 * So the badge names the evidence rather than reducing two different filings to
 * one yes-or-no.
 */
function outsideManagerLabel(p){
  // Two producers, two names for the same field: pins carry sgWhy (rehydrate),
  // firm profiles carry selectsWhy. Reading only one is how this badge kept
  // saying "Reports selecting outside managers" about a firm whose evidence is
  // wrap sponsorship -- right answer, wrong reason, and still not what a rep
  // should repeat to LPL.
  const why = p.sgWhy || p.selectsWhy || "";
  const wrap = p.wrapM ? ` · ${fmtMoney(p.wrapM * 1e6)} wrap` : "";
  if (why === "both") return `Selects outside managers · wrap sponsor${wrap}`;
  if (why === "wrap") return `Wrap sponsor${wrap}`;
  if (why === "selects" || p.selects || p.sg === 1)
    return "Reports selecting outside managers";
  return "No outside-manager selection reported";
}

function rehydrate(c, sourceState=""){
  const { iapd, firms, motions, addrs, cities, desig, regs, gp, xb, pins } = c;
  const out = new Array(pins.length);
  for (let k = 0; k < pins.length; k++){
    const p = pins[k], fm = firms[p[2]];
    out[k] = {
      geometry: { coordinates: [p[0], p[1]] },
      properties: {
        id: p[6], n: advisorDisplayName(p[6], p[7]),
        f: fm[0], m: motions[fm[1]], s: fm[2], ra: fm[3], sg: fm[4], sf: fm[5], sz: fm[6],
        // WHICH signal made sg true, and how big the wrap book is. Appended to
        // the firm row by export_geojson.py, so older builds simply omit them.
        sgWhy: fm[8] || "", wrapM: fm[9] == null ? null : fm[9],
        fc: String(fm[7]), _lon: p[0], _lat: p[1], _state: sourceState,
        a: addrs[p[3]], c: cities[p[4]], z: p[5],
        x: p[8], xb: p[9] < 0 ? "" : xb[p[9]],
        ns: p[10], rs: p[11] < 0 ? "" : regs[p[11]],
        gp: gp[p[12]], d: p[13],
        g: p[14] < 0 ? "" : desig[p[14]],
        pf: p[15], od: p[16],
        // placement: 19 = the filed address is not corroborated by the
        // advisor's employment record, 20 = where that record says they are
        unc: p[19] === 1, home: p[20] || "",
        // 21 = how the location is known. 0 office (street address), 1 remote
        // (the firm filed a city and state but no street), 2 uncertain.
        lt: p[21] == null ? 0 : p[21],
        dr: p[17] ? DRP_LABELS.filter((_, i) => p[17] & (1 << i)) : [],
        u: p[18] ? iapd + p[6] : "",
      },
    };
  }
  return out;
}

async function fetchScopeJson(st, metricPrefix, missingOk=false, signal=null){
  const downloadStart = performance.now();
  let response, text;
  try {
    response = await fetch(dataUrl(`pins_${st}.json`), { signal });
    if (!response.ok){
      if (missingOk) return {
        data: null, downloadMs: performance.now() - downloadStart,
        parseMs: 0, downloadedAt: performance.now(),
      };
      throw new Error(`no data for ${st}`);
    }
    text = await response.text();
  } catch (err) {
    if (err && err.name === "AbortError") throw err;
    if (missingOk) return {
      data:null, downloadMs:performance.now() - downloadStart,
      parseMs:0, downloadedAt:performance.now(),
    };
    throw err;
  } finally {
    PERF.add(`${metricPrefix}:download`, performance.now() - downloadStart);
  }

  const downloadedAt = performance.now();
  const parseStart = performance.now();
  let data;
  /* The parse gets the SAME tolerance the download already has.
   *
   * It did not, and the asymmetry was the bug: a state whose file 404s is
   * reported in `missing` and the other six still draw, but a state whose file
   * arrives TRUNCATED threw a SyntaxError straight past every per-state guard
   * and failed the whole territory -- "Could not load Northeast. The current
   * map was kept.", an empty map, and nothing naming the state or the reason.
   *
   * Truncation is the likelier of the two on the connection that matters.
   * Northeast is roughly 6.5 MB of pins, pins_NY.json alone 3.6 MB, and a rep
   * loading it on a phone is exactly who gets a body cut short. Losing one
   * state's pins is a bad afternoon; losing the territory is a broken app.
   */
  try { data = JSON.parse(text); }
  catch (err) {
    PERF.add(`${metricPrefix}:JSON.parse`, performance.now() - parseStart);
    if (!missingOk) throw new Error(`${st} data is unreadable (${err.message})`);
    return {
      data: null, downloadMs: downloadedAt - downloadStart,
      parseMs: performance.now() - parseStart, downloadedAt,
    };
  }
  PERF.add(`${metricPrefix}:JSON.parse`, performance.now() - parseStart);
  return {
    data,
    downloadMs: downloadedAt - downloadStart,
    parseMs: performance.now() - parseStart,
    downloadedAt,
  };
}

async function loadState(st, signal=null){
  const result = await fetchScopeJson(st, `scope:${st}`, false, signal);
  const features = PERF.timeSync(`scope:${st}:rehydrate`,
    () => rehydrate(result.data, st));
  return {
    features,
    missing: [],
  };
}

// A territory is several state files stitched into one pin set, so every
// advisor-level filter works across the whole footprint unchanged.
async function loadTerritory(name, signal=null){
  const sts = TERRITORIES[name] || [];
  const metric = `scope:T:${name}`;
  const startedAt = performance.now();
  const loaded = await Promise.all(sts.map(st =>
    fetchScopeJson(st, `${metric}/${st}`, true, signal)));
  // Downloads overlap, so elapsed wall time and JSON.parse CPU are both useful;
  // summing download times would exaggerate what the rep actually waited for.
  const lastDownload = loaded.reduce((latest, row) =>
    Math.max(latest, row.downloadedAt || startedAt), startedAt);
  PERF.add(`${metric}:download (parallel elapsed)`, lastDownload - startedAt);
  PERF.add(`${metric}:JSON.parse (CPU total)`,
           loaded.reduce((total, row) => total + (row.parseMs || 0), 0));
  const features = PERF.timeSync(`${metric}:rehydrate`, () =>
    loaded.flatMap((row, i) => row.data ? rehydrate(row.data, sts[i]) : []));
  return {
    features,
    missing: sts.filter((_, i) => !loaded[i].data),
  };
}

/* The dialer boots independently, and now actually does.
 *
 * The comment below has said this for a long time while Dial.init() sat INSIDE
 * the map's data gate -- so a rep resuming yesterday's call list waited on
 * every map dataset first, contacts.json among them at 8.9 MB. dial.js reads
 * no map state at all (no CONTACTS, no contactFor: each queue item carries its
 * own name, firm, phone and email), so there was never a dependency, only an
 * accident of nesting.
 *
 * What still waits for the map is the DEFAULT SCOPE, and it has to: it may call
 * switchScope(), which is meaningless before the scope machinery exists, and it
 * must not overrule a hash or a search that openFirmFromHash()/searchFromHash()
 * are about to apply.
 */
/* ---- TEMPORARY: boot and transition timing -------------------------------
 * Remove once the performance work is decided. Writes to the User Timing API,
 * so the marks show up in a Chrome profile alongside everything else, and
 * `window.PERF.report()` prints the summary in the console.
 *
 * Deliberately measuring the three things that are separately fixable --
 * network, JSON.parse, and building objects/markers -- because the remedy for
 * each is different and file sizes cannot tell them apart.
 */
const PERF = window.PERF = {
  t0: performance.now(),
  spans: [],
  signals: [],
  mark(name){ performance.mark(name); },
  add(name, ms){
    this.spans.push([name, ms]);
    performance.measure(name, { start: performance.now() - ms, duration: ms });
    return ms;
  },
  signal(name, detail={}){
    const event = { name, at: performance.now(), ...detail };
    this.signals.push(event);
    performance.mark(name, { detail: event });
    window.dispatchEvent(new CustomEvent("advisor-map-perf", { detail: event }));
    return event;
  },
  timeSync(name, fn){
    const start = performance.now();
    try { return fn(); }
    finally { this.add(name, performance.now() - start); }
  },
  async time(name, fn){
    const start = performance.now();
    try { return await fn(); }
    finally {
      const ms = performance.now() - start;
      this.add(name, ms);
    }
  },
  report(){
    const rows = this.spans.map(([n, ms]) => ({ phase: n, ms: +ms.toFixed(1) }));
    // Time to a USABLE MAP, stamped when render:first finished -- not when the
    // report was typed. The previous version measured to performance.now(),
    // which silently included however long the operator took to reach for the
    // console, and made a 2.5s boot read as 11s.
    if (this.usableAt != null)
      rows.push({ phase: "→ map usable after", ms: +(this.usableAt - this.t0).toFixed(1) });
    if (performance.memory)
      rows.push({ phase: "JS heap MB", ms: +(performance.memory.usedJSHeapSize / 1e6).toFixed(1) });
    console.table(rows);
    return rows;
  },
};

Dial.onChange(() => {
  renderDialer();
  // Covers lists opened after boot as well as the initially selected list.
  reconcileDesktopDialRoutes().catch(() => {});
});
const dialReady = PERF.time("dial.init", () => Dial.init()).then(() => {
  renderDialer();
  reconcileDesktopDialRoutes().catch(() => {});
  PERF.mark("dialer-usable");
});

// A role may be changed on the phone while the desktop tab sleeps. Refresh the
// small flag set on return so an active derived list can retire itself when its
// final label was removed elsewhere.
document.addEventListener("visibilitychange", async () => {
  if (document.visibilityState !== "visible" || !Dial.state.ready) return;
  try { await Dial.fetchFlags(); renderDialer(); } catch { /* stale labels remain visible */ }
});

let supportTimer = null;
let backgroundIdleHandle = null;
let backgroundIdleKind = "";
let backgroundController = null;
let backgroundRunning = false;
// The large advisor index is intentionally NOT background work. Sharded search
// does not need it, and the few remaining consumers request it when a person
// opens a card. Treating it as background work made every desktop session pay
// for 6.9 MB even when the user never needed the filed-name/office expansion.
const backgroundComplete = () =>
  (NAT_DETAIL_READY || NAT_DETAIL_ERROR) && (CONTACTS_READY || CONTACTS_ERROR);

function cancelScheduledBackground(){
  if (supportTimer != null){
    clearTimeout(supportTimer);
    supportTimer = null;
  }
  if (backgroundIdleHandle != null){
    if (backgroundIdleKind === "idle" && window.cancelIdleCallback)
      cancelIdleCallback(backgroundIdleHandle);
    else clearTimeout(backgroundIdleHandle);
    backgroundIdleHandle = null;
    backgroundIdleKind = "";
  }
  if (backgroundController){
    backgroundController.abort();
    backgroundController = null;
  }
}

function runBackgroundLoads(){
  backgroundIdleHandle = null;
  backgroundIdleKind = "";
  if (pendingScope || backgroundRunning || backgroundComplete()) return;
  backgroundRunning = true;
  const controller = new AbortController();
  backgroundController = controller;
  const signal = controller.signal;
  Promise.resolve()
    .then(() => (NAT_DETAIL_READY || NAT_DETAIL_ERROR)
      ? NAT : PERF.time("data:nationalDetail", () => loadNationalDetail(signal, true)))
    .then(() => {
      if (signal.aborted || pendingScope)
        throw new DOMException("Superseded", "AbortError");
      return (CONTACTS_READY || CONTACTS_ERROR)
        ? CONTACTS : PERF.time("data:contacts", () => loadContacts(signal));
    })
    .catch(err => { if (!err || err.name !== "AbortError") console.error(err); })
    .finally(() => {
      if (backgroundController === controller) backgroundController = null;
      backgroundRunning = false;
      if (!pendingScope) scheduleBackgroundLoads();
    });
}

function scheduleBackgroundLoads(){
  if (pendingScope || backgroundRunning || backgroundIdleHandle != null || backgroundComplete())
    return;
  if (window.requestIdleCallback){
    backgroundIdleKind = "idle";
    backgroundIdleHandle = requestIdleCallback(runBackgroundLoads, { timeout:1500 });
  } else {
    backgroundIdleKind = "timeout";
    backgroundIdleHandle = setTimeout(runBackgroundLoads, 250);
  }
}

function scheduleSupportLoads(){
  if (pendingScope || supportTimer != null) return;
  if (regionalSupportPromise){
    regionalSupportPromise.finally(scheduleBackgroundLoads);
    return;
  }
  // Give an immediate scope choice a clean network lane. The regional view can
  // render without enrichment; its dependent filters stay disabled meanwhile.
  supportTimer = setTimeout(() => {
    supportTimer = null;
    if (pendingScope) return;
    loadRegionalSupport().finally(scheduleBackgroundLoads);
  }, 600);
}

/* Only the compact national grid is on the first-paint critical path. Every
 * enrichment either fills optional UI or powers a control that remains
 * disabled until its data is ready. */
PERF.time("data:national", loadNational).then(() => {
  PERF.mark("national-data-ready");
  applyScopeUI();
  PERF.time("render:first", () => renderAll(true));
  PERF.mark("map-usable");
  PERF.usableAt = performance.now();
  console.info("[perf] map usable — run PERF.report() for the breakdown");
  // A real paint precedes any enrichment or contact traffic. advisor_index is
  // intentionally absent: explicit search/card intent owns that large fetch.
  afterNextPaint(scheduleSupportLoads);
  openFirmFromHash();
  searchFromHash();
  dialReady.then(() => {
    const audienceId = new URLSearchParams(location.search).get("audience");
    if (audienceId) {
      openAudienceFromLink(audienceId);
      return;
    }
    // The rep's opening area, applied only when nothing more specific already
    // decided it. A URL hash and a search are both things the rep asked for
    // just now; a saved default is what to do in the absence of those, and
    // overriding them would make a bookmark or a shared link stop working.
    const want = Dial.setting("defaultScope");
    // A URL hash is something the rep asked for just now -- a bookmark, or a
    // link handed over from the field view. The saved default is what to do in
    // the ABSENCE of an instruction, so it must never overrule one.
    if (want && want !== scope && !location.hash) switchScope(want);
  });
}).catch(err => {
  setBusy("Failed to load national data.");
  console.error(err);
});

function scopeLabel(sc){
  if (sc === "US") return "United States";
  if (sc.startsWith("T:")) return sc.slice(2);
  return STATE_NAMES[sc] || sc;
}

// The feed mixes formats: metadata dates are ISO (2026-07-20) but the firm
// roster's "Latest ADV Filing Date" is US (06/30/2026). Both are ten characters,
// so the old length check appended "T00:00:00Z" to the US ones and produced
// "filed Invalid Date" on every firm profile.
function fmtDate(value){
  if (!value) return "unknown";
  const text = String(value).trim();
  let d;
  const iso = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
  const us  = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(text);
  if (iso) d = new Date(`${text}T00:00:00Z`);
  else if (us) d = new Date(Date.UTC(+us[3], +us[1] - 1, +us[2]));
  else d = new Date(text);
  return isNaN(d) ? text
    : d.toLocaleDateString(undefined, { year:"numeric", month:"short", day:"numeric", timeZone:"UTC" });
}

function fmtPct(value, digits=1){
  return value == null || !Number.isFinite(+value) ? "—" : `${(+value).toFixed(digits)}%`;
}

function profileWebsite(value){
  if (!value) return "";
  const url = /^https?:\/\//i.test(value) ? value : `https://${value}`;
  return /^https?:\/\//i.test(url) ? url : "";
}

// ---- advisor employment history ----
// Sharded by the last two digits of the advisor CRD, matching
// export_advisor_history.py. The whole history is 31 MB; a shard is ~455 KB, so
// opening an advisor costs one small fetch and the shards a rep touches during
// a session stay in the cache.
const HISTORY_SHARDS = new Map();          // bucket -> Promise of its records
function historyShard(advisorCrd){
  const bucket = String(advisorCrd).trim().slice(-2).padStart(2, "0");
  if (!HISTORY_SHARDS.has(bucket)){
    HISTORY_SHARDS.set(bucket, fetch(dataUrl(`history/${bucket}.json`))
      .then(r => r.ok ? r.json() : {})
      .catch(() => ({})));
  }
  return HISTORY_SHARDS.get(bucket);
}

function fmtMonth(iso){
  if (!iso) return "";
  const d = new Date(`${String(iso).slice(0, 10)}T00:00:00Z`);
  return isNaN(d) ? String(iso).slice(0, 10)
    : d.toLocaleDateString(undefined, { year:"numeric", month:"short", timeZone:"UTC" });
}

function tenureFrom(iso){
  if (!iso) return "";
  const start = new Date(`${String(iso).slice(0, 10)}T00:00:00Z`);
  if (isNaN(start)) return "";
  const months = Math.max(0, Math.round((Date.now() - start) / 2629800000));
  if (months < 12) return `${months} month${months === 1 ? "" : "s"}`;
  const years = Math.floor(months / 12), rest = months % 12;
  return rest ? `${years} yr ${rest} mo` : `${years} year${years === 1 ? "" : "s"}`;
}

// Rendered into the already-open advisor panel once the shard arrives, so the
// panel never blocks on the fetch.
function renderAdvisorHistory(p, record){
  const slot = document.getElementById("advisorHistory");
  if (!slot || slot.dataset.for !== String(p.id)) return;
  const [joined, prior] = record || [null, []];
  const tenure = tenureFrom(joined);
  const joinedLine = joined
    ? `<p class="history-joined">Joined <b>${esc(p.f)}</b> ${fmtMonth(joined)}${tenure ? ` · ${tenure}` : ""}</p>`
    : "";
  const rows = (prior || []).map(row => {
    const [firmCrd, name, begin, end] = row;
    const span = [fmtMonth(begin), fmtMonth(end)].filter(Boolean).join(" – ") || "dates not reported";
    return `<button type="button" class="detail-row" data-firm-profile="${esc(firmCrd)}">` +
      `<span class="detail-row-main"><b>${esc(name)}</b><small>${esc(span)} · CRD ${esc(firmCrd)}</small></span>` +
      `<span class="detail-chevron">›</span></button>`;
  }).join("");
  slot.innerHTML = joinedLine + (rows
    ? `<p class="history-label">Previously registered with</p><div class="detail-list">${rows}</div>`
    : (joined ? "" : `<p class="profile-empty">No employment history reported.</p>`));
}

function loadFirmProfiles(){
  if (FIRM_PROFILES) return Promise.resolve(FIRM_PROFILES);
  if (!FIRM_PROFILE_PROMISE){
    FIRM_PROFILE_PROMISE = fetch(dataUrl("firm_profiles.json")).then(r => {
      if (!r.ok) throw new Error("firm profiles unavailable");
      return r.json();
    }).then(data => { FIRM_PROFILES = data; return data; });
  }
  return FIRM_PROFILE_PROMISE;
}

const detailsDrawer = document.getElementById("firmOverview");
const detailsBack = document.getElementById("detailsBack");
let detailsCurrent = null;
let detailsHistory = [];

function detailKey(entry){
  if (!entry) return "";
  if (entry.type === "firm") return `firm:${entry.crd}`;
  if (entry.type === "advisor") return `advisor:${entry.feature.properties.id}:${addrKey(entry.feature.properties)}`;
  if (entry.type === "location") return `location:${entry.location.lat}:${entry.location.lon}:${entry.location.addr}`;
  if (entry.type === "national-location") return `national:${entry.office[0]}:${entry.office[1]}:${natFirmCrd(entry.office)}`;
  return entry.type;
}

function captureDetailMap(){
  const center = map.getCenter();
  return {
    scope, center:[center.lat, center.lng], zoom:map.getZoom(),
    filters:captureAdvisorFilters(), focusedAdvisorId, focusedAdvisorLabel,
  };
}

function syncDetailsHeader(){
  detailsBack.hidden = !detailsHistory.length;
  detailsDrawer.hidden = false;
  map.closePopup();
}

function beginDetails(entry, push=true){
  if (push && detailsCurrent && detailKey(detailsCurrent) !== detailKey(entry))
    detailsHistory.push({ entry:detailsCurrent, map:captureDetailMap() });
  detailsCurrent = entry;
  /* A new subject starts at the top of the panel.
   *
   * The body keeps its scroll position across a re-render, which is right when
   * the SAME thing is being redrawn and wrong the moment it becomes a
   * different one: opening a person from a firm's roster meant scrolling down
   * to find them, then reading their card from whatever offset the roster
   * happened to be at -- usually past their name, phone and email.
   *
   * Here rather than in openAdvisorDetails() because every detail type arrives
   * through this function, and the next one added would otherwise inherit the
   * same bug.
   */
  const body = document.getElementById("firmOverviewBody");
  if (body) body.scrollTop = 0;
  syncDetailsHeader();
}

function syncDetailHash(entry){
  const target = entry?.type === "firm"
    ? `#firm=${encodeURIComponent(entry.crd)}`
    : `${location.pathname}${location.search}`;
  const current = location.hash || `${location.pathname}${location.search}`;
  if (current !== target) history.replaceState({ detail:detailKey(entry) || null }, "", target);
}

async function renderDetailEntry(entry, push=false){
  if (!entry) return;
  if (entry.type === "firm") return openFirmOverview(entry.crd, false, push);
  if (entry.type === "advisor") return openAdvisorDetails(entry.feature, push);
  if (entry.type === "location") return openRoster(entry.location, push);
  if (entry.type === "national-location") return openNationalLocation(entry.office, push);
}

async function goBackDetails(){
  const prior = detailsHistory.pop();
  if (!prior) return;
  const current = detailsCurrent;
  if (prior.map.scope !== scope){
    const outcome = await switchScope(prior.map.scope);
    if (!outcome || outcome.status !== "applied" || detailsCurrent !== current){
      detailsHistory.push(prior);
      return;
    }
  }
  detailsCurrent = null;
  if (prior.map.filters) restoreAdvisorFilters(prior.map.filters);
  focusedAdvisorId = prior.map.focusedAdvisorId || null;
  focusedAdvisorLabel = prior.map.focusedAdvisorLabel || "";
  redraw(false);
  map.setView(prior.map.center, prior.map.zoom, { animate:false });
  await renderDetailEntry(prior.entry, false);
}

function closeDetails(){
  detailsCurrent = null;
  detailsHistory = [];
  openFirmCrd = null;
  detailsDrawer.hidden = true;
  map.closePopup();
  clearMapSelection();
  highlightLocation(null);
  clearSpokes();
  syncDetailHash(null);
}

detailsBack.addEventListener("click", goBackDetails);

function productNarrative(product){
  if (product === "sma_led") return "The reported asset mix suggests an equity SMA may be the more natural opening conversation.";
  if (product === "eicix_led") return "The reported asset mix suggests the mutual fund may be the more natural opening conversation.";
  return "";
}

function profileTable(headers, rows){
  return `<div class="profile-table-wrap"><table class="profile-table"><thead><tr>${headers.map(h => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
}

// Parent rollup. Every entity in a chain is a group, so you can work at the
// altitude that matters: "Focus Operating" (42 firms) and "Clayton, Dubilier &
// Rice Fund XII" (27) are the same money seen from different heights.
function parentGroup(key){
  const groups = FIRM_PROFILES && FIRM_PROFILES.groups;
  return groups && key ? groups[key] : null;
}

// Selecting a parent puts every firm it owns on the map at once. selectedFirms
// is already a multi-select, so the comparison colours and the firm list come
// along for free.
document.addEventListener("click", e => {
  const b = e.target.closest("[data-parent]");
  if (!b) return;
  const group = FIRM_PROFILES?.groups?.[b.dataset.parent];
  if (!group) return;
  const [label, crds] = group;
  selectedFirms = crds.map(String);
  syncFirmColors();
  document.getElementById("clearFirms").hidden = false;
  redraw(true);
  showNotice(`Showing ${crds.length} firms owned through ${label}.`);
  renderParentMembers(label, crds);
});

// the sister firms, listed under the chain so the group can be read as well as
// mapped
function renderParentMembers(label, crds){
  const slot = document.getElementById("parentMembers");
  if (!slot) return;
  const rows = crds.map(crd => {
    const profile = FIRM_PROFILES.profiles[crd];
    const name = profile ? profile.name : `CRD ${crd}`;
    const where = profile ? [profile.city, profile.state].filter(Boolean).join(", ") : "";
    return `<button type="button" class="detail-row" data-firm-profile="${esc(crd)}">` +
      `<span class="detail-row-main"><b>${esc(name)}</b><small>${esc(where)} · CRD ${esc(crd)}` +
      `${profile && profile.raum != null ? ` · ${fmtMoney(profile.raum)}` : ""}</small></span>` +
      `<span class="detail-chevron">›</span></button>`;
  }).join("");
  slot.innerHTML = `<p class="history-label">${esc(label)} · ${crds.length} firms</p>` +
    `<div class="detail-list">${rows}</div>`;
}

// Schedule A lists executive officers AND owners of 5% or more, so a row with
// code NA ("less than 5%") is frequently an officer holding no equity at all --
// 41.3% of roles and 38.7% of tagged people. Calling those OWNER was wrong.
function roleTag(code){
  if (!code) return "OFFICER";
  if (code === "NA") return "OFFICER <5%";
  return `OWNER ${OWNERSHIP_BANDS[code] || code}`;
}

// Form ADV Schedule A/B ownership codes. Bands, never exact percentages.
const OWNERSHIP_BANDS = {
  NA:"under 5%", A:"5–10%", B:"10–25%", C:"25–50%", D:"50–75%", E:"75%+", F:"75%+",
};

// An owner named on Schedule A is often a person we already map -- 55% of the
// owner CRDs are advisors with a mapped branch -- so make those rows open the
// individual. Where they are mapped in another scope, reuse the same jump the
// advisor panel uses for out-of-view offices.
function ownerRow(row){
  const [name, type, title, date, code, ctrl, ownerCrd] = row;
  const band = OWNERSHIP_BANDS[code] || "";
  const stake = code === "NA" ? "officer, under 5%" : (band ? `owns ${band}` : "");
  const bits = [title, stake, ctrl ? "control person" : "",
                date ? `since ${esc(date)}` : ""].filter(Boolean).join(" · ");
  const local = ownerCrd && ALL.some(f => String(f.properties.id) === String(ownerCrd));
  const idx = ownerCrd && !local ? advisorRow(ownerCrd) : null;
  const away = idx && (STATE_TO_TERRITORY[idx[3]] ? terrKey(STATE_TO_TERRITORY[idx[3]]) : idx[3]);
  const attrs = local
    ? ` data-owner-advisor="${esc(ownerCrd)}"`
    : (away ? ` data-advisor-elsewhere="${esc(away)}" data-advisor-id="${esc(ownerCrd)}"` : "");
  const tag = attrs ? "button" : "div";
  return `<${tag} type="button" class="detail-row${attrs ? "" : " static"}"${attrs}>` +
    `<span class="detail-row-main"><b>${esc(name)}</b><small>${bits}` +
    `${ownerCrd ? ` · CRD ${esc(ownerCrd)}` : ""}</small></span>` +
    `${attrs ? `<span class="detail-chevron">›</span>` : ""}</${tag}>`;
}

function renderOwners(p){
  const own = p.own || {};
  const rows = own.a || [];
  if (!own.a){
    return `<section class="profile-section"><h3>Owners and control${infoBox(
      "Schedule A and B are collected only for firms reporting Item 5.G(7), because those are the firms that hire outside managers. This firm is outside that scope.")}</h3>
      <div class="profile-empty">Ownership was not collected for this firm.</div></section>`;
  }
  // Officers holding under 5% are the bulk of Schedule A and rarely the point,
  // so lead with control persons and real stakes and fold the rest away.
  const lead = rows.filter(r => r[5] || (r[4] && r[4] !== "NA"));
  const rest = rows.filter(r => !(r[5] || (r[4] && r[4] !== "NA")));
  const chain = own.chain || [];
  return `<section class="profile-section"><h3>Owners and control${infoBox(
      "Form ADV Schedule A names direct owners and executive officers; Schedule B names indirect owners. " +
      "Ownership is reported in bands, never exact percentages, and is filed at registration and amended " +
      "since -- so treat it as the last filed position, not a live cap table.")}</h3>
    ${chain.length ? `<p class="history-joined">Ultimately owned through <b>${esc(chain[chain.length - 1])}</b></p>
      <details class="owner-chain"><summary>${chain.length} step${chain.length === 1 ? "" : "s"} of ownership · click a level to see its firms</summary>
        <ol>${chain.map((n, i) => {
          const key = (own.chain_keys || [])[i];
          const g = parentGroup(key);
          return `<li>${g
            ? `<button type="button" class="parent-step" data-parent="${esc(key)}">${esc(n)}` +
              `<span class="parent-count">${g[1].length} firms</span></button>`
            : esc(n)}</li>`;
        }).join("")}</ol></details>` : ""}
    ${lead.length ? `<div class="detail-list">${lead.map(ownerRow).join("")}</div>` : ""}
    ${rest.length ? `<details class="owner-chain"><summary>${rest.length} further officer${rest.length === 1 ? "" : "s"} under 5%</summary>
      <div class="detail-list">${rest.map(ownerRow).join("")}</div></details>` : ""}
    ${!rows.length ? `<div class="profile-empty">No direct owners or officers were filed.</div>` : ""}
    <div id="parentMembers"></div>
  </section>`;
}

document.addEventListener("click", e => {
  const b = e.target.closest("[data-owner-advisor]");
  if (!b) return;
  const id = String(b.dataset.ownerAdvisor);
  const f = ALL.find(x => String(x.properties.id) === id);
  if (f) openAdvisorDetails(f);
});

function renderFirmOverview(crd, p){
  const total = p.raum || 0;
  const discShare = total && p.disc != null ? p.disc / total * 100 : null;
  const nondiscShare = total && p.nondisc != null ? p.nondisc / total * 100 : null;
  const website = profileWebsite(p.website);
  const sourceDate = META?.source_date ? fmtDate(META.source_date) : "the current SEC bulk feed";
  document.getElementById("firmOverviewName").textContent = p.name || `CRD ${crd}`;
  document.getElementById("firmOverviewMeta").innerHTML = [
    `CRD ${esc(crd)}`, esc([p.city, p.state].filter(Boolean).join(", ")),
    p.filed ? `filed ${esc(fmtDate(p.filed))}` : ""
  ].filter(Boolean).join(" · ") + infoBox(
    `Firm facts come from the SEC Form ADV bulk feed dated ${sourceDate}; map counts are ` +
    `mapped advisor placements and may include one advisor at multiple offices. ` +
    `Firm identity is preserved by CRD.`);

  const clientRows = (p.clients || []).map((row, i) => {
    const [count, fewer, aum] = row;
    if (count == null && !fewer && aum == null) return "";
    return `<tr><td>${esc(FIRM_PROFILES.client_labels[i])}</td><td>${fewer ? "&lt;5" : count == null ? "—" : count.toLocaleString()}</td><td>${aum == null ? "—" : fmtMoney(aum)}</td></tr>`;
  }).filter(Boolean);
  const assetRows = (p.assets || []).map((pct, i) => {
    if (pct == null) return "";
    const implied = p.non_pooled == null ? null : p.non_pooled * pct / 100;
    return `<tr><td>${esc(FIRM_PROFILES.asset_labels[i])}</td><td>${fmtPct(pct, pct % 1 ? 1 : 0)}</td><td>${implied == null ? "—" : fmtMoney(implied)}</td></tr>`;
  }).filter(Boolean);
  const productAngle = productNarrative(p.product);
  const destinations = firmDestinations(crd);
  const currentHasFirm = scope !== "US" && ALL.some(f => String(f.properties.fc) === String(crd));
  const advisorScope = currentHasFirm ? scope : (destinations.length === 1 ? destinations[0].scope : "");

  // Offices of this firm inside the current viewport, biggest first. The list is
  // capped, so without the sort the cap would hand back an arbitrary thirty
  // rather than the thirty that matter.
  const bounds = map.getBounds();
  const mine = f => String(f.properties.fc) === String(crd);
  const currentLocations = (scope === "US" ? [] : ADDR_BY_I.filter(loc =>
      loc.feats.some(mine) && bounds.contains([loc.lat, loc.lon])))
    .map(loc => ({ loc, n: new Set(loc.feats.filter(mine).map(f => f.properties.id)).size }))
    .sort((a, b) => b.n - a.n || (a.loc.addr || "").localeCompare(b.loc.addr || ""));
  const locationRows = currentLocations.slice(0, 30).map(({ loc, n }) =>
    `<button type="button" class="detail-row" data-a="${loc.i}"><span class="detail-row-main">` +
    `<b>${esc(loc.addr || loc.city || "Filed office")}</b><small>${esc([loc.city, loc.zip].filter(Boolean).join(" "))} · ` +
    `${n.toLocaleString()} advisor${n === 1 ? "" : "s"}</small></span><span class="detail-chevron">›</span></button>`).join("");

  // Named advisors of this firm on screen right now. A list beats a button:
  // "show them on the map" leaves you to hunt for the pins, whereas these rows
  // open the person directly.
  FIRM_ADVISOR_ROWS = [];
  if (scope !== "US"){
    const seen = new Map();
    for (const f of ALL){
      if (!mine(f)) continue;
      // THE ADVISOR FILTERS APPLY HERE TOO.
      //
      // This list is headed "Advisors in current map" and did not honour a
      // single one of them: with "ranked advisors only" switched on, the map
      // showed 6 people and this list showed all 240 at the firm, with no
      // indication the two disagreed. passesBase rather than passesFilters
      // because the firm is already fixed by mine() and a focused advisor
      // deliberately does not delete everybody else.
      if (!passesBase(f.properties)) continue;
      const [lon, lat] = f.geometry.coordinates;
      if (!bounds.contains([lat, lon]) || seen.has(f.properties.id)) continue;
      seen.set(f.properties.id, f);
    }
    // Ranked advisors float to the top. The list pages 40 at a time and a big
    // firm can run to hundreds of rows -- Merrill has 683 in Georgia with 10
    // ranked -- so under a plain alphabetical sort the badge a rep is looking
    // for sits 17 pages down and may as well not exist.
    FIRM_ADVISOR_ROWS = [...seen.values()].sort((a, b) =>
      ((barronsFor(b.properties.id).length || forbesFor(b.properties.id).length) ? 1 : 0) -
      ((barronsFor(a.properties.id).length || forbesFor(a.properties.id).length) ? 1 : 0) ||
      a.properties.n.localeCompare(b.properties.n));
  }
  FIRM_ADVISOR_SHOWN = FIRM_ADVISOR_PAGE;

  const coverageRows = destinations.map(row =>
    `<button type="button" data-profile-advisors-scope="${esc(row.scope)}" data-profile-crd="${esc(crd)}">` +
    `<b>${esc(row.label)}</b><span>${row.placements.toLocaleString()} placement${row.placements === 1 ? "" : "s"} · ${row.offices.toLocaleString()} office${row.offices === 1 ? "" : "s"}</span></button>`
  ).join("");

  const bodyEl = document.getElementById("firmOverviewBody");
  bodyEl.innerHTML = `
    <div class="profile-actions">
      ${website ? `<a href="${esc(website)}" target="_blank" rel="noopener">Firm website ↗</a>` : ""}
      <a href="${firmIapdUrl(crd)}" target="_blank" rel="noopener">Firm IAPD ↗</a>
    </div>
    ${scope !== "US" ? `<section class="profile-section">
      <h3>Locations in current map</h3>
      ${locationRows
        ? `<div class="detail-list">${locationRows}</div>
           ${currentLocations.length > 30 ? `<p class="profile-note">Showing the largest 30 of ${currentLocations.length.toLocaleString()} offices in view.</p>` : ""}`
        : `<div class="profile-empty">No offices for this firm in the current view.</div>`}
      <div class="profile-actions"><button type="button" data-profile-all-offices="${esc(crd)}">Show all offices nationally</button></div>
    </section>` : ""}
    ${scope !== "US" ? `<section class="profile-section">
      <h3>Advisors in current map</h3>
      ${(() => {
        // A firm's ranked headcount is a strong opener for a rep, and it is
        // free once the roster is built.
        const n = FIRM_ADVISOR_ROWS.filter(f => barronsFor(f.properties.id).length).length;
        const nf = FIRM_ADVISOR_ROWS.filter(f => forbesFor(f.properties.id).length).length;
        return (n ? `<p class="profile-note barrons-note"><span class="rbar">BARRON'S</span> ` +
          `${n.toLocaleString()} ranked advisor${n === 1 ? "" : "s"} here.</p>` : "") +
          (nf ? `<p class="profile-note barrons-note"><span class="rfor">FORBES</span> ` +
          `${nf.toLocaleString()} ranked advisor${nf === 1 ? "" : "s"} here.</p>` : "");
      })()}
      <div id="firmAdvisorList"></div>
    </section>` : ""}
    <div class="profile-badges">
      <span class="profile-badge">${esc((p.firm_type || "unclassified").replaceAll("_", " "))}</span>
      ${(p.aka || []).map(n => `<span class="profile-badge">formerly ${esc(n)}</span>`).join("")}
      <span class="profile-badge">${p.platform ? "Home-office / platform access" : "Territory sales access"}</span>
      <span class="profile-badge">${outsideManagerLabel(p)}</span>
      ${p.review ? `<span class="profile-badge">review: ${esc(p.review.replaceAll("_", " "))}</span>` : ""}
    </div>
    ${productAngle ? `<div class="profile-recommendation"><h3>Potential product angle</h3><p>${productAngle}</p></div>` : ""}
    <div class="profile-metrics">
      <div class="profile-metric"><span>Total regulatory AUM</span><b class="mono">${p.raum == null ? "—" : fmtMoney(p.raum)}</b></div>
      <div class="profile-metric"><span>SMA-relevant pool</span><b class="mono">${p.equity_implied == null ? "—" : fmtMoney(p.equity_implied)}</b></div>
      <div class="profile-metric"><span>Fund-relevant pool</span><b class="mono">${p.fund_implied == null ? "—" : fmtMoney(p.fund_implied)}</b></div>
      <div class="profile-metric"><span>Firm advisors</span><b class="mono">${(p.advisors || 0).toLocaleString()}</b></div>
      <div class="profile-metric"><span>Mapped placements</span><b class="mono">${(p.mapped_placements || 0).toLocaleString()}</b></div>
      <div class="profile-metric"><span>Mapped offices / states</span><b class="mono">${(p.mapped_offices || 0).toLocaleString()} / ${(p.mapped_states || 0).toLocaleString()}</b></div>
    </div>
    ${coverageRows ? `<section class="profile-section"><h3>Advisor coverage</h3>
      <p>Choose a sales territory to load advisor-level detail.</p>
      <div class="profile-territories">${coverageRows}</div>
      ${advisorScope ? `<div class="profile-actions"><button type="button" data-profile-advisors-scope="${esc(advisorScope)}" data-profile-crd="${esc(crd)}">Show all advisors in ${esc(scopeLabel(advisorScope))}</button></div>` : ""}
    </section>` : ""}
    ${renderOwners(p)}
    <section class="profile-section"><h3>Discretion${infoBox(
      "The filing does not cross-tab asset type by discretion, so the product pools shown elsewhere on this page are not multiplied by the discretionary share.")}</h3>
      <p><b>${p.disc == null ? "—" : fmtMoney(p.disc)} discretionary (${fmtPct(discShare)})</b> · ${p.nondisc == null ? "—" : fmtMoney(p.nondisc)} non-discretionary (${fmtPct(nondiscShare)}).</p>
      <p>${p.disc_accounts == null ? "—" : p.disc_accounts.toLocaleString()} discretionary accounts · ${p.nondisc_accounts == null ? "—" : p.nondisc_accounts.toLocaleString()} non-discretionary accounts · ${p.accounts == null ? "—" : p.accounts.toLocaleString()} total.</p>
    </section>
    <section class="profile-section"><h3>Client mix — ADV Item 5.D</h3>
      ${clientRows.length ? profileTable(["Client type", "Clients", "Regulatory AUM"], clientRows) : `<div class="profile-empty">No client-category detail was reported.</div>`}
    </section>
    <section class="profile-section"><h3>Asset mix — ADV Item 5.K</h3>
      ${assetRows.length ? profileTable(["Asset category", "%", "Implied AUM"], assetRows) : `<div class="profile-empty">No asset-category percentages were reported.</div>`}
      <p class="profile-note">Implied AUM = reported percentage × ${p.non_pooled == null ? "the applicable non-pooled denominator" : fmtMoney(p.non_pooled)}. The ADV percentages exclude client assets for investment companies, business development companies, and pooled investment vehicles; percentages are approximate and implied dollars are estimates, not reported holdings.</p>
    </section>`;
  renderFirmAdvisorList();
}

// features backing the "Advisors in current map" rows, in render order
let FIRM_ADVISOR_ROWS = [];
const FIRM_ADVISOR_PAGE = 40;
let FIRM_ADVISOR_SHOWN = FIRM_ADVISOR_PAGE;

// One-click queueing straight from a list row.
//
// Building a call list of thirty by opening thirty cards is the kind of
// friction that means the list never gets built. Only offered for advisors we
// can actually reach -- queueing someone with no phone and no email is queueing
// a dead end.
function rowQueueButton(id){
  if (Dial.isDnc(id))
    return `<span class="row-queue dnc" title="Firm-wide do-not-call">&#9940;</span>`;
  const c = contactFor(id);
  if (!c || (!c.w && !c.e)) return `<span class="row-queue" aria-hidden="true"></span>`;
  const on = Dial.inQueue(id);
  return `<button type="button" class="row-queue${on ? " on" : ""}"
    data-row-queue="${esc(id)}"
    title="${on ? "Remove from the call list" : "Add to the call list"}"
    aria-label="${on ? "Remove from" : "Add to"} the call list"
    >${on ? "&#10003;" : "&#43;"}</button>`;
}

// Rendered separately from the rest of the drawer so "show more" can extend the
// list in place without rebuilding the panel and losing the reader's position.
// The list used to stop dead at 40 with a count and no way to reach the rest.
function renderFirmAdvisorList(){
  const slot = document.getElementById("firmAdvisorList");
  if (!slot) return;
  const total = FIRM_ADVISOR_ROWS.length;
  if (!total){
    slot.innerHTML = `<div class="profile-empty">No advisors for this firm in the current view.</div>`;
    return;
  }
  const shown = Math.min(FIRM_ADVISOR_SHOWN, total);
  const rows = FIRM_ADVISOR_ROWS.slice(0, shown).map((f, i) => {
    const q = f.properties;
    const owns = ownerRolesFor(q.id).find(r => String(r.firmCrd) === String(q.fc));
    const bits = [owns ? (owns.title || "Officer") : "",
                  q.x != null ? `${q.x.toFixed(0)} yrs` : "", q.c].filter(Boolean).join(" · ");
    return `<div class="detail-row-wrap">` +
      `<button type="button" class="detail-row" data-fai="${i}"><span class="detail-row-main">` +
      `<b>${esc(q.n)}${q.dr && q.dr.length ? " ⚠" : ""}${contactDots(q.id)}${barronsTag(q.id)}${forbesTag(q.id)}` +
      `${owns ? `<span class="rown${owns.code === "NA" ? " rown-officer" : ""}">${roleTag(owns.code)}</span>` : ""}` +
      `${q.unc ? `<span class="runc">ADDRESS UNCERTAIN</span>`
                : (q.lt === 1 ? `<span class="rcity">CITY LEVEL</span>` : "")}</b>` +
      `<small>${esc(bits)}</small></span><span class="detail-chevron">›</span></button>` +
      personActionButton(q.id, q.n) + rowQueueButton(q.id) + `</div>`;
  }).join("");
  const remaining = total - shown;
  // Counts the whole filtered set, not the 40 on screen -- "add these" should
  // mean the list, not the page of it the rep happens to have scrolled to.
  slot.innerHTML = `<div class="profile-actions bulk-actions">`
      + `<button type="button" id="queueFirmList">&#43;&#9742; Add ${total.toLocaleString()}`
      + ` to call list</button></div>`
    + `<div class="detail-list">${rows}</div>` +
    (remaining
      ? `<div class="profile-actions"><button type="button" id="firmAdvisorMore">` +
        `Show ${Math.min(FIRM_ADVISOR_PAGE, remaining)} more of ${remaining.toLocaleString()}</button></div>`
      : (total > FIRM_ADVISOR_PAGE
          ? `<p class="profile-note">Showing all ${total.toLocaleString()} advisors in view.</p>` : ""));
}

document.addEventListener("click", e => {
  if (!e.target.closest("#firmAdvisorMore")) return;
  FIRM_ADVISOR_SHOWN += FIRM_ADVISOR_PAGE;
  renderFirmAdvisorList();
});
document.addEventListener("click", e => {
  const b = e.target.closest("[data-fai]");
  if (!b) return;
  const f = FIRM_ADVISOR_ROWS[+b.dataset.fai];
  if (f) openAdvisorDetails(f);
});

function setFirmHash(crd){
  const next = crd ? `#firm=${encodeURIComponent(crd)}` : `${location.pathname}${location.search}`;
  history.pushState({ firm: crd || null }, "", next);
}

function openFirmOverview(crd, updateHistory=true, detailPush=true){
  crd = String(crd);
  beginDetails({ type:"firm", crd }, detailPush);
  openFirmCrd = crd;
  document.getElementById("detailsKind").textContent = "Firm";
  document.getElementById("firmOverviewName").textContent = firmLabelForCrd(crd);
  document.getElementById("firmOverviewMeta").textContent = `CRD ${crd}`;
  document.getElementById("firmOverviewBody").innerHTML = `<div class="profile-empty">Loading firm details…</div>`;
  if (updateHistory) syncDetailHash({ type:"firm", crd });
  // The compact first paint carries no firm-office destinations. Explicitly
  // opening a firm is intent to fetch them; repaint the still-open profile when
  // they arrive so destination buttons do not remain temporarily absent.
  if (!NAT_DETAIL_READY){
    loadNationalDetail(null, false).then(() => {
      if (!NAT_DETAIL_READY || openFirmCrd !== crd) return;
      loadFirmProfiles().then(data => {
        if (openFirmCrd === crd && data.profiles[crd])
          renderFirmOverview(crd, data.profiles[crd]);
      }).catch(() => {});
    });
  }
  return loadFirmProfiles().then(data => {
    if (openFirmCrd !== crd) return;
    const p = data.profiles[crd];
    if (!p) throw new Error(`No mapped profile for CRD ${crd}`);
    renderFirmOverview(crd, p);
  }).catch(err => {
    if (openFirmCrd === crd) document.getElementById("firmOverviewBody").innerHTML = `<div class="profile-empty">Firm detail is unavailable. Use the IAPD link for CRD ${esc(crd)}.</div>`;
    console.error(err);
  });
}

function closeFirmOverview(updateHistory=true){
  closeDetails();
}

function openFirmFromHash(){
  const match = location.hash.match(/^#firm=([^&]+)/);
  if (match) openFirmOverview(decodeURIComponent(match[1]), false);
  else if (openFirmCrd) closeFirmOverview(false);
}

// Handed over from the field view's "Search in full map" when a phone search
// found nobody. The field index only covers advisors we hold contact detail
// for; this page covers every registered advisor, so a firm and a website are
// often still reachable. Retyping a name on a phone is exactly the friction
// that makes a rep give up, so the term travels in the URL.
function searchFromHash(){
  const match = location.hash.match(/^#q=([^&]+)/);
  if (!match) return;
  let term = "";
  try { term = decodeURIComponent(match[1]); } catch { return; }
  if (!term.trim()) return;
  searchBox.value = term;
  // Nothing focuses the box on this path, so warm the same two small files the
  // focus handler would have. focus() is deliberate: the results popover is
  // anchored to the box.
  Promise.all([loadSearchManifest().catch(() => {}), loadFirmAliases().catch(() => {})])
    .then(() => { searchBox.focus(); runSearch(); });
}

// Every stale-cache symptom in this app has traced to the browser holding an
// old app.js against fresh data or vice versa. The build stamps the data it
// generated; if that disagrees with what this script was shipped against, say
// so rather than letting the user debug ghost behaviour.
function checkDataVersion(){
  if (!META || !META.generated_utc) return;
  const built = String(META.generated_utc).slice(0, 10).replace(/-/g, "");
  const shipped = DATA_VERSION.replace(/[^0-9]/g, "").slice(0, 8);
  if (built && shipped && built > shipped)
    showNotice(`This page is running an older build (${DATA_VERSION}) than the data it just ` +
               `loaded (generated ${fmtDate(META.generated_utc)}). Reload with Ctrl+F5.`);
}

function renderMetadata(){
  if (!META) return;
  checkDataVersion();
  const placed = META.placed_rows || 0;
  const pct = key => placed ? ((META.precision?.[key] || 0) / placed * 100).toFixed(1) : "0.0";
  document.getElementById("dataMeta").innerHTML =
    `<b>SEC feed ${fmtDate(META.source_date)}</b> · map built ${fmtDate(META.generated_utc)}<br>` +
    `${esc(META.refresh_cadence || "Refreshes with each SEC bulk-feed pipeline run")}<br>` +
    `${META.coverage_pct.toFixed(2)}% geocoded (${META.unplaced_rows.toLocaleString()} unplaced)` +
    `${META.pin_rows ? ` · ${META.pin_rows.toLocaleString()} pins after one-per-advisor placement` : ""}<br>` +
    `${pct("rooftop")}% rooftop · ${pct("approximate")}% approximate · ${pct("neighbour")}% nearest`;
}

// enable or disable the controls the current scope can actually honour. Only the
// national aggregate lacks per-advisor detail; a territory carries full pins, so
// its advisor-level filters stay live.
function applyScopeUI(){
  const national = scope === "US";
  document.getElementById("scopeNote").hidden = !national;
  // Greyed, not hidden. Hiding made the panel jump on every scope change and
  // left people hunting for controls that had silently disappeared; the reason
  // they cannot work nationally is stated instead of implied.
  document.querySelectorAll("[data-needs-state]").forEach(el => {
    el.classList.toggle("filters-disabled", national);
    el.querySelectorAll("input,button").forEach(c => { c.disabled = national; });
  });
  const advNote = document.getElementById("advisorFilterNote");
  if (advNote) advNote.hidden = !national;
  document.querySelectorAll("[data-national-only]").forEach(el => { el.hidden = !national; });
  // The generic scope toggle above must not re-enable a filter whose deferred
  // dataset is still loading or failed.
  syncOwnerUI();
  syncRankedUI();
  syncContactSwitches();
  syncTargetingUI();
  document.title = `Advisor Map — ${scopeLabel(scope)}`;
}

function renderAll(fit, transition=null){
  if (scope === "US") renderNational(fit ? "national" : false);
  else renderMarkers(fit, transition);
  refreshPanel();
}

// one redraw entry point so every control works in either scope
function redraw(fit){
  if (scope === "US") renderNational(fit); else renderMarkers(fit);
  refreshPanel();
}

/* Turning a filter ON used to fly the map to the full extent of everything it
 * matched, across the whole territory. That reads as helpful and is not: a rep
 * working one suburb ticks "Ranked advisors" and is thrown out to a view of six
 * states, with no route back to where they were. A filter answers "which of
 * these people", not "take me somewhere else".
 *
 * So the viewport is left exactly as it was. The one case still worth saying
 * out loud is a filter that empties the CURRENT view while matching elsewhere
 * -- otherwise the rep faces a blank map with no way to tell a working filter
 * from a broken one. That is a sentence, not a camera move.
 */
function reportFilterReach(emptyMsg, label){
  const matches = ALL.filter(f => passesFilters(f.properties));
  if (!matches.length){ showNotice(emptyMsg); return; }
  const bounds = map.getBounds();
  const here = matches.some(f =>
    bounds.contains([f.geometry.coordinates[1], f.geometry.coordinates[0]]));
  if (!here)
    showNotice(`No ${label} in this view — ${matches.length.toLocaleString()} `
      + `elsewhere in ${scopeLabel(scope)}. Zoom out to see them.`);
}

// moving scope invalidates every selection made under the old one
function resetForScopeChange(){
  selectedFirms = []; firmColor = {};
  focusedAdvisorId = null; focusedAdvisorLabel = "";
  document.getElementById("clearFirms").hidden = true;
  clearSearch();                       // clears the visible box, not just its state
  expSel.clear(); reachSel.clear(); geoSel.clear();
  document.querySelectorAll("#expToggle button, #reachToggle button").forEach(b =>
    b.setAttribute("aria-pressed", b.dataset.exp === "all" || b.dataset.reach === "all"));
  document.querySelectorAll("#geoToggle button").forEach(b =>
    b.setAttribute("aria-pressed", b.dataset.geo === "all"));
  highlightLocation(null);
  clearSpokes();
  clearLasso(false);
  // Deliberately NOT reset, so they carry from one state or territory to the
  // next: targeting (5.G(7) + AUM), role at firm, and Barron's ranking. The
  // line is exploration vs. standing strategy -- experience and reach are
  // things you fiddle with inside one territory, whereas "only decision-makers"
  // and "only ranked advisors" are how a rep works every territory, and having
  // to re-apply them at each switch is pure friction. Density is uniform enough
  // that neither empties a territory: 121-293 ranked advisors and 1,264-4,146
  // owners in each of the seven. These two move together on purpose -- filters
  // that look identical in the panel must not behave differently on a switch.
}

// single scope entry point: US, a state, or "T:<territory>". panTo optionally
// recentres after the data is in (used by location search).
let scopeRequest = 0;
let scopeController = null;
/* Open an advisor who is NOT in the scope currently on screen.
 *
 * The map holds one scope at a time, so "Show on map" used to look the advisor
 * up in what happened to be loaded and, failing, tell the rep to go and switch
 * territory themselves. That is the app describing its own implementation:
 * every fact needed to make the switch -- the advisor's state, and which
 * territory owns it -- is already here, and a rep working a national call list
 * hits it constantly because a list is not confined to one territory.
 *
 * Prefers the TERRITORY over the bare state, because that is the scope a rep
 * actually works in and switching to it keeps their neighbours on screen.
 */
function scopeForState(state){
  const st = String(state || "").toUpperCase();
  if (!st) return "";
  for (const [name, states] of Object.entries(TERRITORIES))
    if (states.includes(st)) return terrKey(name);
  // A state in no territory is still a scope of its own.
  return st;
}

/* Put the advisor on screen, not merely in scope.
 *
 * "Show on map" used to open the card and stop, leaving the map wherever it
 * happened to be -- the right territory, but not the right place, so the rep
 * had a contact card and a view of the whole Southeast. Searching the same
 * advisor from the box did pan, because openNationalAdvisor() takes these three
 * steps and openAdvisorAnywhere() took none of them. Same intent, two
 * behaviours, and the difference was invisible from the code unless you read
 * both.
 *
 * A filter can also be hiding the person we just navigated to, in which case
 * flying there would land on empty water. relaxFiltersForAdvisor() is what the
 * search path uses to clear only the filters actually in the way, and it names
 * them so the rep knows the view changed under them.
 */
function revealAdvisor(feature, label){
  const cleared = relaxFiltersForAdvisor([feature], label || "", false);
  focusedAdvisorId = String(feature.properties.id);
  focusedAdvisorLabel = label || "";
  redraw();
  flyToAdvisorGroup({ feats: [feature] });
  openAdvisorDetails(feature, false);
  return cleared;
}

async function openAdvisorAnywhere(crd, state){
  const here = ALL.find(x => String(x.properties.id) === String(crd));
  if (here) {
    // Already in scope, so no territory change -- but still move to them. A
    // rep who asked to see someone on the map means on the screen.
    const cleared = revealAdvisor(here, here.properties.n);
    // Say so if a filter had to come off. Moving the map is what was asked
    // for; quietly widening what the map shows is not, and a rep who set a
    // filter deliberately should not have to notice it went missing.
    if (cleared.length)
      showNotice(`To show ${here.properties.n}, cleared ${joinLabels(cleared)}.`);
    return true;
  }

  const want = scopeForState(state);
  if (!want || want === scope) {
    showNotice(state
      ? `That advisor is not on the map for ${esc(String(state).toUpperCase())}.`
      : "That advisor has no location on file, so there is nothing to show on the map.");
    return false;
  }
  showNotice(`Switching to ${want.startsWith("T:") ? want.slice(2) : want} to show them…`);
  try {
    await switchScope(want, true);
    const found = ALL.find(x => String(x.properties.id) === String(crd));
    if (found) {
      const label = found.properties.n;
      const cleared = revealAdvisor(found, label);
      const where = want.startsWith("T:") ? `${want.slice(2)} sales territory` : scopeLabel(want);
      showNotice(cleared.length
        ? `Switched to ${where} and cleared ${joinLabels(cleared)} to show ${label}.`
        : `Switched to ${where} to show ${label}.`);
      return true;
    }
    // Switched, and they are still not there. Say which of the two it is
    // rather than repeating "not in scope" at somebody who just watched the
    // scope change.
    showNotice("Switched to their territory, but this advisor has no mapped office there.");
  } catch (e) {
    showNotice(e.message || "That territory could not be loaded.");
  }
  return false;
}

/* panTo carries two meanings. As a FLAG it says "the caller will position the
 * map itself, so do not fit bounds"; as a VALUE it is the [lat, lon] to move
 * to. This is the value half, and it must be an actual finite pair -- handing
 * the flag `true` to map.setView() asked Leaflet to coerce a boolean into a
 * LatLng, got null, and threw "Cannot read properties of null (reading 'lat')".
 */
function panToPoint(panTo){
  return (Array.isArray(panTo) && panTo.length === 2
          && Number.isFinite(+panTo[0]) && Number.isFinite(+panTo[1]))
    ? [+panTo[0], +panTo[1]] : null;
}

async function switchScope(next, panTo){
  if (next === scope && !pendingScope){
    /* ALREADY IN THIS SCOPE -- but still go to the place.
     *
     * This returned "applied" without moving the map, and pickLoc() panned
     * entirely through this function. So searching a city or ZIP INSIDE the
     * territory already on screen did nothing: the suggestion was right, the
     * scope was right, and the map sat still. Searching Memphis from the
     * national view worked (US -> Southeast), then Atlanta did not -- both are
     * Southeast, so the second search was a no-op. A rep searching within
     * their own territory, which is most searching, got silence.
     */
    const here = panToPoint(panTo);
    if (here) map.setView(here, 11, { animate:false });
    return { status:"applied", scope:next, request:scopeRequest };
  }
  const request = ++scopeRequest;
  if (scopeController) scopeController.abort();
  cancelScheduledBackground();
  const controller = new AbortController();
  scopeController = controller;
  const transition = { scope:next, request, startedAt:performance.now() };
  PERF.signal("scope:transition-start", { scope:next, request });
  paintScopePending(next, request);
  markerBatchToken++;
  map.stop();
  if (typeof cluster._animationEnd === "function") cluster._animationEnd();

  const settleNationalAfterPaint = () => afterNextPaint(() => {
    if (!pendingScope || pendingScope.request !== request || request !== scopeRequest) return;
    const elapsedMs = performance.now() - transition.startedAt;
    PERF.add("scope:US:to national layer painted", elapsedMs);
    PERF.signal("scope:transition-settled", {
      scope:next, request, elapsedMs, kind:"national",
    });
    clearScopePending(request);
  });

  try {
    let result = { features:[], missing:[] };
    if (next !== "US"){
      result = await (next.startsWith("T:")
        ? loadTerritory(next.slice(2), controller.signal)
        : loadState(next, controller.signal));
    }
    if (request !== scopeRequest)
      return { status:"superseded", scope:next, request };

    // Destructive UI changes happen only after data has arrived. A failed
    // request therefore leaves the current map, filters, lasso, and selector.
    resetForScopeChange();
    if (OUTSIDE_CONTINENTAL.has(next) && continentalOnly){
      setContinentalOnly(false, false);
      showNotice(`Continental U.S. only was turned off to show ${scopeLabel(next)}.`);
    }
    ALL = next === "US" ? [] : result.features;
    if (next !== "US") buildAddrIndex();
    scope = next;
    scopeSel.value = next;
    const failures = failedSupportLabels();
    const notes = [];
    if (result.missing.length)
      notes.push(`Missing jurisdiction data: ${result.missing.join(", ")}.`);
    if (failures.length)
      notes.push(`Unavailable enrichment: ${failures.join(", ")}.`);
    scopeNotice = notes.join(" ");
    applyScopeUI();
    renderAll(!panTo, next === "US" ? null : transition);
    /* panTo carries TWO meanings, and conflating them broke "Show on map".
     *
     * As a flag it says "the caller will position the map itself, so do not
     * fit bounds" -- that is the `!panTo` above. As a value it is the [lat,
     * lon] to move to. Location search passes a real pair; openAdvisorAnywhere
     * passes literal `true`, because all it wants is the flag: it opens the
     * advisor immediately afterwards, and opening pans.
     *
     * map.setView(true, 11) then asked Leaflet to coerce a boolean into a
     * LatLng, got null, and threw "Cannot read properties of null (reading
     * 'lat')" -- from inside switchScope, so it surfaced as "Could not load
     * Southeast" with an empty map and zeros across the header. The territory
     * had in fact loaded; only the final pan failed, and it took the whole
     * scope switch down with it.
     *
     * So move only for an actual coordinate pair. The flag meaning still works
     * for both callers.
     */
    const target = panToPoint(panTo);
    if (target) map.setView(target, 11, { animate:false });
    // A DROPPED STATE MUST BE SAID OUT LOUD, not left in a tooltip.
    //
    // Tolerating a corrupt state file (rather than failing the whole territory)
    // trades a broken map for a quietly incomplete one, and quietly incomplete
    // is the more dangerous of the two: the rep sees a smaller advisor count,
    // every filter agrees with it, and nothing suggests Vermont is absent.
    // scopeNotice alone was not enough -- it surfaces only inside the counter's
    // info tooltip, which nobody opens.
    if (result.missing.length)
      showNotice(`${result.missing.join(", ")} could not be loaded, so those advisors `
        + `are missing from this view and from its counts. Reload to try again.`);
    if (failures.length)
      showNotice(`Some enrichment is unavailable: ${failures.join(", ")}. Affected filters remain disabled.`);
    if (next === "US") settleNationalAfterPaint();
    return { status:"applied", scope:next, request, missing:result.missing };
  } catch (err) {
    if (err && err.name === "AbortError")
      return { status:"superseded", scope:next, request };
    if (request === scopeRequest){
      scopeSel.value = scope;
      clearScopePending(request);
      // Name the reason. "Could not load Northeast. The current map was kept."
      // was true and useless: it cost a round trip through a rep, a screenshot
      // and a guess to learn that one state's file had arrived truncated. The
      // message a rep can read out loud is the one worth printing.
      const why = (err && err.message) ? ` (${err.message})` : "";
      showNotice(`Could not load ${scopeLabel(next)}${why}. The current map was kept. `
        + `If this repeats, it is usually a dropped download -- try again on a stronger connection.`);
      console.error(err);
    }
    return { status:"failed", scope:next, request, error:err };
  } finally {
    if (scopeController === controller) scopeController = null;
  }
}

scopeSel.addEventListener("change", () => switchScope(scopeSel.value));

// ---- location search: type-ahead to a state by city, ZIP, or state name ----
// Suggestions are drawn entirely from our own geocoded data (geo_index.json), so
// there is no external geocoder and no API key in the page. Street addresses are
// out of scope for that reason; city / ZIP / state cover what a rep types.
const searchBox = document.getElementById("globalSearch");
const locOut = document.getElementById("locResults");
const globalOut = document.getElementById("globalResults");
let locSug = [];

function titleCase(s){ return s.toLowerCase().replace(/\b\w/g, c => c.toUpperCase()); }
function locationScope(state){
  const territory = STATE_TO_TERRITORY[state];
  return territory ? terrKey(territory) : state;
}
function locationSub(state, detail=""){
  const territory = STATE_TO_TERRITORY[state];
  return [detail, territory ? `${territory} territory` : ""].filter(Boolean).join(" · ");
}

// Up to ten matches. A city in several states yields one row per state, ranked
// by advisor count, so "Memphis" offers TN and TX rather than guessing.
function locSuggest(q){
  const up = q.toUpperCase();
  const out = [];
  for (const [ab, nm] of Object.entries(STATE_NAMES)){
    if (ab === up || nm.toUpperCase().startsWith(up))
      out.push({ label: nm, tag: ab, sub:locationSub(ab), scopeVal:locationScope(ab) });
  }
  if (GEO && /^\d+$/.test(q)){
    for (const z in GEO.zips){
      if (!z.startsWith(q)) continue;
      const v = GEO.zips[z];
      out.push({ label: `ZIP ${z}`, tag: v[0], sub:locationSub(v[0], `${v[3].toLocaleString()} advisors`),
                 scopeVal:locationScope(v[0]), pan: [v[1], v[2]] });
      if (out.length > 20) break;
    }
  } else if (GEO && up.length >= 2){
    const ch = [];
    for (const c in GEO.cities){
      if (!c.startsWith(up)) continue;
      for (const h of GEO.cities[c]) ch.push({ name: c, st: h[0], lat: h[1], lon: h[2], n: h[3] });
    }
    ch.sort((a, b) => b.n - a.n);
    for (const h of ch.slice(0, 10))
      out.push({ label: titleCase(h.name), tag: h.st, sub:locationSub(h.st, `${h.n.toLocaleString()} advisors`),
                 scopeVal:locationScope(h.st), pan: [h.lat, h.lon] });
  }
  return out.slice(0, 10);
}

function renderLocSuggest(){
  const q = searchBox.value.trim();
  if (!q){ locOut.hidden = true; locOut.innerHTML = ""; locSug = []; return; }
  locSug = locSuggest(q);
  const geoWaiting = !GEO && SUPPORT.geo !== "failed";
  const geoStatus = !GEO && (/^\d{1,5}$/.test(q) || /^[a-z .'-]{2,}$/i.test(q))
    ? `<p class="hint">${geoWaiting
        ? "City and ZIP search is still loading; state search already works."
        : "City and ZIP search is unavailable; state search still works."}</p>`
    : "";
  locOut.hidden = false;
  if (!locSug.length && !geoStatus){
    locOut.innerHTML = ""; locOut.hidden = true;
    return;
  }
  const results = locSug.length ? `<p class="result-label">Locations</p>` + locSug.map((s, i) =>
    `<button class="lres" data-i="${i}">${s.label}<b class="lst">${s.tag}</b>` +
    `${s.sub ? `<span class="sub">${s.sub}</span>` : ""}</button>`).join("") : "";
  locOut.innerHTML = results + geoStatus;
  locOut.querySelectorAll(".lres").forEach(b =>
    b.addEventListener("click", () => pickLoc(locSug[+b.dataset.i])));
}

async function pickLoc(s){
  if (!s) return;
  const outcome = await switchScope(s.scopeVal, s.pan);
  if (!outcome || outcome.status !== "applied" || scope !== s.scopeVal) return;
  clearSearch();
  searchBox.blur();
  focusedAdvisorId = null; focusedAdvisorLabel = "";
  if (OUTSIDE_CONTINENTAL.has(s.tag) && continentalOnly){
    setContinentalOnly(false, false);
    redraw(false);
    showNotice(`Continental U.S. only was turned off to show ${s.label}, ${s.tag}.`);
  }
}


// ---- freehand geographic selection ----
const lassoControl = L.control({ position:"topleft" });
lassoControl.onAdd = () => {
  const div = L.DomUtil.create("div", "leaflet-bar lasso-control");
  div.innerHTML = `<button id="lassoBtn" type="button" title="Draw a geographic selection">Lasso</button>` +
    `<button id="clearLasso" type="button" title="Clear geographic selection" hidden>Clear</button>` +
    // PHONE ONLY, and it sits here because this is the only chrome the map keeps
    // at that width. The desktop button lives in the stats bar, and the stats
    // bar is display:none under 760px -- so bulk queueing, the single fastest
    // way to build a list, disappeared on the device most likely to want a list
    // built quickly. Same handler, same "advisors in view" predicate; CSS
    // decides which of the two is on screen so neither can drift.
    `<button id="queueVisibleMobile" type="button" class="lasso-queue"
       title="Add the advisors in view to the call list">&#43;&#9742;</button>`;
  L.DomEvent.disableClickPropagation(div);
  L.DomEvent.disableScrollPropagation(div);
  return div;
};
lassoControl.addTo(map);
let lassoPolygon = null;
let lassoLayer = null;
let lassoPreview = null;
let lassoArmed = false;
let lassoDrawing = false;
let lassoPoints = [];
const lassoBtn = document.getElementById("lassoBtn");
const clearLassoBtn = document.getElementById("clearLasso");

function setLassoArmed(on){
  lassoArmed = on;
  lassoDrawing = false;
  lassoBtn.classList.toggle("on", on);
  lassoBtn.textContent = on ? "Drag to draw" : "Lasso";
  document.getElementById("map").classList.toggle("lasso-ready", on);
  // Pinch-zoom has to go too, not just dragging. A finger tracing a lasso is
  // one touch, but the second finger of an accidental pinch would zoom the map
  // out from under a half-drawn polygon and leave the points describing an area
  // nobody selected.
  if (on) { map.dragging.disable(); map.touchZoom.disable(); }
  else { map.dragging.enable(); map.touchZoom.enable(); }
}

function clearLasso(doRedraw=true){
  setLassoArmed(false);
  lassoPolygon = null; lassoPoints = [];
  if (lassoLayer){ map.removeLayer(lassoLayer); lassoLayer = null; }
  if (lassoPreview){ map.removeLayer(lassoPreview); lassoPreview = null; }
  clearLassoBtn.hidden = true;
  if (doRedraw) redraw();
}

function pointInLasso(lat, lon){
  if (!lassoPolygon) return true;
  let inside = false;
  for (let i = 0, j = lassoPolygon.length - 1; i < lassoPolygon.length; j = i++){
    const yi = lassoPolygon[i][0], xi = lassoPolygon[i][1];
    const yj = lassoPolygon[j][0], xj = lassoPolygon[j][1];
    const crosses = ((yi > lat) !== (yj > lat)) &&
      (lon < (xj - xi) * (lat - yi) / ((yj - yi) || Number.EPSILON) + xi);
    if (crosses) inside = !inside;
  }
  return inside;
}

lassoBtn.addEventListener("click", () => {
  if (lassoArmed) clearLasso(false);
  else {
    if (lassoLayer){ map.removeLayer(lassoLayer); lassoLayer = null; lassoPolygon = null; }
    setLassoArmed(true);
  }
});
clearLassoBtn.addEventListener("click", () => clearLasso());

map.on("mousedown", e => {
  if (!lassoArmed) return;
  lassoDrawing = true; lassoPoints = [e.latlng];
  if (lassoPreview) map.removeLayer(lassoPreview);
  lassoPreview = L.polyline(lassoPoints, { color:mapAccent(), weight:2, dashArray:"5 4" }).addTo(map);
});
map.on("mousemove", e => {
  if (!lassoDrawing) return;
  const last = lassoPoints[lassoPoints.length - 1];
  if (map.latLngToContainerPoint(last).distanceTo(map.latLngToContainerPoint(e.latlng)) < 5) return;
  lassoPoints.push(e.latlng); lassoPreview.setLatLngs(lassoPoints);
});
function finishLasso(){
  if (!lassoDrawing) return;
  lassoDrawing = false;
  if (lassoPreview){ map.removeLayer(lassoPreview); lassoPreview = null; }
  if (lassoPoints.length < 3){ clearLasso(false); return; }
  lassoPolygon = lassoPoints.map(p => [p.lat, p.lng]);
  lassoLayer = L.polygon(lassoPoints, {
    color:mapAccent(), weight:2, fillColor:mapAccent(), fillOpacity:.08,
  }).addTo(map);
  setLassoArmed(false); clearLassoBtn.hidden = false;
  map.fitBounds(lassoLayer.getBounds(), { padding:[28,28], maxZoom:12, animate:false });
  redraw();
}
map.on("mouseup", finishLasso);
window.addEventListener("mouseup", finishLasso);

/* THE SAME LASSO, WITH A FINGER.
 *
 * Leaflet's `mousedown`/`mousemove`/`mouseup` map events come from DOM mouse
 * events, and a touchscreen does not produce those during a drag -- mobile
 * browsers synthesise a mousedown/mouseup pair only AFTER a tap ends, and
 * never a mousemove in between. So on a phone the lasso armed, said "Drag to
 * draw", and then recorded nothing at all: three handlers that could not fire.
 * Nothing errored, which is why it read as the feature simply not working.
 *
 * These are native touch listeners on the map container, translating each
 * touch point to a LatLng the same way Leaflet does, so both input methods
 * feed one set of lasso points and one finishLasso.
 *
 * `passive:false` IS LOAD-BEARING. Without it the browser refuses the
 * preventDefault below and scrolls the page while the finger draws.
 * preventDefault is applied only when the lasso is armed, so ordinary panning
 * and pinching are untouched.
 */
const mapEl = map.getContainer();
const touchLatLng = (t) => map.containerPointToLatLng(
  L.DomEvent.getMousePosition(t, mapEl));

mapEl.addEventListener("touchstart", (e) => {
  if (!lassoArmed || e.touches.length !== 1) return;
  e.preventDefault();
  lassoDrawing = true;
  lassoPoints = [touchLatLng(e.touches[0])];
  if (lassoPreview) map.removeLayer(lassoPreview);
  lassoPreview = L.polyline(lassoPoints, { color:mapAccent(), weight:2, dashArray:"5 4" }).addTo(map);
}, { passive:false });

mapEl.addEventListener("touchmove", (e) => {
  if (!lassoDrawing || e.touches.length !== 1) return;
  e.preventDefault();
  const ll = touchLatLng(e.touches[0]);
  const last = lassoPoints[lassoPoints.length - 1];
  // Same 5px floor as the mouse path. A fingertip is a wide, jittery cursor and
  // every stationary frame would otherwise add a duplicate vertex.
  if (map.latLngToContainerPoint(last).distanceTo(map.latLngToContainerPoint(ll)) < 5) return;
  lassoPoints.push(ll);
  lassoPreview.setLatLngs(lassoPoints);
}, { passive:false });

// touchcancel as well as touchend: an incoming call or a system gesture ends
// the touch without a touchend, and without this the map would be left
// undraggable with a half-drawn polygon and no way to finish it.
mapEl.addEventListener("touchend", finishLasso);
mapEl.addEventListener("touchcancel", finishLasso);
document.addEventListener("keydown", e => { if (e.key === "Escape" && lassoArmed) clearLasso(false); });

// ---- address index ----
// every pin at one filed address, so a popup can open the whole building's roster
// regardless of what the current filters happen to be showing
const ADDR = new Map();      // "street|city|zip" -> record
const ADDR_BY_I = [];        // same records, indexed for data- attributes
function addrKey(p){ return `${p.a}|${p.c}|${p.z}`; }

// A building is filed under many address lines. Raymond James at 1100 Ridgeway
// Loop, Memphis appears six ways -- RD / ROAD / Rd. / mixed case / "Fl 2" /
// "Suite 500" on line 2 -- and 949 Shady Grove eight ways, one of them the typo
// "Floore 2". Normalising those strings is a losing game, but they already
// share a geocoded coordinate, so the coordinate IS the building.
//
// Coordinate alone would over-merge, though: a street-centroid match can put
// 100 and 200 Main St on one point. Measured nationally, 16,851 multi-address
// coordinates carry a single house number (safe) and 169 mix house numbers
// (not). So the key is coordinate + house number, which keeps those apart.
const BLDG = new Map();
const BLDG_BY_I = [];
function houseNo(street){
  const m = /^\s*(\d+)/.exec(String(street || ""));
  return m ? m[1] : "";
}
function bldgKey(f){
  const [lon, lat] = f.geometry.coordinates;
  return `${lat.toFixed(5)},${lon.toFixed(5)}|${houseNo(f.properties.a)}`;
}

function buildAddrIndex(){
  // must reset: switching state reloads ALL, and a stale index would both mix
  // the previous state's addresses into rosters and invalidate the data-a
  // indices that popup buttons carry
  ADDR.clear();
  ADDR_BY_I.length = 0;
  BLDG.clear();
  BLDG_BY_I.length = 0;
  ALL.forEach(f => {
    const p = f.properties, [lon, lat] = f.geometry.coordinates;
    const k = addrKey(p);
    let a = ADDR.get(k);
    if (!a){
      a = { i: ADDR_BY_I.length, addr: p.a, city: p.c, zip: p.z, lat, lon, feats: [], ids: new Set() };
      ADDR.set(k, a); ADDR_BY_I.push(a);
    }
    a.feats.push(f);
    a.ids.add(p.id);

    const bk = bldgKey(f);
    let b = BLDG.get(bk);
    if (!b){
      b = { i: BLDG_BY_I.length, city: p.c, zip: p.z, lat, lon,
            feats: [], ids: new Set(), lines: new Map(), bldg: true };
      BLDG.set(bk, b); BLDG_BY_I.push(b);
    }
    b.feats.push(f);
    b.ids.add(p.id);
    b.lines.set(p.a, (b.lines.get(p.a) || 0) + 1);
  });
  // label each building with its most-filed address line
  BLDG_BY_I.forEach(b => {
    b.addr = [...b.lines.entries()].sort((x, y) => y[1] - x[1])[0][0];
  });
}
function esc(s){
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// ---- predicates ----
function bandOf(x){ return x < 10 ? "lt10" : x < 20 ? "10to20" : "gt20"; }
function passesExp(p){
  if (!expSel.size) return true;             // nothing selected = no constraint
  if (p.x == null) return false;             // unknown tenure drops out when filtering
  return expSel.has(bandOf(p.x));
}
function reachOf(n){ return n <= 1 ? "one" : n <= 4 ? "few" : "many"; }
function passesReach(p){
  if (!reachSel.size) return true;
  if (p.ns == null) return false;
  return reachSel.has(reachOf(p.ns));
}
function passesQuality(p){
  return !geoSel.size || geoSel.has(p.gp);
}
// Owner or officer of the firm they are filed at. Schedule A was collected only
// for firms reporting 5.G(7), so this narrows rather than answers: an owner at a
// firm outside that scope is a miss we cannot detect. Those firms do not hire
// outside managers, which is why they were out of scope, so the blind spot sits
// where it matters least -- but it is a blind spot, hence the note in the UI.
function passesOwner(p){
  if (!ownerOnly) return true;
  if (SUPPORT.owner !== "ready") return true;
  return ownerRolesFor(p.id).some(r => String(r.firmCrd) === String(p.fc));
}
// Barron's and Forbes together, deliberately. From a filtering point of view
// the useful fact is that somebody is a sizable, recognised advisor -- not
// which magazine said so. Keeping two toggles would make a rep run the same
// search twice and union it by hand.
//
// Unlike every other advisor filter this one is not firm-specific: a ranking
// follows the person, so it passes on any pin for that CRD.
function passesRanked(p){
  if (!rankedOnly) return true;
  if (SUPPORT.barrons !== "ready" || SUPPORT.forbes !== "ready") return true;
  return barronsFor(p.id).length > 0 || forbesFor(p.id).length > 0;
}
// Reachable means a rep can actually act: an email or a phone. A contact
// record with neither is a name, and filtering to "has contact data" must not
// return names.
function passesContactable(p){
  if (!contactableOnly) return true;
  // The switch cannot be turned on before contacts.json lands (see
  // syncContactSwitches), so this is a belt-and-braces guard: if it somehow
  // is on, pass everything rather than reporting an empty state as fact.
  if (!CONTACTS_READY) return true;
  const c = contactFor(p.id);
  return !!(c && (c.e || c.w || c.c));
}
// Assets are on the TEAM or on the individual; either counts. Deliberately not
// "team assets only" -- 2,062 advisors carry their own book with no team, and
// excluding them would hide real money on a filter that says "has assets".
function passesHasAssets(p){
  if (!assetsOnly) return true;
  if (!CONTACTS_READY) return true;
  const c = contactFor(p.id);
  if (!c) return false;
  return !!(c.tm && CONTACTS.teams && CONTACTS.teams[c.tm]) || c.ia > 0;
}
function passesContinental(p){
  return !continentalOnly || !OUTSIDE_CONTINENTAL.has(p._state);
}
function passesGeography(p){
  return pointInLasso(p._lat, p._lon);
}
function passesRaum(p){
  if (!AUM_BOUNDS.length) return true;
  if (p.ra == null) return false;               // unknown RAUM drops out when bounded
  return inAumBands(p.ra * 1e6);
}
function passesTargeting(p){
  if (selectsOnly && p.sg !== 1) return false;   // 5.G(7): hires outside managers
  return passesRaum(p);                           // unknown RAUM drops out when bounded
}
function passesBase(p){
  // Exclusion belongs HERE, not in passesFilters: the firm list, the KPI counts
  // and the markers are all built from passesBase, so an excluded firm put only
  // in passesFilters stayed in the list and the counts never moved.
  if (excludedFirms.has(String(p.fc))) return false;
  if (reg === "dual" && p.d !== 1) return false;
  if (reg === "ria"  && p.d !== 0) return false;
  return passesContinental(p) && passesTargeting(p) && passesExp(p) && passesReach(p) && passesQuality(p) &&
    passesOwner(p) && passesRanked(p) && passesGeography(p) &&
    passesContactable(p) && passesHasAssets(p);
}
function passesFilters(p){
  if (!passesBase(p)) return false;
  if (selectedFirms.length && !selectedFirms.includes(p.fc)) return false;
  // focusedAdvisorId deliberately does NOT filter. Treating it as one deleted
  // everybody else from the dataset, so a building holding 14 Sargent advisors
  // rebuilt itself from a single pin and reported "1 advisor, 1 firm" -- the
  // context that makes the person meaningful. Focus is emphasis: the spider in
  // redrawSpokes draws building -> firm -> person, and each hop keeps its own
  // real count.
  return true;
}
function colorFor(p){
  if (selectedFirms.length) return firmColor[p.fc] || mapMuted();
  return mapAccent();
}

// ---- national layer ----
// The heatmap first rolls firm-offices into screen-space cells. Rendering every
// raw point makes dense eastern markets saturate into one colour at national
// zoom; robust per-viewport scaling keeps regional differences visible.
const natLayer = L.layerGroup();
let natHeatStats = { bins:0, low:0, high:0 };

function natFirm(o){ return NAT.firms[o[3]]; }
function natFirmName(o){ return natFirm(o)[0]; }
function natFirmCrd(o){ return String(natFirm(o)[1]); }
function natFirmScore(o){ return natFirm(o)[2]; }
function natFirmRaum(o){ return natFirm(o)[3]; } // compact millions, matching state pins
function natFirmSelects(o){ return natFirm(o)[4] === 1; }
function nationalOfficesForFirm(crd){
  crd = String(crd);
  return NAT ? NAT.offices.filter(o => natFirmCrd(o) === crd) : [];
}
function firmDestinations(crd){
  const grouped = new Map();
  nationalOfficesForFirm(crd).forEach(o => {
    const state = NAT.states[o[6]];
    const territory = STATE_TO_TERRITORY[state];
    const target = territory ? terrKey(territory) : state;
    const row = grouped.get(target) || {
      scope:target, label:territory || scopeLabel(state), placements:0, offices:0, states:new Set(),
    };
    row.placements += o[2]; row.offices += 1; row.states.add(state); grouped.set(target, row);
  });
  return [...grouped.values()].map(row => ({ ...row, states:[...row.states].sort() }))
    .sort((a, b) => b.placements - a.placements || a.label.localeCompare(b.label));
}
function setSelectedFirm(crd){
  crd = String(crd);
  selectedFirms = [crd]; syncFirmColors();
  document.getElementById("clearFirms").hidden = false;
}
function syncFirmColors(){
  firmColor = {};
  selectedFirms.forEach((crd, index) => { firmColor[crd] = COMPARE[index % COMPARE.length]; });
}
function quantile(sorted, q){
  if (!sorted.length) return 0;
  const i = Math.min(sorted.length - 1, Math.max(0, Math.floor((sorted.length - 1) * q)));
  return sorted[i];
}

function nationalHeatPoints(offices){
  // Approximately one sample per heat radius. Each cell's value is simply the
  // number of advisor placements in it -- the heat map answers "where are the
  // advisors", nothing else. It used to weight each office by its firm's
  // opportunity score, which quietly let a scoring model decide where the
  // country looked hot.
  const cellPx = map.getZoom() <= 4 ? 18 : 14;
  const bins = new Map();
  for (const office of offices){
    const point = map.latLngToContainerPoint([office[1], office[0]]);
    const key = `${Math.floor(point.x / cellPx)}:${Math.floor(point.y / cellPx)}`;
    const value = office[2];
    const bin = bins.get(key) || { value:0, lat:0, lon:0 };
    bin.value += value;
    bin.lat += office[1] * value;
    bin.lon += office[0] * value;
    bins.set(key, bin);
  }
  const rows = [...bins.values()].map(bin => ({
    ...bin, scaled: Math.log1p(bin.value),
  }));
  const distribution = rows.map(row => row.scaled).sort((a, b) => a - b);
  const low = quantile(distribution, .12);
  const high = quantile(distribution, .97);
  const span = Math.max(.001, high - low);
  natHeatStats = { bins:rows.length, low, high };
  return rows.map(row => {
    const relative = Math.max(0, Math.min(1, (row.scaled - low) / span));
    // A nonlinear lift preserves low/mid-market variation without allowing a
    // handful of large platform offices to flatten the rest of the country.
    const intensity = .08 + .92 * Math.pow(relative, .8);
    return [row.lat / row.value, row.lon / row.value, intensity];
  });
}

function natPassesBase(o){
  if (excludedFirms.has(String(natFirmCrd(o)))) return false;
  if (continentalOnly && OUTSIDE_CONTINENTAL.has(NAT.states[o[6]])) return false;
  if (selectsOnly && !natFirmSelects(o)) return false;
  if (AUM_BOUNDS.length){
    const millions = natFirmRaum(o);
    if (millions == null || !inAumBands(millions * 1e6)) return false;
  }
  return pointInLasso(o[1], o[0]);
}
function natPasses(o){
  if (excludedFirms.has(String(natFirmCrd(o)))) return false;
  return natPassesBase(o) &&
    (!selectedFirms.length || selectedFirms.includes(natFirmCrd(o)));
}

function natInView(){
  const b = map.getBounds();
  const out = [];
  for (const o of NAT.offices){
    if (!b.contains([o[1], o[0]])) continue;
    if (!natPasses(o)) continue;
    out.push(o);
  }
  return out;
}

let natTruncated = 0;
function renderNational(fit){
  if (fit === "national"){
    if (continentalOnly) map.setView([39.5, -96.5], 4, { animate: false });
    else map.fitBounds([[17, -178], [72, -64]], { padding:[12,12], maxZoom:3, animate:false });
  }
  cluster.clearLayers();
  if (map.hasLayer(cluster)) map.removeLayer(cluster);
  if (!map.hasLayer(natLayer)) map.addLayer(natLayer);
  natLayer.clearLayers();

  /* GRID MODE: the office array has not arrived yet.
   *
   * Draws the same heat map from compact per-state cells instead of individual
   * offices. No clickable dots, because a cell is not a building and offering a
   * click that opens "a quarter-degree square" would be worse than offering
   * none. loadNationalDetail() re-renders through here the moment it lands.
   *
   * A rep who selects a firm in this window needs the real offices -- a grid
   * carries no firm identity -- so the fetch is pulled forward rather than
   * showing them an unfiltered map that looks like an answer.
  */
  if (!NAT_DETAIL_READY){
    const needsOfficeDetail = selectedFirms.length || excludedFirms.size || raumActive();
    if (needsOfficeDetail){
      NATIONAL_DETAIL_REASON = NAT_DETAIL_ERROR
        ? "Office detail is unavailable, so firm and AUM filters cannot be applied."
        : "Loading office detail to apply firm and AUM filters.";
      // A move/zoom redraw must not become an automatic retry loop after a
      // recorded failure. Deliberate firm/profile actions call the retryable
      // loader themselves.
      if (!NAT_DETAIL_ERROR) loadNationalDetail(null, false);
      // Retain the prior heat points. The compact grid cannot apply firm
      // identity, exclusions, or AUM bands honestly.
      if (!map.hasLayer(heatLayer)) heatLayer.addTo(map);
      return;
    }
    if (!map.hasLayer(heatLayer)) heatLayer.addTo(map);
    const bounds = map.getBounds();
    /* Through nationalHeatPoints(), NOT straight into the layer.
     *
     * Feeding it raw counts painted the whole country solid red: the layer is
     * configured `max: 2.2`, while a grid cell holds hundreds of placements, so
     * every cell pinned to maximum intensity and the map lost all variation.
     *
     * That function is where the scaling lives -- pixel binning, log1p, then a
     * 12th-to-97th percentile stretch. Reusing it is also what makes the grid
     * phase and the detail phase look like the SAME MAP rather than two, so the
     * upgrade a few seconds later is a sharpening rather than a redraw.
     *
     * It reads [lon, lat, value]. Grid cells store latitude, longitude, all
     * placements, selecting-manager placements, and state index.
     */
    const offices = NAT.grid
      .filter(cell => {
        const state = NAT.states[cell[4]];
        const weight = selectsOnly ? cell[3] : cell[2];
        return weight > 0 && (!continentalOnly || !OUTSIDE_CONTINENTAL.has(state)) &&
          bounds.contains([cell[0], cell[1]]) && pointInLasso(cell[0], cell[1]);
      })
      .map(cell => [cell[1], cell[0], selectsOnly ? cell[3] : cell[2]]);
    heatLayer.setLatLngs(nationalHeatPoints(offices));
    natTruncated = 0;
    return;
  }

  const focused = selectedFirms.length
    ? NAT.offices.filter(o => natPasses(o)) : null;
  if (fit && focused?.length){
    const pts = focused.map(o => [o[1], o[0]]);
    if (pts.length === 1) map.setView(pts[0], 11, { animate:false });
    else map.fitBounds(pts, { padding:[36,36], maxZoom:11, animate:false });
  }
  const allVis = focused || natInView();
  let vis;
  if (focused){
    natTruncated = 0;
    removeHeatLayer();
    vis = allVis;
  } else if (natViz === "heat"){
    natTruncated = 0;
    if (!map.hasLayer(heatLayer)) heatLayer.addTo(map);
    heatLayer.setLatLngs(nationalHeatPoints(allVis));
    // clickable dots on top of the heat: the biggest offices in view, by
    // advisor placements alone
    vis = allVis.slice().sort((a, b) => b[2] - a[2]).slice(0, HEAT_HIT_CAP);
  } else {
    removeHeatLayer();
    natTruncated = Math.max(0, allVis.length - NAT_CAP);
    vis = natTruncated
      ? allVis.slice().sort((a, b) => b[2] - a[2]).slice(0, NAT_CAP)
      : allVis;
  }

  // One dot per BUILDING, matching what a dot means in a state or territory.
  // The records are still per firm-office, so a multi-firm building used to draw
  // several circles on one point with only the topmost clickable -- the same
  // stacking that made the old office layer draw ~4,500 invisible markers.
  const byBuilding = new Map();
  vis.forEach(o => {
    const id = o[7];
    let group = byBuilding.get(id);
    if (!group){ group = { rows: [], advisors: 0 }; byBuilding.set(id, group); }
    group.rows.push(o);
    group.advisors += o[2];
  });
  let onlyMarker = null;
  byBuilding.forEach(group => {
    const rows = group.rows, lead = rows[0], n = group.advisors;
    const present = selectedFirms.filter(crd => rows.some(o => natFirmCrd(o) === crd));
    const col = selectedFirms.length
      ? (firmColor[present[0]] || mapMuted())
      : mapAccent();
    const mk = L.circleMarker([lead[1], lead[0]], {
      radius: focused ? Math.max(8, Math.min(18, 6 + 2 * Math.sqrt(n)))
                      : (natViz === "heat" ? 4 : Math.max(3, Math.min(22, 2.6 * Math.sqrt(n)))),
      weight: focused ? 2 : 1, color: "#fff", opacity: focused ? 1 : (natViz === "heat" ? .45 : .75),
      fillColor: col, fillOpacity: focused ? .9 : (natViz === "heat" ? .32 : .8),
      bubblingMouseEvents:false,
    });
    // The whole building, not just the topmost record. The panel's own header
    // says "N firms at this physical office" -- passing `lead` alone made it
    // say six and then list one.
    mk.on("click", () => { markMapSelection(mk); openNationalLocation(lead, true, rows); });
    natLayer.addLayer(mk);
    if (detailsCurrent?.type === "national-location" && rows.includes(detailsCurrent.office))
      markMapSelection(mk);
    if (focused?.length === 1) onlyMarker = mk;
  });
  // Fitting the map emits moveend, which renders this layer again. Reopen a
  // sole focused office after either pass so the destination remains obvious
  // and immediately actionable instead of becoming an unlabeled dot.
}

// One address in Manhattan holds 125 firm-office records. Listing every one
// turns the panel into a directory nobody reads; the biggest are the ones a rep
// would call, and the count is stated either way.
const NAT_LOCATION_CAP = 25;

function openNationalLocation(o, detailPush=true, group=null){
  beginDetails({ type:"national-location", office:o }, detailPush);
  openFirmCrd = null;
  const state = NAT.states[o[6]];
  // Every firm-office record at this address, biggest first. A building where
  // six firms share a door is six conversations, and the panel used to name
  // only whichever record happened to sort first.
  // Restoring from a hash or the back button arrives with one record and no
  // group, so the building is rebuilt from its id rather than the panel
  // quietly reporting a six-firm address as a one-firm address. One pass over
  // the national array, only on opening a panel.
  const rows = (group && group.length
    ? group
    : NAT.offices.filter(row => row[7] === o[7])
  ).slice().sort((a, b) => b[2] - a[2]);
  const placements = rows.reduce((sum, r) => sum + r[2], 0);
  document.getElementById("detailsKind").textContent = "Location";
  document.getElementById("firmOverviewName").textContent = `${STATE_NAMES[state] || state} firm office`;
  document.getElementById("firmOverviewMeta").textContent =
    `${placements.toLocaleString()} advisor placement${placements === 1 ? "" : "s"} · `
    + `${rows.length} firm${rows.length === 1 ? "" : "s"} at this physical office`;
  document.getElementById("firmOverviewBody").innerHTML = `
    <section class="profile-section"><h3>Firm${rows.length === 1 ? "" : "s here"}</h3><div class="detail-list">
      ${rows.slice(0, NAT_LOCATION_CAP).map(row => { const f = natFirm(row); return `
      <button type="button" class="detail-row" data-firm-profile="${esc(f[1])}">
        <span class="detail-row-main"><b>${esc(f[0])}</b><small>CRD ${esc(f[1])}${
          f[3] != null ? ` · ${fmtMoney(f[3] * 1e6)} AUM` : ""} · ${
          row[2].toLocaleString()} placement${row[2] === 1 ? "" : "s"}</small></span>
        <span class="detail-chevron">›</span></button>`; }).join("")}
      ${rows.length > NAT_LOCATION_CAP
        ? `<p class="profile-note">and ${(rows.length - NAT_LOCATION_CAP).toLocaleString()} more
             firm${rows.length - NAT_LOCATION_CAP === 1 ? "" : "s"} at this address.</p>` : ""}
      </div></section>
    <div class="profile-actions"><button type="button" data-open="${esc(locationScope(state))}">Open ${esc(scopeLabel(locationScope(state)))} for advisor detail</button>
      <a href="${firmIapdUrl(natFirm(rows[0])[1])}" target="_blank" rel="noopener">${
        rows.length === 1 ? "Firm IAPD" : "Largest firm's IAPD"} ↗</a></div>
    <p class="profile-note">The lightweight national layer identifies the exact firm-office record but does not carry filed street addresses or advisor identities. Open the territory for detailed navigation.</p>`;
  syncDetailHash(null);
}


document.addEventListener("click", async e => {
  const b = e.target.closest("[data-open]");
  if (!b) return;
  const target = b.dataset.open;
  const entry = detailsCurrent;
  const outcome = await switchScope(target);
  if (!outcome || outcome.status !== "applied" || scope !== target) return;
  if (detailsCurrent === entry && entry?.type === "national-location") closeDetails();
});

// ---- markers ----
// ---- building layer ----
// One marker per BUILDING, clustered. This replaced a pair of views -- advisor
// pins and filed-address bubbles -- that answered different questions with
// different units and disagreed with each other and with the panel.
//
// Buildings rather than filed addresses because the filed strings are noisy:
// Raymond James at 1100 Ridgeway Loop is filed six ways, 949 Shady Grove eight,
// one of them the typo "Floore 2". Measured on the West territory, 6,475 of
// 16,011 address markers sat on only 1,956 distinct points -- roughly 4,500
// markers drawn exactly on top of each other, unclickable at any zoom, with the
// worst point carrying 35. The coordinate IS the building.
//
// Clustered rather than drawn flat because MarkerCluster culls to the viewport:
// the old flat office layer built a DOM node for every address in scope
// regardless of what was on screen (16,011 nodes, 1,662 ms for West) where
// clustering holds a dozen.
function buildingsFor(feats){
  const out = new Map();
  for (const f of feats){
    const p = f.properties, key = bldgKey(f);
    let b = out.get(key);
    if (!b){
      const [lon, lat] = f.geometry.coordinates;
      b = { key, lat, lon, ids: new Set(), uncertain: 0, firms: new Map(), addr: p.a,
            city: p.c, zip: p.z, lines: new Map() };
      out.set(key, b);
    }
    b.ids.add(p.id);
    if (p.unc) b.uncertain = (b.uncertain || 0) + 1;
    if (!b.firms.has(p.fc)) b.firms.set(p.fc, { name: p.f, ids: new Set() });
    b.firms.get(p.fc).ids.add(p.id);
    b.lines.set(p.a, (b.lines.get(p.a) || 0) + 1);
  }
  // label each building by its most-filed address line, as BLDG does
  for (const b of out.values())
    b.addr = [...b.lines.entries()].sort((x, y) => y[1] - x[1])[0][0] || b.addr;
  return [...out.values()];
}

// Firm colour survives the merge. A building holding several selected firms is
// a conic gradient of their colours, which is now also what the firm spokes use.
// A split mark must divide by HEADCOUNT, not by how many firms happen to be
// present. The previous version drew equal wedges per distinct firm, so a
// building holding 263 Merrill advisors and 1 Morgan Stanley rendered as a
// tidy 50/50 -- an even split asserted where the real ratio is 263:1. That is
// worse than not splitting, because it looks like data.
const SLICE_MIN = 0.03;      // a firm below this still gets a visible sliver

// How many slices a circle of a given size can carry legibly. Cluster sizing is
// cubed-logarithmic, so a 56-advisor cluster is only 24px and a flat 30px floor
// suppressed splitting almost everywhere. A small circle can still say "roughly
// half and half"; it cannot say "four firms in these proportions".
function sliceCap(diameter){
  if (diameter < 22) return 1;
  if (diameter < 34) return 2;
  return 4;
}

// [[crd, advisors], ...] -> conic-gradient, or a solid colour when splitting
// would not communicate anything.
function mixBackground(mix, diameter){
  const entries = [...mix.entries()]
    .filter(([crd]) => firmColor[crd])
    .sort((x, y) => y[1] - x[1]);
  if (!entries.length) return selectedFirms.length ? mapMuted() : mapAccent();
  if (entries.length === 1) return firmColor[entries[0][0]];
  const cap = sliceCap(diameter);
  if (cap < 2) return firmColor[entries[0][0]];

  const slices = entries.slice(0, cap);
  const rest = entries.slice(cap).reduce((sum, e) => sum + e[1], 0);
  let parts = slices.map(([crd, n]) => ({ color: firmColor[crd], n }));
  if (rest) parts.push({ color: mapMuted(), n: rest });

  // Give every slice a perceptible minimum, then rescale the remainder so the
  // arcs still sum to a full turn. A 1-in-264 firm should be a sliver, not
  // invisible -- but the leader must still read as the leader.
  const total = parts.reduce((sum, p) => sum + p.n, 0) || 1;
  let shares = parts.map(p => p.n / total);
  const lifted = shares.map(v => Math.max(v, SLICE_MIN));
  const excess = lifted.reduce((a, v) => a + v, 0) - 1;
  if (excess > 0){
    const spare = lifted.reduce((a, v, i) => a + (v > SLICE_MIN ? v - SLICE_MIN : 0), 0);
    shares = lifted.map(v => v > SLICE_MIN ? v - excess * ((v - SLICE_MIN) / (spare || 1)) : v);
  } else {
    shares = lifted;
  }
  let at = 0;
  const stops = parts.map((p, i) => {
    const from = at * 100;
    at += shares[i];
    return `${p.color} ${from.toFixed(2)}% ${(at * 100).toFixed(2)}%`;
  });
  return `conic-gradient(${stops.join(",")})`;
}

// firm -> advisors, for one building or across a cluster's children
function firmMix(buildings){
  const mix = new Map();
  for (const b of buildings)
    b.firms.forEach((firm, crd) => mix.set(crd, (mix.get(crd) || 0) + firm.ids.size));
  return mix;
}

function mixTooltip(mix){
  return [...mix.entries()]
    .filter(([crd]) => firmColor[crd])
    .sort((x, y) => y[1] - x[1]).slice(0, 6)
    .map(([crd, n]) => `${firmLabelForCrd(crd)}: ${n.toLocaleString()}`)
    .join(" · ");
}

function buildingBackground(b, diameter){
  if (!selectedFirms.length) return mapAccent();
  return mixBackground(firmMix([b]), diameter == null ? 999 : diameter);
}

function buildingMarker(b){
  const n = b.ids.size;
  const d = markDiameter(n);
  const multi = b.firms.size > 1 ? ` <i class="office-multi" title="${b.firms.size} firms here"></i>` : "";
  const unsure = b.uncertain / Math.max(1, b.ids.size);
  // a 60px pie cannot be read precisely at any split, so the numbers travel too
  const breakdown = selectedFirms.length ? mixTooltip(firmMix([b])) : "";
  const mk = L.marker([b.lat, b.lon], {
    icon: L.divIcon({
      className: "office-wrap",
      html: `<div class="office${unsure > 0.5 ? " office-unsure" : ""}" ` +
            `title="${esc(breakdown || (unsure > 0.5 ? `${b.uncertain} of ${n} filed here have an employment record elsewhere` : ""))}" ` +
            `style="width:${d}px;height:${d}px;background:${buildingBackground(b, d)};` +
            `font-size:${markFont(d, n.toLocaleString())}px">${n.toLocaleString()}${multi}</div>`,
      iconSize: [d, d], iconAnchor: [d / 2, d / 2],
    }),
    zIndexOffset: n,
    advisorCount: n,          // read by the cluster label
    building: b,
  });
  mk.on("click", () => {
    markMapSelection(mk);
    selectBuilding(b);
    const record = BLDG.get(b.key) || ADDR.get(`${b.addr}|${b.city}|${b.zip}`);
    if (record) openRoster(record);
  });
  return mk;
}

// ---- firm spokes and the focused-advisor spider ----
// Only 10% of buildings hold more than one firm, but those hold roughly half of
// all advisors, so the ambiguity is rare and concentrated. Expanded on
// selection rather than always, so dense metros stay calm; capped because the
// worst measured building holds 15 firms and the cap should not depend on the
// data staying tame.
const SPOKE_CAP = 8;
const SPOKE_PX = 58;              // screen-space, so length is zoom-invariant
const spokeLayer = L.layerGroup();
let selectedBuilding = null;

function buildingByKey(key){
  const mk = BUILDING_MARKERS.get(key);
  return mk ? mk.options.building : null;
}
// the building a roster record or an advisor sits in, by the same key the
// renderer uses (coordinate + house number)
function buildingForRecord(rec){
  if (!rec || rec.lat == null) return null;
  return buildingByKey(`${rec.lat.toFixed(5)},${rec.lon.toFixed(5)}|${houseNo(rec.addr)}`);
}

function selectBuilding(b){
  selectedBuilding = b;
  redrawSpokes();
}

function clearSpokes(){
  selectedBuilding = null;
  spokeLayer.clearLayers();
  if (map.hasLayer(spokeLayer)) map.removeLayer(spokeLayer);
}

// screen-space offset so spokes keep their length and fan as the map zooms
function offsetLatLng(lat, lon, angle, distance){
  const origin = map.latLngToLayerPoint([lat, lon]);
  return map.layerPointToLatLng(
    L.point(origin.x + Math.cos(angle) * distance, origin.y + Math.sin(angle) * distance));
}

function redrawSpokes(){
  spokeLayer.clearLayers();
  const b = selectedBuilding;
  if (!b){ if (map.hasLayer(spokeLayer)) map.removeLayer(spokeLayer); return; }
  if (!map.hasLayer(spokeLayer)) map.addLayer(spokeLayer);

  // When one advisor is focused, collapse to their firm alone. Otherwise a
  // person at a fifteen-firm tower hangs off one of fifteen spokes and the eye
  // cannot find the subject.
  const focusedFirm = focusedAdvisorId
    ? [...b.firms.entries()].find(([, v]) => v.ids.has(focusedAdvisorId))
    : null;
  let entries = focusedFirm ? [focusedFirm]
    : [...b.firms.entries()].sort((x, y) => y[1].ids.size - x[1].ids.size);
  // A one-firm building would draw a spoke carrying the same number as the
  // building it came from -- 14 -> 14. Hang the advisor straight off the
  // building instead and keep the two-hop chain for genuinely mixed buildings.
  const soloFirm = focusedFirm && b.firms.size === 1;
  if (entries.length < 2 && !focusedFirm) return;   // nothing to disambiguate
  const hidden = Math.max(0, entries.length - SPOKE_CAP);
  entries = entries.slice(0, SPOKE_CAP);

  const spread = Math.PI * 1.55;
  const step = entries.length > 1 ? spread / (entries.length - 1) : 0;
  const first = -Math.PI / 2 - spread / 2;
  entries.forEach(([crd, firm], i) => {
    const angle = entries.length > 1 ? first + step * i : -Math.PI / 2;
    const end = soloFirm ? L.latLng(b.lat, b.lon)
                         : offsetLatLng(b.lat, b.lon, angle, SPOKE_PX);
    const color = firmColor[crd] || (selectedFirms.length ? mapMuted() : mapAccent());
    if (!soloFirm) spokeLayer.addLayer(L.polyline([[b.lat, b.lon], end],
      { color, weight: 2, opacity: .85, interactive: false }));
    const n = firm.ids.size;
    const marker = soloFirm ? null : L.circleMarker(end, {
      radius: Math.max(7, Math.min(17, 5 + Math.sqrt(n) * 2.6)),
      weight: 2, color: "#fff", opacity: .95, fillColor: color, fillOpacity: .92,
      bubblingMouseEvents: false,
    });
    if (marker){
      marker.bindTooltip(`${firm.name} · ${n.toLocaleString()} advisor${n === 1 ? "" : "s"} here`,
                         { direction: "top", offset: [0, -6] });
      // the useful click is "show me these people", not the national firm record
      marker.on("click", () => openRoster(BLDG.get(b.key), true, crd));
      spokeLayer.addLayer(marker);
    }

    // second hop: the focused advisor, off their own firm's spoke
    if (focusedFirm && focusedAdvisorId){
      const from = soloFirm ? L.latLng(b.lat, b.lon) : end;
      const tip = offsetLatLng(from.lat, from.lng, angle, SPOKE_PX * (soloFirm ? 1 : 0.72));
      spokeLayer.addLayer(L.polyline([from, tip],
        { color, weight: 2, opacity: .85, dashArray: "4 3", interactive: false }));
      const dot = L.circleMarker(tip, {
        radius: 8, weight: 3, color: "#fff", opacity: 1,
        fillColor: color, fillOpacity: 1, bubblingMouseEvents: false,
      });
      dot.bindTooltip(focusedAdvisorLabel || "Selected advisor",
                      { direction: "top", offset: [0, -6], permanent: true, className: "spoke-label" });
      dot.on("click", () => {
        const f = ALL.find(x => String(x.properties.id) === String(focusedAdvisorId));
        if (f) openAdvisorDetails(f);
      });
      spokeLayer.addLayer(dot);
    }
  });
  if (hidden){
    const end = offsetLatLng(b.lat, b.lon, Math.PI / 2, SPOKE_PX);
    const more = L.circleMarker(end, { radius: 9, weight: 2, color: "#fff",
      fillColor: mapMuted(), fillOpacity: .9, bubblingMouseEvents: false });
    more.bindTooltip(`${hidden} more firm${hidden === 1 ? "" : "s"} here`, { direction: "top" });
    more.on("click", () => {
      const record = BLDG.get(b.key);
      if (record) openRoster(record);
    });
    spokeLayer.addLayer(more);
  }
}

map.on("zoomend moveend", () => { if (selectedBuilding) redrawSpokes(); });

function renderMarkers(fit, transition=null){
  const _t0 = performance.now();
  const feats = ALL.filter(f => passesFilters(f.properties));
  const buildings = buildingsFor(feats);
  // Filtering and building aggregation, separately from the clustering below:
  // if this half is cheap and the next is not, the fix is Leaflet, not data.
  PERF.spans.push([`renderMarkers:aggregate (${feats.length.toLocaleString()} pins ` +
                   `-> ${buildings.length.toLocaleString()} buildings)`,
                   performance.now() - _t0]);

  markerBatchToken++;
  if (map.hasLayer(cluster)) map.removeLayer(cluster);
  cluster.clearLayers();
  cluster = createClusterLayer();
  natLayer.clearLayers();
  if (map.hasLayer(natLayer)) map.removeLayer(natLayer);
  removeHeatLayer();
  if (!map.hasLayer(cluster)) map.addLayer(cluster);

  BUILDING_MARKERS.clear();
  const markers = buildings.map(b => {
    const mk = buildingMarker(b);
    BUILDING_MARKERS.set(b.key, mk);
    return mk;
  });
  addMarkerBatches(cluster, markers, markerBatchToken, 0, transition);
  redrawSpokes();

  if (fit && buildings.length){
    // padding must stay small relative to the container -- a narrow panel-width
    // map cannot absorb 60px a side and silently fails to fit every point
    const sz = map.getSize();
    const pad = Math.max(8, Math.min(50, Math.floor(Math.min(sz.x, sz.y) * 0.08)));
    map.fitBounds(buildings.map(b => [b.lat, b.lon]), { padding: [pad, pad], maxZoom: 14 });
  }
}

// ---- panel follows the viewport ----
function visibleFeatures(){
  const b = map.getBounds();
  return ALL.filter(f => {
    if (!passesBase(f.properties)) return false;
    const [lon, lat] = f.geometry.coordinates;
    return b.contains([lat, lon]);
  });
}

// distinct advisors, not pins — one advisor can sit at several branch offices
function countAdvisors(feats){
  const s = new Set();
  feats.forEach(f => s.add(f.properties.id));
  return s.size;
}

function estimateAumFromAdvisors(feats){
  const byFirm = new Map();
  feats.forEach(feature => {
    const crd = String(feature.properties.fc);
    if (!byFirm.has(crd)) byFirm.set(crd, new Set());
    byFirm.get(crd).add(String(feature.properties.id));
  });
  return estimateAum(byFirm, false);
}

function estimateAumFromPlacements(offices){
  const byFirm = new Map();
  offices.forEach(office => {
    const crd = natFirmCrd(office);
    byFirm.set(crd, (byFirm.get(crd) || 0) + office[2]);
  });
  return estimateAum(byFirm, true);
}

function estimateAum(byFirm, placements){
  let aum = 0, equities = 0, funds = 0, aumCoverage = 0, equityCoverage = 0, fundCoverage = 0;
  byFirm.forEach((visible, crd) => {
    const firm = NAT_FIRM_BY_CRD.get(String(crd));
    if (!firm) return;
    const denominator = placements ? NAT_PLACEMENTS_BY_CRD.get(String(crd)) : firm[7];
    if (!denominator) return;
    const numerator = placements ? visible : visible.size;
    const share = Math.min(1, numerator / denominator);
    if (Number.isFinite(firm[3])) { aum += firm[3] * 1e6 * share; aumCoverage++; }
    if (Number.isFinite(firm[5])) { equities += firm[5] * 1e6 * share; equityCoverage++; }
    if (Number.isFinite(firm[6])) { funds += firm[6] * 1e6 * share; fundCoverage++; }
  });
  return { aum, equities, funds, aumCoverage, equityCoverage, fundCoverage, firms:byFirm.size, placements };
}

// EIC's own book across the advisors currently in view.
//
// EACH ACCOUNT IS COUNTED ONCE, via a set of account indices rather than by
// adding up per-advisor totals. 40% of accounts have several holders, so the
// naive sum would not merely overstate the book by ~39% -- it would CHANGE AS
// THE MAP PANNED, because a second team-mate scrolling into view would add the
// same account again. A number that moves when you drag the map is worse than
// no number, because it looks like it means something.
/* The national view's answer to the same question.
 *
 * It used to be three em dashes and a sentence explaining why. That was honest
 * and useless: "we cannot tell you" is not the same claim as "there is nothing
 * to tell", and a rep opening the map to the whole country saw the emptier of
 * the two. What the national layer cannot do is scope the book to a VIEWPORT --
 * it holds office placements, not advisor identities. It has never needed to in
 * order to state the book itself, which is one de-duplicated number per product
 * and was already sitting in the file.
 *
 * So: the whole book, computed once in build_act_assets.py by counting each
 * account exactly once whoever holds it, and labelled "United States" so it is
 * never mistaken for the viewport figure its neighbours show.
 */
function renderEicNational(){
  const el = (id) => document.getElementById(id);
  const t = (ACT_ASSETS && ACT_ASSETS.totals) || null;
  const scope = el("eicScope"), note = el("eicNationalNote");
  if (!t){
    ["eicAcv", "eicLcv", "eicMf"].forEach(i => { el(i).textContent = "—"; });
    scope.hidden = true; note.hidden = true;
    el("eicAssetsHelp").textContent = ACT_ASSETS_ERROR
      ? "EIC book figures are unavailable."
      : "EIC book figures are still loading.";
    return;
  }
  el("eicAcv").textContent = fmtMoney(t.acv);
  el("eicLcv").textContent = fmtMoney(t.lcv);
  el("eicMf").textContent = fmtMoney(t.mf);
  scope.hidden = false;
  scope.textContent = " · United States";

  const n = (v) => Number(v || 0).toLocaleString();
  const shared = t.shared_accounts
    ? `${n(t.shared_accounts)} of those accounts are shared by more than one advisor and are counted once, not once each. ` : "";
  /* The product gap, stated at national scale. This is the reason the file
   * splits by product at all: the two SMAs are close to mutually exclusive
   * advisor by advisor, and that is a targeting instruction, not trivia. */
  const gaps = (t.gap_acv || t.gap_lcv)
    ? `${n(t.gap_acv)} hold All-Cap but no Large-Cap and ${n(t.gap_lcv)} hold Large-Cap but no All-Cap`
      + (t.both_smas != null ? `; ${n(t.both_smas)} hold both.` : ".")
    : "";
  note.hidden = false;
  note.textContent = `${n(t.advisors)} advisors hold a book with EIC across `
    + `${n(t.accounts)} accounts. ${gaps}`;

  el("eicAssetsHelp").textContent =
    `The entire EIC book, not the current viewport — the national layer carries firm-office `
    + `placements rather than advisor identities, so it cannot scope these to what is on screen. `
    + `Unlike every other figure in this strip, they do not change as you pan or zoom. `
    + `${shared}`
    + `${n(t.accounts_on_map)} of ${n(t.accounts)} accounts reach an advisor who appears on the map; `
    + `the rest are held by contacts we could not match to a CRD, and they are still counted here. `
    + (t.advisors_review ? `${n(t.advisors_review)} of the ${n(t.advisors)} advisors were matched to the CRM on name rather than a confirmed identifier. ` : "")
    + `Pick a state or territory for the book in view, advisor by advisor. `
    + `Source: Act! CRM account records, ${String(ACT_ASSETS.as_of).slice(0, 10)}.`;
}

function renderEicAssets(ids){
  const el = (id) => document.getElementById(id);
  // Leaving the national view puts these tiles back on the viewport, so the
  // "United States" badge and its note have to come off with it -- a stale
  // badge would relabel a Georgia figure as the whole country.
  document.getElementById("eicScope").hidden = true;
  document.getElementById("eicNationalNote").hidden = true;
  // `null` means UNAVAILABLE, an empty array means genuinely nothing in view.
  // A detail view with no advisors on screen really is zero and says so.
  if (!ACT_ASSETS || !ACT_ASSETS.accounts || ids == null){
    ["eicAcv", "eicLcv", "eicMf"].forEach(i => { el(i).textContent = "—"; });
    return;
  }
  const seen = new Set();
  let advisors = 0, review = 0;
  for (const id of ids){
    const b = ACT_ASSETS.advisors[String(id)];
    if (!b) continue;
    advisors++;
    if (b.t === "review") review++;
    for (const i of b.ix) seen.add(i);
  }
  let acv = 0, lcv = 0, mf = 0;
  for (const i of seen){
    const a = ACT_ASSETS.accounts[i];
    if (!a) continue;
    acv += a[0]; lcv += a[1]; mf += a[2];
  }
  el("eicAcv").textContent = fmtMoney(acv);
  el("eicLcv").textContent = fmtMoney(lcv);
  el("eicMf").textContent = fmtMoney(mf);
  el("eicAssetsHelp").textContent =
    `EIC assets held by the ${advisors.toLocaleString()} advisor${advisors === 1 ? "" : "s"} `
    + `in view who have a book with us, across ${seen.size.toLocaleString()} `
    + `account${seen.size === 1 ? "" : "s"}. Each account is counted once even when `
    + `several advisors on a team share it, so these figures do not double count. `
    + (review ? `${review.toLocaleString()} of those advisors were matched to the CRM on name rather than a confirmed identifier. ` : "")
    + `Firm-wide the book is ${fmtMoney(ACT_ASSETS.totals.acv)} All-Cap, `
    + `${fmtMoney(ACT_ASSETS.totals.lcv)} Large-Cap and ${fmtMoney(ACT_ASSETS.totals.mf)} EICIX. `
    + `Source: Act! CRM account records, ${String(ACT_ASSETS.as_of).slice(0, 10)}.`;
}

function renderEstimatedAum(estimate){
  document.getElementById("estimatedAum").textContent = fmtMoney(estimate.aum);
  document.getElementById("estimatedEquities").textContent = fmtMoney(estimate.equities);
  document.getElementById("estimatedFunds").textContent = fmtMoney(estimate.funds);
  const geography = estimate.placements
    ? "In the national view, each firm's pool is allocated by its share of firm-office advisor placements in the current viewport; the lightweight national layer does not carry advisor identities."
    : "In detailed views, each firm's pool is allocated by distinct mapped advisors in the current viewport divided by that firm's distinct mapped advisors nationally.";
  document.getElementById("estimatedAumHelp").textContent =
    `AUM is firm regulatory AUM allocated to the current geography. The product pools use ADV Item 5.K percentages applied to non-pooled regulatory AUM. ` +
    `Equities means exchange-traded equity securities; Funds/ETFs means securities issued by registered investment companies or BDCs. ` +
    `${geography} Allocations are capped at 100% and summed across firms. ` +
    `These are directional opportunity estimates—not actual product holdings—and discretionary share is not applied because the ADV does not cross-tab it by asset type. ` +
    `Coverage in this view: AUM ${estimate.aumCoverage.toLocaleString()} of ${estimate.firms.toLocaleString()} firms; ` +
    `Equities ${estimate.equityCoverage.toLocaleString()} of ${estimate.firms.toLocaleString()} firms; ` +
    `Funds/ETFs ${estimate.fundCoverage.toLocaleString()} of ${estimate.firms.toLocaleString()} firms.`;
}

function refreshPanel(){
  if (scope === "US") return refreshPanelNational();
  const vis = visibleFeatures();

  const shown = selectedFirms.length
    ? vis.filter(f => passesFilters(f.properties)) : vis;
  const panelFeatures = vis;
  const firmsInView = new Set(shown.map(f => f.properties.fc));
  const nAdv = countAdvisors(shown);
  const nOffices = new Set(shown.map(f => addrKey(f.properties))).size;
  countEl.hidden = true;
  document.getElementById("kpiAdvisors").textContent = nAdv.toLocaleString();
  document.getElementById("kpiOffices").textContent = nOffices.toLocaleString();
  // firms actually visible, not firms selected -- selecting two firms and then
  // panning so only one is on screen used to keep reporting 2
  document.getElementById("kpiFirms").textContent = firmsInView.size.toLocaleString();
  document.getElementById("kpiNote").hidden = true;
  // The whole-scope figures need a further full pass over ALL but are only read
  // when the tooltip is opened, so they are deferred to that moment.
  setScopeTotals(() => {
    const scoped = ALL.filter(f => passesBase(f.properties) &&
      (!selectedFirms.length || selectedFirms.includes(f.properties.fc)));
    const offices = new Set(scoped.map(f => addrKey(f.properties))).size;
    const firms = new Set(scoped.map(f => f.properties.fc)).size;
    return `Current viewport uses distinct advisors; an advisor may have more than one office pin. ` +
      `Filtered ${scopeLabel(scope)} scope: ${countAdvisors(scoped).toLocaleString()} distinct advisors, ` +
      `${offices.toLocaleString()} physical office${offices === 1 ? "" : "s"}, ` +
      `${firms.toLocaleString()} firm${firms === 1 ? "" : "s"}.`;
  });
  renderEstimatedAum(estimateAumFromAdvisors(shown));
  // Same feature set the counts above are drawn from, so "advisors in view" and
  // "our assets in view" can never describe different populations.
  renderEicAssets(shown.map(f => f.properties.id));

  // firm rollup, scoped to view, counting distinct advisors
  const agg = {};
  panelFeatures.forEach(f => {
    const p = f.properties;
    const a = (agg[p.fc] ||= { firm: p.f, crd: p.fc, ids: new Set(), dualIds: new Set(),
                              score: p.s, fit: p.sf, size: p.sz, raum: p.ra,
                              offices: new Set(), exp: [], states: new Set(), reg: [] });
    a.ids.add(p.id);
    if (p.d === 1) a.dualIds.add(p.id);
    a.offices.add(p.a + "|" + p.c);
    if (p.x != null) a.exp.push(p.x);
    if (p.ns != null) a.reg.push(p.ns);
    if (p.rs) p.rs.split("|").forEach(s => a.states.add(s));
  });
  const med = arr => arr.length ? arr.slice().sort((x, y) => x - y)[Math.floor(arr.length / 2)] : null;
  FIRMS = sortFirms(Object.values(agg).map(a => ({
    firm: a.firm, crd: a.crd, advisors: a.ids.size, dual: a.dualIds.size,
    offices: a.offices.size, score: a.score,
    fit: a.fit, size: a.size, raum: a.raum,
    relevantAum: relevantAumForFirm(a.crd, a.ids.size),
    medExp: med(a.exp), medStates: med(a.reg), reachStates: a.states.size,
  })));
  renderFirms(document.getElementById("search").value.toLowerCase());
  refreshActiveFilters();
}

// national panel: everything is derived from the office aggregate, so advisor
// counts are sums over offices rather than distinct CRDs. One advisor filed at
// two offices counts twice -- said plainly in the headline rather than implied.
function refreshPanelNational(){
  /* Until the office array lands these counts have nothing to count, and
   * "0 advisors, 0 offices, 0 firms" is a statement about the country rather
   * than about the download. An em dash says the same thing honestly. */
  if (!NAT_DETAIL_READY){
    countEl.hidden = true;
    ["kpiAdvisors", "kpiOffices", "kpiFirms"].forEach(id => {
      document.getElementById(id).textContent = "—";
    });
    const note = document.getElementById("kpiNote");
    note.hidden = false;
    note.textContent = NAT_DETAIL_ERROR
      ? `Office detail is unavailable — ${NAT_DETAIL_ERROR}. The heat map is still usable.`
      : NATIONAL_DETAIL_REASON || (natDetailPromise
          ? "Loading office detail — the heat map is complete, the counts are not."
          : "The heat map is ready; office-level counts are loading separately.");
    setScopeTotals(() => NAT_DETAIL_ERROR
      ? "Office detail is unavailable; national totals cannot be calculated."
      : "Office detail is still loading.");
    ["estimatedAum", "estimatedEquities", "estimatedFunds"].forEach(id => {
      document.getElementById(id).textContent = "—";
    });
    document.getElementById("estimatedAumHelp").textContent = NAT_DETAIL_ERROR
      ? "Estimated national opportunity is unavailable because firm-office detail could not be loaded."
      : "Estimated national opportunity will appear when firm-office detail finishes loading.";
    // Never leave the regional viewport's EIC book under a national map.
    renderEicNational();
    FIRMS = [];
    document.getElementById("firmListMeta").textContent = NAT_DETAIL_ERROR
      ? "Firm list unavailable"
      : "Firm list loading";
    document.getElementById("firms").innerHTML = `<p class="hint">${NAT_DETAIL_ERROR
      ? "National firm detail is unavailable."
      : "Loading national firm detail…"}</p>`;
    refreshActiveFilters();
    return;
  }
  const bounds = map.getBounds();
  const vis = NAT.offices.filter(o => natPassesBase(o) && bounds.contains([o[1], o[0]]));
  const shown = selectedFirms.length
    ? vis.filter(o => selectedFirms.includes(natFirmCrd(o))) : vis;

  const adv = shown.reduce((s, o) => s + o[2], 0);
  const offices = new Set(shown.map(o => o[7])).size;
  const firmsShown = new Set(shown.map(natFirmCrd));
  countEl.hidden = true;
  document.getElementById("kpiAdvisors").textContent = adv.toLocaleString();
  document.getElementById("kpiOffices").textContent = offices.toLocaleString();
  document.getElementById("kpiFirms").textContent = firmsShown.size.toLocaleString();
  const kpiNote = document.getElementById("kpiNote");
  kpiNote.hidden = !natTruncated;
  kpiNote.textContent = natTruncated
    ? `Drawing the largest ${NAT_CAP.toLocaleString()} of ${shown.length.toLocaleString()} firm-offices — zoom in for the rest.`
    : "";
  setScopeTotals(() => {
    const scoped = NAT.offices.filter(natPasses);
    const placements = scoped.reduce((sum, office) => sum + office[2], 0);
    const offices = new Set(scoped.map(office => office[7])).size;
    const firms = new Set(scoped.map(natFirmCrd)).size;
    return `Current viewport uses firm-office placements; one advisor can be counted at multiple offices. ` +
      `Filtered national scope: ${placements.toLocaleString()} placements, ` +
      `${offices.toLocaleString()} physical office${offices === 1 ? "" : "s"}, ` +
      `${firms.toLocaleString()} firm${firms === 1 ? "" : "s"}.`;
  });
  renderEstimatedAum(estimateAumFromPlacements(shown));
  // The national layer carries office placements, NOT advisor identities, so
  // there is no honest way to say whose book is in the VIEWPORT. There is an
  // honest way to say what the book IS, which is what this shows -- labelled
  // "United States" so the two are never confused.
  renderEicNational();

  const agg = {};
  vis.forEach(o => {
    const info = natFirm(o), crd = natFirmCrd(o);
    const a = (agg[crd] ||= { firm: info[0], crd, score: info[2], raum:info[3], advisors: 0,
                            offices: new Set(), states: new Set() });
    a.advisors += o[2];
    a.offices.add(o[7]);
    a.states.add(NAT.states[o[6]]);
  });
  FIRMS = sortFirms(Object.values(agg).map(a => ({
    firm: a.firm, crd: a.crd, advisors: a.advisors, offices: a.offices.size,
    medExp: null, medStates: null, reachStates: a.states.size, dual: 0,
    score: a.score, fit: null, size: null, raum: a.raum,
    // national rows count placements, so allocate on the placement denominator
    relevantAum: relevantAumForFirm(a.crd, a.advisors, true),
  })));
  renderFirms(document.getElementById("search").value.toLowerCase());
  refreshActiveFilters();
}

function sortFirms(arr){
  if (firmSort === "relevant")
    return arr.sort((a, b) => (b.relevantAum ?? -1) - (a.relevantAum ?? -1)
                              || b.advisors - a.advisors || a.firm.localeCompare(b.firm));
  return arr.sort((a, b) => b.advisors - a.advisors
                            || (b.relevantAum ?? -1) - (a.relevantAum ?? -1)
                            || a.firm.localeCompare(b.firm));
}

let FIRMS = [];
function renderFirms(q){
  const box = document.getElementById("firms");
  const list = q ? FIRMS.filter(f => f.firm.toLowerCase().includes(q) || String(f.crd).includes(q)) : FIRMS;
  const max = FIRMS.reduce((m, f) => Math.max(m, f.advisors), 1);
  const limit = 250;
  const meta = document.getElementById("firmListMeta");
  meta.textContent = list.length > limit
    ? `Showing first ${limit.toLocaleString()} of ${list.length.toLocaleString()} matching firms`
    : `Showing all ${list.length.toLocaleString()} matching firm${list.length === 1 ? "" : "s"}`;
  box.innerHTML = "";
  if (!list.length){
    box.innerHTML = `<p class="hint">No firms in this view.</p>`;
    return;
  }
  list.slice(0, limit).forEach(f => {
    const on = selectedFirms.includes(f.crd);
    // A button, not a div. The whole firm list was keyboard-unreachable while
    // the roster beside it already used role="button" tabindex="0" -- the same
    // control in two shapes. A native button brings focus, Enter/Space and
    // screen-reader semantics for free.
    const row = document.createElement("button");
    row.type = "button";
    row.className = "frow" + (on ? " active" : "");
    row.setAttribute("aria-pressed", String(on));
    const col = on ? firmColor[f.crd] : cssVar("--accent");
    if (on) row.style.setProperty("--firm-col", col);
    row.innerHTML = `
      <span class="fn" title="${esc(f.firm)} · CRD ${esc(f.crd)}${f.medExp != null ? ` · ${f.medExp.toFixed(0)}y median experience` : ""} · ${scope === "US" ? `in ${f.reachStates} state${f.reachStates > 1 ? "s" : ""}` : `${f.reachStates} registration states`}">${on ? `<span class="swatch" style="background:${col}"></span>` : ""}${esc(f.firm)}</span>
      <span class="fdetails" role="button" tabindex="0" data-firm-profile="${esc(f.crd)}">Details</span>
      <span class="fmetrics"><span><b class="mono">${f.advisors.toLocaleString()}</b> advisor${f.advisors === 1 ? "" : "s"}</span><span><b class="mono">${f.offices.toLocaleString()}</b> office${f.offices === 1 ? "" : "s"}</span><span title="Equities plus Funds/ETFs"><b>${f.relevantAum == null ? "—" : fmtMoney(f.relevantAum)}</b> relevant AUM</span><span class="fexclude" role="button" tabindex="0" data-exclude-firm="${esc(f.crd)}" title="Remove ${esc(f.firm)} from the map">Exclude</span></span>
      <span class="fb"><i style="width:${Math.max(3, f.advisors / max * 100)}%;background:${col}"></i></span>`;
    row.addEventListener("click", e => {
      if (e.target.closest("[data-firm-profile]")) return;
      if (e.target.closest("[data-exclude-firm]")) return;   // exclude is not a selection
      pickFirm(f.crd, e);
    });
    // the nested Details control cannot be a <button> inside a <button>, so it
    // carries its own keyboard handling
    row.querySelector(".fdetails").addEventListener("keydown", e => {
      if (e.key !== "Enter" && e.key !== " ") return;
      e.preventDefault(); e.stopPropagation();
      openFirmOverview(f.crd);
    });
    // same reason as Details: it cannot be a <button> inside a <button>
    row.querySelector(".fexclude").addEventListener("keydown", e => {
      if (e.key !== "Enter" && e.key !== " ") return;
      e.preventDefault(); e.stopPropagation();
      excludeFirm(f.crd, f.firm);
    });
    box.appendChild(row);
  });
}

function pickFirm(crd, event={}){
  crd = String(crd);
  const i = selectedFirms.indexOf(crd);
  const additive = event.ctrlKey || event.metaKey || event.shiftKey;
  if (additive){
    if (i >= 0) selectedFirms.splice(i, 1);
    else selectedFirms.push(crd);
  } else {
    selectedFirms = (selectedFirms.length === 1 && i === 0) ? [] : [crd];
  }
  syncFirmColors();
  document.getElementById("clearFirms").hidden = !selectedFirms.length;
  // Only move the map when the pick would otherwise show nothing. Fitting on every
  // pick threw a statewide firm's bounds at the viewport and zoomed back out to GA,
  // losing the area the user had navigated to.
  redraw(selectedFirms.length > 0 && !anySelectedInView());
}

function anySelectedInView(){
  const b = map.getBounds();
  if (scope === "US"){
    return NAT.offices.some(o => natPasses(o) && b.contains([o[1], o[0]]));
  }
  return ALL.some(f => {
    if (!passesFilters(f.properties)) return false;
    const [lon, lat] = f.geometry.coordinates;
    return b.contains([lat, lon]);
  });
}
document.getElementById("clearFirms").addEventListener("click", () => {
  selectedFirms = []; firmColor = {};
  document.getElementById("clearFirms").hidden = true;
  redraw();
});

// ---- registration + experience toggles ----
document.getElementById("regToggle").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  reg = b.dataset.reg;
  [...e.currentTarget.children].forEach(x => x.setAttribute("aria-pressed", x === b));
  redraw();
});
// plain click = that band only (click again to clear) · ctrl/cmd/shift = add
function multiSelect(id, attr, set){
  document.getElementById(id).addEventListener("click", e => {
    const b = e.target.closest("button"); if (!b) return;
    const additive = e.ctrlKey || e.metaKey || e.shiftKey;
    const band = b.dataset[attr];

    if (band === "all") set.clear();
    else if (!additive){
      if (set.size === 1 && set.has(band)) set.clear();
      else { set.clear(); set.add(band); }
    } else {
      set.has(band) ? set.delete(band) : set.add(band);
    }

    [...e.currentTarget.children].forEach(x => x.setAttribute("aria-pressed",
      x.dataset[attr] === "all" ? set.size === 0 : set.has(x.dataset[attr])));
    redraw();
  });
}
// Both contact filters can empty the view -- "has assets" especially, since
// the CRM populates it for under 8% of contacts. An empty map reads as broken
// rather than as a result, so each fits to what survives and says so when
// nothing does, exactly as the ranked filter already does.
/* Both switches ask a question only contacts.json can answer, so they stay
 * DISABLED until it arrives -- roughly 400ms on a warm load, longer on a cold
 * one. A switch that silently reports "no advisors here are reachable" while
 * the file is still in flight is not a slow answer, it is a wrong one, and it
 * looks exactly like a real result.
 */
const CONTACT_SWITCHES = [];

function syncContactSwitches(){ CONTACT_SWITCHES.forEach(fn => fn()); }

function wireContactSwitch(id, get, set, emptyMsg, label){
  const box = document.getElementById(id);
  if (!box) return () => {};
  const sync = () => {
    box.checked = get();
    box.disabled = scope === "US" || !CONTACTS_READY;
    const shell = box.closest(".switch");
    shell.classList.toggle("on", get());
    shell.classList.toggle("switch-waiting", scope !== "US" && !CONTACTS_READY);
    shell.title = CONTACTS_READY ? ""
      : CONTACTS_ERROR ? `Contact details unavailable — ${CONTACTS_ERROR}`
      : "Loading contact details…";
  };
  CONTACT_SWITCHES.push(sync);
  box.addEventListener("change", () => {
    if (!CONTACTS_READY){ box.checked = false; return; }
    set(box.checked);
    sync();
    redraw();
    if (!get()) return;
    reportFilterReach(emptyMsg, label);
  });
  return sync;
}
const syncContactableUI = wireContactSwitch("contactableOnly",
  () => contactableOnly, v => { contactableOnly = v; },
  "No advisors with contact data match the current filters.",
  "advisors with contact data");
const syncAssetsUI = wireContactSwitch("assetsOnly",
  () => assetsOnly, v => { assetsOnly = v; },
  "No advisors with an asset figure match the current filters.",
  "advisors with an asset figure");

const ownerBox = document.getElementById("ownerOnly");
function syncOwnerUI(){
  ownerBox.checked = ownerOnly;
  const available = SUPPORT.owner === "ready";
  ownerBox.disabled = scope === "US" || !available;
  const shell = ownerBox.closest(".switch");
  shell.classList.toggle("on", ownerOnly);
  shell.classList.toggle("switch-waiting", scope !== "US" && !available);
  shell.title = SUPPORT.owner === "failed"
    ? `Ownership roles unavailable — ${SUPPORT_ERROR.owner}`
    : (!available ? "Loading ownership roles…" : "");
}
ownerBox.addEventListener("change", () => {
  if (SUPPORT.owner !== "ready"){
    ownerBox.checked = ownerOnly;
    return;
  }
  ownerOnly = ownerBox.checked;
  syncOwnerUI();
  redraw();
});

const rankedBox = document.getElementById("rankedOnly");
function syncRankedUI(){
  rankedBox.checked = rankedOnly;
  const available = SUPPORT.barrons === "ready" && SUPPORT.forbes === "ready";
  rankedBox.disabled = scope === "US" || !available;
  const shell = rankedBox.closest(".switch");
  shell.classList.toggle("on", rankedOnly);
  shell.classList.toggle("switch-waiting", scope !== "US" && !available);
  const failures = [SUPPORT_ERROR.barrons, SUPPORT_ERROR.forbes].filter(Boolean);
  shell.title = failures.length
    ? `Advisor rankings unavailable — ${failures.join("; ")}`
    : (!available ? "Loading advisor rankings…" : "");
}
// Ranked advisors are 9,029 of 397,551 -- 2.3%, and 1.8-2.5% in every
// territory. Still thin enough that switching this on while zoomed into a
// metro can leave nothing on screen, which reads as a broken map rather than
// an empty result, so fit to what survives.
rankedBox.addEventListener("change", () => {
  if (SUPPORT.barrons !== "ready" || SUPPORT.forbes !== "ready"){
    rankedBox.checked = rankedOnly;
    return;
  }
  rankedOnly = rankedBox.checked;
  syncRankedUI();
  redraw();
  if (!rankedOnly) return;
  reportFilterReach("No ranked advisors match the current filters.", "ranked advisors");
});

multiSelect("expToggle", "exp", expSel);
multiSelect("reachToggle", "reach", reachSel);
multiSelect("geoToggle", "geo", geoSel);

// ---- targeting: 5.G(7) toggle + AUM presets ----
const selectsBox = document.getElementById("selectsOnly");
const aumBands = document.getElementById("aumBands");
const clearTgt = document.getElementById("clearTargeting");

function targetingDirty(){ return selectsOnly || raumActive(); }
function syncTargetingUI(){
  syncAumBounds();                    // keep the flattened predicate bounds in step
  aumBands.querySelectorAll("button").forEach(button =>
    button.setAttribute("aria-pressed", button.dataset.aum === "all"
      ? !aumSel.size : aumSel.has(button.dataset.aum)));
  clearTgt.hidden = !raumActive();
}
aumBands.addEventListener("click", e => {
  const button = e.target.closest("button"); if (!button) return;
  const band = button.dataset.aum;
  const additive = e.ctrlKey || e.metaKey || e.shiftKey;
  if (band === "all") aumSel.clear();
  else if (additive){
    aumSel.has(band) ? aumSel.delete(band) : aumSel.add(band);
  } else if (aumSel.size === 1 && aumSel.has(band)) aumSel.clear();
  else { aumSel.clear(); aumSel.add(band); }
  const clearedFirmFocus = scope === "US" && selectedFirms.length > 0;
  if (clearedFirmFocus){
    selectedFirms = []; syncFirmColors();
    document.getElementById("clearFirms").hidden = true;
  }
  syncTargetingUI();
  redraw(clearedFirmFocus ? "national" : false);
  if (clearedFirmFocus)
    showNotice("Cleared the selected-firm comparison to show the national AUM heatmap.");
});
selectsBox.addEventListener("change", () => {
  selectsOnly = selectsBox.checked;
  selectsBox.closest(".switch").classList.toggle("on", selectsOnly);
  syncTargetingUI();
  redraw();
});
// `on` is the state to leave 5.G(7) in: "reset all" restores the default, while
// relaxing filters to reveal a specific advisor has to be able to clear it.
function resetTargeting(on=false){
  selectsOnly = !!on;
  selectsBox.checked = selectsOnly;
  selectsBox.closest(".switch").classList.toggle("on", selectsOnly);
  aumSel.clear();
  syncTargetingUI();
}
clearTgt.addEventListener("click", () => {
  aumSel.clear();
  syncTargetingUI();
  redraw();
});

// reflect the default (5.G(7) on) in the control at startup
selectsBox.checked = selectsOnly;
selectsBox.closest(".switch").classList.toggle("on", selectsOnly);
syncTargetingUI();

// ---- firm-list sort (advisors / relevant AUM) ----
document.getElementById("firmSort").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  firmSort = b.dataset.sort;
  [...e.currentTarget.children].forEach(x => x.setAttribute("aria-pressed", x === b));
  refreshPanel();
});
document.getElementById("toggleFirms").addEventListener("click", e => {
  const body = document.getElementById("firmPanelBody");
  body.hidden = !body.hidden;
  e.currentTarget.setAttribute("aria-expanded", String(!body.hidden));
  e.currentTarget.textContent = body.hidden ? "show" : "hide";
});

// ---- firm search ----
document.getElementById("search").addEventListener("input", e =>
  renderFirms(e.target.value.toLowerCase()));

function firmLabelForCrd(crd){
  const local = FIRMS.find(f => String(f.crd) === String(crd));
  if (local) return local.firm;
  const national = NAT?.firms?.find(f => String(f[1]) === String(crd));
  return national ? national[0] : `CRD ${crd}`;
}

let firmNavigationRequest = 0;

async function showFirmOffices(crd, targetScope=scope){
  const intent = ++firmNavigationRequest;
  crd = String(crd);
  focusedAdvisorId = null; focusedAdvisorLabel = "";
  if (targetScope === "US") return focusFirmNational(crd, false, intent);
  if (scope !== targetScope){
    const outcome = await switchScope(targetScope);
    if (intent !== firmNavigationRequest || !outcome ||
        outcome.status !== "applied" || scope !== targetScope) return false;
  }
  const features = ALL.filter(f => String(f.properties.fc) === crd);
  if (!features.length) return false;
  const cleared = relaxFiltersForAdvisor(features, firmLabelForCrd(crd), false);
  setSelectedFirm(crd); redraw(true);
  const offices = new Set(features.map(f => addrKey(f.properties))).size;
  const suffix = cleared.length ? ` Cleared ${joinLabels(cleared)}.` : "";
  showNotice(`Showing ${offices.toLocaleString()} mapped office${offices === 1 ? "" : "s"} for ${firmLabelForCrd(crd)} in ${scopeLabel(targetScope)}.${suffix}`);
  return true;
}

async function showFirmAdvisors(crd, targetScope=scope, closeDrawer=true){
  const intent = ++firmNavigationRequest;
  crd = String(crd);
  focusedAdvisorId = null; focusedAdvisorLabel = "";
  if (targetScope === "US") return focusFirmNational(crd, closeDrawer, intent);
  if (scope !== targetScope){
    const outcome = await switchScope(targetScope);
    if (intent !== firmNavigationRequest || !outcome ||
        outcome.status !== "applied" || scope !== targetScope) return false;
  }
  const features = ALL.filter(f => String(f.properties.fc) === crd);
  if (!features.length){
    showNotice(`${firmLabelForCrd(crd)} has no mapped advisors in ${scopeLabel(targetScope)}.`);
    return false;
  }
  const cleared = relaxFiltersForAdvisor(features, firmLabelForCrd(crd), false);
  setSelectedFirm(crd); redraw(true);
  if (closeDrawer && openFirmCrd) closeFirmOverview();
  const advisors = new Set(features.map(f => f.properties.id)).size;
  const suffix = cleared.length ? ` Cleared ${joinLabels(cleared)}.` : "";
  showNotice(`Showing ${advisors.toLocaleString()} mapped advisor${advisors === 1 ? "" : "s"} for ${firmLabelForCrd(crd)} in ${scopeLabel(targetScope)}.${suffix}`);
  if (!closeDrawer && detailsCurrent?.type === "firm" && detailsCurrent.crd === crd)
    await openFirmOverview(crd, false, false);
  return true;
}

async function focusFirmNational(crd, closeDrawer=false, intent=null){
  if (intent == null) intent = ++firmNavigationRequest;
  const observedScopeRequest = scopeRequest;
  crd = String(crd);
  focusedAdvisorId = null; focusedAdvisorLabel = "";
  if (!NAT_DETAIL_READY){
    await loadNationalDetail(null, false);
    if (!NAT_DETAIL_READY || intent !== firmNavigationRequest ||
        scopeRequest !== observedScopeRequest) return false;
  }
  const offices = nationalOfficesForFirm(crd);
  if (!offices.length){
    showNotice(`${firmLabelForCrd(crd)} has no mapped national office.`);
    return false;
  }
  if (scope !== "US"){
    const outcome = await switchScope("US");
    if (intent !== firmNavigationRequest || !outcome ||
        outcome.status !== "applied" || scope !== "US") return false;
  }
  // Without this, selecting a firm that fails an active filter drew an empty
  // map with no explanation: renderNational fits and draws only offices that
  // pass, so a firm excluded by 5.G(7), the AUM band or the lasso simply
  // vanished. Relax exactly the filters that hide it, and say which.
  const cleared = relaxFiltersForFirmNational(offices);
  setSelectedFirm(crd); redraw(true);
  if (closeDrawer && openFirmCrd) closeFirmOverview();
  const suffix = cleared.length ? ` Cleared ${joinLabels(cleared)}.` : "";
  showNotice(`Showing all ${offices.length.toLocaleString()} mapped office${offices.length === 1 ? "" : "s"} for ${firmLabelForCrd(crd)}.${suffix}`);
  if (!closeDrawer && detailsCurrent?.type === "firm" && detailsCurrent.crd === crd)
    await openFirmOverview(crd, false, false);
  return true;
}

// The national counterpart of relaxFiltersForAdvisor: clear only the filters
// that would hide this firm's offices, and report what was cleared.
function relaxFiltersForFirmNational(offices){
  const cleared = [];
  const add = label => { if (!cleared.includes(label)) cleared.push(label); };
  if (continentalOnly && !offices.some(o => !OUTSIDE_CONTINENTAL.has(NAT.states[o[6]]))){
    setContinentalOnly(false, false); add("Continental U.S. only");
  }
  if (selectsOnly && !offices.some(natFirmSelects)){
    selectsOnly = false; selectsBox.checked = false;
    selectsBox.closest(".switch").classList.remove("on");
    add("Reports selecting outside managers");
  }
  if (AUM_BOUNDS.length && !offices.some(o => {
        const millions = natFirmRaum(o);
        return millions != null && inAumBands(millions * 1e6);
      })){
    aumSel.clear(); add("the firm AUM range");
  }
  if (lassoPolygon && !offices.some(o => pointInLasso(o[1], o[0]))){
    clearLasso(false); add("the map lasso");
  }
  syncTargetingUI(); syncFilterButtons();
  return cleared;
}

// Clicking a FIRM SEARCH RESULT means "take me to this firm", so it selects the
// firm and moves the map as well as opening the details drawer. That is
// deliberately different from the Details button on a row in "Firms in view",
// which stays non-destructive because there you are browsing a place and should
// not lose the view you navigated to.
async function navigateToFirm(crd){
  crd = String(crd);
  openFirmOverview(crd);
  // Prefer the detail we already hold: if the firm has advisors in the current
  // state or territory, stay there rather than dropping back to the national
  // aggregate, which carries no advisor identities.
  if (scope !== "US" && ALL.some(f => String(f.properties.fc) === crd))
    return showFirmAdvisors(crd, scope, false);
  return focusFirmNational(crd, false);
}

function refreshActiveFilters(){
  const items = [];
  const add = (label, key) => items.push({ label, key });
  if (selectedFirms.length)
    add(`${selectedFirms.length} selected firm${selectedFirms.length === 1 ? "" : "s"}`, "firms");
  if (focusedAdvisorId) add(`Focused on ${focusedAdvisorLabel}`, "advisor");
  if (lassoPolygon) add("Map lasso", "lasso");
  if (raumActive())
    add(`Firm AUM ${[...aumSel].map(key => AUM_BANDS[key].label).join(", ")}`, "raum");
  if (selectsOnly) add("Reports selecting outside managers", "selects");
  if (scope !== "US"){
    if (reg !== "all") add(reg === "dual" ? "Dually registered" : "RIA-only", "reg");
    if (expSel.size) add(`Experience ${[...expSel].join(", ")}`, "exp");
    if (reachSel.size) add(`Registration reach ${[...reachSel].join(", ")}`, "reach");
    if (geoSel.size) add(`Map quality ${[...geoSel].join(", ")}`, "geo");
    if (ownerOnly) add("Owners and officers only", "owner");
    // Distinct advisors, not pins -- an advisor filed at two offices is two
    // pins, and two numbers labelled the same thing must not disagree.
    const distinct = () => {
      const ids = new Set();
      for (const f of ALL) if (passesFilters(f.properties)) ids.add(f.properties.id);
      return ids.size.toLocaleString();
    };
    if (contactableOnly) add(`Has contact data · ${distinct()}`, "contactable");
    if (assetsOnly) add(`Has assets on file · ${distinct()}`, "assets");
    if (rankedOnly){
      // Distinct advisors, not pins. Counting rows here reported 35 against an
      // Advisors KPI of 33, because an advisor filed at two offices is two
      // pins; two numbers labelled the same thing must not disagree.
      const ids = new Set();
      for (const f of ALL) if (passesFilters(f.properties)) ids.add(f.properties.id);
      add(`Ranked advisors · ${ids.size.toLocaleString()}`, "ranked");
    }
  }
  if (excludedFirms.size)
    add(`${excludedFirms.size} firm${excludedFirms.size === 1 ? "" : "s"} excluded`, "excluded");
  const wrap = document.getElementById("activeFilters");
  wrap.innerHTML = items.length
    ? items.map(item => `<button class="filter-chip" data-clear-filter="${item.key}">${esc(item.label)}</button>`).join("")
    : `<span class="hint">None</span>`;
  document.getElementById("filterState").hidden = false;
  document.getElementById("resetAll").hidden = !items.length;
}

function syncFilterButtons(){
  document.querySelectorAll("#regToggle button").forEach(b => b.setAttribute("aria-pressed", b.dataset.reg === reg));
  for (const [id, attr, set] of [["expToggle","exp",expSel],["reachToggle","reach",reachSel],["geoToggle","geo",geoSel]]){
    document.querySelectorAll(`#${id} button`).forEach(b =>
      b.setAttribute("aria-pressed", b.dataset[attr] === "all" ? !set.size : set.has(b.dataset[attr])));
  }
  syncTargetingUI();
  syncOwnerUI();
  syncRankedUI();
  syncContactableUI();
  syncAssetsUI();
  renderExcludedFirms();
}

function resetAllFilters(){
  reg = "all"; expSel.clear(); reachSel.clear(); geoSel.clear();
  ownerOnly = false;
  rankedOnly = false;
  contactableOnly = false; assetsOnly = false;
  excludedFirms.clear();
  resetTargeting(SELECTS_DEFAULT);
  setContinentalOnly(OUTSIDE_CONTINENTAL.has(scope) ? false : true, false);
  selectedFirms = []; firmColor = {}; document.getElementById("clearFirms").hidden = true;
  focusedAdvisorId = null; focusedAdvisorLabel = "";
  clearSearch();
  clearLasso(false); syncFilterButtons(); redraw();
}

document.getElementById("resetAll").addEventListener("click", resetAllFilters);
document.getElementById("listsBtn").addEventListener("click", () => {
  listsEditMode = false; audiencePreview = null;
  openListManager().catch(error => showNotice(error.message || "Lists could not be opened."));
});
document.getElementById("saveAudience").addEventListener("click", saveCurrentAudience);
document.getElementById("activeFilters").addEventListener("click", e => {
  const button = e.target.closest("[data-clear-filter]"); if (!button) return;
  const key = button.dataset.clearFilter;
  if (key === "firms"){ selectedFirms = []; firmColor = {}; document.getElementById("clearFirms").hidden = true; }
  if (key === "advisor"){ focusedAdvisorId = null; focusedAdvisorLabel = ""; }
  if (key === "lasso") clearLasso(false);
  if (key === "selects"){ selectsOnly = false; selectsBox.checked = false; selectsBox.closest(".switch").classList.remove("on"); }
  if (key === "raum"){ aumSel.clear(); syncTargetingUI(); }
  if (key === "reg") reg = "all";
  if (key === "exp") expSel.clear();
  if (key === "reach") reachSel.clear();
  if (key === "geo") geoSel.clear();
  if (key === "owner") ownerOnly = false;
  if (key === "ranked") rankedOnly = false;
  if (key === "contactable") contactableOnly = false;
  if (key === "assets") assetsOnly = false;
  if (key === "excluded") excludedFirms.clear();
  syncFilterButtons(); redraw();
});

// ---- advisor search ----
const advOut = document.getElementById("advisorResults");
let searchTimer = null;

function joinLabels(labels){
  if (labels.length < 2) return labels[0] || "";
  if (labels.length === 2) return `${labels[0]} and ${labels[1]}`;
  return `${labels.slice(0, -1).join(", ")}, and ${labels[labels.length - 1]}`;
}

function captureAdvisorFilters(){
  return {
    selectedFirms:[...selectedFirms], selectsOnly,
    aum:[...aumSel], reg, exp:[...expSel], reach:[...reachSel], geo:[...geoSel], ownerOnly, rankedOnly, excluded:[...excludedFirms],
  };
}

function restoreAdvisorFilters(saved){
  selectedFirms = [...saved.selectedFirms];
  syncFirmColors();
  document.getElementById("clearFirms").hidden = !selectedFirms.length;
  selectsOnly = saved.selectsOnly;
  selectsBox.checked = selectsOnly;
  selectsBox.closest(".switch").classList.toggle("on", selectsOnly);
  // These Sets must be MUTATED, never reassigned. multiSelect() closes over the
  // Set object it was wired to at startup, so swapping in a fresh Set left the
  // toggle handlers writing to an orphan while the predicates read the
  // replacement: experience, registration-reach and map-quality silently
  // stopped filtering after any advisor navigation or Back. `reg` survived only
  // because it is a string read by name rather than a captured object.
  refill(aumSel, saved.aum);
  reg = saved.reg;
  refill(expSel, saved.exp); refill(reachSel, saved.reach); refill(geoSel, saved.geo);
  ownerOnly = !!saved.ownerOnly;
  rankedOnly = !!saved.rankedOnly;
  excludedFirms = new Set(saved.excluded || []);
  syncTargetingUI(); syncFilterButtons();
}

function refill(set, values){
  set.clear();
  (values || []).forEach(v => set.add(v));
}

function relaxFiltersForAdvisor(features, advisorName, announce=true){
  const props = features.map(feature => feature.properties);
  if (!props.length) return [];
  const cleared = [];
  const add = label => { if (!cleared.includes(label)) cleared.push(label); };

  if (continentalOnly && !props.some(passesContinental)){
    setContinentalOnly(false, false); add("Continental U.S. only");
  }
  if (selectedFirms.length && !props.some(p => selectedFirms.includes(p.fc))){
    selectedFirms = []; firmColor = {}; document.getElementById("clearFirms").hidden = true;
    add("the selected-firm filter");
  }
  if (selectsOnly && !props.some(p => p.sg === 1)){
    selectsOnly = false; selectsBox.checked = false;
    selectsBox.closest(".switch").classList.remove("on"); add("Reports selecting outside managers");
  }
  if (raumActive() && !props.some(passesRaum)){
    aumSel.clear();
    syncTargetingUI(); add("the firm AUM range");
  }
  const passesReg = p => reg === "all" || (reg === "dual" ? p.d === 1 : p.d === 0);
  if (reg !== "all" && !props.some(passesReg)){ reg = "all"; add("the registration filter"); }
  if (expSel.size && !props.some(passesExp)){ expSel.clear(); add("the experience filter"); }
  if (reachSel.size && !props.some(passesReach)){ reachSel.clear(); add("the registration-reach filter"); }
  if (geoSel.size && !props.some(passesQuality)){ geoSel.clear(); add("the map-quality filter"); }
  if (ownerOnly && !props.some(passesOwner)){ ownerOnly = false; add("the owners-and-officers filter"); }
  if (rankedOnly && !props.some(passesRanked)){ rankedOnly = false; add("the ranked-advisor filter"); }
  if (contactableOnly && !props.some(passesContactable)){ contactableOnly = false; add("the contact-data filter"); }
  if (assetsOnly && !props.some(passesHasAssets)){ assetsOnly = false; add("the assets-on-file filter"); }
  if (lassoPolygon && !props.some(passesGeography)){ clearLasso(false); add("the map lasso"); }

  // Distinct office records can each satisfy a different constraint while no
  // single pin satisfies their combination. In that rare case, clear the
  // remaining view filters rather than navigating to another invisible pin.
  if (!props.some(passesFilters)){
    reg = "all";
    expSel.clear(); reachSel.clear(); geoSel.clear(); resetTargeting();
    selectedFirms = []; firmColor = {}; document.getElementById("clearFirms").hidden = true;
    if (lassoPolygon) clearLasso(false);
    add("the remaining conflicting filters");
  }
  syncTargetingUI(); syncFilterButtons();
  if (announce && cleared.length) showNotice(`To show ${advisorName}, cleared ${joinLabels(cleared)}.`);
  return cleared;
}

// One input drives both result sections directly. It used to proxy to two
// hidden inputs by dispatching synthetic events, which is how the visible box
// came to hold stale text after a scope change: resetForScopeChange cleared the
// proxy and left the field the user was actually looking at untouched.
function clearSearch(redrawMap = false){
  clearTimeout(searchTimer);
  searchBox.value = "";
  advQuery = ""; locSug = [];
  locOut.innerHTML = ""; locOut.hidden = true;
  advOut.innerHTML = ""; advOut.hidden = true;
  globalOut.hidden = true;
  if (redrawMap) redraw();
}

// The results list is portalled to the viewport, for the same reason the
// sidebar tooltips are: the panel header is a capped, scrolling box
// (overflow-y:auto), and an absolutely-positioned child of it gets cropped at
// its edge -- the list was being cut off by the KPI cards below. A fixed
// element is positioned against the viewport instead, so no ancestor's
// overflow can clip it, and it is re-anchored whenever anything moves.
function positionGlobalResults(){
  if (globalOut.hidden) return;
  const anchor = searchBox.getBoundingClientRect();
  const margin = 10, gap = 6;
  const width = anchor.width;
  let top = anchor.bottom + gap;
  // flip above the box if there is more room up there
  const below = window.innerHeight - top - margin;
  const above = anchor.top - margin - gap;
  const cap = Math.min(420, window.innerHeight * 0.48);
  if (below < Math.min(cap, 180) && above > below){
    globalOut.style.maxHeight = `${Math.max(120, Math.min(cap, above))}px`;
    top = Math.max(margin, anchor.top - gap - globalOut.getBoundingClientRect().height);
  } else {
    globalOut.style.maxHeight = `${Math.max(120, Math.min(cap, below))}px`;
  }
  globalOut.style.left = `${Math.round(anchor.left)}px`;
  globalOut.style.top = `${Math.round(top)}px`;
  globalOut.style.width = `${Math.round(width)}px`;
}

function runSearch(){
  if (!searchBox.value.trim()){ clearSearch(); return; }
  renderLocSuggest();
  advQuery = searchBox.value.trim().toLowerCase();
  renderAdvisorResults();
  globalOut.hidden = locOut.hidden && advOut.hidden;
  positionGlobalResults();
}

// Anchored to a scrolling element, so it has to follow it rather than be set
// once. Capture phase catches scrolls inside the panel and the header alike.
addEventListener("scroll", positionGlobalResults, true);
addEventListener("resize", positionGlobalResults);

searchBox.addEventListener("input", () => {
  clearTimeout(searchTimer);
  if (!searchBox.value.trim()){
    const wasFocused = !!focusedAdvisorId;
    focusedAdvisorId = null; focusedAdvisorLabel = "";
    clearSearch(wasFocused);
    return;
  }
  searchTimer = setTimeout(runSearch, 140);
});
// On first focus, warm the two SMALL things a search needs: the 425 KB search
// manifest and the firm aliases. The 6.9 MB advisor index is no longer on this
// path at all; an advisor card requests it only when its filed-name and
// out-of-scope-office enrichment is actually needed.
searchBox.addEventListener("focus", () => {
  loadSearchManifest().catch(() => {});
  loadFirmAliases().catch(() => {});
}, { once: true });
searchBox.addEventListener("keydown", e => {
  if (e.key === "Escape"){
    e.preventDefault();
    focusedAdvisorId = null; focusedAdvisorLabel = "";
    clearSearch(true);
  } else if (e.key === "Enter"){
    e.preventDefault();
    clearTimeout(searchTimer);
    runSearch();
    // Enter used to take whichever result rendered first in the DOM, and
    // Locations always renders first -- so a firm name that shares a prefix
    // with a city jumped to the city. Pick by what the query looks like.
    const query = searchBox.value.trim();
    const locationLike = looksLikeLocation(query);
    // A location-shaped query never falls through to an unrelated advisor or
    // firm merely because place data has not arrived. While GEO is unavailable
    // an alphabetic query is ambiguous; visible firm/advisor results remain
    // clickable, but Enter waits rather than guessing.
    if (!locationLike && SUPPORT.geo !== "ready" && /^[a-z .'-]{2,}$/i.test(query)){
      showNotice(SUPPORT.geo === "failed"
        ? "City search is unavailable. Choose a visible firm or advisor result, or search by state."
        : "City search is still loading. Choose a visible firm or advisor result, or wait a moment.");
      return;
    }
    const first = locationLike
      ? locOut.querySelector(".lres")
      : advOut.querySelector(".ares") || locOut.querySelector(".lres");
    if (first) first.click();
    else if (locationLike && SUPPORT.geo !== "ready")
      showNotice(SUPPORT.geo === "failed"
        ? "City and ZIP search is unavailable; state search still works."
        : "City and ZIP search is still loading; state search already works.");
  }
});
// Each result type owns what happens to the query text -- a firm result clears
// it because the map now shows that firm, an advisor result keeps it so the
// next name in the list is still one click away. A delegated handler here used
// to overwrite the box for both, which fought openNationalAdvisor's own
// save/restore of the query across the scope switch.
globalOut.addEventListener("click", e => {
  if (!e.target.closest(".ares")) return;
  locOut.innerHTML = ""; locOut.hidden = true;
  globalOut.hidden = true;
});

// A query reads as a location when it is a ZIP, a state code or name, or an
// exact city we hold. Anything else is treated as a firm or advisor name.
function looksLikeLocation(q){
  if (!q) return false;
  if (/^\d{3,5}$/.test(q)) return true;
  const up = q.toUpperCase();
  if (STATE_NAMES[up]) return true;
  if (Object.values(STATE_NAMES).some(name => name.toUpperCase() === up)) return true;
  return !!(GEO && GEO.cities[up]);
}

function renderAdvisorResults(){
  if (!advQuery){ advOut.hidden = true; advOut.innerHTML = ""; return; }
  renderNationalSearch();
}

// Former/other firm names, for search only, so it is fetched beside the advisor
// index rather than on every visit. "Bear Stearns", "Smith Barney", "Alex.
// Brown" and "PaineWebber" all resolve to the surviving CRD through this.
let FIRM_ALIASES = null;
let FIRM_ALIAS_PROMISE = null;
let FIRM_ALIAS_ERROR = "";
function loadFirmAliases(){
  if (FIRM_ALIASES) return Promise.resolve(FIRM_ALIASES);
  if (!FIRM_ALIAS_PROMISE){
    FIRM_ALIAS_PROMISE = fetch(dataUrl("firm_aliases.json"))
      .then(r => { if (!r.ok) throw new Error(`firm aliases ${r.status}`); return r.json(); })
      .then(j => { FIRM_ALIAS_ERROR = ""; FIRM_ALIASES = j; return j; })
      .catch(err => {
        FIRM_ALIAS_ERROR = err.message || String(err);
        FIRM_ALIASES = {};
        return FIRM_ALIASES;
      });
  }
  return FIRM_ALIAS_PROMISE;
}

// Firm names carry punctuation a person typing will not: the alias is
// "BEAR, STEARNS & CO.", so a raw substring test never matches "bear stearns".
// Compare on a form with punctuation and ampersands flattened.
function looseName(value){
  return String(value || "").toLowerCase().replace(/&/g, " and ")
    .replace(/[^\w\s]/g, " ").replace(/\s+/g, " ").trim();
}
// "J.P. MORGAN" flattens to "j p morgan", but nobody types the spaces between
// initials, so also compare with whitespace removed entirely: "jpmorgan"
// matches "jpmorgansecuritiesllc".
function tightName(value){ return looseName(value).replace(/\s+/g, ""); }
function looseIncludes(haystack, needle){
  if (!needle) return false;
  const loose = looseName(haystack);
  if (loose.includes(needle)) return true;
  const tight = needle.replace(/\s+/g, "");
  return tight.length >= 3 && loose.replace(/\s+/g, "").includes(tight);
}

// the alias that matched, so the result can say why a firm appeared
function aliasHit(crd, loose){
  const list = FIRM_ALIASES && FIRM_ALIASES[String(crd)];
  if (!list) return "";
  return list.find(n => looseIncludes(n, loose)) || "";
}

/* CRD -> that advisor's row.
 *
 * Built once when the index loads, because four of the five things this file
 * is used for are CRD lookups, and every one of them was an Array.find() over
 * 412,567 rows. The list view does it INSIDE its loop over visible advisors,
 * so a viewport holding 500 people cost roughly 200 million string
 * comparisons to fetch a filed-as name.
 *
 * Search is the only name-keyed use, and it scans deliberately.
 */
let ADV_BY_CRD = null;
let ADV_INDEX_ERROR = "";

function advisorRow(crd){
  if (!ADV_INDEX) return null;
  if (!ADV_BY_CRD){
    ADV_BY_CRD = new Map();
    for (const row of ADV_INDEX.advisors) ADV_BY_CRD.set(String(row[0]), row);
  }
  return ADV_BY_CRD.get(String(crd)) || null;
}

function loadAdvisorIndex(){
  if (ADV_INDEX) return Promise.resolve(ADV_INDEX);
  if (!ADV_INDEX_PROMISE){
    ADV_INDEX_PROMISE = fetch(dataUrl("advisor_index.json")).then(r => {
      if (!r.ok) throw new Error("national advisor index unavailable");
      return r.json();
    }).then(j => {
      ADV_INDEX_ERROR = "";
      ADV_INDEX = j;
      ADV_BY_CRD = null;
      return j;
    }).catch(err => {
      ADV_INDEX_ERROR = err.message || String(err);
      throw err;
    });
  }
  return ADV_INDEX_PROMISE;
}

/* ---- sharded national advisor search -------------------------------------
 *
 * advisor_index.json is 6.9 MB gzipped and was fetched on the first focus of
 * the search box, so the first search of a session waited for all of it. On a
 * phone that is five to fifteen seconds of a box that looks broken.
 *
 * build_advisor_search.py splits the same 412,567 advisors into prefix shards
 * -- median 2 KB, only 46 of 3,942 above 200 KB -- plus one 425 KB manifest
 * carrying the firm and city dictionaries the result rows index into. Typing
 * "tolm" fetches one small file. This is the scheme the field app has run in
 * production since build_name_index.py, ported rather than invented.
 *
 * The full index still loads, in the background, because advisorRow() answers
 * four other things from it -- card rendering, the list view, owner lookup,
 * history. It simply no longer blocks a keystroke.
 */
let SEARCH_MAN = null, SEARCH_MAN_PROMISE = null, SEARCH_MAN_ERROR = "";
const SEARCH_SHARDS = new Map();      // prefix -> rows[], once resolved
const SEARCH_PENDING = new Map();     // prefix -> Promise, while in flight

function loadSearchManifest(){
  if (SEARCH_MAN) return Promise.resolve(SEARCH_MAN);
  if (!SEARCH_MAN_PROMISE){
    SEARCH_MAN_PROMISE = fetch(dataUrl("advisor_search.json"))
      .then(r => { if (!r.ok) throw new Error("advisor search index unavailable");
                   return r.json(); })
      .then(j => { SEARCH_MAN = j; SEARCH_MAN_ERROR = ""; return j; })
      .catch(err => { SEARCH_MAN_ERROR = err.message || String(err); throw err; });
  }
  return SEARCH_MAN_PROMISE;
}

// The query as name tokens, lowercased -- the normalisation tokens_for()
// applies at build time. Without it "O'Brien" and "Smith-Jones" address the
// wrong shard.
function queryTokens(q){
  return String(q || "").toLowerCase().split(/[^a-z]+/).filter(Boolean);
}

/* The token a shard is chosen by: the LONGEST one in the query.
 *
 * Not the concatenation, and not the first. Stripping the space out of "bob
 * smith" produced the key "bobsmith", whose two-letter prefix addresses a
 * shard no real token starts -- so a search that works today returned nothing.
 * And taking the first token sends "bob smith" to the heavily split "bob",
 * where Bob Smith does not live: nickname expansion files him under "bobb"
 * (from "bobby") and "smit". The longest token is both the most selective and
 * the most likely to be a real surname.
 */
function searchKey(q){
  const toks = queryTokens(q);
  if (!toks.length) return "";
  return toks.reduce((a, b) => (b.length > a.length ? b : a));
}

/* Which shard answers this query, or "" when it cannot address one.
 *
 * Prefixes listed in `split` were too heavy at that length and were rebuilt a
 * character deeper, so a query stopping exactly there has no shard of its own
 * -- except the "." sentinel, holding people whose whole token IS the prefix
 * ("Jon", "Lee", "Ng"). Those are returned, and the caller says that typing
 * more will find the rest.
 */
function shardFor(q){
  if (!SEARCH_MAN) return { prefix:"", exhausted:false };
  const key = searchKey(q);
  if (key.length < SEARCH_MAN.prefix) return { prefix:"", exhausted:false };
  let take = SEARCH_MAN.prefix;
  const split = new Set(SEARCH_MAN.split || []);
  while (split.has(key.slice(0, take)) && take < key.length) take += 1;
  const prefix = key.slice(0, take);
  if (split.has(prefix)) return { prefix: prefix + ".", exhausted:true };
  return { prefix, exhausted:false };
}

function crdShardFor(q){
  if (!SEARCH_MAN) return "";
  const n = SEARCH_MAN.crdPrefix || 3;
  return q.length >= n ? `crd/${q.slice(0, n)}` : "";
}

function loadSearchShard(prefix){
  if (!prefix) return Promise.resolve([]);
  if (SEARCH_SHARDS.has(prefix)) return Promise.resolve(SEARCH_SHARDS.get(prefix));
  if (SEARCH_PENDING.has(prefix)) return SEARCH_PENDING.get(prefix);
  const p = fetch(dataUrl(`search/${prefix}.json`))
    // A missing shard is not an error: no advisor's name starts that way.
    .then(r => r.ok ? r.json() : { rows: [] })
    .then(j => { const rows = j.rows || []; SEARCH_SHARDS.set(prefix, rows); return rows; })
    .catch(() => { SEARCH_SHARDS.set(prefix, []); return []; })
    .finally(() => SEARCH_PENDING.delete(prefix));
  SEARCH_PENDING.set(prefix, p);
  return p;
}

/* Rows matching q, from whatever is already in memory.
 *
 * Synchronous on purpose: renderNationalSearch() runs on every keystroke and
 * must paint with what it has. Anything missing comes back as `pending` so the
 * caller can fetch and re-render -- the same way it already handles national
 * detail and firm aliases arriving late.
 */
function shardSearch(q){
  const numeric = /^\d+$/.test(q);
  const target = numeric ? crdShardFor(q) : shardFor(q).prefix;
  if (!target) return { rows: [], pending: "", exhausted: false };
  if (!SEARCH_SHARDS.has(target)) return { rows: [], pending: target, exhausted: false };
  const lower = q.toLowerCase();
  const want = queryTokens(q);
  const out = [];
  for (const row of SEARCH_SHARDS.get(target)){
    if (numeric){
      if (String(row[0]).startsWith(q)) out.push(row);
      continue;
    }
    /* EVERY query token must appear somewhere in the row, rather than the whole
     * query appearing as one substring of the name.
     *
     * row[7] carries the tokens NOT derivable from the displayed name -- the
     * nickname forms -- so this is what lets "bill" find William Kaiser, which
     * the old whole-file substring scan could never do. Per-token matching
     * extends that to "bob smith" finding Robert Smith: "bob" matches the alt
     * column, "smith" the name. A single substring test over the display name
     * could not, because those two words never appear adjacent anywhere.
     */
    const hay = `${row[1]} ${row[6] || ""} ${row[7] || ""}`.toLowerCase();
    if (want.every(t => hay.includes(t))) out.push(row);
  }
  /* RANK, THEN CAP -- in that order, or the cap silently undoes the feature.
   *
   * The shard is stored alphabetically, so capping first meant searching
   * "bill" returned sixty Billeters and Billupses and not one William: the
   * Williams sort last and never survived the slice. That is the exact
   * complaint build_name_index.py records about an earlier index, reached by a
   * different route -- ordering rather than filtering.
   *
   * A whole token equal to the query beats a token merely starting with it,
   * which beats a match buried mid-string. So "bill" leads with the Bills and
   * the Williams filed as Bill, then the Billupses.
   */
  if (!numeric && out.length > 1){
    const rank = (row) => {
      const toks = `${row[1]} ${row[6] || ""} ${row[7] || ""}`
        .toLowerCase().split(/[^a-z]+/).filter(Boolean);
      if (want.every(t => toks.includes(t))) return 0;
      if (want.every(t => toks.some(h => h.startsWith(t)))) return 1;
      return 2;
    };
    out.sort((a, b) => rank(a) - rank(b) || a[1].localeCompare(b[1]));
  }
  return { rows: out.slice(0, 60), pending: "",
           exhausted: !numeric && shardFor(q).exhausted };
}

// firms and cities are indexes into dictionaries. They ride in the search
// manifest so a result row can name a firm and a city without the full index,
// and fall back to it for anything rendered before the manifest arrives.
function searchFirm(i){
  const d = (SEARCH_MAN && SEARCH_MAN.firms) || (ADV_INDEX && ADV_INDEX.firms) || [];
  return d[i] || "";
}
function searchCity(i){
  const d = (SEARCH_MAN && SEARCH_MAN.cities) || (ADV_INDEX && ADV_INDEX.cities) || [];
  return d[i] || "";
}

function renderNationalSearch(){
  const q = advQuery;
  advOut.hidden = false;
  if (q.length < 2){
    advOut.innerHTML = `<p class="hint">Enter at least two characters to search nationally.</p>`;
    return;
  }
  if (!NAT_DETAIL_READY && !natDetailPromise && !NAT_DETAIL_ERROR){
    loadNationalDetail(null, false).then(() => {
      if (NAT_DETAIL_READY && advQuery === q) renderNationalSearch();
    });
  }
  const rerender = () => { if (advQuery === q) renderNationalSearch(); };
  if (!FIRM_ALIASES) loadFirmAliases().then(rerender);
  // The manifest, then the one shard this query needs. Neither is the 6.9 MB
  // file: that loads in the background for advisorRow() and is not waited on.
  if (!SEARCH_MAN && !SEARCH_MAN_ERROR) loadSearchManifest().then(rerender).catch(rerender);
  const hits = shardSearch(q);
  if (hits.pending) loadSearchShard(hits.pending).then(rerender);

  const loose = looseName(q);
  const firmRows = NAT_DETAIL_READY ? NAT.firms.filter(f =>
    looseIncludes(f[0], loose) || String(f[1]).includes(q) ||
    !!aliasHit(f[1], loose)).slice(0, 20) : [];
  // Matching now happens inside shardSearch(), against one small file rather
  // than a scan of all 412,567 rows. The shard row is deliberately the same
  // shape as an advisor_index row, so everything below is unchanged.
  const advisorRows = hits.rows;
  const firmsHtml = firmRows.length
    ? `<p class="result-label">Firms</p>` + firmRows.map((f, i) =>
      `<button type="button" class="ares" data-national-firm="${i}"><span class="an">${esc(f[0])}</span>` +
      `<span class="af">Firm CRD ${esc(f[1])}${f[3] != null ? ` · ${fmtMoney(f[3] * 1e6)} total RAUM` : ""}` +
      `${aliasHit(f[1], loose) && !looseIncludes(f[0], loose) ? ` · formerly ${esc(aliasHit(f[1], loose))}` : ""}</span></button>`).join("")
    : `<p class="result-label">Firms</p><p class="hint">${NAT_DETAIL_ERROR
        ? "Firm search is unavailable because national office detail could not be loaded."
        : !NAT_DETAIL_READY
          ? "Loading firm search…"
          : FIRM_ALIAS_ERROR
            ? `No current-name firm match. Former-name search is unavailable (${esc(FIRM_ALIAS_ERROR)}).`
            : !FIRM_ALIASES
              ? "No current-name firm match; checking former names…"
              : `No firm match for “${esc(searchBox.value)}”.`}</p>`;
  const currentTerritory = scope.startsWith("T:")
    ? scope.slice(2) : (scope === "US" ? "" : STATE_TO_TERRITORY[scope] || "");
  const advisorsHtml = advisorRows.length
    ? `<p class="result-label">Advisors${advisorRows.length === 60 ? " · first 60" : ""}${
      // A split prefix holds only the people whose whole name token IS these
      // letters -- "Jo" the name, not every John. Saying so matters: silently
      // omitting the Johns is the one way this is worse than the old
      // whole-file scan, and a rep would have no reason to suspect it.
      hits.exhausted ? " · only exact matches for these letters; type one more for the rest" : ""}</p>` + advisorRows.map((row, i) => {
      const territory = STATE_TO_TERRITORY[row[3]] || "Outside assigned territories";
      const outside = currentTerritory && territory !== currentTerritory
        ? " · outside current territory" : "";
      const shownName = advisorDisplayName(row[0], row[1]);
      return `<div class="ares-person"><button type="button" class="ares" data-national-advisor="${i}"><span class="an">${esc(shownName)}</span>` +
        `<span class="af">${esc(searchFirm(row[2]))} · CRD ${esc(row[0])}` +
        `${row[6] ? ` · filed as ${esc(row[6])}` : ""}</span>` +
        `<span class="ac">${esc(searchCity(row[4]))}, ${esc(row[3])} · ${esc(territory)}${outside}${row[5].includes("|") ? ` · also ${esc(row[5].split("|").filter(s => s !== row[3]).join(", "))}` : ""}</span></button>`
        + personActionButton(row[0], shownName) + `</div>`;
    }).join("")
    : `<p class="result-label">Advisors</p><p class="hint">${SEARCH_MAN_ERROR
        ? "National advisor search is unavailable."
        : (!SEARCH_MAN || hits.pending)
          ? "Loading national advisor search…"
          : hits.exhausted
            ? `Too many advisors match “${esc(searchBox.value)}” — type one more letter.`
            : `No advisor match for “${esc(searchBox.value)}”.`}</p>`;
  advOut.innerHTML = firmsHtml + advisorsHtml;
  advOut.querySelectorAll("[data-national-firm]").forEach(el => el.addEventListener("click", () => {
    const firm = firmRows[+el.dataset.nationalFirm];
    clearSearch();
    navigateToFirm(String(firm[1]));
  }));
  advOut.querySelectorAll("[data-national-advisor]").forEach(el => el.addEventListener("click", () =>
    openNationalAdvisor(advisorRows[+el.dataset.nationalAdvisor])));
}

function openNationalAdvisor(row){
  const id = String(row[0]), state = row[3];
  const label = advisorDisplayName(id, row[1]);
  const priorDetail = detailsCurrent
    ? { entry:detailsCurrent, map:captureDetailMap() } : null;
  map.closePopup();
  focusedAdvisorId = null; focusedAdvisorLabel = "";
  const territory = STATE_TO_TERRITORY[state];
  const targetScope = territory ? terrKey(territory) : state;
  const priorScope = scope;
  const priorLasso = !!lassoPolygon;
  const savedQuery = searchBox.value;
  const savedFilters = captureAdvisorFilters();
  const load = targetScope === scope ? Promise.resolve() : switchScope(targetScope);
  load.then(() => {
    if (scope !== targetScope){
      showNotice(`Could not load ${territory || scopeLabel(targetScope)} for ${label}.`);
      return;
    }
    restoreAdvisorFilters(savedFilters);
    searchBox.value = savedQuery; advQuery = savedQuery.trim().toLowerCase();
    const features = ALL.filter(f => String(f.properties.id) === id);
    if (!features.length){
      showNotice(`${label} is in the national index but has no mapped pin in ${territory || scopeLabel(targetScope)}.`);
      return;
    }
    const cleared = relaxFiltersForAdvisor(features, label, false);
    if (priorLasso && targetScope !== priorScope && !cleared.includes("the map lasso"))
      cleared.push("the map lasso");
    focusedAdvisorId = id; focusedAdvisorLabel = label;
    redraw(); advOut.hidden = true; advOut.innerHTML = "";
    const visible = features.filter(f => passesFilters(f.properties));
    if (visible.length){
      flyToAdvisorGroup({ feats:visible });
      const advisorEntry = { type:"advisor", feature:visible[0] };
      if (priorDetail && detailKey(priorDetail.entry) !== detailKey(advisorEntry))
        detailsHistory.push(priorDetail);
      detailsCurrent = null;
      openAdvisorDetails(visible[0], false);
    }
    const switched = targetScope !== priorScope;
    const territoryLabel = territory ? `${territory} sales territory` : scopeLabel(targetScope);
    if (switched && cleared.length)
      showNotice(`Switched to ${territoryLabel} and cleared ${joinLabels(cleared)} to show ${label}.`);
    else if (switched)
      showNotice(`Switched to ${territoryLabel} to show ${label}.`);
    else if (cleared.length)
      showNotice(`To show ${label}, cleared ${joinLabels(cleared)}.`);
  });
}

function flyToAdvisorGroup(row){
  if (!row || !row.feats.length) return;
  if (row.feats.length === 1) return flyTo(row.feats[0]);
  const points = row.feats.map(f => [f.geometry.coordinates[1], f.geometry.coordinates[0]]);
  map.fitBounds(points, { padding:[28,28], maxZoom:14 });
}

function flyTo(f){
  const [lon, lat] = f.geometry.coordinates;
  // A territory render creates a fresh MarkerCluster and fills it in chunks.
  // zoomToShowLayer can wait indefinitely if called while that queue is still
  // attaching markers. Move the map and open the selected profile independently
  // so neither action depends on cluster timing.
  map.setView([lat, lon], 16, { animate:false });
}

// ---- address roster ----
// every advisor filed at one address, grouped by firm. Shows the whole building
// even when filters are narrowing the map, with a note on how many still match.
// features backing the rendered rows, in render order, so a row click can find
// its advisor -- indices are rebuilt on every openRoster
let rosterRows = [];

function rosterBtn(a, label){
  return `<button class="rbtn" data-a="${a.i}"${a.bldg ? ' data-bldg="1"' : ""}>${esc(label)}</button>`;
}

// The building an address sits in, offered only when it holds more people than
// the address itself. Keyed off the ADDR record's own coordinate so no lookup
// of the originating feature is needed.
function bldgForAddr(a, street){
  if (!a) return null;
  const b = BLDG.get(`${a.lat.toFixed(5)},${a.lon.toFixed(5)}|${houseNo(street)}`);
  return (b && b.ids.size > a.ids.size) ? b : null;
}

// onlyFirm scopes the roster to one firm at this building -- what a firm spoke
// means when you click it. The building's other firms stay one click away.
function openRoster(a, detailPush=true, onlyFirm=null){
  if (!a) return;
  beginDetails({ type:"location", location:a }, detailPush);
  openFirmCrd = null;
  rosterRows = [];
  const seen = new Map();                       // one row per advisor, not per pin
  a.feats.forEach(f => {
    if (onlyFirm && String(f.properties.fc) !== String(onlyFirm)) return;
    if (!seen.has(f.properties.id)) seen.set(f.properties.id, f);
  });
  const rows = [...seen.values()];
  const allFirms = new Set(a.feats.map(f => String(f.properties.fc)));
  const matching = rows.filter(f => passesFilters(f.properties)).length;

  const byFirm = {};
  rows.forEach(f => {
    const p = f.properties;
    (byFirm[p.fc] ||= { crd:p.fc, name:p.f, rows:[] }).rows.push(f);
  });
  const groups = Object.values(byFirm).sort((x, y) => y.rows.length - x.rows.length);

  const head = [
    `${rows.length.toLocaleString()} advisor${rows.length === 1 ? "" : "s"}`,
    `${groups.length} firm${groups.length === 1 ? "" : "s"}`,
  ];
  if (a.bldg && a.lines.size > 1) head.push(`${a.lines.size} filed address lines`);
  const unsure = rows.filter(f => f.properties.unc).length;
  if (unsure) head.push(`${unsure.toLocaleString()} address uncertain`);
  if (matching < rows.length) head.push(`${matching.toLocaleString()} match current filters`);

  document.getElementById("detailsKind").textContent = "Location";
  // A city-level group has no street by definition, so the heading would be
  // blank. Name the town instead and say plainly that it is not an address.
  const cityOnly = rows.length && rows.every(f => f.properties.lt === 1);
  document.getElementById("firmOverviewName").innerHTML = a.addr
    ? `${esc(a.addr)}<br>${esc([a.city, a.zip].filter(Boolean).join(" "))}`
    : `${esc(a.city || "Unnamed location")}` +
      (cityOnly ? `<br><span class="head-sub">city-level &mdash; no street address filed</span>` : "");
  document.getElementById("firmOverviewMeta").textContent = head.join(" · ");
  if (onlyFirm && allFirms.size > 1)
    document.getElementById("detailsKind").textContent =
      `Location · ${rows.length ? rows[0].properties.f : "firm"} only`;

  const showAll = onlyFirm && allFirms.size > 1
    ? `<div class="profile-actions"><button type="button" data-a="${a.i}"` +
      `${a.bldg ? ' data-bldg="1"' : ""}>Show all ${allFirms.size} firms at this address</button></div>`
    : "";
  document.getElementById("firmOverviewBody").innerHTML = showAll + groups.map(group => {
    const firm = group.name, crd = group.crd, list = group.rows;
    const col = selectedFirms.length
      ? (firmColor[crd] || cssVar("--m-unc"))
      : cssVar("--accent");
    const people = list
      .slice()
      .sort((x, y) => x.properties.n.localeCompare(y.properties.n))
      .map(f => {
        const p = f.properties;
        const dim = passesFilters(p) ? "" : " dim";
        const bits = [
          p.x != null ? `${p.x.toFixed(0)} yrs` : null,
          p.d ? "Dually registered" : "RIA-only",
          p.ns != null ? `${p.ns} state${p.ns === 1 ? "" : "s"}` : null,
          p.g ? esc(p.g.split("|").join(", ")) : null,
        ].filter(Boolean).join(" · ");
        // disclosures are the one thing a rep must not have to click to discover
        const flag = p.dr && p.dr.length
          ? `<span class="rflag" title="${esc(p.dr.join(", "))}">⚠</span>` : "";
        // principals should be findable by scanning a building roster
        const owns = ownerRolesFor(p.id).find(r => String(r.firmCrd) === String(p.fc));
        const ownTag = owns
          ? `<span class="rown${owns.code === "NA" ? " rown-officer" : ""}" ` +
            `title="${esc(owns.title || "Owner or officer")}">${roleTag(owns.code)}</span>` : "";
        const uncTag = p.unc
          ? `<span class="runc" title="Employment record says ${esc(p.home || "elsewhere")}">ADDRESS UNCERTAIN</span>`
          : (p.lt === 1
             ? `<span class="rcity" title="City and state filed, no street address">CITY&nbsp;LEVEL</span>` : "");
        const barTag = barronsTag(p.id);
        const forTag = forbesTag(p.id);
        // in building mode the person's own filed line is the point -- it is
        // what tells you they are on floor 6 rather than floor 5
        const line = (a.bldg && p.a && p.a !== a.addr)
          ? `<span class="rline">${esc(p.a)}</span>` : "";
        const link = p.u
          ? `<a class="rlink" href="${esc(p.u)}" target="_blank" rel="noopener"
                title="Open ${esc(p.n)} on IAPD">&#8599;</a>`
          : "";
        return `<div class="rrow${dim}" data-ri="${rosterRows.push(f) - 1}" role="button" tabindex="0">
          <span class="rrl"><span class="rn">${esc(p.n)}${flag}${contactDots(p.id)}${barTag}${forTag}${ownTag}${uncTag}</span>
          <span class="rm">${bits}</span>${line}</span>${personActionButton(p.id, p.n)}${link}</div>`;
      }).join("");
    return `<div class="rgrp">
      <div class="rfirm"><span class="swatch" style="background:${col}"></span>
        <button type="button" class="rft detail-noun" data-firm-profile="${esc(crd)}" title="Open ${esc(firm)}">${esc(firm)} · CRD ${esc(crd)}</button>
        <span class="mono rfc">${list.length}</span>
        <span class="detail-chevron">›</span></div>
      ${people}</div>`;
  }).join("");

  syncDetailHash(null);
}

// Opening a location or a building from any drawer row now also takes the map
// there and rings it. The viewport moves only as far as it must: the zoom is
// never reduced, so drilling in from a close-up does not pull you back out.
document.addEventListener("click", e => {
  const b = e.target.closest("[data-a]");
  if (!b || b.dataset.a === undefined) return;
  const rec = b.dataset.bldg ? BLDG_BY_I[+b.dataset.a] : ADDR_BY_I[+b.dataset.a];
  if (!rec) return;
  openRoster(rec);
  map.setView([rec.lat, rec.lon], Math.max(map.getZoom(), 16), { animate:false });
  highlightLocation(rec);
  selectBuilding(buildingForRecord(rec));
});

// Drilling into the firm from an advisor or a location re-scopes the map
// without re-locating it: you asked to see the same place through a narrower
// lens. The fit rule is the one pickFirm already uses -- move only when staying
// put would show nothing at all.
document.addEventListener("click", e => {
  const b = e.target.closest("[data-firm-drill]");
  if (!b) return;
  const crd = String(b.dataset.firmDrill);
  highlightLocation(null);
  setSelectedFirm(crd);
  redraw(!anySelectedInView());
  openFirmOverview(crd);
});

// Clicking a teammate opens that person, the same way any other advisor on
// the map opens. Filters are relaxed first: a teammate is very often hidden by
// the rep's current filters, and silently doing nothing on click is worse than
// showing a person who does not match the screen.
document.addEventListener("click", async e => {
  const b = e.target.closest("[data-teammate]");
  if (!b) return;
  const id = String(b.dataset.teammate);
  const c = contactFor(id);
  const label = advisorDisplayName(id) || ("CRD " + id);
  // At national scope ALL is empty -- the map draws from the aggregate file
  // and loads per-state features only inside a scope. Without this the click
  // always reported "no mapped office", which is not what happened.
  const want = String(b.dataset.teammateState || "");
  let feats = ALL.filter(f => String(f.properties.id) === id);
  if (!feats.length && want && want !== scope){
    const outcome = await switchScope(want);
    if (!outcome || outcome.status !== "applied" || scope !== want) return;
    feats = ALL.filter(f => String(f.properties.id) === id);
  }
  if (!feats.length){
    showNotice(`${label} has no mapped office in ${scopeLabel(scope)}.`);
    return;
  }
  relaxFiltersForAdvisor(feats, feats[0].properties.n, false);
  focusedAdvisorId = id; focusedAdvisorLabel = feats[0].properties.n;
  redraw();
  flyToAdvisorGroup({ feats });
  detailsCurrent = null;
  openAdvisorDetails(feats[0]);
});

// jump to the same advisor's office in another state or territory
document.addEventListener("click", async e => {
  const b = e.target.closest("[data-advisor-elsewhere]");
  if (!b) return;
  const target = b.dataset.advisorElsewhere, id = String(b.dataset.advisorId);
  const label = focusedAdvisorLabel || b.querySelector("b")?.textContent || "this advisor";
  if (scope !== target){
    const outcome = await switchScope(target);
    if (!outcome || outcome.status !== "applied" || scope !== target) return;
  }
  const feats = ALL.filter(f => String(f.properties.id) === id);
  if (!feats.length){
    showNotice(`No mapped office for ${label} in ${scopeLabel(target)}.`);
    return;
  }
  relaxFiltersForAdvisor(feats, feats[0].properties.n, false);
  focusedAdvisorId = id; focusedAdvisorLabel = feats[0].properties.n;
  redraw();
  flyToAdvisorGroup({ feats });
  detailsCurrent = null;
  openAdvisorDetails(feats[0], false);
  showNotice(`Showing ${feats[0].properties.n} in ${scopeLabel(target)}.`);
});

// Clicking a roster row opens that advisor's popup. Everyone at one address
// shares a coordinate, so there is no marker to single out by filtering -- the
// popup is bound to the location directly, which also works for advisors the
// current filters have hidden from the map.
function openAdvisorPopup(f, row){
  openAdvisorDetails(f);
}

function openAdvisorDetails(f, detailPush=true){
  if (!f) return;
  beginDetails({ type:"advisor", feature:f }, detailPush);
  openFirmCrd = null;
  const p = f.properties;
  const at = ADDR.get(addrKey(p));
  document.getElementById("detailsKind").textContent = "Advisor";
  // Beside the NAME, not down in the contact block: these are a property of the
  // person, and a rep scanning the panel should see them in the same glance as
  // who they are looking at. textContent first so the name is escaped, then the
  // marks are appended as markup.
  const nameEl = document.getElementById("firmOverviewName");
  nameEl.textContent = p.n;
  nameEl.insertAdjacentHTML("beforeend", flagMarks(p.id));
  // the individual's own CRD is what a rep quotes on a call and pastes into the
  // CRM, so it belongs beside the name rather than only inside the IAPD link
  document.getElementById("firmOverviewMeta").textContent =
    [p.f, `CRD ${p.id}`, [p.c, p.z].filter(Boolean).join(" ")].filter(Boolean).join(" · ");
  // the prior-firm COUNT is gone from the badges: the History section below
  // names the firms and their dates, which is what the count was standing in for
  const bits = [
    p.x != null ? `${p.x.toFixed(0)} years experience` : "",
    p.d ? "Dually registered" : "RIA-only",
  ].filter(Boolean);
  const bl = bldgForAddr(at, p.a);
  const states = p.rs ? p.rs.split("|") : [];
  // Schedule A role at THIS firm, plus any held at another firm -- an advisor
  // who also owns a second RIA is worth surfacing, not hiding.
  const roles = ownerRolesFor(p.id);
  const here = roles.find(r => String(r.firmCrd) === String(p.fc));
  const elsewhereRoles = roles.filter(r => String(r.firmCrd) !== String(p.fc));
  // Whether the person you are looking at can actually decide anything is the
  // single most consequential fact on this card, so it leads rather than sitting
  // as grey text under the firm row.
  const roleLine = here
    ? `<div class="owner-callout">
         <div class="owner-callout-head">${esc(here.title || "Owner or officer")}</div>
         <div class="owner-callout-tags">
           ${here.code ? `<span class="owner-tag strong">${here.code === "NA"
             ? "Officer · holds under 5%" : `Owns ${esc(OWNERSHIP_BANDS[here.code] || here.code)}`}</span>` : ""}
           ${here.ctrl ? `<span class="owner-tag">Control person</span>` : ""}
           <span class="owner-tag muted">ADV Schedule A</span>
         </div>
       </div>`
    : "";
  // A Barron's ranking is third-party validation a rep can open a call with, so
  // it sits above the fold with the ownership callout. Every entry names its
  // list and year: the four lists rank different universes, and the
  // independent list is a year behind the other three.
  const barronsHits = barronsFor(p.id);
  const barronsLine = barronsHits.length
    ? `<div class="owner-callout barrons-callout">
         <div class="owner-callout-head">BARRON'S RANKED</div>
         <div class="owner-callout-tags">
           ${barronsHits.map(h =>
             `<a class="owner-tag strong barrons-tag" href="${esc(h.url)}" target="_blank"
                 rel="noopener" title="${esc(barronsTitle(h))} — open on Barron's">
                ${esc(barronsRankText(h))}<small>${esc(String(h.year || ""))}</small></a>`).join("")}
         </div>
         <p class="owner-callout-note">${esc(barronsHits.map(barronsTitle).join(" · "))}</p>
       </div>`
    : "";
  // Forbes sits beside Barron's but states its provenance, because most of
  // these CRDs were inferred rather than published. An inferred row says so on
  // its face: a rep about to open a call with a ranking needs to know whether
  // we are certain it is this person.
  const forbesHits = forbesFor(p.id);
  const teamAssets = forbesTeamAssets(p.id);
  const forbesLine = forbesHits.length
    ? `<div class="owner-callout forbes-callout">
         <div class="owner-callout-head">FORBES RANKED</div>
         <div class="owner-callout-tags">
           ${forbesHits.map(h =>
             `<a class="owner-tag strong forbes-tag${h.confirmed ? "" : " forbes-inferred"}"
                 href="${esc(h.url)}" target="_blank" rel="noopener"
                 title="${esc(h.full)}">${h.confirmed ? "" : "&#8776;"}${esc(h.label)}</a>`).join("")}
         </div>
         <p class="owner-callout-note">${esc(forbesHits.map(h => h.full).join(" · "))}</p>
         ${forbesHits.every(h => h.confirmed) ? "" :
           `<p class="owner-callout-note forbes-caveat">&#8776; Identified by matching name,
              firm and location &mdash; Forbes publishes no CRD. Correct for 99.6% of the
              advisors whose CRD is independently known.</p>`}
         ${teamAssets != null
           ? `<p class="owner-callout-note forbes-assets"><b>${esc(fmtMoney(teamAssets))}</b>
                team assets <span class="mini-note">the whole team's book, not this
                advisor's alone</span></p>` : ""}
       </div>`
    : "";
  // Queued after the body is written, below.
  const historyFor = p.id;
  const roleRows = elsewhereRoles.map(r =>
    `<button type="button" class="detail-row" data-firm-profile="${esc(r.firmCrd)}">` +
    `<span class="detail-row-main"><b>Also ${esc(r.title || "an owner")} at another firm</b>` +
    `<small>CRD ${esc(r.firmCrd)}${r.ctrl ? " · control person" : ""}</small></span>` +
    `<span class="detail-chevron">›</span></button>`).join("");
  document.getElementById("firmOverviewBody").innerHTML = `
    ${contactBlock(p)}
    ${barronsLine}
    ${forbesLine}
    ${roleLine}
    <section class="profile-section"><h3>Firm</h3><div class="detail-list">
      <button type="button" class="detail-row" data-firm-drill="${esc(p.fc)}"><span class="detail-row-main">
        <b>${esc(p.f)}</b><small>CRD ${esc(p.fc)}${p.ra != null ? ` · ${fmtMoney(p.ra * 1e6)} total RAUM` : ""} · show this firm here</small></span><span class="detail-chevron">›</span></button>
    ${roleRows}</div></section>
    ${at ? `<section class="profile-section"><h3>Location</h3><div class="detail-list">
      <button type="button" class="detail-row" data-a="${at.i}"><span class="detail-row-main"><b>${esc(p.a || "Filed office")}</b>
        <small>${esc([p.c, p.z].filter(Boolean).join(" "))} · ${at.ids.size.toLocaleString()} advisor${at.ids.size === 1 ? "" : "s"} at this address</small></span><span class="detail-chevron">›</span></button>
      ${bl ? `<button type="button" class="detail-row" data-a="${bl.i}" data-bldg="1"><span class="detail-row-main"><b>Whole building</b>
        <small>${bl.ids.size.toLocaleString()} advisors across ${bl.lines.size} filed address line${bl.lines.size === 1 ? "" : "s"}</small></span><span class="detail-chevron">›</span></button>` : ""}
      ${otherOfficeRows(p)}
    </div><div id="advisorElsewhere" data-for="${esc(p.id)}"></div>${remoteNote(p)}${uncertainNote(p)}${placementNote(p)}</section>` : ""}
    <div class="profile-badges">${bits.map(bit => `<span class="profile-badge">${esc(bit)}</span>`).join("")}</div>
    <section class="profile-section"><h3>History${infoBox(
      "Registration history as filed with the SEC. Dates are registration begin and end dates, which can differ by days from the advisor's actual start and finish.")}</h3>
      <div id="advisorHistory" data-for="${esc(p.id)}"><p class="profile-empty">Loading history…</p></div></section>
    <section class="profile-section"><h3>Email activity${infoBox(
      "Email observed between this firm and this advisor — sent from the emailer or from Outlook, and anything they sent back. "
      + "It covers connected mailboxes only, and only since reply tracking was switched on, so an empty list means nothing has been observed rather than that nothing happened. "
      + "A message can only be opened by the rep whose mailbox holds it.")}</h3>
      <div id="advisorActivity" data-for="${esc(p.id)}"><p class="profile-empty">Loading email activity…</p></div>
      <div class="profile-actions">
        <button type="button" data-follow-open="${esc(p.id)}"
          data-follow-name="${esc(p.n || "")}">Send a follow-up</button>
      </div></section>
    ${p.g ? `<section class="profile-section"><h3>Designations</h3><p>${esc(p.g.split("|").join(", "))}</p></section>` : ""}
    <section class="profile-section"><h3>Disclosures${infoBox(
      "Categories as reported on Form ADV / BrokerCheck. A disclosure is not by itself a finding — open IAPD for the underlying event detail.")}</h3>
      ${p.dr && p.dr.length
        ? `<p class="profile-warn">⚠ ${esc(p.dr.join(", "))}</p>`
        : `<p class="profile-empty">None reported.</p>`}</section>
    ${states.length ? `<section class="profile-section"><h3>Registrations (${states.length})</h3><p>${esc(states.join(", "))}</p></section>` : ""}
    <div class="profile-actions">
      ${p.u ? `<a href="${esc(p.u)}" target="_blank" rel="noopener">Individual IAPD ↗</a>` : ""}
      <a href="${firmIapdUrl(p.fc)}" target="_blank" rel="noopener">Firm IAPD ↗</a>
      <button type="button" data-profile-advisors-scope="${esc(scope)}" data-profile-crd="${esc(p.fc)}">Show this firm's advisors</button>
      ${ADMIN ? `<button type="button" data-act-json="${esc(p.id)}">CRM record (JSON)</button>` : ""}
    </div>`;
  selectBuilding(buildingForRecord(at));
  fillCardHistory(historyFor);
  historyShard(p.id).then(shard => renderAdvisorHistory(p, shard[String(p.id)]));
  loadActivity(p.id);
  // the out-of-scope offices need the national index; fill them in when it
  // arrives rather than making the panel wait for it
  loadAdvisorIndex().then(() => {
    const slot = document.getElementById("advisorElsewhere");
    if (!slot || slot.dataset.for !== String(p.id)) return;
    slot.innerHTML = crossScopeOfficeRows(p);
    // The map shows the name they go by; name the filed one too, because that
    // is what IAPD, a CRM record and a compliance list will say.
    const row = advisorRow(p.id);
    const meta = document.getElementById("firmOverviewMeta");
    if (row && row[6] && !meta.textContent.includes("filed as"))
      meta.textContent += ` · filed as ${row[6]}`;
  }).catch(() => {});
  syncDetailHash(null);
}

// An "i" affordance carrying its explanation in the shared floating tooltip,
// so a caveat is available on demand instead of occupying a paragraph in every
// record. Markup matches the panel's existing .info-wrap so the styling and the
// delegated hover/focus handling are the same.
function infoBox(text){
  return ` <span class="info-wrap"><button type="button" class="info-button" aria-label="More information">i</button>` +
    `<span class="info-popover" role="tooltip">${esc(text)}</span></span>`;
}

// Advisors are commonly filed at several branches. The map de-duplicates them
// by advisor id, so without this the other offices are simply unreachable from
// the person you are looking at.
function otherOfficeRows(p){
  const id = String(p.id), here = addrKey(p);
  const seen = new Set([here]);
  const others = [];
  for (const f of ALL){
    if (String(f.properties.id) !== id) continue;
    const k = addrKey(f.properties);
    if (seen.has(k)) continue;
    seen.add(k);
    const at = ADDR.get(k);
    if (at) others.push(at);
  }
  if (!others.length) return "";
  // a few advisors are filed at dozens of branches (51 is the Georgia record),
  // so cap the list and state what is withheld rather than flooding the panel
  const CAP = 8;
  const rows = others.slice(0, CAP).map(at =>
    `<button type="button" class="detail-row" data-a="${at.i}"><span class="detail-row-main">` +
    `<b>${esc(at.addr || at.city || "Filed office")}</b><small>Also filed here · ` +
    `${esc([at.city, at.zip].filter(Boolean).join(" "))} · ${at.ids.size.toLocaleString()} advisor${at.ids.size === 1 ? "" : "s"}</small>` +
    `</span><span class="detail-chevron">›</span></button>`).join("");
  const rest = others.length - CAP;
  return rows + (rest > 0
    ? `<p class="profile-note">and ${rest.toLocaleString()} further filed address${rest === 1 ? "" : "es"} in ${esc(scopeLabel(scope))}.</p>`
    : "");
}

// Offices OUTSIDE the loaded scope. ALL holds only the current state or
// territory, so an advisor filed in two territories showed just the half you
// happened to be looking at -- Thomas Tolleson (Perigon) has one office in San
// Francisco and one in Atlanta, and neither view admitted the other existed.
// The national search index carries every filed place, so use it and offer a
// jump that loads the scope the office sits in.
function crossScopeOfficeRows(p){
  const row = advisorRow(p.id);
  const places = row && row[7];
  if (!places || places.length < 2) return "";
  const hereStates = new Set(scope === "US" ? []
    : scope.startsWith("T:") ? (TERRITORIES[scope.slice(2)] || []) : [scope]);
  const away = places.filter(([state]) => !hereStates.has(state));
  if (!away.length) return "";
  return `<p class="history-label">Also filed outside this view</p><div class="detail-list">` +
    away.slice(0, 6).map(([state, cityIdx]) => {
      const city = ADV_INDEX.cities[cityIdx] || "";
      const territory = STATE_TO_TERRITORY[state];
      const target = territory ? terrKey(territory) : state;
      return `<button type="button" class="detail-row" data-advisor-elsewhere="${esc(target)}" data-advisor-id="${esc(p.id)}">` +
        `<span class="detail-row-main"><b>${esc([city, state].filter(Boolean).join(", "))}</b>` +
        `<small>Open ${esc(territory || scopeLabel(state))} to see this office</small></span>` +
        `<span class="detail-chevron">›</span></button>`;
    }).join("") + `</div>`;
}

// The advisor is filed here, but their current employment record names a
// different city. Their real workplace is only known to city level, so they are
// left at the filed address and the disagreement is stated rather than resolved.
// A city-only filing is not a failure to locate somebody -- it is the firm
// saying which town they work in and declining to give a desk. That is enough
// to own the territory and place a call, and not enough to drive to, so it is
// stated rather than dressed up as an address.
function remoteNote(p){
  if (p.lt !== 1) return "";
  return `<p class="remote-note"><b>Works in this area.</b> The firm filed a city and ` +
    `state for this advisor but no street address, so the pin sits at the centre of ` +
    `${esc(p.c || "the town")}. Good for territory and a phone call; not an address to visit.</p>`;
}

function uncertainNote(p){
  if (!p.unc) return "";
  return `<p class="uncertain-note"><b>Address uncertain.</b> Filed at this office, but the ` +
    `employment record on file says ${p.home ? esc(p.home) : "somewhere else"}. ` +
    `Treat the street address as a registration, not a confirmed workplace.</p>`;
}

// How the pin was placed. The filed address is always exact; only the map
// position can be approximate, and the Advanced "Map-position quality" filter
// acts on this value -- so it has to be visible somewhere.
function placementNote(p){
  if (!p.gp || p.gp === "rooftop") return "";
  const how = p.gp === "neighbour"
    ? "Pin placed at the nearest validated address on this block."
    : "Approximate map position.";
  return `<p class="profile-note">◎ ${how} The filed address above is unchanged.</p>`;
}

document.getElementById("firmOverviewBody").addEventListener("click", e => {
  if (e.target.closest(".rlink")) return;      // the IAPD link keeps its own job
  const row = e.target.closest(".rrow");
  if (!row) return;
  const f = rosterRows[+row.dataset.ri];
  if (f) openAdvisorPopup(f, row);
});
document.getElementById("firmOverviewBody").addEventListener("keydown", e => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const row = e.target.closest(".rrow");
  if (!row) return;
  e.preventDefault();
  const f = rosterRows[+row.dataset.ri];
  if (f) openAdvisorPopup(f, row);
});

map.on("click", () => {
  if (!lassoArmed && !lassoDrawing && !detailsDrawer.hidden) closeDetails();
});

// ---- viewport wiring ----
// State mode adds every marker up front and lets clustering handle the view, so
// a pan only needs the panel recomputed. National mode draws just the offices in
// view, so it must re-render the layer too or panning leaves the map empty.
let t = null;
map.on("moveend zoomend", () => {
  clearTimeout(t);
  t = setTimeout(() => {
    if (scope === "US") renderNational(false);
    refreshPanel();
  }, 120);
});


document.addEventListener("click", e => {
  const profile = e.target.closest("[data-firm-profile]");
  if (profile){ e.preventDefault(); openFirmOverview(profile.dataset.firmProfile); return; }
  const actJson = e.target.closest("[data-act-json]");
  if (actJson){ e.preventDefault(); showActJson(actJson.dataset.actJson); return; }
  const followOpen = e.target.closest("[data-follow-open]");
  if (followOpen){
    e.preventDefault();
    openFollowUp(followOpen.dataset.followOpen, followOpen.dataset.followName);
    return;
  }
  const activityMsg = e.target.closest("[data-activity-msg]");
  if (activityMsg){
    e.preventDefault();
    showActivityMessage(activityMsg.dataset.activityCrd, activityMsg.dataset.activityMsg);
    return;
  }
  const advisors = e.target.closest("[data-profile-advisors-scope]");
  if (advisors){
    showFirmAdvisors(advisors.dataset.profileCrd, advisors.dataset.profileAdvisorsScope, false);
    return;
  }
  const allOffices = e.target.closest("[data-profile-all-offices]");
  if (allOffices){
    const crd = String(allOffices.dataset.profileAllOffices);
    focusFirmNational(crd, false);
  }
});
document.getElementById("firmOverviewClose").addEventListener("click", () => closeFirmOverview());
window.addEventListener("popstate", openFirmFromHash);
window.addEventListener("hashchange", openFirmFromHash);
// Arriving from the field view a second time only changes the hash -- no
// reload, so boot never runs again. Safe to bind: runSearch does not touch it.
window.addEventListener("hashchange", searchFromHash);
document.addEventListener("keydown", e => { if (e.key === "Escape" && !detailsDrawer.hidden) closeDetails(); });

// ---- CSV export ----
// The interim path to a CRM: everything a rep researched here leaves as a file
// keyed on advisor CRD and firm CRD. Exports exactly what the panel is showing
// -- current filters, current viewport -- so the file reconciles with the
// screen it came from.
function csvCell(value){
  let text = value == null ? "" : String(value);
  // Excel and Sheets execute a cell that opens with = + - or @, so an advisor
  // named "-Smith" or a firm called "=Value Partners" becomes a formula in a
  // file a rep opens without thinking. A leading apostrophe is the standard
  // neutraliser and is not displayed. Tab and CR are included because some
  // spreadsheet versions treat them as formula leaders too.
  if (/^[=+\-@\t\r]/.test(text)) text = `'${text}`;
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

function csvDownload(name, header, rows, extra){
  const provenance = [
    `# Advisor Map export`,
    `# generated ${new Date().toISOString().slice(0, 19).replace("T", " ")}`,
    `# scope: ${scopeLabel(scope)}`,
    `# filters: ${activeFilterSummary()}`,
    `# SEC feed ${META && META.source_date ? META.source_date : "unknown"} · data build ${DATA_VERSION}`,
    `# rows: ${rows.length}${extra ? " · " + extra : ""}`,
    `# ZIP codes starting with 0 (e.g. 01880) must be imported as text, not number`,
  ].join("\n");
  const body = [header.join(","), ...rows.map(r => r.map(csvCell).join(","))].join("\n");
  // BOM so Excel reads UTF-8 and does not mangle "&" or accented names
  const blob = new Blob(["\ufeff" + provenance + "\n" + body], { type:"text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
}

function activeFilterSummary(){
  const bits = [...document.querySelectorAll("#activeFilters .filter-chip")]
    .map(c => c.textContent.trim());
  return bits.length ? bits.join("; ") : "none";
}

// ZIP stays plain. Wrapping it as ="01880" would protect the leading zero in
// Excel but arrives literally in a CRM field, and this file is meant for both.
// The provenance header warns instead; importing as text is the reliable fix.
function csvZip(zip){ return zip || ""; }

function exportAdvisors(){
  const bounds = map.getBounds();
  const seen = new Set();
  const rows = [];
  for (const f of ALL){
    const p = f.properties;
    if (!passesFilters(p)) continue;
    const [lon, lat] = f.geometry.coordinates;
    if (!bounds.contains([lat, lon])) continue;
    const key = `${p.id}|${p.fc}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const idx = advisorRow(p.id);
    const role = ownerRolesFor(p.id).find(r => String(r.firmCrd) === String(p.fc));
    const bar = barronsFor(p.id);
    rows.push([
      p.n, idx && idx[6] ? idx[6] : "", p.id,
      p.f, p.fc,
      role ? role.title : "", role ? (OWNERSHIP_BANDS[role.code] || role.code) : "",
      role && role.ctrl ? "Y" : "",
      p.a, p.c, p._state, csvZip(p.z),
      p.unc ? "Y" : "", p.unc ? p.home : "",
      p.x == null ? "" : p.x.toFixed(1),
      p.d ? "Dual" : "RIA-only",
      p.ns == null ? "" : p.ns, p.rs ? p.rs.split("|").join(" ") : "",
      p.g ? p.g.split("|").join("; ") : "",
      p.dr && p.dr.length ? p.dr.join("; ") : "",
      p.pf || 0,
      STATE_TO_TERRITORY[p._state] || "",
      p.gp || "",
      p.u || "",
      // appended, never inserted: the sort below reads this array positionally
      bar.length ? bar.map(h => `${barronsRankText(h)} (${h.year})`).join("; ") : "",
      bar.length ? bar[0].url : "",
      forbesFor(p.id).map(h => `${h.label}${h.confirmed ? "" : " (inferred)"}`).join("; "),
      forbesTeamAssets(p.id) == null ? "" : Math.round(forbesTeamAssets(p.id)),
    ]);
  }
  rows.sort((a, b) => String(a[3]).localeCompare(String(b[3])) ||
                      String(a[0]).localeCompare(String(b[0])));
  // the KPI counts distinct people; this file carries one row per
  // advisor-firm relationship, so the two legitimately differ
  const people = new Set(rows.map(r => r[2])).size;
  csvDownload(`advisors_${scopeLabel(scope).replace(/\W+/g, "_")}.csv`, [
    "Advisor","Filed name","Advisor CRD","Firm","Firm CRD",
    "Role at firm","Ownership","Control person",
    "Street","City","State","ZIP",
    "Address uncertain","Employment record says",
    "Years experience","Registration","States registered","Registered states",
    "Designations","Disclosures","Prior firms","Territory","Map position","IAPD",
    "Barron's ranking","Barron's profile",
    "Forbes ranking","Forbes team assets ($)",
  ], rows, `${people.toLocaleString()} distinct advisors, one row per advisor-firm relationship`);
  showNotice(`Exported ${rows.length.toLocaleString()} rows for ${people.toLocaleString()} advisors.`);
}

function exportFirms(){
  const rows = FIRMS.map(f => {
    const profile = FIRM_PROFILES ? FIRM_PROFILES.profiles[String(f.crd)] : null;
    const owners = profile && profile.own ? (profile.own.a || []) : [];
    const lead = owners.find(o => o[4] && o[4] !== "NA") || owners[0];
    const chain = profile && profile.own ? (profile.own.chain || []) : [];
    return [
      f.firm, f.crd,
      f.advisors, f.offices,
      f.relevantAum == null ? "" : Math.round(f.relevantAum),
      f.raum == null ? "" : Math.round(f.raum * 1e6),
      profile ? (profile.selects ? "Y" : "N") : "",
      profile ? (profile.firm_type || "") : "",
      profile ? (profile.city || "") : "", profile ? (profile.state || "") : "",
      profile ? profileWebsite(profile.website) : "",
      profile && profile.aka ? profile.aka.join("; ") : "",
      lead ? lead[0] : "", lead ? (OWNERSHIP_BANDS[lead[4]] || lead[4]) : "",
      owners.filter(o => o[5]).map(o => o[0]).slice(0, 5).join("; "),
      chain.length ? chain[chain.length - 1] : "",
      firmIapdUrl(f.crd),
    ];
  });
  csvDownload(`firms_${scopeLabel(scope).replace(/\W+/g, "_")}.csv`, [
    "Firm","Firm CRD","Advisors in view","Offices in view",
    "Relevant AUM (in view, $)","Total regulatory AUM ($)",
    "Selects outside managers","Firm type","City","State","Website","Former names",
    "Largest owner","Ownership","Control persons","Ultimate parent","IAPD",
  ], rows);
  showNotice(`Exported ${rows.length.toLocaleString()} firms.`);
}

document.addEventListener("click", e => {
  if (e.target.closest("#exportAdvisors")) exportAdvisors();
  else if (e.target.closest("#exportFirms")) exportFirms();
});


// ---- firm exclusion ----
// Struck-off firms leave the map AND the "firms in view" list, so the list
// itself cannot offer the way back. The removable pills above it are that way
// back: without them an exclusion would be irreversible except by resetting
// every filter, which is not a fair trade for one mis-click.
const EXCLUDED_NAMES = new Map();          // crd -> name, for the pill labels

function excludeFirm(crd, name){
  crd = String(crd);
  if (name) EXCLUDED_NAMES.set(crd, name);
  excludedFirms.add(crd);
  syncFilterButtons();
  redraw();
  showNotice(`${name || "Firm " + crd} excluded from the map. ` +
             `${excludedFirms.size} firm${excludedFirms.size === 1 ? "" : "s"} excluded.`);
}

function includeFirm(crd){
  excludedFirms.delete(String(crd));
  syncFilterButtons();
  redraw();
}

function renderExcludedFirms(){
  const slot = document.getElementById("excludedFirms");
  if (!slot) return;
  slot.hidden = excludedFirms.size === 0;
  if (!excludedFirms.size){ slot.innerHTML = ""; return; }
  const pills = [...excludedFirms].map(crd => {
    const name = EXCLUDED_NAMES.get(crd) || firmLabelForCrd(crd) || `CRD ${crd}`;
    return `<button type="button" class="excl-pill" data-include-firm="${esc(crd)}" ` +
           `title="Put ${esc(name)} back on the map">${esc(name)}</button>`;
  }).join("");
  slot.innerHTML = `<span class="excl-label">Excluded</span>${pills}` +
    `<button type="button" class="mini" id="clearExcluded">clear all</button>`;
}

document.addEventListener("click", e => {
  const off = e.target.closest("[data-exclude-firm]");
  if (off){
    e.preventDefault(); e.stopPropagation();
    const row = off.closest(".frow");
    excludeFirm(off.dataset.excludeFirm, row?.querySelector(".fn")?.textContent.trim());
    return;
  }
  const on = e.target.closest("[data-include-firm]");
  if (on){ e.preventDefault(); includeFirm(on.dataset.includeFirm); return; }
  if (e.target.closest("#clearExcluded")){
    excludedFirms.clear(); syncFilterButtons(); redraw();
  }
});
