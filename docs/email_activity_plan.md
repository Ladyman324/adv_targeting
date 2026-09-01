# Advisor Activity Layer — build plan

Status: **Phases 0-6 built; queue/sync reliability is checkpointed. Not deployed.**

The next release gate is intentionally split: ETag-safe token/projection writes
and fail-closed one-to-one send controls come first; the durable projection
repair worker and direct-send operation ledger follow as separate checkpoints.
The reply sweep and one-to-one direct sends stay disabled until those durable
workflows pass canary recovery tests.

An external code review found six deployment blockers and a dozen further
defects. All were verified against the code and all were real; the fixes are in,
with the reasoning recorded beside each. See **What the review found** below —
it is the most useful section of this document, because three of the items were
false statements *in this file* rather than latent bugs.


    built     phase 0   advisor email lookup, contactId confirmed as CRD
    built     phase 1   conversationId captured, backfilled, audited
    built     phase 2   reply sweep: mailbox-wide, classified, provenanced
    built     phase 3   Outlook outbound captured, attributed, cc-tagged
    built     phase 4   activity timeline -- desk AND field, read on demand
    built     phase 5   engagement projection + work queue -- desk AND field
    built     phase 6   reply and follow-up, with attachments -- desk AND field
    deleted   phase 7   application permissions -- unnecessary

    The full API suite and repository audit pass at each checkpoint.

Nothing is deployed. `dist/api.tgz` carries all of this plus the older unshipped
`store.js` TABLES fix and the CC/BCC work.

## What this is

Not "email reply tracking." An **activity and next-action layer**: the app records
what happened with each advisor across email and phone, regardless of whether the
rep acted inside this application or inside Outlook, and turns that into a
prioritised list of who to work next.

If we track only mail our own emailer sent, an advisor relationship conducted
entirely in Outlook — which is how most one-off correspondence happens — looks
*dead* in the app. That is worse than no data, because a rep would trust it.

## What already exists (verified in code, not assumed)

This section is the reason the plan below is small. Read it before scoping
anything.

**Refresh tokens are persisted, and unattended operation is already proven.**
`shared/email-auth.js` holds an MSAL `ConfidentialClientApplication` whose token
cache is serialised, AES-256-GCM encrypted (`shared/email-crypto.js`) under
`EMAIL_TOKEN_ENCRYPTION_KEY`, and AAD-bound to `userId|mailboxId` so a cache row
cannot be moved between users. `tokenFor(userId)` calls `acquireTokenSilent` and
**writes the rotated cache back**, which is what keeps the refresh token alive.

`email-worker` is queue-triggered and `email-bounce-sweep` is timer-triggered
(every two hours), and both call `tokenFor(userId)` for an arbitrary rep with
nobody signed in. **Background mailbox access under delegated permission is not a
question to answer — it is in production.**

**The phase 7 I originally proposed — application permissions scoped by Exchange
RBAC — is deleted.** It is unnecessary. Delegated is strictly better here: each
token reaches exactly one mailbox by construction, with no tenant-wide grant to
defend.

**Draft-first sending is already implemented.** `graph-mail.js` has `createDraft`
and `sendDraft`. There is no `sendMail` path to migrate.

**ImmutableId is already in use.** `Prefer: IdType="ImmutableId"` is set on every
request in `graph-mail.js`. Codex's correction was already standing practice.

**A canonical app-side message id already exists, and it is better than the
`X-EIC-Outreach-ID` header we discussed.** Every draft is stamped twice:

    singleValueExtendedProperties: [{ id: APP_PROPERTY_ID, value: message.id }]
    internetMessageHeaders:        [{ name: "X-EIC-Message-Id", value: message.id }]

and `findByAppId()` queries Graph by that extended property. The header is
human-readable for troubleshooting; the extended property is *queryable*, which
the header is not. Nothing to add.

**`internetMessageId` is already stored** as `graphInternetMessageId` on a
`messages` table, with a state machine (`submitted` / `sent`) and a
`sentByInternetId()` index used by bounce matching.

**`internetMessageHeaders` is readable under our permission** — `recentInbox()`
already `$select`s it. Codex's open question is answered empirically.

**The sweep architecture we were going to design already exists.**
`email-bounce-sweep` is a timer function that iterates connected mailboxes, reads
recent inbox mail, matches against our own sent records, and is idempotent via a
`bounceSeen` table. It carries a feature flag, a hard-coded cron (deliberately not
a `%SETTING%`, because an unresolved setting takes the whole Functions host down),
a lookback window, and a standing rule that the mailbox is never modified.

Copy that shape. Do not reinvent it.

## Permissions: nothing to request, ever

Confirmed delegated and consented — and `GRAPH_SCOPES` in `email-auth.js` matches
exactly:

    Mail.ReadWrite   read + create/modify messages in the signed-in rep's mailbox
    Mail.Send        send as the signed-in rep
    offline_access   refresh tokens -> unattended operation
    openid, profile, User.Read

Everything in every phase below is authorised today.

Because the permission boundary is not constraining us, the discipline lives in
code and in the audit. See "Data minimisation".

## The actual gaps

1. **`conversationId` is never captured.** The `$select` in `createDraft`,
   `getMessage` and `findByAppId` is
   `id,internetMessageId,isDraft,sentDateTime,parentFolderId,subject`. This is
   the single most important missing field and the main blocker for thread-based
   reply matching.
2. **No inbound activity record.** The bounce sweep reads the inbox and discards
   everything that is not an NDR.
3. **Inbox only, no Junk.** A filtered reply is currently invisible.
4. **No delta.** The bounce sweep re-scans a lookback window each run, which is
   fine at two-hourly bounce cadence but wasteful for reply detection.
5. **No Sent Items sync**, so Outlook-originated mail is invisible.
6. **`recentInbox` selects `body`.** Fine for NDR parsing; for reply detection we
   want metadata only, with body fetched on demand.

## Decisions

### Capture `conversationId` — and backfill it

Add `conversationId` to the `$select` in `graph-mail.js` and persist it on the
`messages` row. Small change, unblocks everything else.

Because sent messages are already tagged with the `EICMessageId` extended
property, historical rows can be **backfilled** via `findByAppId()` rather than
starting from zero. Worth doing for at least the recent window so reply detection
has history to match against on day one.

### Reply detection gets its own function. Do not extend the bounce sweep.

Tempting to piggyback — same inbox read, same connection loop. Don't.

