"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const bounce = require("../shared/email-bounce");

const ndr = (over = {}) => ({
  from: { emailAddress: { address: "postmaster@morganstanley.com" } },
  subject: "Undeliverable: Quarterly Commentary",
  internetMessageHeaders: [
    { name: "Content-Type", value: 'multipart/report; report-type=delivery-status' },
    { name: "References", value: "<orig-123@eicatlanta.com>" },
  ],
  body: { contentType: "text", content: [
    "Your message could not be delivered.",
    "Final-Recipient: rfc822; a.white@morganstanley.com",
    "Action: failed",
    "Status: 5.1.1",
    "Diagnostic-Code: smtp; 550 5.1.1 User unknown",
  ].join("\n") },
  ...over,
});

const sent = new Map([["orig-123@eicatlanta.com",
  { id: "msg-1", recipientEmail: "a.white@morganstanley.com", contactId: "1000084" }]]);

test("a hard bounce is recognised and matched to our message", () => {
  const r = bounce.assess(ndr(), sent);
  assert.equal(r.act, true);
  assert.equal(r.verdict.kind, "hard");
  assert.equal(r.verdict.code, "5.1.1");
  assert.equal(r.address, "a.white@morganstanley.com");
  assert.equal(r.message.contactId, "1000084");
});

test("a soft bounce is ignored", () => {
  // 4.x.x recovers on its own. Suppressing here would drop an advisor over a
  // temporary greylist.
  const r = bounce.assess(ndr({ body: { contentType: "text",
    content: "Status: 4.4.7\nAction: delayed" } }), sent);
  assert.equal(r.act, false);
  assert.equal(r.reason, "soft");
});

test("permanent failures that are not the address's fault never suppress", () => {
  for (const [code, why] of [["5.2.2", "mailbox full"], ["5.7.1", "blocked by their policy"],
                             ["5.3.4", "message too large"], ["5.7.26", "DMARC"]]) {
    const r = bounce.assess(ndr({ body: { contentType: "text",
      content: `Status: ${code}\nFinal-Recipient: rfc822; a.white@morganstanley.com` } }), sent);
    assert.equal(r.act, false, `${code} (${why}) must not suppress`);
    assert.equal(r.reason, "policy");
  }
});

test("an ordinary email is not a bounce", () => {
  const r = bounce.assess({ from: { emailAddress: { address: "advisor@rjf.com" } },
    subject: "Re: Quarterly Commentary", internetMessageHeaders: [],
    body: { contentType: "text", content: "Thanks, will read this week." } }, sent);
  assert.equal(r.act, false);
  assert.equal(r.reason, "not-a-bounce");
});

test("an auto-reply that merely looks official is not a bounce", () => {
  // Looks like an NDR by sender, says nothing definite. Must not act: this is
  // the false-positive that silently suppresses a reachable advisor.
  const r = bounce.assess(ndr({ subject: "Automatic reply: Out of office",
    body: { contentType: "text", content: "I am out of the office until Monday." } }), sent);
  assert.equal(r.act, false);
  assert.equal(r.reason, "not-a-bounce");
});

test("a bounce we cannot match to a sent message is left alone", () => {
  const r = bounce.assess(ndr({ internetMessageHeaders: [
    { name: "References", value: "<someone-elses@example.com>" }] }), new Map());
  assert.equal(r.act, false);
  assert.equal(r.reason, "unmatched");
});

test("the address comes from OUR record, never from the report", () => {
  // A forged NDR naming a different victim must not suppress that victim. The
  // report is only ever evidence that something failed.
  const forged = ndr({ body: { contentType: "text", content: [
    "Final-Recipient: rfc822; someone.else@bigfirm.com",
    "Status: 5.1.1"].join("\n") } });
  const r = bounce.assess(forged, sent);
  assert.equal(r.act, false);
  assert.equal(r.reason, "recipient-mismatch");
});

test("HTML-bodied reports are read too", () => {
  const r = bounce.assess(ndr({ body: { contentType: "html",
    content: "<html><p>Status: 5.1.10</p><p>Final-Recipient: rfc822; a.white@morganstanley.com</p></html>" } }), sent);
  assert.equal(r.act, true);
  assert.equal(r.verdict.code, "5.1.10");
});

test("a success report is not treated as a bounce", () => {
  const r = bounce.assess(ndr({ subject: "Delivery Status Notification (Relayed)",
    body: { contentType: "text", content: "Status: 2.0.0\nAction: relayed" } }), sent);
  assert.equal(r.act, false);
});

test("the original id is found in any of the places servers put it", () => {
  for (const header of ["References", "In-Reply-To", "X-MS-Exchange-Original-Message-Id"]) {
    const r = bounce.assess(ndr({ internetMessageHeaders: [
      { name: "Content-Type", value: "multipart/report; report-type=delivery-status" },
      { name: header, value: "<orig-123@eicatlanta.com>" }] }), sent);
    assert.equal(r.act, true, `should match via ${header}`);
  }
  // And in the quoted original inside the body, which is where Exchange often
  // leaves it when the headers are stripped by an intermediate hop.
  const r = bounce.assess(ndr({ internetMessageHeaders: [
      { name: "Content-Type", value: "multipart/report; report-type=delivery-status" }],
    body: { contentType: "text", content: [
      "Status: 5.1.1", "Final-Recipient: rfc822; a.white@morganstanley.com",
      "----- Original message -----", "Message-ID: <orig-123@eicatlanta.com>"].join("\n") } }), sent);
  assert.equal(r.act, true, "should match via the quoted original");
});
