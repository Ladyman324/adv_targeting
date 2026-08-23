"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const core = require("../shared/email-core");

test("renders only approved merge fields and exposes missing values", () => {
  const result = core.renderTemplate(
    "Hi {{first_name}} {{last_name}} at {{company_name}} — {{unknown}}",
    { name: "SMITH, JANE", firm: "Example & Co." });
  assert.equal(result.rendered, "Hi JANE SMITH at Example & Co. — {{unknown}}");
  assert.deepEqual(result.missing, []);
  assert.deepEqual(result.unknown, ["unknown"]);
  assert.deepEqual(result.unresolved, ["unknown"]);
});

test("missing merge values remain visible instead of becoming empty text", () => {
  const result = core.renderTemplate("Hello {{first_name}} at {{company_name}}", { name: "", firm: "" });
  assert.equal(result.rendered, "Hello {{first_name}} at {{company_name}}");
  assert.deepEqual(result.missing.sort(), ["company_name", "first_name"]);
});

test("email HTML conversion escapes user content", () => {
  const html = core.plainTextToSafeHtml("Hello <script>alert(1)</script>\nSecond line");
  assert.doesNotMatch(html, /<script>/i);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /<br>/);
});

test("sanitizer removes scripts, event handlers, and unsafe schemes", () => {
  const html = core.sanitizeEmailHtml('<p onclick="bad()">Hi<script>bad()</script><a href="javascript:bad()">x</a></p>');
  assert.doesNotMatch(html, /script|onclick|javascript/i);
  assert.match(html, /<p>Hi<a>x<\/a><\/p>/);
});

test("guardrails block direct sends above 250 and all campaign-sized batches", () => {
  const cfg = { ...core.config(), directBatchMax: 250 };
  assert.equal(core.guardrail(250, "send", cfg).blocked, false);
  assert.equal(core.guardrail(251, "send", cfg).blocked, true);
  assert.equal(core.guardrail(15000, "drafts", cfg).level, "campaign");
});

test("direct send is disabled unless production is explicitly enabled", () => {
  const oldNode = process.env.NODE_ENV, oldFlag = process.env.EMAIL_DIRECT_SEND_ENABLED;
  process.env.NODE_ENV = "test"; process.env.EMAIL_DIRECT_SEND_ENABLED = "1";
  assert.equal(core.config().directSendEnvironmentEnabled, false);
  if (oldNode === undefined) delete process.env.NODE_ENV; else process.env.NODE_ENV = oldNode;
  if (oldFlag === undefined) delete process.env.EMAIL_DIRECT_SEND_ENABLED; else process.env.EMAIL_DIRECT_SEND_ENABLED = oldFlag;
});

test("small print keeps the size it was given, and the two blocks match", () => {
  // The sanitizer's font-size allowlist was /^\d+(px|pt|em|rem|%)$/ -- no
  // decimals -- so "7.5pt" was silently dropped and the footer inherited the
  // signature's 10pt. The small print was never small, and setting it to match
  // the disclosure would have done nothing at all.
  process.env.EMAIL_FORESIDE_REPS = "rep@eicatlanta.com";
  const html = core.corporateSignature({ displayName: "Bo", mail: "rep@eicatlanta.com" },
    "https://example.test/p?t=abc");
  const sizes = [...html.matchAll(/font-size:([0-9.]+pt)/g)].map((m) => m[1]);
  assert.deepEqual(sizes, ["10pt", "7.5pt", "7.5pt"],
    "signature at 10pt, then the disclosure and the archive footer matching each other");
  assert.match(html, /<p style="margin:0 0 6pt"/, "pt margins must survive too");
  delete process.env.EMAIL_FORESIDE_REPS;
});

