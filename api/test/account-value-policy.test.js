"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const AssetProductFilters = require("../../webapp/asset-product-filters");
const book = {accounts:[[100,0,0,0,100]],totals:{acv:100,lcv:0,mf:0,midcap:0},
  as_of:"2026-11-06",advisors:{high:{t:"confirmed",ix:[0],acv:100},
    low:{t:"confirmed",ix:[0],acv:80,av:{0:[80,0,0,0,80]}}}};
test("a lower-value advisor filter uses that advisor's ACT amount",()=>{
  const selected=AssetProductFilters.accountValues(book,["low"]);
  assert.equal(selected.size,1);assert.equal(selected.get(0)[0],80);
  assert.equal(book.accounts[0][0],100);
});
test("shared broker codes count once at the highest included amount",()=>{
  for(const ids of [["low","high"],["high","low"],["high","high","low"]]){
    const selected=AssetProductFilters.accountValues(book,ids);
    assert.equal(selected.size,1);assert.equal(selected.get(0)[0],100);
  }
});
test("ACT-only and CRD holders deduplicate together, preserving the filtered ACT-only balance",()=>{
  const key = "act-00000000000000000000000001";
  const mixed = {...book, accounts:[...book.accounts,[0,20,0,0,20]],
    act_contacts:{[key]:{ix:[0,1],av:{0:[75,0,0,0,75]},acv:75,lcv:20}}};
  assert.equal(AssetProductFilters.holder(mixed,key).acv,75);
  assert.equal(AssetProductFilters.accountValues(mixed,[key]).get(0)[4],75);
  const both=AssetProductFilters.accountValues(mixed,["high",key,key]);
  assert.deepEqual([...both.values()].map(value=>value[4]),[100,20]);
});
test("selection preserves one whole source allocation, not product maxima",()=>{
  const mixed={accounts:[[100,0,0,0,100]],advisors:{
    a:{ix:[0]},b:{ix:[0],av:{0:[0,90,0,0,90]}}}};
  assert.deepEqual(AssetProductFilters.accountValues(mixed,["a","b"]).get(0),[100,0,0,0,100]);
  assert.deepEqual(AssetProductFilters.accountValues(mixed,["b"]).get(0),[0,90,0,0,90]);
});
test("legacy books remain compatible and distinct codes stay distinct",()=>{
  const legacy={accounts:[[10,0,0,0],[0,20,0,0]],advisors:{a:{ix:[0,1]},b:{ix:[0]}}};
  const selected=AssetProductFilters.accountValues(legacy,["a","b","missing"]);
  assert.equal(selected.size,2);assert.equal([...selected.values()].reduce((n,a)=>n+a[0]+a[1],0),30);
});
test("team and viewport summaries both retain a filtered advisor's lower amount",()=>{
  const source=fs.readFileSync(path.join(__dirname,"../../webapp/app.js"),"utf8");
  function declaration(name){const tail=source.slice(source.indexOf("function "+name+"("));
    const end=tail.search(/\r?\n}/);assert.ok(end>0);return tail.slice(0,end)+"\n}";}
  const elements={};const document={getElementById:(id)=>elements[id]||(elements[id]={})};
  const context={ACT_ASSETS:book,AssetProductFilters,document,fmtMoney:String};
  vm.createContext(context);vm.runInContext(declaration("teamBook")+"\n"+declaration("renderEicAssets"),context);
  assert.equal(context.teamBook(["low"]).acv,80);
  assert.equal(context.teamBook(["low","high"]).acv,100);
  context.renderEicAssets(["low"]);assert.equal(elements.eicAcv.textContent,"80");
  context.renderEicAssets(["low","high"]);assert.equal(elements.eicAcv.textContent,"100");
  const key = "act-00000000000000000000000001";
  context.ACT_ASSETS = {...book, act_contacts:{[key]:{ix:[0],av:{0:[75,0,0,0,75]}}}};
  context.renderEicAssets([key]);assert.equal(elements.eicAcv.textContent,"75");
  context.renderEicAssets([key,"high"]);assert.equal(elements.eicAcv.textContent,"100");
});
