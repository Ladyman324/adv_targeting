"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const AssetProductFilters = require("../../webapp/asset-product-filters");
const source = fs.readFileSync(path.join(__dirname,"../../webapp/app.js"),"utf8");
function between(start,end){
  const a=source.indexOf(start), b=source.indexOf(end,a);
  assert.ok(a>=0 && b>a, start); return source.slice(a,b);
}
function setup(){
  const buttons=["any","acv","lcv","midcap","mf"].map(product=>({
    dataset:{product},disabled:false,pressed:"false",
    setAttribute(name,value){if(name==="aria-pressed")this.pressed=String(value);},
  }));
  const handlers={}, nodes={};
  nodes.assetProductToggle={querySelectorAll:()=>buttons,addEventListener:(_,fn)=>{handlers.product=fn;}};
  nodes.assetFilterStatus={hidden:true,textContent:""};
  nodes.activeFilters={addEventListener:(_,fn)=>{handlers.clear=fn;}};
  const books={acv:{acv:10,ix:[0]},alsoAcv:{acv:10,ix:[0]},lcv:{lcv:20,ix:[1]},
    mf:{mf:30,ix:[2]},midcap:{midcap:5,ix:[3]},zero:{n:1,ix:[4]}};
  const noop=()=>{};
  const context={assetsOnly:false,assetProducts:new Set(),SUPPORT:{act:"ready"},scope:"GA",AssetProductFilters,
    bookFor:id=>books[id]||null,document:{getElementById:id=>nodes[id]||(nodes[id]={})},
    redraw:noop,reportFilterReach:noop,refill:(set,items)=>{set.clear();(items||[]).forEach(x=>set.add(x));},
    selectedFirms:[],selectsOnly:false,aumSel:new Set(),roleSel:new Set(),reg:"all",lastEmailed:"",lastCalled:"",
    joinedFirm:"",emailScope:"me",callScope:"me",ownerOnly:false,rankedOnly:false,excludedFirms:new Set(),
    continentalOnly:true,contactableOnly:false,audienceScopeDefinition:()=>({kind:"state",value:"GA",states:["GA"]}),
    syncFirmColors:noop,syncFilterButtons:noop,clearLasso:noop,loadAudienceActivity:async()=>{},
    audienceActivityScope:()=>"me",setContinentalOnly:noop,selectsBox:{checked:false,closest:()=>({classList:{toggle:noop}})},
    syncTargetingUI:noop,activityOptionCache:new Map(),resetTargeting:noop,SELECTS_DEFAULT:false,
    OUTSIDE_CONTINENTAL:new Set(),clearSearch:noop,
    fmtMoney:n=>String(n), ACT_ASSETS:{advisors:books,accounts:[[10,7,0,0],[0,20,0,0],[0,0,30,0],[0,0,0,5]],
      totals:{acv:10,lcv:27,mf:30,midcap:5},as_of:"2026-11-02"},
  };
  vm.createContext(context);
  vm.runInContext(between('const assetProductToggle =','const ownerBox =')
    +between('function passesHasAssets(','function passesContinental(')
    +between('function captureAudienceDefinition(','async function saveCurrentAudience(')
    +between('async function applyAudienceDefinition(','function currentAudiencePreview(')
    +between('function captureAdvisorFilters(','function refill(')
    +between('function resetAllFilters(','document.getElementById("resetAll")')
    +between('document.getElementById("activeFilters").addEventListener','// ---- advisor search ----')
    +between('function renderEicAssets(','function renderEstimatedAum('),context);
  return {c:context,buttons,nodes,click(key){handlers.product({target:{closest:()=>buttons.find(b=>b.dataset.product===key)}});},
    clear(){handlers.clear({target:{closest:()=>({dataset:{clearFilter:"assets"}})}});}};
}
const plain=x=>JSON.parse(JSON.stringify(x));
test("desktop product row preserves Any EIC, OR, exclusivity and last-off behavior",()=>{
  const {c,click}=setup();
  assert.equal(c.passesHasAssets({id:"prospect"}),true);
  click("any"); assert.equal(c.passesHasAssets({id:"prospect"}),false); assert.equal(c.passesHasAssets({id:"zero"}),true);
  click("acv"); click("mf");
  assert.equal(c.passesHasAssets({id:"acv"}),true); assert.equal(c.passesHasAssets({id:"mf"}),true);
  assert.equal(c.passesHasAssets({id:"lcv"}),false);assert.equal(c.passesHasAssets({id:"zero"}),false);
  click("any"); assert.equal(c.assetProducts.size,0);assert.equal(c.assetsOnly,true);
  click("any");assert.equal(c.assetsOnly,false);
  click("midcap");assert.equal(c.passesHasAssets({id:"midcap"}),true);
  click("midcap");assert.equal(c.passesHasAssets({id:"prospect"}),true);
});
test("desktop disables product controls nationally or without canonical book data",()=>{
  const {c,click,buttons,nodes}=setup();click("lcv");
  assert.equal(buttons.find(b=>b.dataset.product==="lcv").pressed,"true");
  c.SUPPORT.act="loading";c.syncAssetsUI();assert.ok(buttons.every(b=>b.disabled));
  assert.equal(c.passesHasAssets({id:"lcv"}),false);click("acv");assert.deepEqual([...c.assetProducts],["lcv"]);
  c.SUPPORT.act="failed";c.syncAssetsUI();assert.match(nodes.assetFilterStatus.textContent,/unavailable/);
  c.SUPPORT.act="ready";c.scope="US";c.syncAssetsUI();click("acv");assert.deepEqual([...c.assetProducts],["lcv"]);
});
test("specific saved audiences use v2; legacy Any EIC remains v1",async()=>{
  const {c,click}=setup();click("acv");
  const definition=plain(c.captureAudienceDefinition());assert.equal(definition.version,2);
  assert.deepEqual(definition.filters.assetProducts,["acv"]);
  await c.applyAudienceDefinition({definition:{...definition,filters:{assetProducts:["mf","lcv"],assetsOnly:false}}});
  assert.equal(c.assetsOnly,true);assert.deepEqual([...c.assetProducts],["lcv","mf"]);
  await c.applyAudienceDefinition({definition:{version:1,scope:{value:"GA"},filters:{assetsOnly:true}}});
  assert.equal(c.assetsOnly,true);assert.equal(c.assetProducts.size,0);assert.equal(c.captureAudienceDefinition().version,1);
  await assert.rejects(c.applyAudienceDefinition({definition:{version:3}}),/unsupported/);
});
test("saved product audiences reject unavailable book data or invalid products before mutation",async()=>{
  const {c,click}=setup();click("acv");c.SUPPORT.act="failed";c.loadRegionalSupport=async()=>{};
  await assert.rejects(c.applyAudienceDefinition({definition:{version:2,scope:{value:"GA"},filters:{assetProducts:["mf"]}}}),/unavailable/);
  assert.deepEqual([...c.assetProducts],["acv"]);
  await assert.rejects(c.applyAudienceDefinition({definition:{version:2,filters:{assetProducts:["bad"]}}}),/Unsupported EIC/);
});
test("history restore, active-chip clear and reset-all retain or clear product criteria together",()=>{
  const {c,click,clear}=setup();click("acv");const saved=plain(c.captureAdvisorFilters());
  clear();assert.equal(c.assetsOnly,false);assert.equal(c.assetProducts.size,0);
  c.restoreAdvisorFilters(saved);assert.equal(c.assetsOnly,true);assert.deepEqual([...c.assetProducts],["acv"]);
  c.resetAllFilters();assert.equal(c.assetsOnly,false);assert.equal(c.assetProducts.size,0);
});
test("product-filtered summaries count a shared account once and retain other product balances",()=>{
  const {c,click,nodes}=setup();click("acv");
  const ids=["acv","alsoAcv","lcv"].filter(id=>c.passesHasAssets({id}));
  c.renderEicAssets(ids);
  assert.equal(nodes.eicAcv.textContent,"10");assert.equal(nodes.eicLcv.textContent,"7");
  assert.equal(nodes.eicMf.textContent,"0");assert.match(nodes.eicAssetsHelp.textContent,/1 account/);
});
