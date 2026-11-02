/* Shared desktop/Field product semantics. No data fetching or identity rules. */
(function(root, factory){
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.AssetProductFilters = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function(){
  "use strict";
  const PRODUCTS = Object.freeze([
    Object.freeze({ id:"acv", label:"ACV", title:"All-Cap Value SMA" }),
    Object.freeze({ id:"lcv", label:"LCV", title:"Large-Cap Value SMA" }),
    Object.freeze({ id:"midcap", label:"MCV", title:"Mid-Cap / unallocated in ACT!" }),
    Object.freeze({ id:"mf", label:"EICIX", title:"EICIX mutual fund" }),
  ]);
  const keys = PRODUCTS.map(p => p.id);
  function normalize(values){
    if (values === undefined) return [];
    if (!Array.isArray(values) || values.length > keys.length
        || values.some(v => typeof v !== "string" || !keys.includes(v)))
      throw new Error("Unsupported EIC product selection.");
    return keys.filter(k => values.includes(k));
  }
  function transition(assetsOnly, values, key){
    const selected = normalize(values);
    if (key === "any")
      return { assetsOnly:!(assetsOnly && !selected.length), assetProducts:[] };
    if (!keys.includes(key)) throw new Error("Unsupported EIC product selection.");
    const next = selected.includes(key) ? selected.filter(k => k !== key) : selected.concat(key);
    return { assetsOnly:next.length > 0, assetProducts:normalize(next) };
  }
  function matches(book, values){
    const selected = normalize(values);
    if (!book || typeof book !== "object" || Array.isArray(book)) return false;
    return !selected.length || selected.some(k => typeof book[k] === "number"
      && Number.isFinite(book[k]) && book[k] > 0);
  }
  function label(values){
    const selected = normalize(values);
    return selected.length ? PRODUCTS.filter(p => selected.includes(p.id)).map(p => p.label).join(" / ") : "Any EIC";
  }
  return Object.freeze({ PRODUCTS, normalize, transition, matches, label });
});
