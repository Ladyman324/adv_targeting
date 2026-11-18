# Email signature — app settings values

### Photo framing

For new signatures the image height matches the contact block's explicit
16-pixel lines, including address line breaks but excluding disclosures.
Its width scales proportionally to the selected photo/crop, without distortion.
The crop preview uses the editor's output aspect ratio, not the old photo's.
Recipient-side text wrapping or accessibility overrides can still alter alignment.

Settings → Email signature → Adjust photo provides drag/keyboard positioning,
1–3× zoom, and an actual signature-size preview. Save crop keeps the original
website/uploaded photo and framing metadata, so reopening the editor does not
progressively degrade the image. Reset to original resets framing; Cancel makes
no changes. Replacing a photo starts a new original. Cropping does not enable
photos or change signatures already frozen into generated/scheduled messages.

These go in the Function App's environment variables. They are **firm-wide
constants**, identical for every rep. The per-rep half of the signature (name,
title, phone, email) is generated from each rep's Microsoft Entra profile when
they connect their mailbox. Optional designations are saved in each user's
signature settings.

Built by `corporateSignature()` in `api/shared/email-core.js`.

## Per-user designations

Settings > Email signature > Designations after my name stores an optional
80-character suffix, for example `CIMA®` or `CFA, CIMA®`. The app adds it after
the Microsoft 365 display name in new-message signatures and signature previews.
It does not rename the mailbox, change body merge fields, edit ACT!, or alter
signatures frozen into previously prepared or scheduled messages. Clear and save
the field to remove the suffix. Do not add it if the Microsoft 365 name already
contains the same designation.

## Using the photo in Outlook

Choose **Download photo for Outlook** in signature settings, then insert the
saved file through Outlook's signature editor. The Map preview loads a private,
authenticated image; copying browser HTML does not reliably transfer that image
or its table layout into Outlook. Classic Outlook uses File > Options > Mail >
Signatures and the picture button. New Outlook/web uses Settings > Accounts >
Signatures. Keep the photo's aspect ratio when resizing. For a side-by-side
layout, create a two-column table in Word and paste that into the signature
editor, then insert the photo file.

See [Microsoft's signature instructions](https://support.microsoft.com/en-us/outlook/mail/how-to-add-and-change-an-email-signature-in-outlook).

## Optional sender photos

Settings on desktop and Field include an **Include my photo** control, off by
default, and a signature preview. The current EIC website's 26 headshots are
mapped to exact enabled Microsoft account IDs in
`api/shared/signature-photos/seed.json`. This inventory does not update M365
profile photos or require directory-admin permissions.

Users can replace their own photo with a JPEG, PNG, or WebP. The browser
resizes uploads to 320 pixels and encodes JPEG; the API validates JPEG/PNG
bytes, dimensions, and a 512 KB maximum. Uploading does not opt the user in.
The photo sits beside clickable Microsoft 365 contact details; corporate
disclosures and preference links remain below it.

Generated messages freeze the chosen image hash and signature HTML. Images
are embedded as CID attachments from immutable private blobs in the existing
email image container. Replacing or restoring a photo retains old blobs for
scheduled work and historical previews. Retry reviews preserve that same
snapshot. Batch follow-ups continue without a new signature or photo.

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

## Professional designations and job titles

The name, job title and contact details come from the Microsoft 365 profile.
If a designation is missing there, the rep can now add it in Settings using the
per-user designation field described above. Job-title corrections still belong
in Microsoft 365. Corporate disclosures remain server-controlled.

---

## What the reps' own layouts will lose

The two sample signatures differ in layout — one uses a horizontal rule and
pipe-separated fields, the other stacked lines. The generated signature uses one
layout for everyone. That uniformity is intentional: it guarantees the
disclosure is present and identical on every message, which per-rep pasted
signatures cannot.