Bounce suppression is **permanent and destructive**: a false positive silently
stops us contacting a reachable advisor, which is exactly what that module's
docstring is built around. Reply detection is **additive** and low-risk. Sharing a
code path means a bug in the new, fast-moving thing can break the old,
safety-critical thing.

Separate timer function, reusing `graph-mail`'s reader. Same feature-flag and
hard-coded-cron pattern.

### Sweep scheduling: separate functions, different cadences, offset crons

Considered running reply detection inside the bounce sweep, on the same
two-hourly timer. Rejected, for three reasons — the third one found in the code.

**The costs scale differently.** The bounce sweep re-reads a 48-hour lookback
window every run, so its cost is roughly linear in how often it runs. A
delta-based reply sweep that finds nothing costs one small call per
`(rep, folder)` — near-free, and flat in frequency. Coupling them would force the
expensive one to run at the cheap one's cadence, or the cheap one to be
throttled by the expensive one's budget. That asymmetry is the whole argument
for delta.

**The urgencies differ.** A dead address stays dead; two hours is ample, as that
module's own docstring says. A reply is worth knowing this morning. Different
numbers, and there is no reason to average them.

**And the risk classes differ.** Bounce suppression is permanent and destructive
-- its docstring is built entirely around never producing a false positive.
Reply detection is additive. A crash in new, fast-moving code should not be able
to abort a half-finished suppression pass.

    email-bounce-sweep    0 20 */2 * * *     unchanged
    email-reply-sweep     0 2,17,32,47 * * * *   every 15 min, never on :20
    email-engagement-repair 0 7,22,37,52 * * * * five minutes after reply sweep

**The minute offset is not cosmetic.** Reply and bounce both read Graph and may
refresh the same mailbox token, so their schedules remain separated even though
token-cache writes now use ETags and conflict retries. The repair timer starts
five minutes after reply sweep; it reads only Table Storage, never Graph, and
drains any projection work the inline refresh could not acknowledge.

### Mailbox-wide watermark, NOT per-folder delta

**Reversed from the earlier draft**, on the requirement that a rep changing their
own Outlook rules must not silently break this. Enumerating folders cannot meet
that bar: a rule created tomorrow routes advisor mail somewhere we do not poll,
detection stops, and the app keeps saying "no reply recorded" with total
confidence. Nobody would find out.

Graph's message *delta* is explicitly per-folder, so delta cannot satisfy the
requirement at all. But the `/me/messages` **collection spans every folder**, and
it accepts a `receivedDateTime` filter:

    GET /me/messages
        ?$filter=receivedDateTime ge {watermark}
        &$select=id,internetMessageId,conversationId,from,toRecipients,
                 receivedDateTime,subject,internetMessageHeaders
        &$orderby=receivedDateTime desc

That is folder-agnostic by construction. A rep can invent any rule they like and
the sweep still sees the message.

It is also *simpler* than the delta design it replaces:

    per-folder delta      state per (rep, folder), folder discovery, token
                          expiry (410 resyncRequired), and a permanent hole
                          whenever a folder is created between discoveries
    mailbox watermark     one timestamp per rep

And it fits the semantics better. Delta reports *changes* — moves, flags, reads,
deletes — almost none of which we care about. We care about arrivals, which is
exactly what a `receivedDateTime` watermark expresses.

Two things it needs:

- **An overlap window.** Advance the watermark to `now - N minutes`, not `now`,
  so a message delivered slightly out of order is not stepped over.
- **Idempotency by message id.** A `replySeen` table, exactly like the existing
  `bounceSeen`, so the overlap costs nothing but a duplicate read.

Cost is comparable to delta in the common case: the filter is on an indexed
property and returns nothing when nothing has arrived.

**A pre-existing gap this exposes.** `recentInbox()` in `graph-mail.js` queries
`/me/mailFolders/inbox/messages` — Inbox only. The bounce sweep therefore has the
same blind spot today: an NDR filtered out of the Inbox is invisible to it.
Lower stakes than a lost reply (a hard bounce usually recurs on the next send)
but the same class of bug, and switching that call to `/me/messages` would close
it for both. Worth doing while the reply sweep is being written, as a separate
change with its own test.

One consequence to keep in mind: `/me/messages` includes **Sent Items and
Drafts**, not just received mail. That is a feature for phase 3 — one sweep can
feed both inbound and outbound activity — but phase 2 must distinguish them, by
whether the sender is the rep's own mailbox rather than by folder.

### Metadata in Azure, content in Exchange

Store that a communication happened. Fetch the content from Graph only when a rep
clicks to read it — `uniqueBody` (the part unique to that message, not the whole
quoted chain), rendered and discarded, never persisted.

    stored:      appMessageId, advisorCRD, repId, advisorEmail, direction, source,
                 occurredAt, conversationId, internetMessageId, graphImmutableId,
                 subject, classification, matchMethod, campaignId, templateId
    NOT stored:  body, uniqueBody, attachments, full conversation

This keeps Exchange the system of record and stops us building a second archive of
our own reps' mailboxes.

## Data minimisation

A delta query returns **everything** — internal mail, HR, personal, newsletters.
The permission does not filter this; our code must.

    inbound message
      -> is the sender (or any recipient, for Sent Items) a known advisor address?
         NO  -> discard, increment a counter, log nothing
         YES -> classify and persist metadata

Two things that are easy to get wrong:

- **Never log message metadata for debugging.** Verbose logging is how a shadow
  index of rep correspondence gets built by accident. Counters only.
- **The filter needs the advisor address universe inside the Function.**
  `contacts.json` is 42MB and cannot be loaded per sweep. The pipeline must emit a
  compact `AdvisorEmail` (email -> CRD) lookup into Table Storage for point
  lookup. `build_contacts.py` already derives this mapping — it is an export, not
  new logic. **Prerequisite for phases 2 and 3.**

## Matching, and never guessing

    1. conversationId matches a stored outbound      -> thread_match
    2. In-Reply-To / References contains a stored
       internetMessageId                             -> references_match
    3. sender is a known advisor, no thread          -> sender_only
    4. a human linked it                             -> manual

No subject-line matching at any priority. Subjects are reusable and editable and
would produce confident wrong answers.

`sender_only` means **"new email from advisor"** — real and valuable — and must
never render as "replied to campaign X". Same provenance discipline as
`phone_kind`, `city_source`, and the `confirmed`/`high`/`review` tiers.

### Classification

V1: `reply | auto_reply | bounce | unknown`.

