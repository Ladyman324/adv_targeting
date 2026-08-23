# Questions for Act! support — writing call History via the Web API

**Account:** Act! Premium Cloud, database `EQUITYINVESTMENT`, endpoint
`https://apius.act.com/act.web.api`
**API version:** `1.1.1324.1` (SDK `25.101.0.0`), reported by `GET /api/system`
**Date of testing:** 13–14 August 2026

---

## What we are building

An internal web application our sales team uses to work call lists. When a rep
logs the outcome of a call, we want that to appear in Act! as a History record
on the advisor's contact, so the rest of the firm sees it in the CRM rather than
only in our tool.

## The problem — largely solved, one question left

`POST /api/History` attributes the record to **whichever user authenticated**,
and our application authenticates once, as a single account, on behalf of many
reps. Recorded that way, every rep's call would appear in Act! under one name —
worse for us than not writing at all.

We have since found a route that works: scheduling an activity for the rep and
clearing it produces History whose Record Manager is that rep. The remaining
questions are about the details of that route, and about capabilities we could
not confirm from the documentation.

## What we have already tested

All tests were made against our own staff contact record and deleted afterwards.

| # | Attempt | Result |
|---|---|---|
| 1 | `POST /api/History` with `recordManagerID` and `createUserID` set to another active user's id | Record created; `recordManager` and `createUserName` both returned as the **authenticating** user |
| 2 | As above, plus `recordManager`, `createUserName`, `editUserID`, `editUserName` sent as strings | Same — attributed to the authenticating user |
| 3 | `PATCH /api/History/{id}` setting `recordManagerID` / `recordManager` on an existing record | Record manager unchanged |
| 4 | Searched the published Swagger (254 paths) for any impersonation, on-behalf-of, delegation or run-as concept, and for header parameters | None found; no endpoint accepts any header parameter |

### The web interface can do what the API cannot

This is the part we would most like explained. We ran a controlled comparison —
same database, same signed-in user, same field, within a few minutes of each
other:

| History record | Record Manager changed via | Result, read back through `GET /api/contacts/{id}/history` |
|---|---|---|
| `1374270e-21e8-44d7-8454-379e97785ed0` | Act! web interface (`RecordHistoryDrawer.aspx`) | `recordManager` = **Mr. Matt Keeter** — the change persisted |
| `2920cede-2709-43a7-81f6-ce233e1fe48c` | `PATCH /api/History/{id}` | `recordManager` = **Mr. Robert Ladyman** — unchanged |

The second record was never opened in the web interface. Its value was read
immediately after the PATCH and again several minutes later; it was unchanged
both times, so this is not a caching or eventual-consistency effect.

In both cases `createUserName` correctly remained the user who created the
record. That distinction is exactly what we want — the creator records what
wrote the row, the Record Manager records who is responsible for it — and the
web interface maintains it correctly. We simply cannot reach it from the API.

Note also that the PATCH returned **success**. The field was silently dropped
rather than rejected, which is why we do not believe this is a permissions
issue; the same user performing the same change in the web interface is allowed
to do it.

**We are no longer blocked by this**, having found the activity route described
below. We report it because a `PATCH` that returns success while silently
discarding a field is, we think, a defect worth knowing about regardless of
whether setting Record Manager directly is intended to be supported.

---

## Our questions

**1. Why does History created by clearing an activity file its contact as an
Invitee rather than an Associated Contact?**

We have solved attribution ourselves and no longer need question 1 as originally
written — `POST /api/organizers/{userId}/tasks` followed by
`PUT /api/tasks/{taskId}/clear` produces History whose `recordManager` is the
user the activity was scheduled for. That works exactly as we need.

One issue remains. In the Act! web interface's history grid, entries we create
this way place the contact in the **Invitee** column and leave **Associated
contact** empty. History a user records manually does the opposite.

The two records are indistinguishable through your API. We retrieved both with
`GET /api/History/{id}` and compared every field:

| | manually recorded | created by clearing an activity |
|---|---|---|
| `contacts` | `[1] Mr. Robert Ladyman` | `[1] Mr. Robert Ladyman` |
| `contactCompany` / `contactEmail` / `contactPhone` | populated | identical |
| fields populated on one and absent on the other | **none** | **none** |

