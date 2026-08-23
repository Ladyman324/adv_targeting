# Email settings in Azure

Every value below is an **Application setting** on the Function App
(`eicadvisormail` → Settings → Environment variables → App settings). Changing
one takes effect on the next restart — Azure restarts the app automatically when
you save.

Nothing here needs a redeploy. That is the point of the list.

> **Never set `ALLOW_DEV_IDENTITY` in Azure.** It bypasses authentication.

---

## Required for the new features

| Setting | Example | What it does |
|---|---|---|
| `EMAIL_APPROVAL_PASSCODE` | *(four digits you choose)* | The shared 4-digit approval code. **Leave unset to switch the passcode off entirely.** |
| `EMAIL_PASSCODE_OVER` | `10` | Batches with more recipients than this need the passcode. `0` means every batch. |
| `EMAIL_UNSUBSCRIBE_SECRET` | a long random string | Signs the preference link in every footer. **Required, or no email carries a working unsubscribe link.** |
| `EMAIL_PUBLIC_BASE_URL` | `https://lively-stone-05fce880f.7.azurestaticapps.net` | The public origin used to build that link. No trailing slash. |
| `EMAIL_FORESIDE_REPS` | `shalley@eicatlanta.com,dmckinney@eicatlanta.com` | Comma-separated mailbox addresses of Foreside registered representatives. Only these get the Foreside paragraph. |

### Generating the unsubscribe secret

Run this and paste the output straight into Azure. **Do not send it to me or
anyone else** — anyone holding it can forge an opt-out for any address.

```bash
node -e "console.log(require('crypto').randomBytes(32).toString('base64'))"
```

Changing it later invalidates the links in emails already sent. Those recipients
get a "this link is not valid" page telling them to reply instead, so it fails
safely — but treat it as set-once.

---

## Limits and thresholds

| Setting | Default | What it does |
|---|---|---|
| `EMAIL_DIRECT_BATCH_MAX` | `250` | Most recipients allowed in one **direct send**. Above this, drafts only. |
| `EMAIL_EXTERNAL_24H_LIMIT` | `5000` | External recipients one user may send to in a rolling 24 hours. |
| `EMAIL_REVIEW_SUMMARY_OVER` | `25` | Above this, the batch summary must be reviewed. |
| `EMAIL_REVIEW_LARGE_OVER` | `50` | Above this, "review every message" warning. |
| `EMAIL_REVIEW_ELEVATED_OVER` | `100` | Above this, "elevated send" warning. |
| `EMAIL_DRAFTS_ONLY_OVER` | `250` | Above this, direct sending is unavailable regardless of other settings. |
| `EMAIL_CANCELLATION_SECONDS` | `30` | The window in which a rep can still stop an approved send. |
| `EMAIL_MAX_ATTACHMENT_BYTES` | `15728640` | 15 MB. Per-attachment ceiling. |
| `EMAIL_MAX_MESSAGE_BYTES` | `20971520` | 20 MB. Whole-message ceiling. |
| `EMAIL_MAX_BODY_CHARS` | `30000` | Hard-capped at 30,000 whatever you set — Azure Table strings max out at 64 KiB. |

**15,000 recipients is a hard stop in code** and is not configurable. A batch
that size is a marketing campaign and belongs in a platform built for it.

---

## Signature and disclosure

| Setting | What it does |
|---|---|
| `EMAIL_SIGNATURE_COMPANY_NAME` | Defaults to `Equity Investment Corporation`. |
| `EMAIL_SIGNATURE_ADDRESS` | Office address line. |
| `EMAIL_SIGNATURE_WEBSITE` | Defaults to `https://www.eicatlanta.com`. |
| `EMAIL_SIGNATURE_DISCLOSURE` | **Firm-wide** compliance text, on every email from everyone. Blank lines separate paragraphs. |
| `EMAIL_FORESIDE_DISCLOSURE` | Overrides the built-in Foreside wording. Only reaches reps listed in `EMAIL_FORESIDE_REPS`. |

### The three tiers, and why they are separate

