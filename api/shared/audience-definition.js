"use strict";

const crypto = require("crypto");
const MAX_DEFINITION_BYTES = 8 * 1024;
const STATES = new Set((
  "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS "
  + "MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA "
  + "WV WI WY DC PR VI").split(" "));
const TERRITORIES = {
  West: ["AK", "CA", "HI", "ID", "MT", "NV", "OR", "WA", "WY"],
  Southwest: ["AR", "AZ", "CO", "KS", "LA", "NM", "OK", "TX", "UT"],
  Midwest: ["IA", "IL", "IN", "KY", "MI", "MN", "MO", "ND", "NE", "OH", "SD", "WI"],
  Southeast: ["AL", "GA", "MS", "NC", "SC", "TN"],
  "Mid-Atlantic": ["DC", "DE", "MD", "NJ", "PA", "VA", "WV"],
  Northeast: ["CT", "MA", "ME", "NH", "NY", "RI", "VT"],
  "Florida/PR": ["FL", "PR", "VI"],
};
const ARRAY_FILTERS = new Set(
  ["selectedFirms", "aum", "exp", "reach", "geo", "excluded"]);
const BOOLEAN_FILTERS = new Set([
  "selectsOnly", "ownerOnly", "rankedOnly", "continentalOnly",
  "contactableOnly", "assetsOnly",
]);
const FILTER_KEYS = new Set([...ARRAY_FILTERS, ...BOOLEAN_FILTERS, "reg"]);
const ENUMS = {
  aum: new Set(["lt100m", "100m1b", "1b10b", "10b100b", "gt100b"]),
  exp: new Set(["lt10", "10to20", "gt20"]),
  reach: new Set(["one", "few", "many"]),
  geo: new Set(["rooftop", "approximate", "neighbour", "city"]),
};

function bad(message, statusCode = 400) {
  const error = new Error(message);
  error.statusCode = statusCode;
  return error;
}
function object(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)
      || Object.getPrototypeOf(value) !== Object.prototype)
    throw bad(`${label} must be an object.`);
  return value;
}
function text(value, max, label) {
  const result = String(value === undefined || value === null ? "" : value).trim();
  if (result.length > max) throw bad(`${label} is too long.`);
  return result;
}
function array(value, label, maxItems = 200, maxWidth = 80) {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length > maxItems)
    throw bad(`${label} must be a bounded array.`);
  return [...new Set(value.map((item) => text(item, maxWidth, label)).filter(Boolean))];
}
function enumArray(value, key) {
  const result = array(value, `filters.${key}`);
  if (result.some((item) => !ENUMS[key].has(item)))
    throw bad(`filters.${key} contains an unsupported value.`);
  return result;
}
function normalizeScope(value) {
  const scope = object(value, "scope");
  for (const key of Object.keys(scope))
    if (!["kind", "value", "states", "label"].includes(key))
      throw bad(`scope.${key} is not supported.`);
  const kind = text(scope.kind, 16, "scope.kind").toLowerCase();
  const states = array(scope.states, "scope.states", 53, 2)
    .map((state) => state.toUpperCase()).sort();
  const rawValue = text(scope.value, 80, "scope.value");
  if (!["state", "territory"].includes(kind))
    throw bad("scope.kind must be state or territory.");
  if (!states.length || states.some((state) => !STATES.has(state)))
    throw bad("scope.states contains an unsupported state.");
  if (!rawValue || rawValue.toUpperCase() === "US")
    throw bad("A national audience is not supported.");
  if (kind === "state") {
    const state = rawValue.toUpperCase();
    if (states.length !== 1 || states[0] !== state)
      throw bad("A state scope must name its one state.");
    return { kind, value: state, states, label: text(scope.label || state, 100, "scope.label") };
  }
  const name = rawValue.startsWith("T:") ? rawValue.slice(2) : "";
  const expected = TERRITORIES[name];
  if (!expected || JSON.stringify(states) !== JSON.stringify([...expected].sort()))
    throw bad("A territory scope must use a canonical T:<name> value and exact states.");
  return { kind, value: `T:${name}`, states,
    label: text(scope.label || name, 100, "scope.label") };
}
function normalizeFilters(value) {
  const input = value === undefined ? {} : object(value, "filters");
  for (const key of Object.keys(input))
    if (!FILTER_KEYS.has(key)) throw bad(`filters.${key} is not supported.`);
  const filters = {};
  for (const key of ARRAY_FILTERS)
    filters[key] = ENUMS[key] ? enumArray(input[key], key)
      : array(input[key], `filters.${key}`);
  for (const key of BOOLEAN_FILTERS) filters[key] = input[key] === true;
  filters.reg = text(input.reg || "all", 16, "filters.reg").toLowerCase();
  if (!["all", "dual", "ria"].includes(filters.reg))
    throw bad("filters.reg is invalid.");
  return filters;
}
function normalizeDefinition(value) {
  const input = object(value, "definition");
  for (const key of Object.keys(input))
    if (!["version", "scope", "filters"].includes(key))
      throw bad(`definition.${key} is not supported.`);
  if (Number(input.version) !== 1) throw bad("definition.version must be 1.");
  const definition = {
    version: 1,
    scope: normalizeScope(input.scope),
    filters: normalizeFilters(input.filters),
  };
  const json = JSON.stringify(definition);
  if (Buffer.byteLength(json, "utf8") > MAX_DEFINITION_BYTES)
    throw bad(`definition must be at most ${MAX_DEFINITION_BYTES} bytes.`);
  return { definition, json };
}
function safeId(value) {
  const id = String(value || "").toLowerCase();
  if (!/^a-[a-z0-9-]{8,38}$/.test(id)) throw bad("Audience id is invalid.");
  return id;
}
function newId() { return `a-${crypto.randomUUID()}`; }

module.exports = { MAX_DEFINITION_BYTES, normalizeDefinition, safeId, newId };
