"use strict";

const crypto = require("crypto");
const sanitizeHtml = require("sanitize-html");

const ABSOLUTE_BATCH_STOP = 15000;
const DEFAULTS = {
  directBatchMax: 250,
  rollingExternalLimit: 5000,
  cancellationSeconds: 30,
  mailboxIntervalSeconds: 5,
  maxMessageBytes: 20 * 1024 * 1024,
  maxAttachmentBytes: 15 * 1024 * 1024,
  maxBodyChars: 30000,
  // The four review tiers. These were literals inside guardrail(), which meant
  // the one number a compliance officer is most likely to want to move was the
  // one number that needed a redeploy.
  reviewSummaryOver: 25,
  reviewLargeOver: 50,
  reviewElevatedOver: 100,
  draftsOnlyOver: 250,
  // Above this a batch cannot be approved without the shared passcode.
  passcodeOver: 10,
  // Campaign health. A batch that is bouncing this hard is working a bad list,
  // and every further send damages the firm's sending reputation.
  bouncePausePercent: 3,
  bounceMinSample: 25,
};

const BUILTIN_TEMPLATES = [
  {
    id: "meeting",
    name: "Meeting introduction",
    subject: "Time to meet — {{first_name}}",
    bodyText: "Hi {{first_name}},\n\nI cover your area and expect to be nearby shortly. Would you have 20 minutes for a short introduction to how we run our value strategies?\n\nHappy to work around your calendar.",
    defaultAttachmentIds: [],
  },
  {
    id: "materials",
    name: "Materials follow-up",
    subject: "The material I mentioned",
    bodyText: "Hi {{first_name}},\n\nFollowing up on our conversation — I’m sending the overview we discussed.\n\nLet me know if it would help to walk through it together.",
    defaultAttachmentIds: [],
  },
  {
    id: "check-in",
    name: "Check-in",
    subject: "Checking in",
    bodyText: "Hi {{first_name}},\n\nIt has been a while since we last spoke. Nothing urgent — I wanted to see how things are going at {{company_name}} and whether anything has changed on the manager side.",
    defaultAttachmentIds: [],
  },
  {
    id: "cold",
    name: "Value equity introduction",
    subject: "Value equity SMAs — a short introduction",
    bodyText: "Hi {{first_name}},\n\nWe manage All-Cap and Large-Cap Value separately managed accounts and work with advisors who select outside managers for their clients.\n\nIf that is something {{company_name}} looks at, I would welcome a short conversation.",
    defaultAttachmentIds: [],
  },
];

const ALLOWED_FIELDS = new Set(["first_name", "last_name", "company_name",
  "sender_name", "sender_title"]);
const TOKEN = /\{\{\s*([a-zA-Z0-9_]+)\s*\}\}/g;
// Charts belong in the body, where the Word templates put them. The syntax
// deliberately matches the merge fields: an administrator who can write
// {{first_name}} should not have to learn a second notation for a chart.
const IMAGE_TOKEN = /\{\{\s*image:([a-z0-9][a-z0-9-]{0,60})\s*\}\}/gi;

