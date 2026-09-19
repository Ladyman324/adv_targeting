# Email reliability and incident recovery

The Graph mail transport uses an Azure Table lease keyed by tenant and mailbox.
Drafts, attachments, sends, reconciliation, and reply/bounce scans using that
transport share one concurrent request and a mailbox-wide cooldown. Requests
hold the lease until their response body is consumed. The lease exceeds the
bounded HTTP timeout; an abandoned lease expires automatically. Worker ownership
is renewed and checked before Graph requests, including attachment chunks.

Actual send requests retain a minimum ten-second interval. Reads and draft
preparation do not inherit that interval. HTTP 429 responses honor Retry-After
(seconds or HTTP date); missing headers use a conservative fallback. Cooldown
and lease deferrals do not consume the campaign failure budget.

Campaign messages persist sendOutcome, sendStartedUtc, and sendAttemptId BEFORE
calling send. A definite rejection or non-dispatch can return to the send queue.
A lost response or failed storage write after acceptance remains uncertain:
recovery only reconciles and does not submit again. A successful lookup showing
a sent item confirms submission, not delivery to the recipient's inbox.

Draft creation intent is also persisted. If its response is lost and lookup
finds nothing, keep checking rather than create another draft. After 24 hours,
unresolved draft/send outcomes require investigation. Read errors never constitute
proof that Outlook has no draft or sent message. Reconciliation backs off up to
15 minutes between checks and records the next due time durably.

Keep EMAIL_MAILBOX_INTERVAL_SECONDS=10 and EMAIL_CAMPAIGN_REPAIR_ENABLED=1.
An empty EMAIL_CAMPAIGN_REPAIR_USER_IDS enables recovery for every connected user;
a nonempty value deliberately restricts coverage. Recovery covers approved,
active batches and retains existing day/capacity and suppression safeguards.
It does not retry existing terminal failures or replay unknown submissions.

EmailAudit failure records include method, sanitized operation, HTTP status,
Graph/client request IDs, duration, Retry-After, and persisted send outcome.
Never log tokens, request bodies, upload-session URLs, or recipient addresses
for transport diagnostics.

Release validation includes concurrent hosts sharing a mailbox, independent
mailboxes, spacing, cooldown persistence, stale-owner release, GET timeout,
definite 429 rejection, lost POST response, failed post-send database write,
duplicate queue hints, and reconciliation past the old six-attempt ceiling.
Run an explicitly approved internal-mailbox batch after deployment before using
real recipients for acceptance testing.

For the September 18 incident, leave terminal records untouched during deployment.
The three unknown sends need Outlook/Sent Items or Exchange trace verification;
the seven draft failures need reconciliation before any controlled retry.
Do not manufacture a new batch or mark items sent from error codes alone.