Detect auto-replies from `Auto-Submitted` and `X-Auto-Response-Suppress` in
`internetMessageHeaders`, plus postmaster / mailer-daemon senders. Reuse
`shared/email-bounce.js` rather than growing a second classifier that drifts.

**An out-of-office must never surface as "advisor replied."** That single error
would cost the feature its credibility faster than anything else here.

### Language

    reply detected · no reply recorded · auto-reply detected
    delivery failure detected · delivery status unknown

Never "no reply". Absence is not evidence.

## Phases

### Phase 0 — prerequisites (mostly answered)

- ~~Confirm token persistence~~ **Done. Persisted, encrypted, rotating, and
  already driving two unattended functions.**
- ~~Verify ImmutableId~~ **Done. Already in use.**
- ~~Confirm whether `messages.contactId` resolves to advisor CRD~~ **Done. It
  is the CRD** -- `email-service.js` builds it as
  `String(raw.contactId || raw.crd || "")`, so outbound messages are already
  linked to advisors and reply matching can join straight through. Worth a spot
  check that no caller passes a non-CRD contact id.
- ~~Emit the `AdvisorEmail` (email -> CRD) lookup~~ **Done.**
  `src/export_advisor_emails.py` -> `data/output/advisor_emails.json.gz`,
  1.40 MB holding 122,831 resolved addresses, 172 published as ambiguous, and
  386 domain -> firm mappings. Blob rather than Table Storage, because the
  sweep's lookups are many and mostly MISSES: a per-message table round trip
  would spend its whole budget proving that internal mail is not from an
  advisor, where an in-memory Map costs nothing. Carries a `contentHash` over
  the data alone, so "did the universe change?" is answerable without the
  `generated` timestamp getting in the way. Upload is a separate `--upload`
  step because it needs a storage credential.
- ~~Ask reps whether Outlook rules route advisor mail out of Inbox~~ **Moot.**
  The sweep is mailbox-wide; see the scheduling and watermark decisions above.
  No folder enumeration means no folder assumption to invalidate.

### Phase 1 — capture `conversationId` — DONE

`MESSAGE_FIELDS` in `graph-mail.js` now selects `conversationId` on every
lookup; the worker persists it as `graphConversationId` at draft time and
backfills it on send for anything drafted before the field existed (fill-blank
only, never overwrite). `sentByInternetId()` returns it so reply matching can
use it. Three tests in `test/email-worker.test.js`, and an audit check --
*a Graph message field we select is a field we can store* -- that catches a
property being fetched and then silently dropped by `patchMessage`'s whitelist.

Historical sent mail predating this can still be backfilled via `findByAppId()`
if a one-off is wanted; in-flight batches backfill themselves.

### Phase 2 — inbound reply detection — DONE

`api/email-reply-sweep/` on a 15-minute timer, built on the bounce-sweep
pattern: feature flag (`EMAIL_REPLY_SWEEP_ENABLED=1`), literal cron, per-message
idempotency (`replySeen`, mirroring `bounceSeen`), and the mailbox never
modified.

  * `shared/advisor-lookup.js` loads the blob once per cold start into a Map and
    **fails closed** -- if it cannot load, the sweep aborts rather than deciding
    that nobody is an advisor and marking every message seen on the way past.
  * `shared/email-reply.js` separates *what is this* (`classify`) from *what does
    it answer* (`match`), so an out-of-office can never be counted as a reply.
    Routes: `thread_match`, `references_match`, `sender_only`. No subject
    matching at any priority.
  * Only a thread route may name a campaign. A `sender_only` sighting is stored
    as real advisor activity with an empty `batchId`.
  * An ambiguous address is stored with a blank `advisorCrd` -- naming one of
    four Stifel Johnsons would be a fabrication.
  * The watermark advances only on a clean, untruncated pass.

Audit: *no mailbox reader is scoped to a single folder*, *the reply sweep cannot
run on the bounce sweep's minute*, *the reply sweep stores no message body*.

**Also fixed, in `graph-mail.js`, two pre-existing bugs the folder question
exposed:**

    recentInbox queried /me/mailFolders/inbox/messages
        -> an NDR filed out of the Inbox by a rule was invisible to the bounce
           sweep. Now /me/messages, which spans every folder.

    $top=100 over a 48-hour window with NO paging
        -> a rep receiving more than 100 messages in the window silently lost
           the oldest from view. Same shape as the Baird truncation: a plausible
           answer, quietly incomplete. recentMail() now follows @odata.nextLink
           and RETURNS `truncated` rather than hiding it.

Widening the bounce sweep's scope put the rep's own sent mail in its view, so it
now skips messages whose sender is the mailbox itself -- a message cannot be a
delivery report about itself, and the only thing that may ever suppress an
address is something that genuinely arrived from outside.

### Phase 3 — Outlook-originated mail — PARTLY DONE

The sweep reads `/me/messages`, which already includes Sent Items, and records a
rep's own sends as `direction=outbound` — one row per advisor on the message, so
one person replying does not mark the others as engaged.

App sends are distinguished from manual ones by the `X-EIC-Message-Id` header
the emailer already stamps on every draft: `source` is `app` or `outlook`, and
an app send carries its campaign message id. The header is used rather than the
`EICMessageId` extended property because headers are already fetched for
auto-reply detection, where the extended property would need its own `$expand`
on every message read.

**The Cc question is decided: a Cc counts as contact.** A rep copying somebody
is nearly always copying a member of the same practice, and touching the team is
what matters — so it resets the quiet clock like any other contact. It is
*tagged* rather than discounted: `recipientRole` is `to` or `cc` on every
outbound row, because "written to" and "copied" are different facts about a
relationship and a timeline should be able to say which.

Remaining: an app send is only seen once the sweep reaches Sent Items, so there
is up to a 15-minute lag before it appears on a timeline. Replies and follow-ups
sent from the app record themselves immediately and do not have this lag.

### Phase 4 — activity timeline — DONE (email only)

An **Email activity** section on the advisor profile, filled after the panel
draws — the same lazy pattern as registration history, because making every card
wait on a round trip to serve a section most reps will not read is the wrong
trade.

    GET /api/email?op=activity&crd=...            the timeline
    GET /api/email?op=activity_message&crd=&id=   one message's text, on click