test("widening the style allowlist did not let anything dangerous through", () => {
  const junk = [
    '<p style="font-size:expression(alert(1))">x</p>',
    '<p style="margin:url(javascript:alert(1))">x</p>',
    '<p style="font-size:12pt;behavior:url(#x)">x</p>',
    '<img src="javascript:alert(1)">',
    '<a href="javascript:alert(1)">x</a>',
  ];
  for (const bad of junk) {
    const clean = core.sanitizeEmailHtml(bad);
    assert.doesNotMatch(clean, /javascript:|expression\(|behavior:/i, `leaked through: ${bad}`);
  }
  // And the legitimate values still pass.
  assert.match(core.sanitizeEmailHtml('<p style="font-size:7.5pt">x</p>'), /font-size:7\.5pt/);
  assert.match(core.sanitizeEmailHtml('<p style="margin:0 0 6pt">x</p>'), /margin:0 0 6pt/);
});

test("the signature address can span lines, without becoming an injection point", () => {
  const NL = String.fromCharCode(10), BS = String.fromCharCode(92);
  const two = "1776 Peachtree Street NW, Suite 600S";

  // Azure's App Settings box is single-line, so a portal user types \n.
  process.env.EMAIL_SIGNATURE_ADDRESS = `${two}${BS}nAtlanta, GA 30309`;
  let html = core.corporateSignature({ displayName: "Bo", mail: "b@eicatlanta.com" });
  assert.match(html, /Suite 600S<br \/>Atlanta, GA 30309/);

  // A real newline works too, for anyone setting it from the CLI or an ARM template.
  process.env.EMAIL_SIGNATURE_ADDRESS = `${two}${NL}Atlanta, GA 30309`;
  html = core.corporateSignature({ displayName: "Bo", mail: "b@eicatlanta.com" });
  assert.match(html, /Suite 600S<br \/>Atlanta, GA 30309/);

  // A single-line address is untouched.
  process.env.EMAIL_SIGNATURE_ADDRESS = `${two}, Atlanta, GA 30309`;
  html = core.corporateSignature({ displayName: "Bo", mail: "b@eicatlanta.com" });
  assert.match(html, /Suite 600S, Atlanta, GA 30309/);
  assert.doesNotMatch(html, /Suite 600S, Atlanta, GA 30309<br \/>Atlanta/);

  // Escaping happens BEFORE the markers become <br>. The other order would let
  // this one setting inject markup into every outgoing signature.
  process.env.EMAIL_SIGNATURE_ADDRESS = `<img src=x onerror=alert(1)>${BS}nLine two`;
  html = core.corporateSignature({ displayName: "Bo", mail: "b@eicatlanta.com" });
  assert.doesNotMatch(html, /<img[\s>]/i, "must not become a real tag");
  assert.match(html, /&lt;img/, "kept as visible text instead");
  assert.match(html, /Line two/, "and the break still applies");

  delete process.env.EMAIL_SIGNATURE_ADDRESS;
});

test("the sender's own name and title merge from the signature profile", () => {
  const profile = { displayName: "Bo Ladyman", jobTitle: "Regional Director",
    mail: "b@eicatlanta.com" };
  const recipient = { name: "Dana Whitfield", firm: "Whitfield Wealth Partners" };

  const out = core.renderTemplate(
    "Hi {{first_name}},\n\n{{sender_name}}\n{{sender_title}}", recipient, profile);
  assert.equal(out.rendered, "Hi Dana,\n\nBo Ladyman\nRegional Director");
  assert.deepEqual(out.missing, []);
  assert.deepEqual(out.unknown, []);

  // The body and the signature block read the same profile, so the name in the
  // text can never disagree with the name signing it.
  assert.match(core.corporateSignature(profile), /Bo Ladyman/);

  // A profile captured before jobTitle was collected leaves the field empty.
  // That is reported as missing -- the send is held rather than going out with
  // a bare "{{sender_title}}" sitting in the text.
  const stale = core.renderTemplate("{{sender_title}}", recipient,
    { displayName: "Bo Ladyman" });
  assert.deepEqual(stale.missing, ["sender_title"]);
  assert.equal(stale.rendered, "{{sender_title}}");

  // No sender at all: still missing, never a crash.
  assert.deepEqual(core.renderTemplate("{{sender_name}}", recipient).missing,
    ["sender_name"]);
});