1. **`EMAIL_SIGNATURE_DISCLOSURE`** — firm-wide. Everyone.
2. **The Foreside paragraph** — only reps named in `EMAIL_FORESIDE_REPS`. This is
   a statement about *one person's own registration*. Putting it on every email
   asserts a registration most users of this app do not hold, which is why it
   cannot live in the firm-wide setting.
3. **The archive / confidentiality footer** — everyone, always, and it is **built
   into the code, not a setting**. It carries the unsubscribe link, so it is not
   something that should be editable to the point of accidental removal.

---

## Safety switches

| Setting | What it does |
|---|---|
| `EMAIL_DIRECT_SEND_ENABLED` | `1` enables direct sending. Anything else = drafts only. |
| `EMAIL_DIRECT_SEND_KILL_SWITCH` | `1` disables direct sending immediately, overriding the above. |
| `EMAIL_TEST_ADDRESS_ALLOWLIST` | Comma-separated addresses. When set, a direct send fails if **any** recipient is not on it. Leave unset in normal operation. |
| `EMAIL_INTERNAL_DOMAINS` | Defaults to `eicatlanta.com`. Decides what counts as external for the 24-hour limit. |

---

## Act! Mail Code

| Setting | Example | What it does |
|---|---|---|
| `ACT_MAIL_CODE_FIELD` | `customFields.email__y_n` | The Act! Web API property behind the "Mail Code" picklist. **Unset = no contact field is written**, and the opt-out is recorded as Act! history only. |

Confirmed 2026-08-18 against a live single-contact GET, and consistent with the
2026-08-13 export of 47,466 contacts — not inferred from the UI label: `customFields.email__y_n` holds
`2` (22,195), `P` (15,996), `N` (4,947), `U` (3,064), `NC` (527). Dotted paths are
supported, which this field needs.

An opt-out sets Mail Code to `U` (UNSUBSCRIBE), and only ever tightens it -- a
record already at `U` is left alone. The write is a read-modify-write that sends
the whole contact back with one property changed, so it is safe whether Act!
treats the payload as a merge or a replace.

To discover the property name, as an email administrator open:

    /api/email?op=act_fields&crd=1000084

Use any CRD you know is in the crosswalk. The response lists the contact's
property names and flags any whose value already looks like a Mail Code
(`1 2 3 C N NC P U`). Put that property name in `ACT_MAIL_CODE_FIELD`.

## Bounce sweeping

| Setting | Default | What it does |
|---|---|---|
| `EMAIL_BOUNCE_SWEEP_ENABLED` | *(off)* | `1` turns the sweeper on. Anything else and it does nothing — it will not even enumerate mailboxes. |
| `EMAIL_BOUNCE_LOOKBACK_HOURS` | `48` | How far back in each inbox to look on every run. |
| `EMAIL_BOUNCE_PAUSE_PERCENT` | `3` | Hard-bounce rate above which a sending batch pauses itself. |
| `EMAIL_BOUNCE_MIN_SAMPLE` | `25` | Messages that must have gone out before the percentage means anything. |

### Campaign health

A batch that is bouncing above the threshold **pauses itself** and records
`batch_paused_bounce_rate` in the audit trail. It pauses and never cancels:
pausing is reversible by someone who can look at the bounces and judge, while
cancelling would destroy a half-sent campaign on an automated percentage.

Counts are recomputed from the messages by `refreshBatch()` rather than
incremented, so concurrent workers cannot race the counter.

### Send order

Messages are paced apart by `EMAIL_MAILBOX_INTERVAL_SECONDS`, but **not in list
order**. Lists come out of the map grouped by firm, so pacing on list order sent
130 consecutive messages to one wirehouse — the exact shape a receiving gateway
throttles, and the reputational cost lands on `eicatlanta.com` for all mail.

The order is round-robined across recipient domains at approval time and stored
on each message, so consecutive sends go to different organisations wherever the
batch allows. Same total duration; spread across firms rather than in blocks.
Deterministic, so a retry cannot reshuffle a queue already scheduled.

Runs every two hours (a **literal** cron in `function.json`, not a `%SETTING%` —
an unresolved setting stops the host loading the function, which would take the
sending API down with it).