`shared/email-activity.js` owns both. Three decisions worth keeping:

  * **The timeline is shared; the content is not.** Rows from every rep are
    visible, because "Kate already emailed them on Tuesday" is what stops two
    people working the same advisor in the same week — the same reasoning that
    makes `/api/flags` and `/api/dnc` firm-wide. A message can only be OPENED by
    the rep whose mailbox holds it, checked *before* the Graph call so the
    refusal explains itself instead of arriving as a 404 that reads as
    "deleted".
  * **The wording is computed on the server.** Whether something counts as a
    reply is a judgement the desk and the phone must not make differently —
    `label()` and `basis()` live in one place, for the same reason
    `display_name.py` does. A `sender_only` sighting reads "Email received",
    never "Reply received".
  * **`uniqueBody`, as plain TEXT.** This is mail written by people outside the
    firm being rendered inside our own page. Text removes the vulnerability
    class rather than defending against it: there is no markup to sanitise, so
    there is nothing for a sanitiser to get wrong. The client escapes it again
    on the way to the DOM. Verified in the browser with an `<img onerror>`
    subject — zero tags created.

`Open in Outlook` uses the message `webLink` for anything beyond reading. It
cannot be iframed, so it opens Outlook itself.

Audit: *advisor-authored email is fetched as text, never as HTML*, *reading a
message checks whose mailbox it is first*.

**Now on the phone too.** `field.js` carries the same timeline in a collapsed
`Email activity` block, fetched on first open — the same discipline as `Full
history`, because a rep opening a card is usually about to dial and nothing may
come between them and the number. Narrower by design: date, what happened,
subject. Tap targets are 44px, and the year is elided for current-year dates
because a phone row has no width to waste.

The wording is the server's in both clients, pinned by the audit check *the desk
and the phone use the server's word for what happened*. This is the same
divergence that made Cosmo Boyd findable on the desk and invisible on the phone,
and it is now impossible to reintroduce by accident.

**Still email-only.** The plan's larger idea — one `Activity` model spanning
calls, notes and follow-ups, so an advisor has a *relationship* history rather
than an email history beside a call history — is not built. `CallLog` is
untouched and still renders separately.

### Phase 5 — engagement state and work queue — DONE

`shared/email-engagement.js` folds a rep's activity into one row per
(rep, advisor) in `EmailEngagement`, refreshed by the sweep for **only the
advisors it touched**. A cache, not a second source of truth: `fold()` is a pure
function of the log and everything in it is regenerable, except the rep's own
decisions (`replyState`, `actedAt`, `nextActionAt`) which are carried across.

Five notification reasons, in this order:

    batch_held       batch held/stopped -- review required
    batch_followup_due  the rep's campaign reminder is due
    reply_followup   follow-up needed
    due              follow-up due
    bounced          address needs fixing

Ordinary advisor replies are deliberately **not notifications**. Outlook is the
rep's inbox and response surface; repeating each message here makes the app a
second, worse inbox. Replies remain in the durable activity log, relationship
timeline and engagement projection, and still remove that advisor from a batch
follow-up. Likewise, `quiet_warm` remains a useful relationship/filter signal
but is not allowed to fill the notification badge automatically.

**Exactly one reason per advisor**, plus one row for each actionable batch. A
queue that lists somebody three times is a queue a rep stops reading. Within a
reason, longest-waiting first — that is the one most at risk of being forgotten.

Reply work still has states for explicit one-person follow-ups and durable
projection semantics:

    none -> new -> reviewed -> follow_up -> scheduled -> done

`actedAt` distinguishes a reviewed conversation from a genuinely newer reply.
That state is still important even though `new` alone no longer creates an app
notification.

**No volume metric anywhere**, and the audit enforces it — *the work queue is
built from engagement, never from volume*. "117 emails sent this week" rewards
sending, which is the behaviour the 25-a-day limit exists to restrain, and a
dashboard read every morning beats a limit met once a day. `outbound30d` is
stored as profile context but `reason()` may not read it.

`quiet_warm` still fires only for advisors who have **replied at some point**,
and `done` is deliberately not excluded from that signal. It belongs in
relationship filters or a deliberate daily-work view, not a badge that can grow
without a rep asking it to.

UI on **both** clients: a flag button opens **Needs attention** — headline counts
of things needing doing, then advisor and campaign rows. A due campaign opens a
fresh recipient calculation and follow-up review; a held batch opens its review
screen. The list reloads after an advisor action rather than hiding the row,
because the advisor may still be in the queue for a different reason.

Campaign reminders are measured from the **last actual delivery** in the batch,
so a multi-day campaign cannot ask for follow-up while its final tranche is
still going out. Candidate arithmetic is recomputed twice: when review opens and
again when the derived batch is created. Human replies, hard bounces, later
opt-outs, unsent rows and messages whose original is unavailable in Outlook all
come off the list. The reminder choice and parent link are stored on initial
create; the parent is ETag-claimed before the child becomes editable; duplicate
tabs have one winner; partial writes are canceled and release the claim; and a
stale interrupted build can be retired safely on retry.

The field version carries a count on the flag itself, so a rep between meetings
can see there IS something without opening it. It is **silent at zero and silent
on error** — a badge showing a number when the queue could not even be read is
worse than no badge, because a rep would trust it. Tapping a row goes straight to
the advisor's card, or says plainly that their area's tile is not loaded, which
is the same rule the dialer already uses.

    GET  /api/email?op=queue_work
    GET  /api/email?op=follow_up_candidates&id=BATCH_ID
    POST /api/email  { op: "create_follow_up", batchId, text, includeAttachments }
    POST /api/email  { op: "reply_state", crd, state }

### Phase 6 — in-app reply — DONE

`shared/email-reply-send.js`. A rep reading a reply can answer it there; the
alternative — read here, switch to Outlook, find the thread, answer — is enough
friction that the reply happens later or not at all.

**Graph `createReply`, never a fabricated `RE:`.** This is the load-bearing
decision. A new message with `RE:` prefixed looks right to a human and is a NEW
conversation to every mail system involved, which would silently cost us the
strongest matching route we have: when the advisor answers, `conversationId`
leads nowhere and the sweep drops to references or sender-only. **Building the
feature the obvious way would have degraded the feature underneath it.** It also
fails better — anything going wrong mid-compose leaves a real Outlook draft in
the rep's own mailbox rather than text lost inside our page.

**The same gates as the bulk sender.** Suppression is checked before any draft
exists, and the compliance blind copy comes from `core.complianceBcc()` — the
same function the worker uses. A hard bounce or an opt-out is a statement about
the *address*, not about which screen the mail was typed on, and a route with
fewer checks would become the route everything used.

