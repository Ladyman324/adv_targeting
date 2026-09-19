# Emailer: operating guide and reliability walkthrough

## Using the new batch review controls

Open Email activity, open the batch, then choose **Check status & retry** (also
available beside Review messages on the completed-batch screen).

1. Select the relevant rows. Nothing is selected automatically.
2. Choose **Check sent status**. This makes read-only Outlook requests and saves
   the result and check time. It does not create, change, or send Outlook mail.
   Positive evidence that the original message was sent updates the app's state.
3. Read both the application status and Outlook result. A found original draft,
   missing item, unavailable check, and confirmed sent item are different outcomes.
   Missing is NOT proof of unsent. A check failure invalidates earlier draft evidence.
4. If a representative composed a separate replacement, select the row and choose
   **Already sent manually**. Confirm the action. The app stores the actor's user
   ID and timestamp, displays Handled manually, and excludes it from this batch's
   future sending. This is the representative's statement, not verified delivery.
   Pause or cancel active work first; wait for any leased/in-progress work. Nothing
   here recalls a request already submitted to Microsoft. Existing Outlook drafts
   are not deleted, and the exclusion does not apply to separately created batches.
5. For retry, select only rows the server lists as eligible. Choose **Retry selected**
   and confirm that these messages have NOT already been sent outside the app.
   Send-mode batches queue sending; draft-only batches only prepare Outlook drafts.
6. **Reload status** reads saved application state only. **Check sent status** is
   the distinct action that queries Outlook again. In-flight processing remains
   owned by the background workers; closing the browser does not stop it.

Retry is deliberately conservative. It requires positive original-draft evidence
checked within ten minutes, an allowlisted safe failure, no bounce/manual exclusion/
uncertain submission, a connected mailbox, and no suppressed recipient. A pending
or running batch cannot be bulk-resumed through this action. All selected rows are
validated before starting; concurrent changes can cause individual rows not to queue,
and results say so. Worker-side safety checks still run again before sending.
Terminal unknown sends are never unlocked by a draft or empty search result.

An expired or future reserved capacity day cannot be overridden by Retry selected.
For an expired day, review the safe unsent recipients in a new batch with a new
delivery plan. Do not recreate uncertain or manually handled messages. A failure
before any traceable Outlook draft exists may also require a new reviewed batch;
the UI does not infer absence from a failed search.

## What happens from composition to completion

1. **Sign-in and mailbox connection.** Application sign-in controls access. A
   separate delegated Microsoft 365 connection lets the backend work in that
   representative's mailbox. Tokens stay encrypted server-side. Reconnection is
   required when Microsoft demands interaction; workers do not bypass MFA.
2. **Compose and persist.** EmailBatches holds batch-level choices; EmailMessages
   holds one personalized message per recipient. Templates, attachment variants,
   and approved recipient identity/routing determine the final To/Cc/Bcc envelope.
   Unsaved/unapproved editing work is not eligible for automatic sending.
3. **Validate and preview.** The server verifies identity, required material and
   current versions, merge fields, suppression, copies, policy, and capacity. The
   user reviews the exact content, attachment set, recipient count, and delivery plan.
4. **Approve and reserve.** Approval persists the mode and each message's planned
   send time/day. The production cancellation window is 20 seconds; the plan can
   defer work further. Production capacity is 25 external envelope recipients per
   representative per Eastern business day, including external teammate Cc copies.
   Oversized work is distributed over available days rather than ignoring the limit.
5. **Publish work hints.** The API writes durable state and publishes identifiers
   to the email-work Azure Queue. The browser is no longer required. The queue
   delivery is a request to inspect the durable state, not unconditional permission
   to send. Duplicate queue deliveries are expected and must be harmless.
6. **Scheduled preflight, when applicable.** Explicit scheduled batches run a
   preflight at their due time. Changed identity, unavailable/current material,
   mailbox or capacity problems put work on hold for review rather than silently
   sending stale content. A hold-notification task can notify the representative.
7. **Draft worker.** Wait for the planned time; fail closed if the reserved Eastern
   day has passed. Claim the message with a conditional storage write and expiring
   ownership ID. Check current identity/materials, look for the tracked Outlook
   message, persist draft-creation intent, create/reuse the original draft, and
   attach the selected documents/images. Draft-only mode ends at Draft ready.
   Send mode durably queues the next phase.
8. **Shared Microsoft request gate.** Graph mail operations using the shared
   transport coordinate by tenant/mailbox in EmailPolicy. Only one request owns
   that mailbox gate at once across Azure hosts. Actual sends are at least ten
   seconds apart; reads and attachment preparation do not each wait ten seconds.
   A busy gate defers work. Microsoft throttling honors Retry-After and establishes
   a mailbox-wide cooldown; no-response requests also get a drain interval.
