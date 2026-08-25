"use strict";

const test = require("node:test");
const crypto = require("node:crypto");
const assert = require("node:assert/strict");

process.env.EMAIL_UNSUBSCRIBE_SECRET = "test-secret-for-unit-tests-only";
const suppress = require("../shared/email-suppress");
const core = require("../shared/email-core");

test("an unsubscribe token round-trips to exactly the address it names", () => {
  const token = suppress.signToken("Advisor@MorganStanley.com", "1000084");
  assert.deepEqual(suppress.readToken(token), { email: "advisor@morganstanley.com", crd: "1000084" });
});

test("tokens minted before the CRD was added still work", () => {
  // Those links are already sitting in inboxes; breaking them would silently
  // strand every recipient of every batch sent before this change.
  const legacy = suppress.signToken("advisor@rjf.com");
  assert.deepEqual(suppress.readToken(legacy), { email: "advisor@rjf.com", crd: "" });
});

// The token is encrypted, and the reason is not abstract. The unsubscribe page
// asks the visitor to type the address the message was sent to -- a test a mail
// gateway's sandbox fails, because it will press a button but has no value to
// put in a field. A base64 payload handed it that value for free.
test("the token does not carry the address in readable form", () => {
  const token = suppress.signToken("advisor@morganstanley.com", "1000084");
  assert.doesNotMatch(token, /advisor/i, "the address must not be readable in the URL");
  const decoded = Buffer.from(token.replace(/^e\./, "")
    .replace(/-/g, "+").replace(/_/g, "/"), "base64").toString("latin1");
  assert.doesNotMatch(decoded, /advisor@morganstanley/i,
    "base64-decoding the token must not reveal the address either");
  // Each minting is distinct: a fresh nonce, so the same address never produces
  // the same URL twice and the tokens cannot be compared for equality.
  assert.notEqual(token, suppress.signToken("advisor@morganstanley.com", "1000084"));
});

test("links already in inboxes, in the pre-encryption format, still open", () => {
  // Built the way the old signToken() built them: HMAC over a base64 payload,
  // keyed on the bare secret. Every batch sent before today carries one of
  // these, and they must keep working for as long as the mail exists.
  const b64url = (buf) => buf.toString("base64")
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const key = crypto.createHash("sha256")
    .update(process.env.EMAIL_UNSUBSCRIBE_SECRET).digest();
  const mint = (claim) => {
    const payload = b64url(Buffer.from(claim, "utf8"));
    const mac = b64url(crypto.createHmac("sha256", key).update(payload).digest()).slice(0, 27);
    return `${payload}.${mac}`;
  };
  assert.deepEqual(suppress.readToken(mint("advisor@rjf.com|1000084")),
    { email: "advisor@rjf.com", crd: "1000084" });
  assert.deepEqual(suppress.readToken(mint("advisor@rjf.com")),
    { email: "advisor@rjf.com", crd: "" });
  // And a legacy token whose payload was edited is still refused.
  const [, mac] = mint("advisor@rjf.com").split(".");
  assert.equal(suppress.readToken(`${b64url(Buffer.from("victim@rjf.com"))}.${mac}`), null);
});

test("a sealed token with any byte altered is refused, not trusted", () => {
  const token = suppress.signToken("advisor@rjf.com", "1000084");
  const body = token.slice(2);
  for (const at of [0, 6, 14, body.length - 1]) {
    const ch = body[at] === "A" ? "B" : "A";
    const edited = `e.${body.slice(0, at)}${ch}${body.slice(at + 1)}`;
    assert.equal(suppress.readToken(edited), null, `edit at ${at} should be refused`);
  }
  assert.equal(suppress.readToken("e."), null);
  assert.equal(suppress.readToken("e.not-base64-at-all"), null);
});

test("the CRD is inside the signature, not merely appended", () => {
  const token = suppress.signToken("advisor@rjf.com", "1000084");
  const mac = token.split(".")[1];
  const swapped = Buffer.from("advisor@rjf.com|9999999", "utf8").toString("base64")
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "") + "." + mac;
  assert.equal(suppress.readToken(swapped), null);
});

test("a tampered token is rejected rather than trusted", () => {
  const token = suppress.signToken("advisor@morganstanley.com");
  const [payload, mac] = token.split(".");
  // Swap in another address, keep the original signature. This is the attack
  // that would let anyone unsubscribe anyone by editing a query string.
  const forged = Buffer.from("victim@rjf.com", "utf8").toString("base64")
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "") + "." + mac;
  assert.equal(suppress.readToken(forged), null);
  // And a mangled signature over a genuine payload.
  assert.equal(suppress.readToken(`${payload}.${"a".repeat(mac.length)}`), null);
  assert.equal(suppress.readToken(""), null);
  assert.equal(suppress.readToken("no-dot-here"), null);
});