**Follow-up starts a NEW conversation.** The queue produces `quiet_warm` —
somebody who answered once and went quiet — and reviving that on the old thread
would make the advisor read a three-month-old conversation to work out why we
are writing. A blank sheet, no template: a templated re-engagement reads exactly
like what it is. The signature still comes through, because that is firm
identity rather than content.

**The follow-up recipient is never sent by the client.** It is taken from the
advisor's own activity log, so the endpoint cannot be used to mail an arbitrary
address from a rep's mailbox. An advisor with no observed address is refused
rather than guessed at.

**Attachments, from two sources.** The approved document library, and a file off
the rep's own device — the second never touches our storage, arriving on the
request and going straight onto the draft.

What makes an uncurated file acceptable is that `core.complianceBcc()` fires on
**any** attachment to an external recipient, so material reaching an advisor
reaches the compliance mailbox by the same mechanism whether it was sent to one
person or a hundred. An earlier version of this file passed `attachments: []`
into that function, which silently disabled the obligation; it now receives the
real list, and a test asserts the copy is applied *before* the send, because a
blind copy added afterwards copies nobody.

Limits: 5 attachments, the configured `EMAIL_MAX_ATTACHMENT_BYTES` in total,
enforced on the bytes actually received rather than on a size the browser
claimed. `attachmentFileName()` no longer forces `.pdf` on a rep's own file —
that rule exists for the approved library, whose names carry no extension, and
applying it to an upload turns `forecast.xlsx` into `forecast.xlsx.pdf`, which
is the exact bug it was written to prevent.

Deliberately not a mail client: plain text, no formatting, 5,000 characters.
`Open in Outlook` is one tap away and is the honest answer to anything larger.
The reply composer is offered only on **inbound** mail — replying to our own
send would mail ourselves.

The send is recorded immediately rather than waiting up to fifteen minutes for
the sweep, because a rep who presses Send and sees no change concludes it failed
and sends again. This immediate row uses API completion time, while the sweep
later sees Graph's canonical `sentDateTime`; they can differ and therefore form
two timestamped rows today. Direct send remains disabled until its durable
operation ledger can reconcile one canonical sent event.

Both clients disable the button and textarea for the whole round trip. Verified
in-browser: a double-click sends exactly one message. A sent email has no undo.

Audit: *an in-app reply keeps the thread and passes the same gates as a
campaign* — now covering BOTH paths, verified against four ways of breaking it
(reply skipping the suppression gate, follow-up skipping compliance, the gate
helpers gutted, and follow-up reusing `createReply`).

    POST /api/email  { op: "reply_send", crd, id, text, replyAll, documentIds, files }
    POST /api/email  { op: "follow_up",  crd, subject, text, documentIds, files }

Entry points: the reply composer sits under a message in the timeline;
**Follow up** sits on every work-queue row and on the advisor profile, on both
clients. Putting it on the queue row is the point — without it, a `quiet_warm`
row tells a rep to re-engage somebody and gives them nowhere to do it.

**Not built:** templates in the one-to-one composer. Replies and re-engagement
are personal, and a template reads like a template. Worth revisiting only if
reps ask.

### ~~Phase 7 — background service~~ DELETED

Unnecessary. Delegated tokens already run unattended in production.

## Audit checks

Built:

    a Graph message field we select is a field we can store
    the advisor email lookup agrees with the contacts it was built from
    no mailbox reader is scoped to a single folder
    the reply sweep cannot run on the bounce sweep's minute
    the reply sweep stores no message body
    advisor-authored email is fetched as text, never as HTML
    reading a message checks whose mailbox it is first
    the desk and the phone use the server's word for what happened
    the work queue is built from engagement, never from volume
    an in-app reply keeps the thread and passes the same gates as a campaign
    a partial write never uses Replace
    the store is tested against real Table Storage semantics

Each was verified by deliberately breaking the thing it guards and confirming it
reports. One of them caught a false positive in itself on the first run --
`email-store.js` has two `const strings = [...]` whitelists and the check matched
`patchBatch`'s -- which is the argument for testing a check rather than trusting
that green means working.

Still to add, once there is data to check:

- **every rep's sweep watermark has advanced within the expected window**
- **every connected rep's token is usable, and `needsReconnect` reps are
  surfaced to the rep rather than only skipped**
- auto-replies and bounces never count toward anything the UI calls a reply
  (enforced in code and covered by tests; needs a UI to check against)
- the derived state record is reproducible from raw events (phase 5)

The two bold ones are silent-blindness failures: the feature keeps rendering,
nothing errors, replies simply stop being found. Same shape as `TABLES` missing
`contactflags` and the Baird truncation. Alarm, do not merely log.

## Safeguards around token health

`email-auth.js` already sets `needsReconnect` on `InteractionRequiredAuthError`,
and the bounce sweep already skips those reps without alarming — correct for
bounces, **wrong for replies**. A rep whose token lapsed silently stops getting
reply detection while their screens keep saying "no reply recorded".

So:

- **Surface `needsReconnect` to the rep**, not just to the sweep. The fix is that
  they sign in, which they do anyway — this turns a support ticket into a click.
- **Degrade the claim.** If a rep's sync is stale, their views must stop asserting
  "no reply recorded" as though it were observed.
- **Alarm on staleness, not errors.** Errors throw and get noticed; a sweep that
  stops running throws nothing. Measure last-successful-sync against wall clock.
- **Test it.** Revoke a test rep's sessions and confirm the system notices and
  says so. An alarm never seen to fire is an assumption.

## What the review found

### The systemic lesson, which matters more than any single bug

**198 tests passed while two of the worst defects were live**, and both were the
same mistake: writing a partial entity with Azure Table Storage's `Replace`
mode, which deletes every property absent from the payload.

    putSweepState()   the error path wrote {lastError} alone and erased
                      watermarkUtc -- one token lapse sent that rep back to a
                      48-hour window
    putEngagement()   wrote a folded state with no actedAt and erased it, so a
                      reply a rep had handled came back as new

The hand-written mocks could not catch either, because they recorded what was
WRITTEN and modelled nothing about what a write DESTROYS. A mock that merges
where the real store replaces does not merely miss the bug — it certifies it.

`test/helpers/fake-table.js` now implements the semantics that actually bit:
Replace vs Merge, ascending `(partitionKey, rowKey)` iteration, and rejection of
the characters Azure forbids in a key. Two audit checks pin it — *a partial
write never uses Replace* and *the store is tested against real Table Storage
semantics*. The first caught a second instance of the bug the moment it was
written, in `putEngagement`, which had been left on Replace.

