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
  assert.equal(barrons.config().years.independent,2025);
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
