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
 *   3. FORBES.configure({years:{"best-in-state":2026,"top-wealth":2026}})
 *   4. await FORBES.run()   -- it visits both lists and downloads the JSON.
 * Optionally select categories:["best-in-state"]. Verify the publication year
 * on the actual page: metadata alone cannot request a historic edition.
 */
(() => {
const LISTS = [
  { name: "best-in-state", path: "/lists/best-in-state-wealth-advisors/" },
  { name: "top-wealth",    path: "/lists/top-wealth-advisors/" },
];
let selectedCategories = LISTS.map(list => list.name), years = {}, running = false;
function configure(options = {}){
  if (running) throw new Error("Cannot change configuration during a harvest");
  const categories = options.categories === undefined ? selectedCategories : options.categories;
  if (!Array.isArray(categories) || !categories.length || new Set(categories).size !== categories.length ||
      categories.some(name => !LISTS.some(list => list.name === name))) throw new Error("Unknown or empty ranking categories");
  const updates = options.years || {};
  for (const [name, year] of Object.entries(updates)){
    if (!LISTS.some(list => list.name === name) || !Number.isInteger(year) || year < 1900 || year > 2200)
      throw new Error("Invalid ranking edition; supply a four-digit year by category");
  }
  selectedCategories = categories.slice();
  years = {...years, ...updates};
  return configuration();
}
function configuration(){
  return {categories:selectedCategories.slice(), years:{...years}};
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

// Support the legacy data assignment and the newer Next.js JSON payload.
// Parse their data without executing source-page JavaScript or flattening
// nested objects. Complete national-list collection also uses the UI below.
function recordsFrom(doc){
  const found = [], unique = new Map();
  for (const script of doc.querySelectorAll("script")){
    const text = script.textContent || "";
    if (!text.includes('"personName"') && script.id !== '__NEXT_DATA__') continue;
    // Parse data, never execute source-page JavaScript. This also preserves
    // records with nested fields, which the old flat-object regex missed.
    const marker = /window\["forbes"\]\["simple-site"\]\s*=\s*/.exec(text);
    const raw = (marker ? text.slice(marker.index + marker[0].length) : text).trim().replace(/;\s*$/, "");
    let root;
    try { root = JSON.parse(raw); } catch { continue; }
    const stack = [root];
    while (stack.length){
      const rec = stack.pop();
      if (!rec || typeof rec !== "object") continue;
      if ((rec.personName || (rec.name && rec.uri && String(rec.naturalId || '').startsWith('faris/')))
          && Number.isInteger(Number(rec.rank)) && Number(rec.rank) > 0) found.push(rec);
      for (const value of Object.values(rec)) if (value && typeof value === "object") stack.push(value);
    }
  }
  for (const rec of found){
    const qa = pattern => (rec.qas || []).find(q => pattern.test(q.question || ''))?.answer || null;
    const row = {
      rank: Number(rec.rank),
      // "Georgia - Atlanta (High Net Worth)" -- 112 of these, NOT 51 states.
      // Kept verbatim; parse_forbes.py splits it, so a change in Forbes'
      // wording is visible in one place rather than silently mis-parsed here.
      category: rec.category || null,
      name: rec.personName || rec.name,
      firm: rec.organization || null,
      city: rec.city || null,
      uri: rec.uri || null,
      teamAssets: rec.teamAssets || qa(/^team assets/i),
      minAccountSize: rec.minAccountSize || qa(/^minimum account/i),
      typicalNetWorth: rec.typicalNetWorth || qa(/^typical net worth/i),
      typicalSize: rec.typicalSize || qa(/^typical size/i),
      sourceId: rec.naturalId || null,
    };
    const key = JSON.stringify([row.uri || row.name, row.category, row.rank]);
    if (unique.has(key) && JSON.stringify(unique.get(key)) !== JSON.stringify(row))
      throw new Error("Conflicting Forbes recognition records; review source payload");
    unique.set(key, row);
  }
  return [...unique.values()];
}

function pagination(doc = document){
  const script = doc.querySelector('#__NEXT_DATA__');
  if (!script) return null;
  const data = JSON.parse(script.textContent);
  const table = data.props?.pageProps?.schema?.blocks?.find(b => b.component === 'Table')?.data;
  return table ? {year:Number(table.year), total:Number(table.totalCount), limit:Number(table.limit)} : null;
}

function rendered(doc = document){
  const table = [...doc.querySelectorAll('table')].find(t => /TEAM ASSETS/i.test(t.querySelector('thead')?.innerText || ''));
  if (!table) return [];
  const headers = [...table.querySelectorAll('thead th,thead td')].map(e=>e.innerText.replace(/\s+/g,' ').trim());
  return [...table.querySelectorAll('tbody tr')].filter(tr => tr.querySelector('a[href*="/profile/"]') &&
    !tr.querySelector('[class*="SkeletonLoader_"]')).map(tr => {
    const cells = [...tr.children], anchor = tr.querySelector('a[href*="/profile/"]');
    const uri = /\/profile\/([^/?#]+)/.exec(anchor?.getAttribute('href') || '')?.[1];
    const rank = Number(cells[0]?.innerText.trim());
    const field = pattern => { const i=headers.findIndex(h=>pattern.test(h));return i<0 ? null : cells[i]?.innerText.trim() || null; };
    const name = field(/^NAME$/i)?.replace(/^(?:(?:PROFILE SPOTLIGHT|PROMOTED)\s*)+/i,'').trim();
    if (!uri || uri==='undefined' || !name || !Number.isInteger(rank) || rank < 1) throw new Error('Unrecognized Forbes rendered row');
    return {rank,name,uri,category:null,firm:field(/^FIRM$/i),city:field(/^CITY$/i),
      minAccountSize:field(/^MINIMUM ACCOUNT/i),teamAssets:field(/^TEAM ASSETS/i),
      typicalNetWorth:field(/^TYPICAL NET WORTH/i),typicalSize:field(/^TYPICAL SIZE/i),sourceId:null};
  });
}

async function currentRows(doc = document, expectedYear = null){
  const pages = pagination(doc);
  const sourceYear = pages?.year || Number(/\b(20\d{2})\b/.exec(doc.title || '')?.[1]);
  if (expectedYear != null && sourceYear !== expectedYear) throw new Error('Forbes publication year does not match configured edition');
  if (!pages) return recordsFrom(doc);
  if (!Number.isInteger(pages.total) || pages.total < 1 || pages.total > 20000 || !Number.isInteger(pages.limit) || pages.limit < 1)
    throw new Error('Unrecognized Forbes pagination metadata');
  const out = [], seen = new Set();
  for (let number=1;number<=Math.ceil(pages.total/pages.limit);number++){
    let rows = rendered(doc);
    const readyDeadline=Date.now()+30000;
    while (!rows.length){
      if (Date.now()>readyDeadline) throw new Error('Empty Forbes rendered page');
      await sleep(250);rows=rendered(doc);
    }
    for (const row of rows){
      if (seen.has(row.uri)) throw new Error('Forbes pagination repeated advisors');
      seen.add(row.uri);out.push(row);
    }
    if (out.length>=pages.total) break;
    const next = [...doc.querySelectorAll('[data-testid="pagination-tile"]')].find(b=>b.innerText.trim()===String(number+1));
    if (!next) throw new Error('Forbes next page unavailable');
    const first = rows[0].uri;
    next.click();
    const deadline=Date.now()+30000;
    while (!rendered(doc).length || rendered(doc)[0]?.uri===first){
      if (Date.now()>deadline) throw new Error('Forbes page change timed out');
      await sleep(250);
    }
    await sleep(400);
  }
  if (out.length!==pages.total) throw new Error('Forbes total count does not match captured advisors');
  return out;
}

// Load a list in a hidden iframe so one console session can collect them all
// without navigating away and losing the script.
function loadList(path, timeoutMs = 45000, expectedYear = null){
  return new Promise((resolve, reject) => {
    const frame = document.createElement("iframe");
    frame.style.cssText = "position:fixed;left:-9999px;top:0;width:1400px;height:1400px;border:0";
    let settled = false, poll, deadline;
    const finish = (v,error) => { if (!settled){ settled = true; clearInterval(poll); clearTimeout(deadline); frame.remove(); error ? reject(error) : resolve(v); } };
    deadline = setTimeout(() => finish([]), timeoutMs + 1000);
    frame.onload = () => {
      const started = Date.now();
      poll = setInterval(() => {
        let rows = [];
        try { rows = recordsFrom(frame.contentDocument); } catch { /* not ready */ }
        if (rows.length){ clearInterval(poll); currentRows(frame.contentDocument, expectedYear).then(v=>finish(v),e=>finish(null,e)); }
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
  for (const list of LISTS.filter(list => selectedCategories.includes(list.name))){
    // the page we are already on needs no iframe
    const here = location.pathname.replace(/\/?$/, "/") === list.path;
    const rows = here ? await currentRows(document, years[list.name]) : await loadList(list.path, 45000, years[list.name]);
    if (!rows.length) throw new Error(`No Forbes records for ${list.name}; no complete download produced`);
    rows.forEach(r => out.push({ list: list.name, year:years[list.name] ?? null, ...r }));
    console.log(`[${list.name}] ${rows.length} advisors`);
    if (!here) await sleep(1000);
  }
  const assets = out.filter(r => r.teamAssets).length;
  console.log(`Done: ${out.length} rows, ${assets} with team assets ` +
              `(${(assets / Math.max(1, out.length) * 100).toFixed(1)}%)`);
  download(out, "forbes_rankings.json");
  return out;
}

window.FORBES = {
  run: async () => {
    if (running) throw new Error("Harvest already running");
    running = true;
    try { return await run(); } finally { running = false; }
  },
  configure, config:configuration, records: recordsFrom, rendered, pagination, currentRows,
};
console.log("Forbes harvester ready. Run:  await FORBES.run()");
})();