So the distinction the UI renders is not expressed in the API representation,
and we can find no field to set.

We also tried the documented association operation, **`PUT
/api/history/{id}/contacts/{contactId}`**, against a history record created this
way (`ea409157-921f-4973-b6e5-d35c909799a8`, 14 August). It returned success and
the grid was unchanged: the contact remained under **Invitee** with **Associated
contact** still empty, and `GET /api/History/{id}` showed the same single
`contacts` array before and after.

So the distinction the UI renders appears not to be reachable from the API at
all — not as a field on the model, and not through the one endpoint documented
for associating a contact with a history item.

  a. Is there a supported way to make activity-cleared History associate its
     contact the way manually recorded History does?
  b. If not — does the Invitee/Associated Contact distinction affect anything
     beyond that grid? Specifically, do lookups, activity reports, or
     contact-history queries treat the two differently? If it is presentational
     only, we are content to leave it.

**2. History created by clearing an activity is filed under the contact, when
that contact is an Act! user — and we can find no way to change it.**

When we clear an activity against an ordinary contact, the resulting History
carries `recordManager` = the user the activity was scheduled for. Correct, and
exactly what we need.

When the contact record is itself linked to an **Act! user**, the History is
filed under *that* user instead, regardless of who the activity was scheduled
for. Tested against the live database on 14 August:

| authenticated as | contact | contact is an Act! user | scheduled for | resulting `recordManager` |
|---|---|---|---|---|
| Ladyman | Tolleson | no | Ladyman | Ladyman — correct |
| Ladyman | A. White | yes | Ladyman | **A. White** |
| Ladyman | A. White | yes | **Keeter** | **A. White** |

The third row is the one we cannot explain: the activity was scheduled for a
third user, the task read back correctly as scheduled for that user, and the
History still went to the contact's own user account.

We also tried, all on the live database, all ignored:

- `recordManagerID`, `recordManager` and `manageUserID` on the `history` object
  of `PUT /api/tasks/{taskId}/clear` — the record is created with the contact's
  user regardless
- the same fields on `POST /api/History`
- `PATCH /api/History/{id}` afterwards — **returns 200 and changes nothing**

  a. Is this intended behaviour for contacts linked to a user?
  b. Is there any supported way to set the Record Manager of History created
     this way?

Our application currently deletes the History record when it detects the wrong
Record Manager, because a CRM showing a colleague making a call they did not
make is worse for us than no record at all. We would rather write it correctly.

**3. Your API publishes no machine-readable specification that we can reach.**

`GET /swagger/v1/swagger.json`, `/swagger/docs/v1`, `/swagger.json` and
`/$metadata` all return **403 with an AWS API Gateway error** —
`Invalid key=value pair (missing equal-sign) in Authorization header` — with a
valid bearer token that the `/api/*` routes accept. Only `/api/*` appears to be
routed to the application.

`GET /api/metadata/{type}/fields` works, but only for contact (190), company
(40), group (27) and opportunity (15). `activity`, `history`, `note`, `user` and
`task` all return an empty list rather than an error, so there is no way to
discover the History or Activity schema — including which fields are writable.

Is a specification available to customers by another route? Without one we can
only establish what your API does by trying it, which is how we spent two days
on question 2.

**4. Does Act! Premium Cloud offer OAuth or any delegated authorisation flow?**
The documentation describes only Basic authentication exchanged for a JWT. If a
rep could authorise our application once, against their own Act! account,
attribution would be correct by construction and we would not need to hold
credentials. Is an authorisation-code or refresh-token flow available on any
API version?

**5. Is there an administrator-level impersonation capability?**
i.e. an account that can create records on behalf of another named user, in the
way many CRM APIs support.

**6. Can Record Manager be set per row through the import API
(`POST /api/s3-import`)?**
Is `History` a supported `entityType` for import, and is Record Manager a
mappable field? A batch route would be acceptable to us if the per-record API
cannot do it.

