---
description: Hunt for silent bugs in a named subsystem, with a reproduction required for every finding.
---

Hunt for bugs in: **$ARGUMENTS**

If no scope was given, ask for one and stop. Valid scopes are `contacts`,
`tiles`, `names`, `dialer`, `field`, `desktop`, `api`, `deploy`, or a git diff
reference. This codebase is ~80 Python scripts, a 4,400-line `app.js` and a
managed Functions API; an unscoped hunt produces a long, low-signal report that
nobody reads.

## Step 1 — run the audit first

```bash
python src/audit.py --verbose
```

Any failure there is a real, already-proven bug. Report those and fix them
before looking for new ones. If the audit is green, continue — it does not
cover much.

## Step 2 — the failure patterns that actually occur here

Every significant bug in this project has been **silent**: plausible output, no
exception, a wrong answer nobody had cause to question. Not crashes, not
exceptions, not type errors. Look for these specifically, in rough order of how
often they have bitten:

1. **Two implementations of one rule.** Python writes the data, JavaScript
   reads it, a Node function validates it, a Python fixture imitates that
   function. Four places, one rule, no compiler. *(dev shim vs Azure store
   returned different field names for the same record; `sw.js` was excluded
   from the hash that names its own cache.)*
2. **A predicate applied in one place but not its sibling.** *(The firm drawer
   listed 267 advisors while the map showed 3, because the list never called
   `passesFilters`.)*
3. **A list mutated under a cursor or index.** *(Removing a do-not-call entry,
   and separately reordering the queue, each silently skipped the next person.)*
4. **`[0]`, or `else if`, where the data has several.** *(An email column took a
   colleague's address for 9,171 advisors; an `else if` hid $4.42B of book
   value across 726 advisors.)*
5. **Counting rows where you mean people.** *(A duplicate row made someone's own
   direct line look shared.)*
6. **Join keys built differently on each side.** *(The tile builder returned
   zero rows because two files spelled `addr_key` differently.)*
7. **A boundary that returns success when it failed.** *(An expired session
   returned 200 with HTML, which parsed loosely looks like a successful empty
   write; the service worker then cached that login page as the app.)*
8. **A threshold or default nobody validated.** *(`NAMESAKE_CAP = 3` was
   rejecting correct matches. The 84,222 numbers labelled "Direct" have still
   never been checked against reality.)*

## Step 3 — the rule that makes this worth running

**No finding without a reproduction.** Every bug you report must come with a
command that demonstrates it and the actual output of running it — a count that
disagrees, a join that returns nothing, a stored record with an empty field, a
mutation that the code fails to notice.

A suspicion is not a finding. If you cannot demonstrate it, either build the
check that would, or leave it out and say what you could not verify.

This is not pedantry: most bugs here were invisible in the source and obvious
in the data. Reading alone would have missed them, and a report of thirty
plausible-sounding suspicions is worse than no report, because it costs trust
in the tool.

## Step 4 — leave something behind

For every confirmed bug, add a check to `src/audit.py` that would have caught
it, and verify the check FAILS before the fix and PASSES after. A green suite
that cannot go red is worthless, so prove the new check catches its own
mutation.

That is the compounding part: each fix leaves a permanent guard, and the class
of bug that has dominated this project stops being able to recur quietly.

## Reporting

Order findings by consequence — wrong data shown to a rep beats a cosmetic
issue. For each: what breaks, the reproduction and its output, and the smallest
fix. Say plainly what you checked and found clean, so the absence of a finding
means something.
