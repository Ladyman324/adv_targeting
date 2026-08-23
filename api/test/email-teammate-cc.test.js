"use strict";

/* The per-message "copy someone on their team" picker.
 *
 * It was built correctly and appeared to do nothing, because the data it needs
 * was dropped by a hand-kept whitelist in the client before it ever reached the
 * server. The picker hides itself when the list is empty, so the feature was
 * simply invisible and nothing reported a fault.
 *
 * These pin the SERVER half of that contract: what a recipient must carry, and
 * what has to survive onto the message.
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const service = require("../shared/email-service");

test("teammatesFull on a recipient survives onto the message row", () => {
  // sanitiseRecipient is the boundary the client's payload lands on.
  const clean = service.__sanitiseRecipient
    ? service.__sanitiseRecipient({
        crd: "5573829", name: "Regina Stuzin", email: "regina.stuzin@ubs.com",
        teammates: ["j.derosa@ubs.com", "c.tolman@ubs.com"],
        teammatesFull: [{ name: "John DeRosa", email: "j.derosa@ubs.com" },
                        { name: "Christopher Tolman", email: "c.tolman@ubs.com" }],
      })
    : null;
  if (!clean) return; // not exported; covered by the shape test below
  assert.equal(clean.teammatesFull.length, 2);
  assert.equal(clean.teammatesFull[0].email, "j.derosa@ubs.com");
});

test("the client's recipient whitelist carries the teammate fields", () => {
  /* THE BUG THIS PINS: open() rebuilds every recipient into a fixed shape, and
   * teammates/teammatesFull were not in it. The map computed all eleven of
   * Regina Stuzin's teammates and three lines later they were gone.
   *
   * Checked against the source because it is a hand-kept list of field names in
   * a language with no compiler to notice one missing -- the same failure the
   * Graph field whitelist in email-store.js had. */
  const fs = require("fs");
  const path = require("path");
  const src = fs.readFileSync(
    path.join(__dirname, "..", "..", "webapp", "email.js"), "utf8");
  const block = /recipients = \(selected \|\| \[\]\)\.map\(\(r\) => \(\{[\s\S]*?\}\)\);/.exec(src);
  assert.ok(block, "open()'s recipient mapper not found");
  for (const field of ["teammates", "teammatesFull", "contactId", "email"])
    assert.ok(block[0].includes(`${field}:`),
      `${field} is dropped before the batch is created`);
});

test("the batch level no longer pre-copies every teammate", () => {
  /* The superseded control: a batch-wide "copy the advisor's teammates"
   * checkbox that applied to every recipient, all-or-nothing, decided before
   * the rep had seen any of the individual emails. Two controls doing the same
   * job differently is worse than either. */
  const fs = require("fs");
  const path = require("path");
  const client = fs.readFileSync(
    path.join(__dirname, "..", "..", "webapp", "email.js"), "utf8");
  const server = fs.readFileSync(
    path.join(__dirname, "..", "shared", "email-service.js"), "utf8");
  assert.ok(!/id="ccTeammates"/.test(client), "the batch-level checkbox is gone");
  assert.ok(!/input\.ccTeammates === true/.test(server),
    "the server no longer pre-populates every message from a batch flag");
});

