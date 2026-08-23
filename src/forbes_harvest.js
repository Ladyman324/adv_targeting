/* Forbes wealth-advisor rankings -> JSON, run from the browser console.
 *
 * WHY THE BROWSER
 * Unlike Barron's these lists are public, but they are client-rendered: the
 * ranking table is hydrated from a JSON payload embedded in a <script> tag, so
 * a server-side fetch gets markup with no rows in it. Reading the payload from
 * a loaded page is both simpler and far gentler than scraping -- the ENTIRE
 * list arrives in one page load. 11,305 advisors, one request. Contrast
 * barrons_harvest.js, which needs ~1,800 requests over 20 minutes.
 *
 * WHAT IT DOES NOT GET
 * A CRD. Forbes links no BrokerCheck anywhere -- not in the payload, not on
 * the advisor profile pages, which carry zero FINRA references. Joining these
 * people to the SEC universe is a separate problem; see src/forbes_match.py.
 *
 * HOW TO RUN
 *   1. Open https://www.forbes.com/lists/best-in-state-wealth-advisors/
 *   2. DevTools -> Console, paste this file, Enter.
 *   3. await FORBES.run()   -- it visits both lists and downloads the JSON.
 */
(() => {
const LISTS = [
  { name: "best-in-state", path: "/lists/best-in-state-wealth-advisors/" },
  { name: "top-wealth",    path: "/lists/top-wealth-advisors/" },
];

const sleep = ms => new Promise(r => setTimeout(r, ms));

// The payload is a JSON blob inside a <script>. Rather than guess at its
// envelope -- which is a Forbes implementation detail and will change -- pull
// out every object that has a "rank", which is what a person record is.
function recordsFrom(doc){
  const script = [...doc.querySelectorAll("script")]
    .find(s => /"rank"\s*:/.test(s.textContent));
  if (!script) return [];
  const objects = script.textContent.match(/\{[^{}]*"rank"\s*:\s*\d+[^{}]*\}/g) || [];
  const out = [];
  for (const raw of objects){
    let rec;
    try { rec = JSON.parse(raw); } catch { continue; }
    if (!rec.personName) continue;
    out.push({
      rank: rec.rank,
      // "Georgia - Atlanta (High Net Worth)" -- 112 of these, NOT 51 states.
      // Kept verbatim; parse_forbes.py splits it, so a change in Forbes'
      // wording is visible in one place rather than silently mis-parsed here.
      category: rec.category || null,
      name: rec.personName,
      firm: rec.organization || null,
      city: rec.city || null,
      uri: rec.uri || null,
      teamAssets: rec.teamAssets || null,
      minAccountSize: rec.minAccountSize || null,
      typicalNetWorth: rec.typicalNetWorth || null,
      typicalSize: rec.typicalSize || null,
    });
  }
  return out;
}

// Load a list in a hidden iframe so one console session can collect them all
// without navigating away and losing the script.
function loadList(path, timeoutMs = 45000){
  return new Promise(resolve => {
    const frame = document.createElement("iframe");
    frame.style.cssText = "position:fixed;left:-9999px;top:0;width:1400px;height:1400px;border:0";
    let settled = false;
    const finish = v => { if (!settled){ settled = true; frame.remove(); resolve(v); } };
    frame.onload = () => {
      const started = Date.now();
      const poll = setInterval(() => {
        let rows = [];
        try { rows = recordsFrom(frame.contentDocument); } catch { /* not ready */ }
        if (rows.length){ clearInterval(poll); finish(rows); }
        else if (Date.now() - started > timeoutMs){ clearInterval(poll); finish([]); }
      }, 500);
    };
    frame.onerror = () => finish([]);
    frame.src = path;
    document.body.appendChild(frame);
  });
}

function download(obj, filename){
  const blob = new Blob([JSON.stringify(obj, null, 1)], { type:"application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
}

async function run(){
  const out = [];
  for (const list of LISTS){
    // the page we are already on needs no iframe
    const here = location.pathname.replace(/\/?$/, "/") === list.path;
    const rows = here ? recordsFrom(document) : await loadList(list.path);
    rows.forEach(r => out.push({ list: list.name, ...r }));
    console.log(`[${list.name}] ${rows.length} advisors`);
    if (!here) await sleep(1000);
  }
  const assets = out.filter(r => r.teamAssets).length;
  console.log(`Done: ${out.length} rows, ${assets} with team assets ` +
              `(${(assets / Math.max(1, out.length) * 100).toFixed(1)}%)`);
  download(out, "forbes_rankings.json");
  return out;
}

window.FORBES = { run, records: recordsFrom };
console.log("Forbes harvester ready. Run:  await FORBES.run()");
})();