### Blockers, all fixed

**1. A failed sweep erased its own watermark.** `putSweepState` now Merges.

**2. Truncation deadlock.** The mailbox read was newest-first and the watermark
only advanced on a *complete* window — so a busy mailbox returned the same
newest pages forever while older replies behind them were never reached, and the
watermark could never move. It would have looked like a working sweep that
simply never found those replies. Reading is now **oldest-first**, which makes a
truncated pass a contiguous block from the watermark; it advances to the last
message actually processed, so progress is always made and nothing is skipped.
An *error* still holds the watermark, because an error leaves a hole.

**3. Direct-send activity is not yet fully idempotent.** The key contains both
reverse `occurredAt` and a hash of the Graph id. An in-app send stamps API
completion time while the sweep stamps Graph's `sentDateTime`, so the disabled
direct path can still produce two rows. Reverse ticks fixed newest-first reads;
canonical operation-to-sent-event reconciliation belongs to the durable ledger
and is a prerequisite for enabling direct send.

**4. `listActivity` returned the OLDEST rows.** It stopped at a row count while
reading ascending keys that began with a forward timestamp, then sorted what it
had. A busy advisor's timeline could show nothing from the last month, and every
consumer inherited it — the fold, ownership checks, and which address a
follow-up went to. Reverse ticks make the cut correct.

**5. `actedAt` was deleted on every refresh.** `fold()` read it and did not
return it. Now carried, with a test that runs **two persisted refresh cycles**
rather than one isolated fold.

**6. One-to-one sends were not retry-safe.** Both paths sent and then recorded;
a lost response meant a retry sent a second email. The client now generates an
`operationId`, the draft is stamped with it, and `alreadySent()` reconciles
before doing anything — the campaign worker's pattern.

### Reply-all, and a policy decision

The review said reply-all bypasses suppression and should be blocked. **It is
allowed instead**, deliberately:

> A suppression means "do not send this address marketing". Answering a message
> somebody sent us — or replying on a thread they were already part of — is not
> marketing. Refusing to answer an advisor because they once unsubscribed is
> unhelpful and slightly rude.

So the rule now depends on who started it:

    reply / reply-all   ALLOWED to a suppressed address, and the rep is told
    follow-up           BLOCKED -- that is us initiating contact with somebody
                        who asked us not to

The review's *other* concern survives independently and is addressed: reply-all
pulls in the original's To and Cc, which may include an assistant, the advisor's
own client, or somebody internal to their firm. Ticking **Reply to all** now
resolves and displays exactly who it reaches, marking any suppressed address.
Disclosure rather than refusal — which is what Outlook would have done.

### Also fixed

  * **Non-advisor mail is no longer recorded at all.** The sweep wrote every
    unrelated message id to `replySeen`, contradicting this document's own
    privacy claim and growing without bound. Dropped; the ten-minute overlap
    costs an in-memory Map lookup instead.
  * **Graph ids are hashed before use as row keys.** They are base64-flavoured
    and can contain `/`, which Azure rejects outright — `clean()` only truncated.
  * **The advisor lookup has a TTL** (15 minutes, `ADVISOR_LOOKUP_TTL_MS`) and
    keeps a stale copy if a refresh fails, rather than caching forever and
    ignoring a fresh upload until the process recycled.
  * **In-app sends refresh the projection immediately**, so the queue stops
    saying "needs attention" about a reply just answered.
  * **Queue actions now match queue reasons.** `Snooze` is the missing verb —
    `Done` deliberately cannot clear a quiet contact or a bounce, so without it
    a rep had no way to set either aside. A bounced row offers *Address is fine*
    rather than a `Follow up` that suppression would reject.
  * **`nextActionAt` / `scheduled` / "Follow-up due" now have an API and UI.**
    They existed in the model with no way to set them, so that reason could
    never fire — Phase 5 was not actually complete.
  * **`rebuild()` now exists.** This file and the module docstring both claimed
    it did.
  * **Clients check `response.ok`** on queue writes, instead of reloading as
    though a failed write had succeeded.
  * **The field badge refreshes on resume**, which this document had flagged and
    left undone.
  * **The newest address wins.** `fold()` overwrote `advisorEmail` on every row
    while rows arrived newest-first, so it kept the OLDEST — and a follow-up
    would have gone to an address the advisor left years ago.

### Still open

  * **Ambiguous and firm-level rows are orphaned.** They are partitioned under a
    firm CRD or `unknown` while timelines query an advisor CRD, so the
    "unattributed" marker in the UI is largely unreachable and the promised
    manual-resolution workflow does not exist. Either build an unattributed
    inbox with a link-to-advisor action, or remove the marker.
  * **Projection failures are still swallowed** rather than retried. A durable
    dirty-flag or a periodic `rebuild()` sweep is the fix; `rebuild()` now
    exists to make the second easy.
  * **Out-of-scope queue navigation is manual** — the row tells a rep to switch
    states rather than loading the right scope for them.
  * **Azurite.** The faithful double catches the class of bug that got through,
    but a real emulator would also cover query semantics, batch limits and
    concurrency. Worth adding before the next storage-backed feature.

## Our own people are never tracked

18 of EIC's own registered reps appear in the SEC feed, so they are on the map
like any other advisor — and the activity timeline is **firm-wide**. Left alone,
every rep would have been able to read when their colleagues emailed each other,
which is nobody else's business and far more likely to be sensitive than
anything an advisor sends us.

Enforced in three places, deliberately:

    export        internal addresses are excluded from byEmail, byCrd, byDomain
                  and ambiguous, so the sweep cannot recognise them at all
    runtime       classifyAddress() rejects any address whose domain is in
                  EMAIL_INTERNAL_DOMAINS, read fresh on every sweep
    UI            the section says "a colleague, not a prospect" and the
                  follow-up button is hidden

**The runtime check is the boundary; the export is an optimisation.** The export
ran at some point in the past with whatever `EMAIL_INTERNAL_DOMAINS` was set to
*then*; the runtime reads it as it is *now*. So adding a domain — a new office,
an acquisition, a dba — takes effect on the next sweep rather than the next
pipeline run, and somebody hired this morning is never tracked even though the
blob predates them. A test covers exactly that case: a colleague still listed in
a stale blob leaves no trace.

`internalCrds` is published so the UI can SAY so. An unexplained empty timeline
looks like a bug, and somebody would eventually "fix" it.