What it does: reads each connected mailbox for non-delivery reports, matches them
to messages we sent via the stored Internet message id, and suppresses addresses
that permanently failed. Suppressed addresses are blocked by
`validateMessage`, which has always read that table and never had anything
writing to it.

**Hard bounces only.** Deliberately conservative, because the failure modes are
not symmetric — a missed bounce means one wasted email next quarter, while a
false bounce permanently and silently stops us contacting a reachable advisor:

- `4.x.x` (soft) is ignored entirely; it recovers on its own.
- Permanent failures that are **not about the address** — `5.2.2` mailbox full,
  `5.3.4` message too large, `5.7.1` blocked by their policy, `5.7.26` DMARC —
  are recorded and never suppressed.
- A report that looks official but states nothing definite (an out-of-office from
  an odd mailbox) is not a bounce.
- The address comes from **our** sent record, never from the report. An NDR is
  text anyone can send us; reading the address out of it would let a stranger
  suppress an advisor by forging one.

**The mailbox is never modified** — no marking read, no moving, no deleting.
Idempotency comes from our own `EmailBounceSeen` table.

Hard bounces also push Act! Mail Code **`N`** ("No mail; cannot locate; bounce
backs"), plus a history entry, subject to the same never-downgrade rule as
opt-outs: a contact already at `U` stays `U`, because asking not to be emailed
outranks having a broken address. Requires `ACT_MAIL_CODE_FIELD`.

### Turning it on

Set `EMAIL_BOUNCE_SWEEP_ENABLED=1`. Watch the Function App logs for
`Bounce sweep: N mailbox(es), N report(s) examined, N address(es) suppressed.`
The first run examines 48 hours of inbox, so expect it to find historic NDRs.

To review what has been suppressed, open Storage Explorer → `EmailSuppressions`.
There is no un-suppress in the app, by design.

## The Act! do-not-email floor

`api/shared/act_mail_codes.json` ships inside the API and holds every address and
CRD that Act! marks do-not-email (`U`, `NC`, `N`, `BB`). It is consulted on every
batch **in addition to** the live suppression table.

This exists because the app's own list only knew about clicks on its own footer
link. On the 2026-08-13 export, **2,383 contacts that Act! marks do-not-email were
still selectable and sendable** — 701 of them explicit UNSUBSCRIBEs.

Keyed on address **and** CRD, deliberately. Act! users overwrite the email field
with a note (`unsubscribed 3/27/26`, `retired`), which destroys the address but
not the opt-out, while we still hold a working address from SEC data. 823 people
were reachable only via the CRD.

Refresh it after each export:

    python src/build_act_mail_codes.py data/raw/act_contacts_<date>.json

then redeploy the API. An empty file is the dangerous state — it reads as "nobody
is suppressed" rather than as a fault — so `src/audit.py` fails if it drops below
2,000 entries.

## The do-not-email list

Opt-outs are stored in the `EmailSuppression` table in the storage account,
keyed on a hash of the address.

- **Keyed on the email address, not CRD.** Someone who opts out is asking about
  the address they were mailed at. A CRD key would suppress every address we
  hold for that person off one click, and would miss anyone whose address we
  hold without a CRD match.
- **Enforced twice** — recipients are dropped when the batch is built (the rep
  is told how many and who), and re-checked at approval so an opt-out arriving
  mid-edit is still honoured.
- **Recorded in Act!** as an activity and history entry on the contact, using
  the same task-then-clear route as call logging. It does **not** set a contact
  field — no do-not-email field name has been confirmed with Act! support, and
  guessing one risked a partial update being treated as a full replace. See
  `docs/act_support_questions.md`. The local write happens first regardless: a
  CRM outage must never turn into "we could not unsubscribe you."
- The contact is resolved through the CRD crosswalk, and the CRD travels inside
  the signed link token. An opt-out for an address with no CRD match is still
  honoured locally; it just cannot be mirrored into Act! yet.
- **No delete in the app**, deliberately. Removing a suppression is a compliance
  decision, taken in the storage account by someone who means it.

To review the list, open Storage Explorer → `EmailSuppression`.