function numberSetting(name, fallback) {
  const n = Number(process.env[name]);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

/* May this internal address be emailed?
 *
 * Matches a full address or the domain it belongs to, so a firm can admit its
 * whole staff without listing them -- and so a new colleague does not have to
 * be added to a setting before a rehearsal batch will reach them.
 */
function internalRecipientAllowed(email, cfg) {
  const address = String(email || "").trim().toLowerCase();
  if (!address) return false;
  const list = (cfg || config()).internalRecipientAllowlist;
  if (!list || !list.size) return false;
  if (list.has(address)) return true;
  const domain = address.split("@")[1] || "";
  return !!domain && list.has(domain);
}

function config() {
  return {
    directBatchMax: numberSetting("EMAIL_DIRECT_BATCH_MAX", DEFAULTS.directBatchMax),
    rollingExternalLimit: numberSetting("EMAIL_EXTERNAL_24H_LIMIT", DEFAULTS.rollingExternalLimit),
    cancellationSeconds: numberSetting("EMAIL_CANCELLATION_SECONDS", DEFAULTS.cancellationSeconds),
    mailboxIntervalSeconds: numberSetting("EMAIL_MAILBOX_INTERVAL_SECONDS", DEFAULTS.mailboxIntervalSeconds),
    maxMessageBytes: numberSetting("EMAIL_MAX_MESSAGE_BYTES", DEFAULTS.maxMessageBytes),
    maxAttachmentBytes: numberSetting("EMAIL_MAX_ATTACHMENT_BYTES", DEFAULTS.maxAttachmentBytes),
    // Azure Table string properties are capped at 64 KiB; keep both the text
    // and rendered HTML snapshots safely beneath that physical boundary.
    maxBodyChars: Math.min(numberSetting("EMAIL_MAX_BODY_CHARS", DEFAULTS.maxBodyChars), 30000),
    internalDomains: new Set(String(process.env.EMAIL_INTERNAL_DOMAINS || "eicatlanta.com")
      .split(",").map((x) => x.trim().toLowerCase()).filter(Boolean)),
    directSendEnvironmentEnabled: process.env.NODE_ENV === "production"
      && process.env.EMAIL_DIRECT_SEND_ENABLED === "1"
      && process.env.EMAIL_DIRECT_SEND_KILL_SWITCH !== "1",
    // Marketing material sent outside the firm has to be retained by whoever is
    // responsible for it. Set to an empty string to switch the blind copy off.
    materialBcc: (process.env.EMAIL_MATERIAL_BCC === undefined
      ? "mktgmaterial@eicatlanta.com" : String(process.env.EMAIL_MATERIAL_BCC)).trim().toLowerCase(),
    /* Who a rep may copy on a message, as an ALLOWLIST.
     *
     * Set in the Function App's Application settings, semicolon separated.
     * Either form works:
     *
     *   kate@eicatlanta.com; will@eicatlanta.com
     *   Kate Renta <kate@eicatlanta.com>; Will Smith <will@eicatlanta.com>
     *
     * The name is only a label for the picker in Settings -- Exchange resolves
     * internal addresses to display names itself when the draft is created, so
     * bare addresses send exactly the same message. It is an allowlist and not
     * just a menu because the server checks against it: a tampered request
     * cannot turn this app into a way to copy arbitrary addresses on client
     * correspondence.
     */
    internalRecipients: String(process.env.EMAIL_INTERNAL_RECIPIENTS || "")
      .split(";").map((entry) => entry.trim()).filter(Boolean)
      .map((entry) => {
        const m = /^(.*?)\s*<([^>]+)>$/.exec(entry);
        const address = (m ? m[2] : entry).trim().toLowerCase();
        const name = (m ? m[1] : "").trim();
        return { address, name: name || address };
      })
      .filter((r) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(r.address)),
    testAllowlist: new Set(String(process.env.EMAIL_TEST_ADDRESS_ALLOWLIST || "")
      .split(",").map((x) => x.trim().toLowerCase()).filter(Boolean)),
    /* Who may be addressed despite being one of our own.
     *
     * DELIBERATELY NOT testAllowlist, which looks like the obvious home and is
     * a trap: that list is a production send gate, and the moment it is
     * non-empty EVERY direct send must have all its recipients inside it. Using
     * it to admit five colleagues would have silently blocked every advisor
     * campaign -- a far larger failure than the one being fixed.
     *
     * Entries are addresses or bare domains: "eicatlanta.com" (or
     * "@eicatlanta.com") admits everyone there, a full address admits one
     * person. Empty means no internal recipient is addressable.
     */
    internalRecipientAllowlist: new Set(
      String(process.env.EMAIL_INTERNAL_RECIPIENT_ALLOWLIST || "")
        .split(",").map((x) => x.trim().toLowerCase().replace(/^@/, "")).filter(Boolean)),
    reviewSummaryOver: numberSetting("EMAIL_REVIEW_SUMMARY_OVER", DEFAULTS.reviewSummaryOver),
    reviewLargeOver: numberSetting("EMAIL_REVIEW_LARGE_OVER", DEFAULTS.reviewLargeOver),
    reviewElevatedOver: numberSetting("EMAIL_REVIEW_ELEVATED_OVER", DEFAULTS.reviewElevatedOver),
    draftsOnlyOver: numberSetting("EMAIL_DRAFTS_ONLY_OVER", DEFAULTS.draftsOnlyOver),
    // 0 disables the passcode entirely; anything above it is the recipient count
    // past which approval must be confirmed with the shared code.
    passcodeOver: Number.isFinite(Number(process.env.EMAIL_PASSCODE_OVER))
      && Number(process.env.EMAIL_PASSCODE_OVER) >= 0
      ? Number(process.env.EMAIL_PASSCODE_OVER) : DEFAULTS.passcodeOver,
    passcode: String(process.env.EMAIL_APPROVAL_PASSCODE || "").trim(),
    bouncePausePercent: numberSetting("EMAIL_BOUNCE_PAUSE_PERCENT", DEFAULTS.bouncePausePercent),
    // Below this many delivered messages a percentage means nothing -- one
    // bounce out of three is 33% and tells you only that one address was bad.
    bounceMinSample: numberSetting("EMAIL_BOUNCE_MIN_SAMPLE", DEFAULTS.bounceMinSample),
    // Reps who are Foreside registered representatives, by their mailbox
    // address. Only these get the Foreside paragraph -- see foresideDisclosure().
    foresideReps: new Set(String(process.env.EMAIL_FORESIDE_REPS || "")
      .split(",").map((x) => x.trim().toLowerCase()).filter(Boolean)),
  };
}

function splitName(value) {
  const name = String(value || "").trim();
  if (!name) return { first_name: "", last_name: "" };
  if (name.includes(",")) {
    const [last, rest] = name.split(",", 2);
    return { first_name: (rest || "").trim().split(/\s+/)[0] || "", last_name: last.trim() };
  }
  const words = name.split(/\s+/);
  return { first_name: words[0] || "", last_name: words.length > 1 ? words[words.length - 1] : "" };
}

/* `sender` is the Microsoft 365 profile the signature is built from -- the same
 * object corporateSignature() reads, so {{sender_name}} in the body and the
 * name in the signature block can never disagree.
 *
 * jobTitle is the field to watch. A profile captured before PROFILE_VERSION 2
 * has no jobTitle at all, so {{sender_title}} resolves empty for anyone whose
 * mailbox was connected back then. That is reported as a missing merge value
 * like any other, which is the right outcome: the send is held rather than
 * going out with a bare "{{sender_title}}" in the text.
 */
function mergeValues(recipient, sender) {
  const names = splitName(recipient && recipient.name);
  const s = sender || {};
  return {
    first_name: String((recipient && recipient.firstName) || names.first_name || ""),
    last_name: String((recipient && recipient.lastName) || names.last_name || ""),
    company_name: String((recipient && (recipient.companyName || recipient.firm)) || ""),
    sender_name: String(s.displayName || ""),
    sender_title: String(s.jobTitle || ""),
  };
}

// ---------------------------------------------------------------------------
// Template linting, run while an administrator types and again before publish.
//
// The failure this exists to prevent is not a crash: renderTemplate() leaves an
// unrecognised token exactly where it found it, so a bad field does not error,
// it SENDS. "Hi {first_name}," reaching two hundred advisors is the outcome
// being designed against, and the single-brace form is the one that reads
// correctly to a human writing it.
// ---------------------------------------------------------------------------
const WORD_ARTIFACTS = [
  [/�/g, "replacement characters (�) -- the text lost its encoding somewhere"],
  [/ /g, "non-breaking spaces pasted from Word"],
  // Bullet characters followed by tabs are NOT listed here any more: the
  // renderer turns them into real list items, so warning about them would be
  // complaining about something that now works. A warning nobody needs to act
  // on is how a linter trains people to ignore it.
];

function lintTemplate({ subject = "", bodyText = "", maxBodyChars = 30000 } = {}) {
  const errors = [], warnings = [];
  const both = `${subject}\n${bodyText}`;

  if (!String(subject).trim()) errors.push({ code: "missing_subject", message: "Subject is required." });
  if (!String(bodyText).trim()) errors.push({ code: "missing_body", message: "Body is required." });

  // Well-formed tokens naming a field we cannot fill.
  const unknown = new Set();
  for (const m of both.matchAll(TOKEN)) {
    const key = String(m[1]).toLowerCase();
    if (!ALLOWED_FIELDS.has(key)) unknown.add(m[1]);
  }
  if (unknown.size) errors.push({ code: "unknown_field",
    message: `Not a field we can fill: ${[...unknown].map((f) => `{{${f}}}`).join(", ")}. `
      + `Available: ${[...ALLOWED_FIELDS].map((f) => `{{${f}}}`).join(", ")}.` });

  // Single braces. These never match TOKEN, so they are sent verbatim.
  const single = new Set();
  for (const m of both.matchAll(/(^|[^{])\{\s*([a-zA-Z0-9_]+)\s*\}([^}]|$)/g)) single.add(m[2]);
  if (single.size) errors.push({ code: "single_brace",
    message: `Use double braces: ${[...single].map((f) => `{${f}}`).join(", ")} `
      + `will not be filled in. Write {{${[...single][0]}}} instead.` });

  // Opened and never closed, or closed and never opened.
  const opens = (both.match(/\{\{/g) || []).length, closes = (both.match(/\}\}/g) || []).length;
  if (opens !== closes) errors.push({ code: "unbalanced_braces",
    message: `Unbalanced braces: ${opens} "{{" and ${closes} "}}". A field left half-written is sent as written.` });

  if (String(bodyText).length > maxBodyChars) errors.push({ code: "body_too_long",
    message: `Body is ${String(bodyText).length} characters; the limit is ${maxBodyChars}.` });

  const used = new Set([...both.matchAll(TOKEN)].map((m) => String(m[1]).toLowerCase())
    .filter((k) => ALLOWED_FIELDS.has(k)));
  if (!used.size) warnings.push({ code: "no_personalisation",
    message: "No merge fields are used, so every advisor receives identical text." });
  if ([...subject.matchAll(TOKEN)].length) warnings.push({ code: "field_in_subject",
    message: "The subject uses a merge field. A recipient with that value missing gets an odd subject line -- check the second preview." });

  for (const [pattern, description] of WORD_ARTIFACTS) {
    if (pattern.test(both)) warnings.push({ code: "word_artifact", message: `Found ${description}.` });
    pattern.lastIndex = 0;
  }

  return { ok: !errors.length, errors, warnings, fieldsUsed: [...used] };
}

function renderTemplate(text, recipient, sender) {
  const values = mergeValues(recipient, sender);
  const missing = [];
  const unknown = [];
  const rendered = String(text || "").replace(TOKEN, (whole, raw) => {
    const key = String(raw).toLowerCase();
    if (!ALLOWED_FIELDS.has(key)) {
      unknown.push(key);
      return whole;
    }
    if (!values[key]) {
      missing.push(key);
      return whole;
    }
    return values[key];
  });
  const unresolved = [...rendered.matchAll(TOKEN)].map((m) => m[1]);
  return { rendered, missing: [...new Set(missing)], unknown: [...new Set(unknown)], unresolved };
}

/* Bullet and numbered lines, recognised in the shapes people actually paste.
 *
 * Word emits "•\t" for a first-level bullet and "o\t" for a second, and
 * hand-typed lists arrive as "- " or "* ". None of those survived: the renderer
 * produced paragraphs and <br> only, so a bullet reached the advisor as a
 * literal character followed by a tab -- and HTML collapses tabs, so the
 * hanging indent the author saw in Word was simply gone.
 *
 * Matched AFTER escaping, which is safe because none of these markers is
 * affected by escaping, and which keeps the rule that markup in the body can
 * never reach the output.
 */
const BULLET = /^[\s]*(?:[•·◦*-]|o)[\s\t]+(.*)$/;
const NUMBERED = /^[\s]*\d{1,2}[.)][\s\t]+(.*)$/;

