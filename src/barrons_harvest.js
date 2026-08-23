/* Barron's Top Advisor rankings -> JSON, run from the browser console.
 *
 * WHY IT RUNS IN THE BROWSER
 * The rankings sit behind the Barron's paywall, so requests need your logged-in
 * session. They are also rendered client-side: fetching a ranking URL returns
 * 1.9 MB of HTML with no <table> in it, so the page has to actually execute.
 * This script therefore loads each ranking page in a hidden same-origin iframe,
 * waits for the table to hydrate (~1s), and reads the rendered DOM.
 *
 * Advisor detail pages ARE server-rendered, so the CRD comes from a plain
 * fetch -- no iframe needed. The CRD is the join key to everything else we
 * hold, via the BrokerCheck link:
 *     brokercheck.finra.org/individual/summary/3187674  ->  advisor CRD 3187674
 *
 * HOW TO RUN
 *   1. Open any page on barrons.com while signed in.
 *   2. Open DevTools -> Console, paste this whole file, press Enter.
 *   3. Leave the tab open. Progress prints as it goes; it takes ~15-20 minutes.
 *   4. It downloads barrons_rankings.json when finished.
 *
 * Safe to interrupt: every page is checkpointed to localStorage, so re-running
 * resumes rather than starting over. Call BARRONS.reset() to start clean.
 *
 * Throttled deliberately. Do not lower the delays.
 */