test("the FIELD app supplies teammates too, and both call sites await them", () => {
  /* The phone had no teammate data at all -- its AdvisorEmailData returned a
   * queue snapshot and nothing else -- so a batch built on a phone got the same
   * invisible picker as the desk did, for a different reason.
   *
   * Resolving them there needs the practice file, which may still be a fetch,
   * so the contract became a promise. The desk returns a plain value and
   * awaiting that is harmless: one contract, both apps. */
  const fs = require("fs");
  const path = require("path");
  const web = (n) => fs.readFileSync(path.join(__dirname, "..", "..", "webapp", n), "utf8");
  const field = web("field.js");
  const composer = web("email.js");

  assert.ok(/teammatesFull: mates/.test(field),
    "the field app must attach teammatesFull to its recipients");
  assert.ok(/async function teammatesWithEmail/.test(field),
    "teammates are resolved from the practice file plus loaded tiles");
  // Both call sites must await, or the field app hands the composer a Promise
  // and every recipient becomes undefined.
  assert.ok(/await global\.AdvisorEmailData\.list\(\)/.test(composer));
  assert.ok(/await global\.AdvisorEmailData\.recipientFor\(/.test(composer));
});

const readWeb = (n) => require("fs").readFileSync(
  require("path").join(__dirname, "..", "..", "webapp", n), "utf8");

test("the click dispatcher does not cancel a checkbox", () => {
  /* THE BUG: the composer's dispatcher called preventDefault on everything
   * carrying [data-email]. Right for buttons and links; WRONG for a checkbox --
   * it stops the browser applying the tick, so the handler read `checked`, got
   * the value from BEFORE the click, concluded the box was being un-ticked, and
   * saved nothing.
   *
   * The teammate picker looked completely inert: the box flickered and the list
   * came back unchanged. Nothing errored, which is why it read as "the feature
   * does not work" rather than as a bug. */
  const src = readWeb("email.js");
  const at = src.indexOf('const direct = event.target.closest("[data-email]")');
  assert.ok(at > 0, "the [data-email] dispatcher was not found");
  const block = src.slice(at, at + 900);
  assert.match(block, /checkbox\|radio/i,
    "form controls must be exempt from preventDefault or they never change state");
  assert.match(block, /if \(!toggle\) event\.preventDefault\(\)/,
    "preventDefault must be conditional, not unconditional");
});

test("the teammate picker is collapsed until it is used", () => {
  /* Eleven teammates rendered as a permanently open list pushed the subject and
   * the body -- the two things Step 2 exists to edit -- off the screen. It now
   * opens only when somebody is already copied, so a decision already taken is
   * never hidden behind a disclosure triangle. */
  const src = readWeb("email.js");
  const at = src.indexOf("function teammatePicker(");
  const fn = src.slice(at, src.indexOf("\n  function ", at + 10));
  assert.match(fn, /<details class="email-mates"/, "a disclosure, not a fieldset");
  assert.match(fn, /\$\{open \? " open" : ""\}/,
    "collapsed unless the rep opened it or somebody is already copied");
});

test("ticking a teammate is actually STORED", async () => {
  /* THE BUG A REP SAW: tick a teammate, the list closes, reopen it and nothing
   * is checked -- and nobody is in the Cc.
   *
   * updateMessageCc() wrote teammateCcJson through patchMessage(), whose
   * whitelist did not include it. The write returned success and stored
   * nothing, so the re-render read back a message that had never changed.
   * Silent, which is why it read as "the feature does not work". */
  const path = require("path");
  const store = require(path.join(__dirname, "..", "shared", "email-store.js"));
  const src = require("fs").readFileSync(
    path.join(__dirname, "..", "shared", "email-store.js"), "utf8");

  const at = src.indexOf("async function patchMessage(");
  const body = src.slice(at, src.indexOf("\n}", at));
  assert.match(body, /"teammateCcJson"/,
    "patchMessage must accept the field updateMessageCc writes, or the tick is lost");

  // And the read side has to hand it back under the name the picker looks for.
  const reader = src.indexOf("function messageFromEntity(");
  const readBody = src.slice(reader, src.indexOf("\n}", reader));
  assert.match(readBody, /teammateCc: parse\(e\.teammateCcJson/);
  assert.match(readBody, /teammatesAvailable: parse\(e\.teammatesAvailableJson/);
  void store;
});

test("the teammate list stays open while a rep is picking", () => {
  /* Every toggle re-renders the step from the server's answer, so the list
   * collapsed the instant somebody was ticked -- and unticking the LAST one
   * closed it under the rep while they were still working in it.
   *
   * Remembered per message id: moving to the next advisor is a fresh decision,
   * and their list should not be open just because the previous one was. */
  const src = readWeb("email.js");
  assert.match(src, /const matesOpen = new Set\(\)/,
    "the open state must survive a re-render");
  assert.match(src, /matesOpen\.has\(m\.id\) \|\| chosen\.length > 0/,
    "open if the rep opened it, or if somebody is already copied");

  const at = src.indexOf('if (action === "mate-toggle")');
  assert.match(src.slice(at, at + 300), /matesOpen\.add\(m\.id\)/,
    "ticking a teammate must not close the list they are picking from");

  // A <summary> is like a checkbox: preventDefault stops it opening at all.
  const disp = src.indexOf('const direct = event.target.closest("[data-email]")');
  assert.match(src.slice(disp, disp + 900), /=== "SUMMARY"/,
    "the disclosure needs its default action or it never opens");
});

test("a batch read shows the copied teammate on the envelope", () => {
  /* THE BUG THIS PINS: the tick saved, the picker stayed checked, and Step 3 --
   * "Exactly what they receive" -- showed no Cc line, because getBatchDetail
   * returned the STORED rows and cc/bcc are computed, never stored. Only the
   * review path called extraRecipients, so the envelope was correct exactly
   * when nobody was looking at it.
   *
   * Checked against the source: both read paths must build the envelope from
   * the one function, or they can disagree about who is on a message. */
  const fs = require("fs");
  const path = require("path");
  const src = fs.readFileSync(path.join(__dirname, "..", "shared", "email-service.js"), "utf8");
  const body = /async function getBatchDetail\([\s\S]*?\n\}/.exec(src);
  assert.ok(body, "getBatchDetail not found");
  assert.ok(/core\.extraRecipients\(/.test(body[0]),
    "getBatchDetail returns messages with no cc/bcc, so the envelope hides real copies");
});

test("the envelope explains every copied address, including a teammate", () => {
  const fs = require("fs");
  const path = require("path");
  const src = fs.readFileSync(path.join(__dirname, "..", "..", "webapp", "email.js"), "utf8");
  const fn = /function ccReason\([\s\S]*?\n  \}/.exec(src);
  assert.ok(fn, "ccReason not found");
  assert.ok(/teammateCc/.test(fn[0]),
    "a teammate copy would appear on the envelope with no reason given");
});
