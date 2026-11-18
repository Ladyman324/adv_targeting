# Advisor lists and email review

## Exporting to Excel

The **CSV** button beside the Advisors count exports advisors passing the current
filters inside the visible map. There is one row per advisor/firm relationship,
so the row count can exceed the distinct-advisor count. Contact email, phones,
greeting, match tier and source are included where available.

In **Lists**, **Export CSV** exports every member of the selected static list or
smart view, including people without a phone or approved email route. It does not
switch the current calling list, advance the cursor, change membership, or prepare
email. A static-list export distinguishes current Map email from the saved list's
email; a stale saved email is not permission to send to it.

These are UTF-8 CSV files that Excel opens, rather than native .xlsx workbooks.
The first row is the column header. Scope, filters, source date, build and export
time are recorded in columns. Formula-like cells are neutralized. In Excel use
Data > From Text/CSV and set ZIP and phone columns to Text to preserve leading
zeroes and punctuation. An export does not confer email eligibility.

## Why someone is left out of email preparation

The recipient precheck returns the exact reason code and a next step, and the
composer shows these before asking whether to continue with eligible contacts.
This change explains existing decisions; it does not approve additional contacts.

| Code | Meaning and next step |
| --- | --- |
| `missing_or_invalid_email` | No valid current email. Verify the person's own primary email in ACT! or the roster. |
| `contact_identity_not_approved` | Contact matching needs review. Verify the person's CRD, full name and current firm. |
| `act_identity_not_approved` | The ACT! row is not linked to an approved advisor identity. Verify the asserted CRD and person. |
| `approved_email_not_unique` / `duplicate_email` | Several advisors claim the address. Check shared, assistant and duplicate records; do not guess the recipient. |
| `act_email_does_not_match_approved_identity` | The ACT! and approved route disagree on the email. Verify the exact address and link. |
| `contact_presentation_not_approved` | A usable greeting or surname needs review. |
| `roster_current_firm_conflict` | Roster and current registration disagree; check a possible move. An ACT! relationship label cannot fix roster evidence. |

Client, prospect, analyst and rotation labels define business relationships, not
identity approval. Some contacts are not registered advisors. Do not assign them
another person's CRD to get past a check. Use the name, exact code, email and ACT!
contact ID to ask the Map team for a record-specific review. Actual ACT!
corrections reach the application through the next published ACT! refresh;
refreshing the browser alone does not import ACT! changes.

The separately mentioned ACT! field/group "MAP Advisor Review" has not been
identified in the application code or the November 14 snapshot's field keys.
Its three displayed labels still need an affected contact or the exact labels
before we can map them to underlying decisions or recommend a specific ACT! edit.
