"use strict";
// Browser-console harvester checks, entirely synthetic (no fetch, no browser).
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm"), assert = require("node:assert/strict");
function context(filename){
  const storage = new Map();
  const sandbox = {
    window:{}, console:{log(){}}, localStorage:{
      getItem:key => storage.get(key) || null,
      setItem:(key,value) => storage.set(key,value), removeItem:key => storage.delete(key),
    },
    location:{pathname:"/lists/best-in-state-wealth-advisors/"},
    document:{
      title:'Forbes 2027 Best-In-State Wealth Advisors List',
      querySelector:() => null,
      querySelectorAll:() => [{textContent:JSON.stringify({rank:1,personName:"Example Advisor",uri:"example",category:"Georgia - State"})}],
      createElement:() => ({click(){},remove(){}}), body:{appendChild(){}},
    },
    Blob, URL:{createObjectURL:() => "blob:test",revokeObjectURL(){}},
    setTimeout:callback => callback(),
    fetch(){throw new Error("Network forbidden in fixture");},
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,"../src",filename),"utf8"),sandbox);
  return {api:sandbox.window, storage};
}
async function main(){
  const {api,storage} = context("barrons_harvest.js");
  const barrons = api.BARRONS;
  assert.equal(barrons.config().years.independent,2026);
  const original = barrons.config().checkpointKey;
  storage.set(original,JSON.stringify({lists:{old:[]}}));
  barrons.configure({years:{top1500:2027},categories:["top1500"],states:["GA"]});
  assert.notEqual(barrons.config().checkpointKey,original);
  assert.deepEqual(JSON.parse(JSON.stringify(barrons.state())),{});
  const key = barrons.config().checkpointKey;
  assert.throws(() => barrons.configure({years:{top1500:"2028"}}),/edition/);
  assert.throws(() => barrons.configure({categories:["invented"]}),/categories/);
  assert.throws(() => barrons.configure({states:["XX"]}),/states/);
  assert.equal(barrons.config().checkpointKey,key);
  barrons.configure({states:["FL"]});
  assert.notEqual(barrons.config().checkpointKey,key);
  barrons.configure({years:{...barrons.config().years,top1500:2026},categories:["top1500","women","independent","top100"],states:null});
  assert.equal(barrons.config().checkpointKey,original);
  barrons.reset();
  assert.equal(storage.has(original),false);

  const forbes = context("forbes_harvest.js").api.FORBES;
  assert.throws(() => forbes.configure({years:{"top-wealth":true}}),/edition/);
  assert.throws(() => forbes.configure({categories:[]}),/categories/);
  forbes.configure({years:{"best-in-state":2027},categories:["best-in-state"]});
  const rows = await forbes.run();
  assert.equal(rows.length,1);
  assert.equal(rows[0].year,2027);
  assert.equal(rows[0].list,"best-in-state");
  const nested = {personName:'Example {Advisor}',rank:2,uri:'nested',category:'Georgia',
    organization:'Example Firm',teamAssets:'$2B',nested:{source:'fixture'},naturalId:'faris/65/2026/2'};
  const doc = {querySelectorAll:() => [
    {textContent:'{"rank":999,"unrelated":true}'},
    {textContent:'window["forbes"] = window["forbes"] || {}; window["forbes"]["simple-site"] = '+JSON.stringify({rows:[nested]})},
    {textContent:JSON.stringify({rows:[nested]})},
  ]};
  const extracted = forbes.records(doc);
  assert.equal(extracted.length,1,'nested records and repeated payloads are lossless and deduplicated');
  assert.equal(extracted[0].name,'Example {Advisor}');
  assert.equal(extracted[0].teamAssets,'$2B');
  assert.equal(extracted[0].sourceId,'faris/65/2026/2');
  const modern={name:'New Format Advisor',rank:'3',uri:'modern',naturalId:'faris/123',organization:'Example Firm',
    qas:[{question:'Team Assets (Custodied)',answer:'$3B'},{question:'Minimum account size for new business',answer:'$2M'}]};
  const newRows=forbes.records({querySelectorAll:()=>[{id:'__NEXT_DATA__',textContent:JSON.stringify({rows:[modern]})}]});
  assert.equal(newRows.length,1);assert.equal(newRows[0].name,'New Format Advisor');
  assert.equal(newRows[0].teamAssets,'$3B');assert.equal(newRows[0].minAccountSize,'$2M');
  let pageIndex=0;
  const modernHeaders=['RANK','NAME','FIRM','CITY','MINIMUM ACCOUNT SIZE','TEAM ASSETS (CUSTODIED)','TYPICAL NET WORTH','TYPICAL SIZE'];
  const modernTable={querySelector:()=>({innerText:'TEAM ASSETS'}),querySelectorAll:selector=>{
    if (selector.startsWith('thead')) return modernHeaders.map(innerText=>({innerText}));
    const anchor={innerText:'Advisor '+pageIndex,getAttribute:()=>'/profile/person-'+pageIndex+'/'};
    return [{querySelector:()=>({}),children:[]},
      {children:[String(pageIndex+1),'Advisor','Firm','City','$1M','$2B','$3M','$4M'].map(innerText=>({innerText})),querySelector:s=>s.includes('/profile/') ? anchor : null}];
  }};
  const modernDoc={title:'Forbes 2026 Top Wealth Advisors',
    querySelector:()=>({textContent:JSON.stringify({props:{pageProps:{schema:{blocks:[{component:'Table',data:{year:'2026',totalCount:2,limit:1}}]}}}})}),
    querySelectorAll:selector=>selector==='table' ? [modernTable] : [{innerText:'2',click(){pageIndex=1;}}]};
  const complete=await forbes.currentRows(modernDoc,2026);
  assert.equal(complete.length,2);assert.equal(complete[1].uri,'person-1');
  assert.equal(complete[1].teamAssets,'$2B');
  assert.equal(complete[1].name,'Advisor','name comes from the visible NAME cell, not the empty row-overlay link');
  await assert.rejects(()=>forbes.currentRows(modernDoc,2025),/publication year/);
  pageIndex=0;
  await assert.rejects(()=>forbes.currentRows({...modernDoc,querySelectorAll:s=>s==='table'?[modernTable]:[]},2026),/next page unavailable/);
  assert.throws(() => forbes.records({querySelectorAll:() => [{textContent:JSON.stringify([
    nested,{...nested,organization:'Different Firm'}])}]}),/Conflicting/);
  assert.equal(forbes.records({querySelectorAll:() => [{textContent:JSON.stringify({...nested,rank:0})}]}).length,0);

  const headers=['2026Rank','Advisor','Firm','Location','Typical Account ($mil)','Team Assets ($bil)',''];
  const table={querySelectorAll:() => headers.map(textContent=>({textContent}))};
  const tr={children:['1','Example Advisor','Example Firm','Portage, Mich.','5','11',''].map(textContent=>({
    textContent,querySelectorAll:()=>[]})),closest:()=>table,
    querySelector:()=>({getAttribute:()=>'/advisor/finder/example-advisor?source=ranking'})};
  const snap=barrons.snapshot({querySelectorAll:()=>[tr],
    querySelector:()=>({querySelector:()=>({innerText:'2026Rank'})}),
    body:{innerText:'PAGE\n2\nOF 10'}});
  assert.equal(snap.year,2026);assert.equal(snap.page,2);assert.equal(snap.pages,10);
  assert.equal(snap.rows[0].detail['Team Assets ($bil)'],'11');
  assert.equal(snap.rows[0].detail['Typical Account ($mil)'],'5');
  assert.equal(snap.rows[0].slug,'example-advisor');
  const app = fs.readFileSync(path.join(__dirname,"../webapp/app.js"),"utf8");
  const rankingContext = {BARRONS:{advisors:{"1":[["top100",1,null,2024,""],["women",5,null,2026,""]]}},
    FORBES:{advisors:{"1":[["old","old",1,"c","",2024],["new","new",5,"h","",2026]],"2":[["legacy","legacy",1,"h",""]]}}};
  for (const name of ["barronsFor","forbesFor","forbesBest"]){
    const code = app.match(new RegExp("function " + name + "\\([^]*?\\n}"));
    assert.ok(code, name + " function fixture extraction");
    vm.runInNewContext(code[0],rankingContext);
  }
  assert.equal(rankingContext.barronsFor("1")[0].year,2026);
  assert.equal(rankingContext.forbesBest(rankingContext.forbesFor("1")).year,2026);
  assert.equal(rankingContext.forbesFor("2")[0].year,null);
  console.log("Ranking harvest offline configuration tests passed");
}
main().catch(error => {console.error(error);process.exitCode=1;});