(() => {
const YEAR = { top1500: 2026, women: 2026, independent: 2025, top100: 2026 };

const RANKINGS = {
  // by state: ranks restart at 1 in each state, so state is part of the key
  top1500: { byState: true, path: st => `/advisor/report/top-financial-advisors/1000/${YEAR.top1500}/${st}` },
  women:       { path: () => `/advisor/report/top-financial-advisors/women` },
  independent: { path: () => `/advisor/report/top-financial-advisors/independent` },
  top100:      { path: () => `/advisor/report/top-financial-advisors/100` },
};

const STATES = ["al","ak","az","ar","ca","co","ct","de","dc","fl","ga","hi","id","il","in","ia",
  "ks","ky","la","me","md","ma","mi","mn","ms","mo","mt","ne","nv","nh","nj","nm","ny","nc","nd",
  "oh","ok","or","pa","ri","sc","sd","tn","tx","ut","vt","va","wa","wv","wi","wy"];

const PAGE_DELAY = 400;      // between ranking pages
const ADVISOR_DELAY = 250;   // between advisor detail fetches
const MAX_PAGES = 40;        // guard only; the real count is read from the page
const KEY = "barronsHarvest.v1";

const sleep = ms => new Promise(r => setTimeout(r, ms));
const store = {
  read(){ try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch { return {}; } },
  write(s){ localStorage.setItem(KEY, JSON.stringify(s)); },
};

// Load a ranking page in a hidden iframe and read the hydrated table.
function scrapePage(url, timeoutMs = 30000){
  return new Promise(resolve => {
    const frame = document.createElement("iframe");
    frame.style.cssText = "position:fixed;left:-9999px;top:0;width:1400px;height:1400px;border:0";
    let settled = false;
    const finish = value => { if (!settled){ settled = true; frame.remove(); resolve(value); } };
    frame.onload = () => {
      const started = Date.now();
      const poll = setInterval(() => {
        let rows = [];
        try {
          rows = [...frame.contentDocument.querySelectorAll("tbody tr")]
            .filter(r => r.querySelectorAll("td").length > 1);
        } catch { /* not ready */ }
        if (rows.length){ clearInterval(poll); finish({ rows: rows.map(readRow), pages: pageCount(frame) }); }
        else if (Date.now() - started > timeoutMs){ clearInterval(poll); finish({ rows: [], pages: 0 }); }
      }, 250);
    };
    frame.onerror = () => finish({ rows: [], pages: 0 });
    frame.src = url;
    document.body.appendChild(frame);
  });
}

// Pagination is a combobox reading "PAGE 2 OF 4", not links -- there are no
// ?page= anchors to count. Discovering the end by loading pages until one comes
// back empty would burn the full timeout on the last page of all 51 states.
function pageCount(frame){
  try {
    const row = [...frame.contentDocument.querySelectorAll("tr")]
      .find(r => /previous/i.test(r.textContent));
    const m = row && /of\s+(\d+)/i.exec(row.innerText.replace(/\s+/g, " "));
    return m ? parseInt(m[1], 10) : 1;
  } catch { return 1; }
}

// A row is: rank | advisor | firm | location | (spacer) | detail panel.
// The detail panel is label/value pairs and differs per ranking -- Top 1500
// carries none, the national lists carry Team Assets and friends -- so it is
// read generically rather than by fixed column.
function readRow(tr){
  const cells = [...tr.children];
  const text = i => (cells[i] ? cells[i].textContent.trim() : "");
  const link = tr.querySelector('a[href*="/advisor/finder/"]');
  const slug = link ? link.getAttribute("href").split("?")[0].split("/finder/")[1] : null;

  const detail = {};
  const panel = cells[cells.length - 1];
  if (panel){
    const leaves = [...panel.querySelectorAll("*")]
      .filter(e => e.children.length === 0 && e.textContent.trim())
      .map(e => e.textContent.trim());
    for (let i = 0; i + 1 < leaves.length; i += 2) detail[leaves[i]] = leaves[i + 1];
  }
  return {
    rank: parseInt(text(0).replace(/\D/g, ""), 10) || null,
    advisor: text(1), firm: text(2), location: text(3),
    slug, detail,
  };
}

async function harvestList(name, cfg, state){
  const first = await scrapePage(`${cfg.path(state)}?page=1`);
  if (!first.rows.length) return [];
  const rows = [...first.rows];
  const seen = new Set(rows.map(r => r.slug));
  const total = Math.min(first.pages || 1, MAX_PAGES);
  for (let page = 2; page <= total; page++){
    await sleep(PAGE_DELAY);
    const batch = await scrapePage(`${cfg.path(state)}?page=${page}`);
    for (const r of batch.rows)
      if (r.slug && !seen.has(r.slug)){ seen.add(r.slug); rows.push(r); }
  }
  return rows;
}

async function crdFor(slug){
  const res = await fetch(`/advisor/finder/${slug}`, { credentials: "include" });
  if (!res.ok) return null;
  const doc = new DOMParser().parseFromString(await res.text(), "text/html");
  const a = doc.querySelector('a[href*="brokercheck"]');
  if (!a) return null;
  const m = /individual\/summary\/(\d+)/.exec(a.getAttribute("href") || "");
  return m ? m[1] : null;
}

function download(obj, filename){
  const blob = new Blob([JSON.stringify(obj, null, 1)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
}

async function run(){
  const state = store.read();
  state.lists ||= {};
  state.crd ||= {};

  // ---- phase 1: the ranking tables ----
  for (const [name, cfg] of Object.entries(RANKINGS)){
    const targets = cfg.byState ? STATES : [null];
    state.lists[name] ||= {};
    for (const st of targets){
      const key = st || "_";
      if (state.lists[name][key]) continue;            // already harvested
      const rows = await harvestList(name, cfg, st);
      state.lists[name][key] = rows;
      store.write(state);
      console.log(`[${name}${st ? "/" + st : ""}] ${rows.length} advisors`);
    }
  }

  // ---- phase 2: CRDs, one fetch per distinct advisor ----
  const slugs = new Set();
  for (const byKey of Object.values(state.lists))
    for (const rows of Object.values(byKey))
      rows.forEach(r => r.slug && slugs.add(r.slug));
  const pending = [...slugs].filter(s => !(s in state.crd));
  console.log(`CRD lookup: ${pending.length} of ${slugs.size} advisors remaining`);

  let done = 0;
  for (const slug of pending){
    try { state.crd[slug] = await crdFor(slug); }
    catch { state.crd[slug] = null; }
    if (++done % 25 === 0){ store.write(state); console.log(`  ${done}/${pending.length}`); }
    await sleep(ADVISOR_DELAY);
  }
  store.write(state);

  // ---- phase 3: one flat record per advisor-ranking ----
  const out = [];
  for (const [name, byKey] of Object.entries(state.lists))
    for (const [key, rows] of Object.entries(byKey))
      for (const r of rows)
        out.push({
          ranking: name,
          year: YEAR[name],
          state: key === "_" ? null : key.toUpperCase(),
          rank: r.rank,
          advisor: r.advisor, firm: r.firm, location: r.location,
          crd: state.crd[r.slug] || null,
          barrons_url: `https://www.barrons.com/advisor/finder/${r.slug}`,
          detail: r.detail,
        });

  const withCrd = out.filter(r => r.crd).length;
  console.log(`Done: ${out.length} rows, ${withCrd} with a CRD (${(withCrd / out.length * 100).toFixed(1)}%)`);
  download(out, "barrons_rankings.json");
  return out;
}

window.BARRONS = { run, reset: () => localStorage.removeItem(KEY), state: () => store.read() };
console.log("Barron's harvester ready. Run:  await BARRONS.run()");
})();
