"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const core = require("../shared/email-core");
const { COMPANY_DISPLAY_NAMES, companyDisplayName } = require("../shared/email-company-names");
const { identityPresentationRefresh } = require("../shared/email-service");
const approved = require("../shared/email-company-names.json");

const unchanged = ["Ameriprise", "Chevy Chase Trust", "Corient", "Edward Jones",
  "Focus Partners", "Janney Montgomery Scott", "LPL Financial", "Mariner",
  "Mercer Advisors", "Merrill Lynch", "Morgan Stanley", "Northwestern Mutual",
  "RBC Wealth Management", "Raymond James", "Red Door Wealth Management",
  "Robert W. Baird", "Sanctuary Wealth", "UBS", "Waverly Advisors", "Wealthspire Advisors"];
const changes = [
  ["Raymond James (branch pages)", "Raymond James"],
  ["Alex. Brown (Raymond James)", "Alex. Brown"],
  ["Stifel Nicolaus", "Stifel"],
  ["Wells Fargo Advisors", "Wells Fargo"],
  ["Truist Wealth", "Truist"],
];

test("the approved full CSV has exactly 442 explicit labels plus the five roster changes", () => {
  assert.equal(Object.keys(COMPANY_DISPLAY_NAMES).length, 442);
  assert.equal(approved.approvedLabelCount, 442);
  assert.equal(approved.sourceAuditSha256, "f5f72013a667ece7d300605c32a4874d7d7a49e4c64ee8e39408dc7b0c66a7a3");
  assert.equal(Object.isFrozen(COMPANY_DISPLAY_NAMES), true);
  for (const name of unchanged) assert.equal(companyDisplayName(name), name);
  for (const [source, display] of changes) assert.equal(companyDisplayName(source), display);
});

test("recognized formatting variations preserve canonical acronyms and approved names", () => {
  for (const [source, display] of changes) assert.equal(companyDisplayName(`  ${source.toUpperCase()}  `), display);
  assert.equal(companyDisplayName("Ubs"), "UBS");
  assert.equal(companyDisplayName("rbc wealth management"), "RBC Wealth Management");
  assert.equal(companyDisplayName("lpl   financial"), "LPL Financial");
});

test("approved ACT aliases and review adjustments use explicit display names", () => {
  for (const [value, expected] of [
    ["UBS Financial Services Inc.", "UBS"],
    ["Morgan Stanley Wealth Management, LLC", "Morgan Stanley"],
    ["Wells Fargo Advisors, LLC", "Wells Fargo"],
    ["Wells Fargo Advisors Financial Network", "Wells Fargo"],
    ["Stifel Nicolaus & Company Inc", "Stifel"],
    ["Graystone Consulting from Morgan Stanley", "Graystone Consulting"],
    ["Greystone Consulting From Morgan Stanley", "Graystone Consulting"],
    ["Alex. Brown - A Division of Raymond James", "Alex. Brown"],
    ["Truist Investment Services", "Truist"],
    ["& Partners", "&Partners"],
    ["Duncan.Newman Associates", "Duncan-Newman Associates"],
    ["Wlliam Blair", "William Blair"],
  ]) assert.equal(companyDisplayName(value), expected);
  for (const value of ["Thrivent Financial", "Thrivent Financial For Lutherans",
    "Thrivent Financial for Lutherans", "Thrivent Investment Management Inc."])
    assert.equal(companyDisplayName(value), "Thrivent");
});

test("legacy affiliations stay pending and are never automatically assigned successor brands", () => {
  assert.equal(approved.heldForAffiliationReview.length, 23);
  for (const value of approved.heldForAffiliationReview) assert.equal(companyDisplayName(value), value);
  for (const value of ["Tiedemann Wealth Management, LLC", "Securities America, Inc.",
    "Royal Alliance Associates, Inc.", "SunTrust Investment Services, Inc."])
    assert.equal(companyDisplayName(value), value);
});

test("unfamiliar practices are not guessed or parent-collapsed", () => {
  for (const value of ["  Custom Practice LLC  ", "Unknown UBS Practice", "Another Firm Inc.",
    "constructor", "__proto__", "toString"]) {
    assert.equal(companyDisplayName(value), value);
  }
});

test("the full explicit map has no conflicting case or whitespace keys", () => {
  const seen = new Map();
  for (const [source, display] of Object.entries(COMPANY_DISPLAY_NAMES)) {
    const key = source.trim().replace(/\s+/g, " ").toLowerCase();
    if (seen.has(key)) assert.equal(seen.get(key), display);
    seen.set(key, display);
    assert.equal(companyDisplayName(source), display);
  }
  assert.equal(seen.size, 441);
});

test("company merge uses approved display in subject/body without changing recipient metadata", () => {
  for (const [source, display] of changes) {
    const recipient = Object.freeze({ companyName: source, firm: "Original regulatory entity",
      firstName: "Dana", lastName: "Example", email: "dana@example.test",
      source, routingHash: "unchanged-routing-proof" });
    assert.equal(core.renderTemplate("Checking in at {{company_name}}", recipient).rendered,
      `Checking in at ${display}`);
    assert.equal(core.renderTemplate("Hi {{first_name}}, how are things at {{ COMPANY_NAME }}?", recipient).rendered,
      `Hi Dana, how are things at ${display}?`);
    assert.equal(recipient.companyName, source);
    assert.equal(recipient.firm, "Original regulatory entity");
    assert.equal(recipient.routingHash, "unchanged-routing-proof");
    assert.equal(core.mergeValues({ firm: source }).company_name, display);
  }
});

test("blank company remains unresolved rather than inventing a name", () => {
  assert.equal(companyDisplayName(null), "");
  assert.equal(companyDisplayName(undefined), "");
  const result = core.renderTemplate("Hello {{company_name}}", {});
  assert.equal(result.rendered, "Hello {{company_name}}");
  assert.deepEqual(result.missing, ["company_name"]);
});

test("existing stored text is not rewritten just because a display rule changes rendering", () => {
  const approved = { name: "Dana Example", greetingName: "Dana", lastName: "Example", firm: "Wells Fargo Advisors" };
  const message = Object.freeze({ recipientName: approved.name, greetingName: approved.greetingName,
    recipientLastName: approved.lastName, companyName: approved.firm,
    bodyText: "How are things at Wells Fargo Advisors?", subject: "Wells Fargo Advisors",
    bodyOverridden: false, subjectOverridden: false });
  const result = identityPresentationRefresh(message, approved, {
    commonSubject: "{{company_name}}", commonBodyText: "How are things at {{company_name}}?",
  }, null, []);
  assert.equal(result.changed, false);
  assert.equal(result.blocked, false);
  assert.equal(result.patch.companyName, "Wells Fargo Advisors");
  assert.equal(result.patch.bodyText, undefined);
  assert.equal(result.patch.subject, undefined);
  assert.equal(message.bodyText, "How are things at Wells Fargo Advisors?");
});

test("already-reviewed literal text is not searched and replaced", () => {
  const literal = "How are things at Wells Fargo Advisors?";
  assert.equal(core.renderTemplate(literal, { firm: "Wells Fargo Advisors" }).rendered, literal);
});