**7. Service account licensing.**
If the recommended answer is a dedicated integration account, does such an
account consume a standard user licence, and is there an account type intended
for API integrations? Our database already contains an inactive user named
**"Synch Admin"** with no email address — if that is a supported integration
account type, we would like to know how it is provisioned.

**8. Rate limits for sustained use.**
We see `X-RateLimit-Limit` / `X-RateLimit-Remaining` / `X-RateLimit-Reset`
headers. What is the sustained limit for our plan, and is there a higher tier
for integrations?

---

## What we will do with the answer

Attribution is solved, so our application will write each rep's call outcomes
under their own Act! identity via the activity route described above.

The association question decides one thing only: whether we keep that route as
built, or change how we attach the contact. We will not fall back to posting
under a single integration account — recording calls under the wrong person's
name is worse for us than an unusual value in one grid column.

---

# Do-not-email: is there a contact field we should be setting?

> **Q1 ANSWERED 2026-08-18** from the Act! online portal: the database carries a
> **Mail Code** picklist, and it already has a `U = UNSUBSCRIBE!!!!` value, plus
> `NC = No mail by request` and `N = No mail; cannot locate; bounce backs`. An
> opt-out through our footer link now targets `U`, never downgrading an existing
> value. Questions 2-4 below are still open; see the note at the end.

**Status: open. Our application does NOT currently set any contact field.**

We have added an email opt-out flow. When an advisor clicks the preference link
in the footer of one of our emails, we:

1. write them to our own suppression list (this is what actually stops the mail
   — it is checked when a batch is built and again at approval), and
2. record an **activity and history** on their Act! contact, using the same
   task-then-clear route we use for call outcomes, so the opt-out is visible to
   anyone working the record.

We deliberately stopped short of writing a contact field, because we could not
confirm any of the following. Please advise:

**1. Is there a standard do-not-email field on the contact entity?**
   If so, what is its exact API name, and what type does it take?

**2. What verb updates a single field on a contact?**
   Does `PATCH api/contacts/{id}` apply a partial update, or does Act! treat the
   payload as a full entity replace? We were not willing to risk the second —
   a replace with a two-field payload would blank the rest of the record.

**3. Is there an append-safe notes field?**
   `contactNotes` (if that is even the right name) appears to be a plain string.
   Is there a way to add a note without overwriting existing content, or should
   notes always go through the history route as we do now?

**4. Can contacts be looked up by email address?**
   We currently resolve contacts through our own CRD → contact-id crosswalk. Is
   `GET api/contacts?$filter=emailAddress eq '<address>'` valid, and is that
   filter indexed? A supported lookup would let us honour an opt-out for an
   address we hold without a CRD match.

## What we will do with the answer

If there is a real field and a safe verb, we will set it in addition to the
history record. Until then the history record stands on its own — our own
suppression list is the enforcement, so nothing is at risk while this is open.

---

## Follow-up after the Mail Code answer (2026-08-18)

The picklist answered *which* field. Two things it could not answer, and how we
handled each:

**The API property name — CONFIRMED 2026-08-18.** `customFields.email__y_n`,
verified against a live single-contact GET (`op=act_fields&crd=2066775`
returned it with value `2`), not only against the bulk export. Set
`ACT_MAIL_CODE_FIELD=customFields.email__y_n`.

Originally recorded as open, as follows. "Mail Code" is the label shown in the Act! UI, not
necessarily the property name the Web API exposes. We do not guess it: it is
supplied as the `ACT_MAIL_CODE_FIELD` app setting, and with that unset we write
no field at all and record the opt-out as history only. To find it, an email
administrator calls `GET /api/email?op=act_fields&crd=<a known CRD>`, which
returns the contact's property names plus the value of any property that already
looks like a Mail Code. That pinpoints the field without dumping the record.

**PATCH versus PUT — still unconfirmed, and the only piece left.** We stopped
needing the answer for safety: the
write is a read-modify-write that sends the **whole entity** back with one
property changed. That is correct whether Act! treats the payload as a merge or
as a replace, so the ambiguity no longer matters.

Question 4 (lookup by email address) remains genuinely open and still limits us:
an opt-out for an address we hold with no CRD match is honoured locally but
cannot be mirrored into Act!.