test("without a configured secret no link is emitted at all", () => {
  const saved = process.env.EMAIL_UNSUBSCRIBE_SECRET;
  delete process.env.EMAIL_UNSUBSCRIBE_SECRET;
  try {
    assert.equal(suppress.signToken("advisor@rjf.com"), "");
    assert.equal(suppress.manageUrl("advisor@rjf.com"), "");
  } finally { process.env.EMAIL_UNSUBSCRIBE_SECRET = saved; }
});

test("the archive footer is present for everyone and links only when configured", () => {
  const withLink = core.archiveFooter("https://example.test/api/email-preferences?t=abc");
  assert.match(withLink, /archived for SEC review/);
  assert.match(withLink, /<a href="https:\/\/example\.test[^"]*">click here<\/a>/);
  const without = core.archiveFooter("");
  assert.match(without, /To manage your email preferences, click here\./);
  assert.doesNotMatch(without, /<a /);
});

test("the Foreside paragraph appears only for named registered representatives", () => {
  process.env.EMAIL_FORESIDE_REPS = "rep@eicatlanta.com";
  const cfg = core.config();
  const rep = core.corporateSignature({ displayName: "A Rep", mail: "rep@eicatlanta.com" }, "", cfg);
  const other = core.corporateSignature({ displayName: "B Other", mail: "other@eicatlanta.com" }, "", cfg);
  assert.match(rep, /Foreside Funds Distributors/);
  assert.doesNotMatch(other, /Foreside/);
  // The universal footer is on BOTH -- that is the whole distinction being tested.
  assert.match(rep, /archived for SEC review/);
  assert.match(other, /archived for SEC review/);
  delete process.env.EMAIL_FORESIDE_REPS;
});

test("the passcode is required only above the threshold and compared exactly", () => {
  process.env.EMAIL_APPROVAL_PASSCODE = "8317";
  process.env.EMAIL_PASSCODE_OVER = "10";
  const cfg = core.config();
  assert.equal(core.passcodeRequired(10, cfg), false);
  assert.equal(core.passcodeRequired(11, cfg), true);
  assert.equal(core.passcodeMatches("8317", cfg), true);
  assert.equal(core.passcodeMatches("8318", cfg), false);
  assert.equal(core.passcodeMatches("831", cfg), false);   // length mismatch
  assert.equal(core.passcodeMatches("", cfg), false);
  assert.equal(core.passcodeMatches(null, cfg), false);

  // Unset code disables the gate entirely rather than locking everyone out --
  // a threshold with no code would block every batch above it forever.
  delete process.env.EMAIL_APPROVAL_PASSCODE;
  assert.equal(core.passcodeRequired(500, core.config()), false);
  delete process.env.EMAIL_PASSCODE_OVER;
});

test("guardrail tiers follow the configured thresholds, not the old literals", () => {
  process.env.EMAIL_REVIEW_SUMMARY_OVER = "5";
  process.env.EMAIL_REVIEW_LARGE_OVER = "8";
  process.env.EMAIL_REVIEW_ELEVATED_OVER = "12";
  process.env.EMAIL_DRAFTS_ONLY_OVER = "20";
  const cfg = core.config();
  assert.equal(core.guardrail(4, "drafts", cfg).level, "normal");
  assert.equal(core.guardrail(6, "drafts", cfg).level, "summary");
  assert.equal(core.guardrail(9, "drafts", cfg).level, "large");
  assert.equal(core.guardrail(13, "drafts", cfg).level, "elevated");
  assert.equal(core.guardrail(21, "drafts", cfg).level, "drafts-only");
  for (const k of ["EMAIL_REVIEW_SUMMARY_OVER", "EMAIL_REVIEW_LARGE_OVER",
                   "EMAIL_REVIEW_ELEVATED_OVER", "EMAIL_DRAFTS_ONLY_OVER"]) delete process.env[k];
});

test("a template with no charts saves cleanly (IMAGE_TOKEN is exported)", () => {
  // The regression: core.IMAGE_TOKEN was undefined inside email-store.js, and
  // matchAll(undefined) does NOT throw -- it matches an empty regex at every
  // position, so m[1] came back undefined and every save was rejected for
  // referencing an image called "undefined".
  assert.ok(core.IMAGE_TOKEN instanceof RegExp, "IMAGE_TOKEN must be exported");
  assert.ok(core.IMAGE_TOKEN.global, "must be global or matchAll throws");

  const scan = (text, known) => [...new Set([...String(text).matchAll(core.IMAGE_TOKEN)]
    .map((m) => String(m[1]).toLowerCase()).filter((x) => !known.has(x)))];

  assert.deepEqual(scan("Plain body, no charts at all.", new Set()), []);
  assert.deepEqual(scan("See {{image:chart-a}}.", new Set(["chart-a"])), []);
  assert.deepEqual(scan("See {{image:chart-zz}}.", new Set(["chart-a"])), ["chart-zz"]);
});
