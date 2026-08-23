# Moving email templates into the application

## What exists today

`I:\_Marketing\Materials\Email Templates` holds **62 Word documents**, plus an
`Archive` folder and a folder of chart JPEGs.

They are not free-form. The blank starter defines a fixed header block that most
templates follow:

```
Title:
Original Author:
Document #:
Attachments required:
Prior Versions/Historical Edits:
Approval Date:
Subject:
```

That is a compliance record, and it is more structure than the application
currently has. A template in the app today is only `{ id, name, subject,
bodyText }` — there is nowhere to record who approved it, when, which document
number it is, or which attachments it requires.

### Three findings that shape the design

**1. Most templates are built around a chart.** 45 of the 62 embed at least one
image — performance pages, style boxes, factsheets, rolling-return charts —
pasted directly into the email body. Total: 57 embedded images.

The application has no inline images. Bodies are plain text converted to safe
HTML. **This is the central decision** and is covered below.

**2. "Attachments required" is per-template, not per-batch.** For example, the
Value Prospecting template specifies *"Most recent BROKER SPECIFIC approved Case
for Value, BROKER approved ACV presentation book."* Today the app lets the rep
choose attachments freely at batch time, which means the template's requirement
is guidance in prose rather than something the system enforces.

**3. Bodies contain instructions to the rep, mixed into the content.** One
template ends with *"INSERT LCV PERFORMANCE PAGE HERE AND ATTACH THE LCV
PERFORMANCE PAGE SHEET + LCV FACTSHEET (GENERIC)."* That is a note, not text to
send, and there is currently nowhere else to put it.

Also worth noting: merge fields are informal. Templates open with "Hi Advisor,"
or just "Advisor," — there is no equivalent of `{{first_name}}`. Migration is an
opportunity to personalise properly, not merely a copy exercise.

---

## The decision: what to do about the charts

### Option A — convert charts to PDF attachments

Each chart becomes an approved document; the body references it.

- Fits the model already built; no new work beyond template management
- The compliance story is stronger — every chart is versioned and hashed
- **But it is a visible downgrade.** An advisor who currently sees a performance
  chart in the message would have to open an attachment. Three quarters of the
  library is designed around that chart being visible immediately.

### Option B — support inline images in templates *(recommended)*

The admin uploads a PNG alongside the template; it is sent as an inline
attachment referenced from the body, which is how these emails are built today.

- Preserves what the templates are actually for
- Same approval controls as documents: versioned, hashed, admin-only
- Graph supports this directly (`isInline` + `contentId`)
- **Cost:** a real piece of work — image storage, a body syntax for placing
  them, inline attachment handling in the send path, and preview rendering

### Option C — link to images hosted elsewhere

Not recommended. Outlook blocks remote images by default, so the chart is
invisible until the advisor clicks "download pictures" — the worst of both
options.

**Recommendation: Option B.** Option A can ship first if speed matters, but it
will read as a regression to anyone who used the Word templates, and the request
to "put the chart back" is inevitable.

---

## Proposed template model

Extending what a template holds, to match the Word header block:

| Field | Source | Purpose |
|---|---|---|
| `id` | derived from title | stable key |
| `name` | Title | what the rep picks from |
| `documentNumber` | Document # | compliance reference, e.g. `25032501` |
| `author` | Original Author | who wrote it |
| `approvedBy` / `approvalDate` | Approval Date | the record that matters |
| `subject` | Subject | merge-enabled |
| `bodyText` | body | merge-enabled |
| `requiredDocumentIds` | Attachments required | **enforced**, not advisory |
| `repNotes` | the INSERT/ATTACH instructions | shown to the rep, never sent |
| `inlineImageIds` | embedded charts | Option B only |
| `status` | new | `draft` / `approved` / `retired` |
| `version` | new | increments on publish, as documents do |

`requiredDocumentIds` is the one that changes behaviour: the batch cannot be
approved without those attachments present and current. That turns a line of
prose into a control.

---

## Migration

**Do not import all 62.** The directory contains several generations of the same
template — `KT ACV Factsheet 24012910` alongside `KT ACV Factsheet 26013003`,
year-end performance templates from 2023 in seven broker-specific variants. Most
are superseded.

Suggested approach:

1. Whoever owns the library marks the **currently approved** set. The estimate
   is 15–20 templates, not 62.
2. For each, the admin creates it in the app: paste subject and body, set the
   metadata, attach required documents, upload the chart (Option B).
3. Replace "Hi Advisor," with `{{first_name}}` as they go.
4. Leave the Word directory in place, read-only, as the historical record.

Bulk import is possible — the text extracts cleanly from `.docx` — but the
bodies need human attention anyway for merge fields, rep notes, and which
generation is current. Doing 15–20 by hand is a day's work and produces a clean
library rather than a migrated mess.

---

## Suggested build order

1. **Template management UI for admins** — create, edit, publish, retire, with
   the metadata fields above. Mirrors the document panel already built.
2. **Required attachments enforced** at batch approval.
3. **Rep notes** surfaced in the composer, never sent.
4. **Inline images** (Option B), if chosen.

Steps 1–3 are independent of the image decision and can start immediately.

## Bullet points

Type them as ordinary lines and they become real bullets in the sent email:

    Three points about our approach:
    - We believe the key to long-term success is avoiding significant losses.
    - Our approach has historically produced a narrower range of outcomes.
    - Our results come from engineering out as much volatility as we can.

Any of these markers work, so a paste from a Word template usually needs no
tidying at all:

| You type | You get |
|---|---|
| `- text` or `* text` | a bulleted list |
| a Word bullet, with or without a tab | a bulleted list |
| `o text` (Word's second level) | a bulleted list |
| `1. text` or `1) text` | a numbered list |

A list ends where ordinary prose resumes, so an introductory line above the
bullets and a paragraph below them both stay paragraphs. Switching from bullets
to numbers starts a new list rather than mixing markers.

The preview panes in the template editor render lists exactly as the sent email
will, so what you see there is what the advisor gets.

**Do not paste HTML.** Angle brackets are escaped, so `<ul>` arrives as visible
text. The plain-text markers above are the way to get a list.