9. **Send worker.** Recheck state, time, ownership and mailbox; inspect the original
   Outlook item; stop if already sent. Verify the actual recipients and approved
   routing, suppression and kill switch. Persist send intent BEFORE requesting
   the send. Microsoft acceptance is recorded as Submitted, not verified delivery.
10. **Reconciliation worker.** Inspect the tracked Outlook message for positive
    send evidence. A lost send response or failed post-send storage write is
    uncertain and goes here, never directly back to send. Checks back off for up
    to 24 hours; unresolved outcomes become failures needing review. No automatic
    second submission is authorized merely because Sent Items is still empty.
11. **Batch aggregation.** Message states produce batch summaries. Authentication
    interruptions require reconnection; bounce-rate safeguards can pause sending.
    Sent, failed, canceled, and manually handled records remain distinguishable.
12. **Later observations.** The reply sweeper records mailbox activity roughly
    every 15 minutes. The dedicated bounce sweeper is scheduled every two hours.
    Hard bounces suppress the address; temporary deferrals/policy failures are
    recorded separately. A sent item does not establish inbox delivery or reading.

## Other workers and recovery

- **Campaign repair:** every five minutes, currently enabled for all connected
  users. Scans approved active batches, respects future times, leases, retry times,
  and a two-minute quiet period, then reconstructs missing work hints. It does not
  replay terminal failures, canceled/manual rows, or uncertain sends. A pass is
  bounded (two messages per user, 50 total), so a large recovery is gradual.
- **Direct-send worker:** one-to-one reply/follow-up operations use a separate
  operation ledger and queue, not campaign EmailMessages. The phases are prepare,
  send, reconcile, and finalize activity records. Stable operation IDs prevent
  repeating an accepted operation when the browser retries.
- **Direct-send repair:** a separate five-minute timer exists, but its production
  enable flag was absent in the September 19 audit, so its dispatcher is disabled.
  Campaign recovery's rollout does not enable this other subsystem.
- **Engagement repair:** a separate 15-minute timer repairs derived activity views;
  repairing a view is distinct from authorizing a send. Timer existence alone does
  not mean every optional feature is enabled for every user.
- **Queue failures:** the host allows eight failed deliveries before moving a
  repeatedly failing item to a poison queue. Application retries use explicit
  delayed work and durable next-attempt times. Do not replay poison messages blindly.

There is no universal approval-age cutoff for legacy work without capacity dates.
The existing reserved-day expiry and 24-hour uncertainty horizon are narrower,
different controls. Scheduled future work is not stale merely because approval
happened several days earlier.

## Remaining reliability improvements, in priority order

1. Alert an administrator on poison-queue growth, old active work, repeated Graph
   throttling, disconnected mailboxes, and stalled recovery timers. A health page
   alone is not proactive notification. Track queue age separately from send age.
2. Audit the one-to-one operation backlog and enable its independent repair flag
   after explicit rollout approval, as was done for campaign recovery.
3. Give campaign work the same transactional-outbox discipline used by direct
   sends: save each state transition and pending work obligation atomically. The
   current campaign timer reconstructs gaps, but cross-row changes and queue writes
   are not a single transaction. A crash during a selected retry can leave only
   part of the user's selection accepted; the rest must be reviewed again.
4. Introduce an explicit stale-work policy for legacy unscheduled approvals,
   measured against intended delivery time. Preserve deliberately future schedules.
5. Build a reviewed retry-plan workflow for expired-day safe failures. It must
   reserve new capacity and display the new dates rather than silently moving mail.
6. Consider more prompt bounce observation with an incremental/checkpointed scan,
   balanced against the shared mailbox request budget. Never retry a bounce as
   though it were an application timeout.
7. Exercise approved internal acceptance batches and restart/failure drills.
   Mock tests cover safety decisions but cannot guarantee Microsoft's real-world
   delivery, mailbox consistency, or exactly-once external effects.

## Verification and rollback notes

Run npm test --prefix api and the canonical src/build_api.sh. The optional
tests/email-review-browser.cjs smoke test uses Playwright with fully intercepted
fake APIs at desktop and phone widths (set PLAYWRIGHT_MODULE and CHROME_EXECUTABLE
if using a temporary installation and installed Chrome). It never sends live mail.

New storage fields are additive. Manual handling is stored as canceled plus an
explicit actor/time marker, keeping older workers conservative. Deploy the API
before the UI. Old bulk-retry clients now fail closed without a selection and
confirmation; reopening the updated application loads the new controls.

Production values above were read on September 19, 2026. Recheck settings before
assuming future deployments have the same rollout scopes or schedules.