## Follow-up could not reach anybody, and why

A rep testing the feature got:

    "No email address has been observed for this advisor, so a follow-up cannot
     be sent from here."

`followUp()` took its recipient from the **activity log** — never from the
request, so a crafted call could not turn the endpoint into a relay. Right
instinct, wrong source: the log is empty until the sweep has observed somebody,
so on day one a follow-up could reach nobody at all. The message described the
symptom and blamed the advisor; the cause was that the code had nowhere else to
look.

The export now also emits **`byCrd`** — CRD to address — and the server resolves
the recipient itself. The client still names only a CRD, so the security
property is unchanged and there is now nothing to verify. It cost 1.4 MB on a
blob read once per cold start. The activity log remains as a fallback, which is
worth keeping: an advisor who wrote from an address the pipeline does not hold
is still reachable at the one they actually used.

## Backfilling history

`POST /api/email { op: "backfill", days: 365 }`, per rep, on their own mailbox.

There is **no separate backfill job**. It moves that rep's watermark back and
records how far forward the catch-up runs; the ordinary sweep does the rest a
page at a time. That only works because reading is oldest-first and the
watermark advances to whatever was actually processed — the same change that
fixed the truncation deadlock. A year of mail is a few hours of ordinary runs.

**History arrives already handled.** Without that, a year of backfill would mark
every reply in it `new`, and a rep would open "Needs attention" to four hundred
rows from eight months ago and never trust the queue again. While catching up,
the projection is seeded so imported replies land on the timeline in full
without presenting themselves as work nobody did. Replies arriving *after* the
catch-up surface normally, and a test pins both halves.

Seeding exposed a genuine logic bug: `fold()` forced `new` whenever the state
was `none`, regardless of `actedAt` — so nothing could ever mark a reply as
accounted for unless somebody had already given it a state. It now becomes
`reviewed`, which is what "seen, but never triaged" actually means.

**Not ten years.** The filter is built from *today's* advisor universe, so a
message from 2019 sent to an address at a firm that person has since left will
not match anything and is discarded — you would pay the full scan and get less
back the further you reach. Many firms also retain mail for only 3–7 years, and
an advisor who last replied eight years ago is not "warm, no contact in a
while", they are cold. Start at one year, measure the match rate, and extend if
it holds up. Extending later is cheap: re-scanning skips what is recorded and
re-assessing a miss is one in-memory lookup.

## A CRM mis-link found and fixed alongside this

Not part of the activity layer, but found by an audit check that had been
failing throughout and is worth recording because the fix generalises.

Five advisors were linked to the WRONG Act! contact — a different person sharing
their surname at the same firm. They were not blocked; they were **actively
linked**, so a logged call, a history read or a do-not-email mark would have
been written onto the namesake's record. Correctly attributed, entirely
plausible, unfindable afterwards.

    2189526   Arthur Rollins    art_rollins@ml.com       ->  Lorin Rollins    lorin.rollins@ml.com
    4056289   Janice R Cope     janice.cope@ml.com       ->  S. Cope          brent_cope@ml.com
    4824978   Jed Dolce         jed.dolce@ubs.com        ->  Donn Dolce       donn.dolce@ubs.com
    5231410   Ashley D Brunson  ashley_brunson@ml.com    ->  April Brunson    april_brunson@ml.com
    7474801   Elizabeth Gautier elizabeth.gautier2@ml.com -> Louis Gautier    andrew.gautier@bofa.com

They survived the existing rule because that rule forgives a name disagreement
when the match won by a wide margin — and these won by 0.52 to 0.82, because the
runner-up was the actual advisor, scoring almost identically on the surname,
firm and city they share. **The margin cannot separate them.** The email address
can, and the firm wrote it.

`build_act_lookup.py` now consults the address as a second, independent signal:
a pair is dropped when the name disagrees AND the given-name part of the two
addresses disagrees, with the surname excluded before comparing (or every one of
these looks like agreement — `rollins` is on both sides).

**Deliberately not "drop when the emails disagree".** 32 pairs do, and 27 are
one person with two address formats — `gregory.delmonte`/`gdelmonte`,
`bob.robinson`/`rrobinson`. Their names agree, so they never reach the rule. It
takes both signals failing, which is exactly the five and nothing else.

Result: 26,342 syncable CRDs (down 5), and the audit check passes for the first
time. Those five now log locally and do not reach Act! — which is the correct
degraded state, and the same one 21,124 unmatched advisors are already in.

## Durable engagement repair checkpoint

Every advisor-level activity write now carries its projection-repair obligation
in the same `EmailActivity` partition. The activity row and one dirty marker per
`(advisor, rep)` are committed in a single Table transaction, so a crash cannot
leave durable activity with no way to rebuild its queue projection. Inline
refresh conditionally deletes the marker; new activity changes the marker ETag
and makes that delete fail safely. A disabled, user-allowlisted timer drains any
markers left by storage conflicts or process failures.

Historical-import provenance is stored on each event using the backfill request
cutoff, not the later repair time. Historical replies still establish the
relationship and appear on the timeline, but they do not become unread work;
historical bounces do not become active address work; and old warm contacts use
the import cutoff as their quiet baseline. Current events at or after the cutoff
remain actionable. Projection folds now query the complete activity set for one
rep and advisor instead of taking 500 advisor-wide rows and filtering afterward.

This repairs projection failures only after an activity row exists. It does not
close the post-Graph/pre-activity window in direct send; the operation ledger is
still required before direct send can be enabled.

## Durable one-to-one send checkpoint

Replies and individual follow-ups now use a separate `EmailDirectSendOps`
state machine and `email-direct-work` queue. The HTTP request performs only the
reversible work needed to build a complete Outlook draft, stamps the operation
identifier last, records the immutable draft ID, and returns `202`. The worker
rechecks the kill switch, canary, suppression, Eastern daily capacity, and mailbox pacing
immediately before it conditionally enters `submitting` and calls Graph once.

`submitting` is the irreversible boundary. A timeout, network failure, Graph
5xx, or expired worker lease becomes `ambiguous`; it never becomes `prepared`
again and is never automatically submitted again. Reconciliation looks for the
stamped non-draft item. Only Graph's immutable ID and `sentDateTime` create the
activity row, so the later mailbox sweep converges on the same event. Activity
or projection failures leave the operation `reconciled` for bookkeeping retry
without touching `/send`.