function blockToHtml(lines) {
  const out = [];
  let list = null;                       // { tag, items }
  const flush = () => {
    if (!list) return;
    const style = list.tag === "ul"
      ? "margin:0 0 12px;padding-left:22px"
      : "margin:0 0 12px;padding-left:24px";
    out.push(`<${list.tag} style="${style}">`
      + list.items.map((t) => `<li style="margin:0 0 4px">${t}</li>`).join("")
      + `</${list.tag}>`);
    list = null;
  };
  let para = [];
  const flushPara = () => {
    if (!para.length) return;
    out.push(`<p style="margin:0 0 12px">${para.join("<br>")}</p>`);
    para = [];
  };

  for (const line of lines) {
    const bullet = line.match(BULLET);
    const numbered = bullet ? null : line.match(NUMBERED);
    if (bullet || numbered) {
      flushPara();
      const tag = bullet ? "ul" : "ol";
      // A change of list type starts a new list rather than mixing markers.
      if (list && list.tag !== tag) flush();
      if (!list) list = { tag, items: [] };
      list.items.push((bullet ? bullet[1] : numbered[1]).trim());
      continue;
    }
    flush();
    if (line.trim()) para.push(line);
  }
  flush();
  flushPara();
  return out.join("");
}

function plainTextToSafeHtml(text, images = []) {
  const byId = new Map((images || []).map((i) => [String(i.id).toLowerCase(), i]));
  const escaped = String(text || "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  const html = escaped.split(/\n{2,}/)
    .map((block) => blockToHtml(block.split(/\n/))).join("");
  // Escaping happens FIRST and the token is matched afterwards, so the only
  // route to an <img> tag is a token naming an image already approved onto this
  // template. Markup pasted into the body cannot reach the output.
  return html.replace(IMAGE_TOKEN, (whole, raw) => {
    const image = byId.get(String(raw).toLowerCase());
    if (!image) return whole;
    const alt = String(image.name || raw).replace(/[<>"']/g, "");
    return `<img src="cid:${image.cid}" alt="${alt}" `
      + `style="max-width:100%;height:auto;display:block;margin:12px 0">`;
  });
}

function sanitizeEmailHtml(html) {
  return sanitizeHtml(String(html || ""), {
    // ul/ol/li are here so a template can carry real bullets. Without them a
    // list pasted from Word survived only as literal bullet CHARACTERS followed
    // by tabs, and HTML collapses tabs -- so the hanging indent the author saw
    // in Word was gone by the time the advisor read it.
    allowedTags: ["p", "br", "b", "strong", "i", "em", "u", "a", "table", "tbody", "tr", "td",
                  "span", "div", "img", "ul", "ol", "li"],
    allowedAttributes: { a: ["href"], img: ["src", "alt", "style"], "*": ["style"] },
    allowedSchemes: ["http", "https", "mailto", "tel"],
    // cid: only, and only on images -- a part already attached to this message.
    // http(s) image sources are deliberately absent: Outlook blocks remote
    // images by default, so a linked chart would simply be missing.
    allowedSchemesByTag: { img: ["cid"] },
    allowedStyles: {
      "*": {
        color: [/^#[0-9a-f]{3,8}$/i, /^rgb\(/i],
        "font-family": [/^[a-z0-9 ,.'"-]+$/i],
        "font-size": [/^\d+(\.\d+)?(px|pt|em|rem|%)$/],
        "font-weight": [/^(normal|bold|[1-9]00)$/],
        "line-height": [/^[0-9.]+(px|em|%)?$/],
        margin: [/^(-?\d+(\.\d+)?(px|pt|em|rem|%)?\s*){1,4}$/], "margin-top": [/^(-?\d+(\.\d+)?(px|pt|em|rem|%)?\s*){1,4}$/],
        "margin-bottom": [/^(-?\d+(\.\d+)?(px|pt|em|rem|%)?\s*){1,4}$/], padding: [/^(-?\d+(\.\d+)?(px|pt|em|rem|%)?\s*){1,4}$/],
        // padding-left carries the list indent. Without it the bullets render
        // flush against the text, which is the thing being fixed.
        "padding-left": [/^\d+(\.\d+)?(px|pt|em|rem|%)$/],
        // Without these an inline chart renders at its natural pixel width and
        // runs off the side of a phone. They were being stripped silently.
        "max-width": [/^\d+(px|%)$/], height: [/^(auto|\d+(px|%))$/],
        display: [/^(block|inline|inline-block)$/],
      },
    },
    disallowedTagsMode: "discard",
  });
}


// Foreside is a per-person registration statement, so it ships as a default an
// admin can override rather than something every rep inherits.
const FORESIDE_DEFAULT =
  "Please Note: Registered Representative of Foreside Funds Distributors LLC, the "
  + "distributor for EIC Value Fund. Foreside Funds Distributors LLC is not affiliated "
  + "with Equity Investment Corporation, the Fund's investment advisor. Managed account "
  + "and other advisory services are offered through EIC.\n\n"
  + "The investment objectives, risks, charges and expenses of EIC Value Fund should be "
  + "considered carefully before investing. A prospectus with this and other information "
  + "about the Fund is available by visiting www.eicvalue.com or by calling 1-855-430-6487. "
  + "The prospectus should be read carefully before investing.";

// Goes on every email from every user, below everything else. The preference
// link is the unsubscribe path and it is PER-RECIPIENT: the URL carries a signed
// token naming one address, so the page it opens can only ever suppress that
// address. With no URL the sentence still reads correctly -- the words are
// compliance text in their own right -- it simply is not a link, which is the
// right way for a misconfigured environment to fail.
function archiveFooter(manageUrl) {
  const esc = (v) => String(v || "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const text = "EIC e-mails are archived for SEC review purposes, and may contain "
    + "confidential and privileged information. If you receive this in error, please "
    + "reply to inform sender of the message's misdirection, and delete it and any "
    + "attachments from your computer. You are not authorized to read, print, retain, "
    + "copy or disseminate it without our consent, and doing so may be unlawful. "
    + "To manage your email preferences, ";
  const link = manageUrl ? `<a href="${esc(manageUrl)}">click here</a>` : "click here";
  return `<div style="font-size:7.5pt;color:#888;margin-top:12px">`
    + `<p style="margin:0">${esc(text)}${link}. Thank you.</p></div>`;
}

function corporateSignature(profile, manageUrl = "", cfg = config()) {
  const p = profile || {};
  const inline = (value) => String(value || "").replace(/&/g, "&amp;")
    .replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  const company = process.env.EMAIL_SIGNATURE_COMPANY_NAME || "Equity Investment Corporation";
  const address = process.env.EMAIL_SIGNATURE_ADDRESS || "";
  const website = process.env.EMAIL_SIGNATURE_WEBSITE || "https://www.eicatlanta.com";
  const disclosure = process.env.EMAIL_SIGNATURE_DISCLOSURE || "";
  /* ONE number, not two. Business first; the mobile is the fallback for anyone
   * whose Microsoft 365 record carries no desk line. Labelled for which it is,
   * because "T" against a mobile number is simply wrong. */
  const businessPhone = String((p.businessPhones && p.businessPhones[0]) || "").trim();
  const mobilePhone = String(p.mobilePhone || "").trim();
  const phone = businessPhone || mobilePhone;
  const phoneLabel = businessPhone ? "T" : "M";
  /* The address may run to more than one line.
   *
   * Azure's App Settings value box is single-line, so a real newline cannot be
   * typed there -- the two-character sequence 
 is accepted instead, and an
   * actual newline is honoured too for anyone setting this from the CLI or an
   * ARM template.
   *
   * Escaped FIRST, then the markers are turned into <br>. The other order would
   * let an address field inject markup, which is a silly way to lose control of
   * every outgoing signature.
   */
  const addressHtml = inline(address)
    .replace(new RegExp("\\\\n|\\r?\\n", "g"), "<br>");

  const rows = [
    `<strong>${inline(p.displayName)}</strong>`,
    p.jobTitle ? inline(p.jobTitle) : "",
    inline(company),
    phone ? `${phoneLabel} ${inline(phone)}` : "",
    p.mail ? `<a href="mailto:${String(p.mail).replace(/["<>]/g, "")}">${inline(p.mail)}</a>` : "",
    addressHtml,
    website ? `<a href="${inline(website)}">${inline(website.replace(/^https?:\/\//, ""))}</a>` : "",
  ].filter(Boolean).join("<br>");
  // Blank lines separate paragraphs; without this the required notices run
  // together into a single unreadable block.
  const paras = (text) => String(text || "").split(/\n\s*\n/)
    .map((para) => para.trim()).filter(Boolean)
    .map((para) => `<p style="margin:0 0 6pt">${inline(para).replace(/\n/g, "<br>")}</p>`)
    .join("");

  // Three tiers, and they are NOT interchangeable:
  //   1. EMAIL_SIGNATURE_DISCLOSURE -- firm-wide, everyone.
  //   2. Foreside -- only reps named in EMAIL_FORESIDE_REPS. This is a statement
  //      about one person's own registration; applying it firm-wide asserts a
  //      registration that most users of this app do not hold.
  //   3. The archive/confidentiality footer -- everyone, always last, and the
  //      only part carrying the preference link.
  const foreside = cfg.foresideReps.has(String(p.mail || "").trim().toLowerCase())
    ? paras(process.env.EMAIL_FORESIDE_DISCLOSURE || FORESIDE_DEFAULT) : "";
  const compliance = paras(disclosure) + foreside;

  return sanitizeEmailHtml(`<div style="font-family:Arial,sans-serif;font-size:10pt;color:#333;margin-top:18px">${rows}`
    // Matched to archiveFooter() below -- same size and colour. These are all
    // small print doing the same job, and two nearly-but-not-quite identical
    // sizes stacked on top of each other reads as a formatting accident rather
    // than as a distinction anyone intended.
    + `${compliance ? `<div style="font-size:7.5pt;color:#888;margin-top:10px">${compliance}</div>` : ""}`
    + `${archiveFooter(manageUrl)}</div>`).slice(0, cfg.maxBodyChars);
}

/* The compliance blind copy on anything carrying an attachment.
 *
 * Not a default the rep can turn off, and not a checkbox: the requirement is
 * that the material desk holds a copy of every piece of literature that leaves
 * the firm, which a per-message opt-out defeats on exactly the sends where it
 * matters. It IS shown in the preview -- a hidden recipient a sender does not
 * know about is a different and worse problem.
 *
 * Two exclusions, both for the same reason -- nothing left the firm:
 *   - no attachment, so there is no material to retain;
 *   - an internal recipient, which is what sending yourself a test looks like.
 *     A rep checking their own formatting should not be filing literature.
 */
function complianceBcc(message, cfg = config()) {
  const address = cfg.materialBcc;
  if (!address || !validEmail(address)) return [];
  if (!(message && Array.isArray(message.attachments) && message.attachments.length)) return [];
  if (!isExternal(message.recipientEmail, cfg)) return [];
  // Never blind-copy the desk on a message addressed to the desk.
  if (String(message.recipientEmail || "").trim().toLowerCase() === address) return [];
  return [address];
}

/* Everyone to copy on one message: compliance, the rep themselves, and an
 * internal colleague -- resolved in one place so the draft, the preview and the
 * worker cannot disagree about who is on it.
 *
 * `prefs` are the rep's own account settings:
 *   copySelf     "" | "cc" | "bcc"     standing, from Settings
 *   copyInternal "" | "cc" | "bcc"     standing, from Settings
 *   copyInternalTo   an address, which MUST be on the allowlist
 *   ccColleague      one address for THIS batch, allowlisted when the batch was
 *                    created and not re-derivable from a rep's saved settings
 *
 * message.teammateCc is the advisor's own practice, resolved per message at
 * batch creation and already filtered for suppression there.
 *
 * The compliance blind copy is unchanged and unconditional -- it is not a
 * preference and a rep cannot switch it off.
 */
function extraRecipients(message, prefs = {}, sender = {}, cfg = config()) {
  const cc = [], bcc = [];
  const seen = new Set([String(message && message.recipientEmail || "").trim().toLowerCase()]);
  const add = (address, where) => {
    const clean = String(address || "").trim().toLowerCase();
    // Never copy somebody who is already on the message, in any capacity.
    if (!clean || !validEmail(clean) || seen.has(clean)) return;
    seen.add(clean);
    (where === "cc" ? cc : bcc).push(clean);
  };

  for (const address of complianceBcc(message, cfg)) add(address, "bcc");

  const self = String(prefs.copySelf || "").toLowerCase();
  if (self === "cc" || self === "bcc") add(sender.mail || sender.userPrincipalName, self);

  /* Per-batch copies. Both are CC rather than BCC on purpose: the advisor
   * should be able to see that a colleague is on the message, because they may
   * reply to all and because a hidden copy of a client-facing email is a
   * different thing entirely. */
  for (const address of (message && message.teammateCc) || []) add(address, "cc");
  add(prefs.ccColleague, "cc");

  const internal = String(prefs.copyInternal || "").toLowerCase();
  if (internal === "cc" || internal === "bcc") {
    const wanted = String(prefs.copyInternalTo || "").trim().toLowerCase();
    // Allowlisted or nothing. An address that has since been removed from the
    // App Setting stops being copied on the next send rather than lingering in
    // whatever a rep saved months ago.
    if (cfg.internalRecipients.some((r) => r.address === wanted)) add(wanted, internal);
  }
  return { cc, bcc };
}

function validEmail(value) {
  const s = String(value || "").trim();
  return s.length <= 254 && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(s);
}

function isExternal(email, cfg = config()) {
  const domain = String(email || "").toLowerCase().split("@").pop();
  return !cfg.internalDomains.has(domain);
}


/* The order a batch is actually sent in.
 *
 * Messages were paced by their ORDINAL, which is list order, and lists come out
 * of the map grouped by firm. So a 400-person batch sent 130 consecutive
 * messages to morganstanley.com, one every few seconds, then 100 consecutive to
 * ml.com. That is the exact shape a receiving gateway throttles or tarpits, and
 * the cost lands on eicatlanta.com's reputation for ALL mail, not just this
 * batch.
 *
 * Round-robins across recipient domains instead, so consecutive sends go to
 * different organisations wherever the batch allows it. The same 400 messages
 * take the same total time; they simply arrive spread across firms rather than
 * in blocks.
 *
 * Deterministic: same input, same order, every time. That matters because the
 * position is stored and a retry must not reshuffle the queue.
 */
function interleaveByDomain(messages) {
  const groups = new Map();
  for (const m of messages || []) {
    const domain = String(m.recipientEmail || "").toLowerCase().split("@")[1] || "";
    if (!groups.has(domain)) groups.set(domain, []);
    groups.get(domain).push(m);
  }
  // Largest group first, ties broken by name, so the order never depends on
  // Map insertion or on how the rep happened to build the list.
  const queues = [...groups.entries()]
    .sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]))
    .map(([, list]) => list.slice().sort((a, b) => (a.ordinal || 0) - (b.ordinal || 0)));

  const out = [];
  while (out.length < (messages || []).length) {
    let moved = false;
    for (const queue of queues) {
      const next = queue.shift();
      if (!next) continue;
      out.push(next);
      moved = true;
    }
    if (!moved) break;                       // defensive; cannot happen
  }
  return out;
}


/* Should this batch stop sending?
 *
 * A rep working a stale list can burn the firm's sending reputation long before
 * anyone reads a report, and the damage is not confined to this application --
 * it lands on eicatlanta.com for all mail. This is the automatic brake.
 *
 * Pause, never cancel. Pausing is reversible by a human who can look at the
 * bounces and decide; cancelling would destroy a half-sent campaign on the
 * strength of an automated percentage. The whole point is to stop and ask.
 */
function campaignHealth(sentCount, hardBounces, cfg = config()) {
  const sent = Number(sentCount) || 0;
  const bounced = Number(hardBounces) || 0;
  const rate = sent ? (bounced / sent) * 100 : 0;
  if (sent < cfg.bounceMinSample) {
    return { pause: false, rate, sent, bounced, reason: "too few sent to judge" };
  }
  if (rate > cfg.bouncePausePercent) {
    return { pause: true, rate, sent, bounced,
      reason: `${bounced} of the first ${sent} messages hard bounced (${rate.toFixed(1)}%), `
        + `above the ${cfg.bouncePausePercent}% limit. Sending is paused so the list can be checked.` };
  }
  return { pause: false, rate, sent, bounced, reason: "" };
}


/* WHY direct sending is unavailable, in words.
 *
 * Four separate conditions gate it, and the screen said only "Direct send off"
 * -- which is true, useless, and indistinguishable from a bug. Someone who has
 * just set EMAIL_DIRECT_SEND_ENABLED=1 and still sees no button has no way to
 * learn that a kill switch left on from last week is the reason.
 *
 * Naming the setting is deliberate. This is an internal tool used by a handful
 * of people, the values are configuration rather than secrets, and the
 * alternative is a support conversation for something answerable on screen.
 */
function directSendBlockedBy(cfg = config(), killed = false, killReason = "") {
  if (process.env.NODE_ENV !== "production")
    return "This environment is not marked production, so direct sending is off "
      + "(NODE_ENV must be \"production\").";
  if (process.env.EMAIL_DIRECT_SEND_ENABLED !== "1")
    return "Direct sending is not switched on for this application "
      + "(EMAIL_DIRECT_SEND_ENABLED must be \"1\").";
  if (process.env.EMAIL_DIRECT_SEND_KILL_SWITCH === "1")
    return "The emergency kill switch is on (EMAIL_DIRECT_SEND_KILL_SWITCH). "
      + "Clear it in the Function App settings to send directly again.";
  if (killed)
    return killReason || "An administrator has paused all direct sending.";
  return "";
}

function guardrail(recipientCount, mode, cfg = config()) {
  const n = Number(recipientCount) || 0;
  if (n >= ABSOLUTE_BATCH_STOP) return { blocked: true, level: "campaign", message: `${ABSOLUTE_BATCH_STOP.toLocaleString()} recipients is a campaign-sized send and is not supported.` };
  if (mode === "send" && n > cfg.directBatchMax) return { blocked: true, level: "blocked", message: `Direct sending is limited to ${cfg.directBatchMax} recipients per batch.` };
  if (n > cfg.draftsOnlyOver) return { blocked: false, level: "drafts-only", message: "This is larger than normal individualized correspondence; direct sending is unavailable." };
  if (n > cfg.reviewElevatedOver) return { blocked: false, level: "elevated", message: "Elevated send: confirm the recipients and attachments carefully." };
  if (n > cfg.reviewLargeOver) return { blocked: false, level: "large", message: "Large send: review every personalized message before approval." };
  if (n > cfg.reviewSummaryOver) return { blocked: false, level: "summary", message: "Review the full batch summary before approval." };
  return { blocked: false, level: "normal", message: "" };
}

// Does this approval need the shared passcode? Both halves must be configured:
// a threshold with no code set would lock every rep out of every batch, which is
// a worse failure than not having the check at all.
function passcodeRequired(recipientCount, cfg = config()) {
  return !!cfg.passcode && cfg.passcodeOver >= 0
    && (Number(recipientCount) || 0) > cfg.passcodeOver;
}

// Timing-safe, and length-tolerant: comparing with === leaks the answer one
// character at a time, which matters more here than usual because the secret is
// only four digits wide.
function passcodeMatches(supplied, cfg = config()) {
  const a = Buffer.from(String(supplied || ""), "utf8");
  const b = Buffer.from(cfg.passcode, "utf8");
  if (!b.length || a.length !== b.length) return false;
  return crypto.timingSafeEqual(a, b);
}

module.exports = {
  // IMAGE_TOKEN is exported because email-store.js validates {{image:x}} tokens
  // against the images approved onto a template. It was omitted, which made
  // core.IMAGE_TOKEN undefined there -- and matchAll(undefined) does not throw,
  // it silently matches an EMPTY regex at every position, so m[1] came back
  // undefined and every template save was rejected for referencing an image
  // called "undefined". Templates with no charts at all failed too.
  ABSOLUTE_BATCH_STOP, BUILTIN_TEMPLATES, ALLOWED_FIELDS, IMAGE_TOKEN, config, splitName,
  mergeValues, renderTemplate, plainTextToSafeHtml, sanitizeEmailHtml, extraRecipients,
  corporateSignature, validEmail, isExternal, guardrail, lintTemplate,
  passcodeRequired, passcodeMatches, archiveFooter, complianceBcc, FORESIDE_DEFAULT, interleaveByDomain, campaignHealth, directSendBlockedBy,
  internalRecipientAllowed
};
