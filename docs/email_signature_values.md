# Email signature — app settings values

These go in the Function App's environment variables. They are **firm-wide
constants**, identical for every rep. The per-rep half of the signature (name,
title, phone, email) is generated from each rep's Microsoft Entra profile when
they connect their mailbox — nothing to configure there.

Built by `corporateSignature()` in `api/shared/email-core.js`.

---

## `EMAIL_SIGNATURE_COMPANY_NAME`

```
Equity Investment Corporation
```

Already the code default. Only set it if you want different wording.

---

## `EMAIL_SIGNATURE_WEBSITE`

```
https://www.eicatlanta.com
```

Already the code default. Skip.

---

## `EMAIL_SIGNATURE_ADDRESS`

```
1776 Peachtree Street NW, Suite 600 S, Atlanta, GA 30309
```

One line. The rendered signature puts it on its own row, so it does not need
internal line breaks.

---

## `EMAIL_SIGNATURE_DISCLOSURE`

Paste as one value, **keeping the blank lines** — they become paragraph breaks.

```
Please Note: Registered Representative of Foreside Funds Distributors LLC, the distributor for EIC Value Fund.  Foreside Funds Distributors LLC is not affiliated with Equity Investment Corporation, the Fund's investment advisor.  Managed account and other advisory services are offered through EIC.

The investment objectives, risks, charges and expenses of EIC Value Fund should be considered carefully before investing. A prospectus with this and other information about the Fund is available by visiting www.eicvalue.com or by calling 1-855-430-6487. The prospectus should be read carefully before investing.

EIC e-mails are archived for SEC review purposes, and may contain confidential and privileged information.   If you receive this in error, please reply to inform sender of the message's misdirection, and delete it and any attachments from your computer.  You are not authorized to read, print, retain, copy or disseminate it without our consent, and doing so may be unlawful.
```

### Two things deliberately left out — decide before using this

**1. The preferences link.** Both sample signatures end with a "click here" link
to manage contact/email preferences, worded differently by rep ("contact
preferences" vs "email preferences"). It is omitted above because the disclosure
is HTML-escaped, so a link pasted into this setting renders as literal text.

Options:

- Supply a plain URL and append a sentence such as *"To manage your email
  preferences, visit https://…"* — works today with no code change
- Have the app own it, pointing at its own suppression list — better, since the
  app already tracks who should not be contacted, but needs a route building
- Omit it, if compliance considers these relationship emails rather than
  solicitations

This is a compliance decision, not a technical one.

**2. Verbatim confirmation.** The text above is transcribed from two rep
signatures that agreed word for word. Have whoever owns the disclosure confirm
it before it goes on every outbound message.

---

## Open item — professional designations

The generated signature uses `displayName` from Entra. If Entra holds
"Stephen Halley" rather than "Stephen Halley, CIMA®", the designation is lost.
Same for `jobTitle` — the signature will show whatever Entra holds, not
"Senior Director-Western United States" unless that is what is on the profile.

Check one rep's Entra profile before rollout. Two ways to fix a mismatch:

- **Correct the Entra profile** — best, since it fixes the designation
  everywhere in Microsoft 365, not just this app
- **Add a small per-rep override** in the app's settings screen, limited to
  name suffix and title, leaving the compliance block server-controlled

The second is a modest change and reuses the now-unused `emailSignature` key in
`SETTING_KEYS`.

---

## What the reps' own layouts will lose

The two sample signatures differ in layout — one uses a horizontal rule and
pipe-separated fields, the other stacked lines. The generated signature uses one
layout for everyone. That uniformity is intentional: it guarantees the
disclosure is present and identical on every message, which per-rep pasted
signatures cannot.