The operation and its `q|` outbox marker are created atomically in one user
partition. Queue messages contain only version, work kind, user ID, and
operation ID. Tables and browser storage contain identifiers, hashes, states,
and timestamps—not bodies, recipient lists, attachment bytes, or access tokens.
Outlook remains the content store. A default-off, user-allowlisted repair timer
dispatches stranded identifiers and converts expired `submitting` leases to
`ambiguous` before enqueueing reconciliation.

## Before this can run

0. `dist/api.tgz` must be rebuilt — `api/shared/act_contacts.json` changed with
   the CRM mis-link fix above and ships inside the API package.
1. `pip install azure-storage-blob` then
   `python src/export_advisor_emails.py --upload` — the sweep fails closed
   without the blob, deliberately, so this is a hard prerequisite.
2. Deploy the API with `EMAIL_REPLY_SWEEP_ENABLED`,
   `EMAIL_ENGAGEMENT_REPAIR_ENABLED`, `EMAIL_DIRECT_REPAIR_ENABLED`,
   `EMAIL_DIRECT_SEND_OPS_ENABLED`, and `EMAIL_DIRECT_SEND_ENABLED` still
   disabled. Configure a new secret `EMAIL_DIRECT_SEND_HMAC_KEY` (at least 32
   random bytes) before testing, but do not print it or commit it. Verify release
   provenance and storage health before enabling any workflow.
3. Inspect whether production `EmailActivity` already contains pre-marker rows.
   If it does, audit their backfill/decision provenance before migration; do not
   silently reinterpret all old rows as current or historical.
4. Set `EMAIL_ENGAGEMENT_REPAIR_USER_IDS` to one connected test user's Static
   Web Apps user ID, then set `EMAIL_ENGAGEMENT_REPAIR_ENABLED=1`. An unmatched
   allowlist claims no markers. Keep the allowlist in place through the reply
   canary.
5. For the reply-sweep canary, set `EMAIL_REPLY_SWEEP_ENABLED=1` and
   `EMAIL_REPLY_SWEEP_USER_IDS` to one connected test user's Static Web Apps
   user ID. An unmatched allowlist intentionally reads no mailbox. Clear the
   allowlist only after the repair backlog and reconnect metrics stay healthy.
   `ADVISOR_LOOKUP_CONTAINER` / `ADVISOR_LOOKUP_BLOB` default to
   `lookups` / `advisor_emails.json.gz`.
6. For the one-to-one canary, set the same single user in
   `EMAIL_DIRECT_SEND_OPS_USER_IDS` and `EMAIL_DIRECT_REPAIR_USER_IDS`; enable
   `EMAIL_DIRECT_REPAIR_ENABLED`, then `EMAIL_DIRECT_SEND_OPS_ENABLED`, and only
   then `EMAIL_DIRECT_SEND_ENABLED`. Keep `EMAIL_TEST_ADDRESS_ALLOWLIST` on the
   controlled external address. Exercise a normal reply, Graph timeout/5xx,
   duplicate queue delivery, and a finalization failure. Confirm that uncertain
   operations say **Do not resend; verify in Outlook** and produce one Graph
   `/send` call at most. Disable either send switch immediately if they do not.
7. Expand the user canary only after `ambiguous`, oldest-marker, retry, and
   poison-queue observations remain healthy. Do not clear the allowlists merely
   because a normal send worked.
8. Watch the first sweep log line: `scanned` should be large, `ours` small. If
   `ours` is near `scanned`, the filter is wrong and should be stopped.

## Open questions

- ~~Do reps rule advisor mail out of Inbox?~~ Moot. Nothing is folder-scoped.
- ~~Does `messages.contactId` resolve to advisor CRD?~~ Yes.
- Advisor replies from an address we do not hold — personal, or an assistant.
  `byDomain` now catches the case where the address is at a known firm domain
  (recorded as firm-level, person unnamed). A genuinely personal address still
  misses; `matchMethod = manual` is the intended escape once there is a UI.
- Two reps mailing the same advisor produce two rows for one thread. Dedupe on
  `internetMessageId`, or accept per-rep rows since the timeline is per-rep.
  Leaning accept.
- `sentByConversation()` scans a rep's whole message partition on every sweep.
  Fine at current volume; if it becomes slow, cache it per run or index it.
- The first sweep after enabling reaches back 48 hours and may well report
  `truncated`. That is now SAFE rather than a stall: reading is oldest-first, so
  the run advances to the last message it processed and the next run continues
  from there. It may take several runs to catch up on a busy mailbox, and each
  one makes real progress.
- ~~The timeline is not in the FIELD app~~ Built.
- ~~The work queue is desk-only~~ Built on both.
- The field badge is loaded at startup, on each open and when the app becomes
  visible again. It is not a real-time inbox and deliberately does not count
  individual replies.
- ~~No Follow up, no attachments~~ Both built. Templates deliberately not.
- A Cc now resets the quiet relationship signal, by decision. If it turns out
  reps copy people they are not really working, `quiet_warm` could be narrowed
  to `recipientRole = "to"` — the tag is already stored, so that is a one-line
  change with no migration.
- Uploaded attachment bytes travel as base64 in the JSON request body, which
  inflates them by a third. Fine for a fact sheet; if reps start sending large
  files this wants an upload session instead.
- `activityOwner()` scans one advisor's partition per View click. Small today;
  if an advisor accumulates hundreds of rows it should become a keyed lookup.
- `listEngagement()` reads a rep's whole partition per queue open. Fine at a few
  hundred advisors per rep; beyond that the queue wants a filtered query or a
  pre-computed "needs attention" flag on the row.
- ~~`refresh()` reads 500 advisor-wide rows before filtering the rep.~~ Fixed.
  Projection folds now page the complete user/advisor source set. Timeline reads
  retain their small newest-first limit because they are presentation, not a
  correctness boundary.
- The engagement projection is refreshed inline by reply sweep and direct
  reply/follow-up, and repaired from durable markers by the disabled timer. An
  advisor whose queue reason is purely time-based (`quiet_warm`, `due`) needs no
  write-time refresh because `reason()` evaluates stored timestamps at read
  time. Revisit that only if reasons move to write time.
- Direct reply and follow-up now write activity immediately so the UI updates
  after Send; reply sweep remains the discovery/reconciliation writer for
  Outlook and campaign mail. Graph's immutable id makes repeated observations
  idempotent when their event timestamps agree. Stable sent-time reconciliation
  belongs with the direct-send ledger.
