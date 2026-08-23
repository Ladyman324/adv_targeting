"""The first write to the Act! CRM. One history record, on your own contact.

WHY THIS IS A SEPARATE FILE
---------------------------
`src/act_client.py` only reads. Nothing in it can alter the CRM, so it is safe
to run at any time, against anything, without thinking. That property is worth
keeping, so the first thing that can change the database lives somewhere you
have to deliberately reach for.

WHY A HISTORY RECORD AND NOT A FIELD
------------------------------------
A history entry is ADDITIVE. It creates a new row and overwrites nothing, so the
worst case is one stray entry that DELETE removes. Setting a field -- even on
your own record -- destroys whatever was there, and "it was only my own record"
is a bad thing to discover you were wrong about.

It is also the write we actually want. `POST /api/History` is how a call logged
in the field reaches the CRM the rest of the firm uses, which is the whole point
of Phase 4.

WHY YOUR OWN CONTACT
--------------------
`GET /api/contacts/myrecord` resolves the signed-in user's own contact record.
No id is typed, so there is no possibility of a mistyped GUID landing a test
entry on a real advisor's history.

SAFETY
------
Dry run is the DEFAULT. Without --confirm this posts nothing; it prints exactly
what it would send and stops. --confirm is the only way to write, and the id of
whatever gets created is printed so it can be removed with --delete.

Usage:
    $env:ACT_PASSWORD = 'your password in SINGLE quotes'

    # 1. look around -- reads only
    python src/act_write_test.py --user <you> --db EQUITYINVESTMENT --inspect

    # 2. see exactly what would be sent -- still writes nothing
    python src/act_write_test.py --user <you> --db EQUITYINVESTMENT --test

    # 3. actually post it
    python src/act_write_test.py --user <you> --db EQUITYINVESTMENT --test --confirm

    # 4. remove it
    python src/act_write_test.py --user <you> --db EQUITYINVESTMENT --delete <id>
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from act_client import Act, ActError, BASE                # noqa: E402

MARKER = "[API TEST — safe to delete]"

# EVERY RUN GETS ITS OWN TAG. The marker above is a constant, so a second run
# matched the first run's records too: it evaluated whichever sorted first and
# printed delete commands for records it had not created. On a test whose whole
# purpose is reading one field off one new record, grading the wrong row is the
# failure that looks most like success.
RUN = datetime.datetime.now().strftime("%H%M%S")
RUN_TAG = f"{MARKER} run {RUN}"


def post(act: Act, path: str, body: dict):
    """The only function here that changes anything."""
    import requests                                       # noqa: PLC0415
    r = requests.post(f"{act.base}/{path.lstrip('/')}", json=body, timeout=60,
                      headers={"Authorization": f"Bearer {act.token()}",
                               "Content-Type": "application/json"})
    if not r.ok:
        raise ActError(f"POST {path} -> {r.status_code} {r.text[:400]}")
    try:
        return r.json()
    except ValueError:
        return r.text.strip()


def delete(act: Act, path: str):
    import requests                                       # noqa: PLC0415
    r = requests.delete(f"{act.base}/{path.lstrip('/')}", timeout=60,
                        headers={"Authorization": f"Bearer {act.token()}"})
    if not r.ok:
        raise ActError(f"DELETE {path} -> {r.status_code} {r.text[:400]}")
    return r.status_code


def myrecord(act: Act) -> dict:
    me = act.get("api/contacts/myrecord")
    if isinstance(me, list):
        me = me[0] if me else {}
    if not me or not me.get("id"):
        raise ActError("api/contacts/myrecord returned no contact. Your Act! user "
                       "may not have a contact record; pass --contact-id instead.")
    return me


def inspect(act: Act, contact_id: str | None) -> None:
    me = myrecord(act) if not contact_id else {"id": contact_id, "fullName": "(by id)"}
    print(f"target contact : {me.get('fullName')}")
    print(f"           id  : {me['id']}\n")

    hist = act.get(f"api/contacts/{me['id']}/history")
    hist = hist if isinstance(hist, list) else (hist or {}).get("value") or []
    print(f"existing history entries: {len(hist)}")
    # Newest first, so a record just written is the one at the top.
    hist = sorted(hist, key=lambda h: str(h.get("created") or ""), reverse=True)
    for h in hist[:5]:
        # historyType is an OBJECT ({id, name}), not a string. Printed raw it
        # rendered as a truncated dict, which hid the one thing worth reading.
        ht = h.get("historyType")
        name = ht.get("name") if isinstance(ht, dict) else (ht or "")
        tid = ht.get("id") if isinstance(ht, dict) else h.get("historyTypeID")
        # BOTH names, always. This printed `createUserName or recordManager`,
        # which meant a change to the record manager was invisible -- the
        # creator is always set, so the fallback never fired. That is the exact
        # field under investigation, hidden by the way it was displayed.
        creator = str(h.get("createUserName") or "")
        manager = str(h.get("recordManager") or "")
        # The id is printed because it is the ONLY way to delete an entry, and
        # leaving it out meant a test record could be created and then not
        # cleanly removed without going to the Act! UI to find it.
        print(f"   {str(h.get('created'))[:10]}  type={str(tid):<4} {str(name)[:20]:<20}"
              f" {str(h.get('regarding') or '')[:38]}")
        print(f"      id={h.get('id')}  created-by={creator}  manager={manager}")

    types = act.get("api/history-types")
    types = types if isinstance(types, list) else (types or {}).get("value") or []
    print(f"\nhistory types available: {len(types)}")
    for t in types[:25]:
        print(f"   id={str(t.get('id')):<6} {t.get('name')}")
    return me, types


def users(act: Act) -> list[dict]:
    """The Act! user list, keyed the way the app will key it: by email address.

    The web app knows the signed-in person's email from Entra (`/.auth/me`), and
    Act! user names are email addresses, so no hand-maintained mapping table is
    needed between the two. That is worth protecting -- a mapping file would be
    one more thing to go stale when somebody joins or leaves.
    """
    us = act.get("api/users")
    us = us if isinstance(us, list) else (us or {}).get("value") or []
    print(f"{len(us)} Act! users\n")
    # ROLE is shown. It was in the UserInfo model from the start and simply not
    # printed, which left "would being an administrator help?" unanswerable from
    # data we already had.
    print(f"  {'email / username':<34} {'display name':<24} {'role':<14} {'status':<9} id")
    for u in sorted(us, key=lambda u: str(u.get("email") or u.get("username") or "")):
        ident = str(u.get("email") or u.get("username") or "")
        print(f"  {ident:<34} {str(u.get('displayName') or ''):<24}"
              f" {str(u.get('role') or '?'):<14} {str(u.get('status') or ''):<9} {u.get('id')}")
    return us


def permissions(act: Act) -> None:
    """What the signed-in account is actually allowed to do.

    Asked because "would being an admin help?" is a reasonable question and a
    guessable one. It should not be guessed: the answer is a read away.

    Note the shape of the failure we are diagnosing. PATCH returned SUCCESS and
    silently dropped the field. Permission problems normally arrive as 403. A
    200 that ignores what you sent is the signature of a field that is not part
    of the contract, not of a right you lack -- so a role change is unlikely to
    be the fix. Worth confirming rather than asserting.
    """
    try:
        perms = act.get("api/users/permissions")
    except ActError as e:
        print(f"api/users/permissions -> {str(e)[:160]}")
        return
    print(json.dumps(perms, indent=2, default=str)[:4000])


def find_user(us: list[dict], ident: str) -> dict:
    """Locate one user by email or username, case-insensitively."""
    want = ident.strip().lower()
    for u in us:
        for field in ("email", "username"):
            if str(u.get(field) or "").strip().lower() == want:
                return u
    raise ActError(f"No Act! user with email or username {ident!r}. "
                   f"Run --users to see the list.")


def attribution_test(act: Act, me: dict, target_email: str, type_id,
                     confirm: bool, with_names: bool = False) -> None:
    """Can a history record be attributed to someone OTHER than the caller?

    THE QUESTION THIS ANSWERS. If the app holds one set of credentials, every
    rep's logged call would be written to Act! under whoever's account the app
    authenticates as -- proven earlier, `createUserName` came back as the
    authenticated user. That would put false attribution into the CRM the whole
    firm reads, which is worse than not writing at all.

    If Act! honours an explicit recordManagerID / createUserID on POST, the
    design is a service account plus per-rep attribution. If it ignores them and
    stamps the caller regardless, the rep's name has to go in the text instead
    and the record is authored by the integration. Both are workable; they are
    different builds, and guessing which one applies is not an option.
    """
    us = act.get("api/users")
    us = us if isinstance(us, list) else (us or {}).get("value") or []
    target = find_user(us, target_email)
    tid = target.get("id")
    print(f"\nattributing to : {target.get('displayName')} "
          f"<{target.get('email') or target.get('username')}>")
    print(f"          id   : {tid}")
    print(f"authenticated as: {act.user}")

    body = build_entry(me["id"], type_id,
                       "Attribution test: posted by one Act! user, with "
                       "recordManagerID and createUserID naming another. "
                       "Contains no client information. Safe to delete.")
    body["regarding"] = f"{MARKER} attribution check"
    # Both id fields, because which one Act! reads is exactly what is unknown.
    body["recordManagerID"] = tid
    body["createUserID"] = tid
    if with_names:
        # Second attempt: the History model also exposes the NAME fields as
        # writable strings. Sending the ids alone was ignored. This is a long
        # shot -- there is no impersonation concept anywhere in the 254
        # endpoints, and an API that let any user forge authorship as a
        # colleague would be a security defect -- but it is one request, and it
        # is the difference between assuming and knowing.
        name = str(target.get("displayName") or "")
        body["recordManager"] = name
        body["createUserName"] = name
        body["editUserID"] = tid
        body["editUserName"] = name

    print("\n--- WOULD POST to api/History ---")
    print(json.dumps(body, indent=2))
    print("---------------------------------")
    if not confirm:
        print("\nDRY RUN. Nothing was written. Add --confirm to post it.")
        return

    created = post(act, "api/History", body)
    new_id = created.get("id") if isinstance(created, dict) else created
    print(f"\ncreated: {new_id}")

    back = act.get(f"api/contacts/{me['id']}/history")
    back = back if isinstance(back, list) else (back or {}).get("value") or []
    row = next((h for h in back if str(h.get("id")) == str(new_id)), None)
    if not row:
        print("[!] could not read the new record back to check attribution.")
        return
    got_mgr = row.get("recordManager")
    got_creator = row.get("createUserName")
    print(f"\nread back:")
    print(f"   recordManager  : {got_mgr}")
    print(f"   createUserName : {got_creator}")
    wanted = str(target.get("displayName") or "")
    honoured = wanted and (wanted in str(got_mgr) or wanted in str(got_creator))
    print("\n" + ("VERDICT: Act! HONOURS explicit attribution.\n"
                  "  -> a single service account can post on every rep's behalf."
                  if honoured else
                  "VERDICT: Act! IGNORES explicit attribution and stamps the caller.\n"
                  "  -> records will be authored by whichever account the app uses;\n"
                  "     the rep's name has to go in the details text instead."))
    print(f"\nTo remove it:\n  python src/act_write_test.py --user {act.user} "
          f"--db {act.database} --delete {new_id}")


def patch(act: Act, path: str, body: dict):
    import requests                                       # noqa: PLC0415
    r = requests.patch(f"{act.base}/{path.lstrip('/')}", json=body, timeout=60,
                       headers={"Authorization": f"Bearer {act.token()}",
                                "Content-Type": "application/json"})
    if not r.ok:
        raise ActError(f"PATCH {path} -> {r.status_code} {r.text[:400]}")
    try:
        return r.json()
    except ValueError:
        return r.text.strip()


def set_manager(act: Act, history_id: str, target_email: str,
                contact_id: str, confirm: bool) -> None:
    """Reassign an EXISTING history record's Record Manager.

    Prompted by an observation worth more than the previous two tests: the Act!
    UI can edit Record Manager on a history record. That makes it a mutable
    ASSIGNMENT field rather than an immutable audit stamp -- and if the UI can
    change it, PATCH may be able to as well, even though POST ignored it.

    If this works the Phase 4 design becomes two-step: post as a service
    account, then reassign to the rep. `createUserName` would still record the
    service account, which is honest -- the integration did create the row --
    while Record Manager, the column Act! actually shows in the history list,
    names the human responsible.
    """
    us = act.get("api/users")
    us = us if isinstance(us, list) else (us or {}).get("value") or []
    target = find_user(us, target_email)
    tid = target.get("id")
    body = {"id": history_id, "recordManagerID": tid,
            "recordManager": str(target.get("displayName") or "")}
    print(f"reassigning history {history_id}")
    print(f"          to : {target.get('displayName')} <{target_email}>  id={tid}")
    print("\n--- WOULD PATCH api/History/{id} ---")
    print(json.dumps(body, indent=2))
    print("------------------------------------")
    if not confirm:
        print("\nDRY RUN. Nothing was changed. Add --confirm to apply it.")
        return

    patch(act, f"api/History/{history_id}", body)
    back = act.get(f"api/contacts/{contact_id}/history")
    back = back if isinstance(back, list) else (back or {}).get("value") or []
    row = next((h for h in back if str(h.get("id")) == str(history_id)), None)
    if not row:
        print("[!] could not read the record back.")
        return
    got = str(row.get("recordManager") or "")
    print(f"\nread back:")
    print(f"   recordManager  : {got}")
    print(f"   createUserName : {row.get('createUserName')}")
    wanted = str(target.get("displayName") or "")
    print("\n" + (f"VERDICT: PATCH REASSIGNS the record manager.\n"
                  f"  -> post as a service account, then reassign to the rep."
                  if wanted and wanted in got else
                  f"VERDICT: PATCH did not change it -- still {got!r}.\n"
                  f"  -> attribution is fixed at creation; name the rep in the text."))


def put(act: Act, path: str, body: dict):
    import requests                                       # noqa: PLC0415
    r = requests.put(f"{act.base}/{path.lstrip('/')}", json=body, timeout=60,
                     headers={"Authorization": f"Bearer {act.token()}",
                              "Content-Type": "application/json"})
    if not r.ok:
        raise ActError(f"PUT {path} -> {r.status_code} {r.text[:400]}")
    try:
        return r.json()
    except ValueError:
        return r.text.strip()


def activity_route(act: Act, me: dict, target_email: str, confirm: bool) -> None:
    """Schedule an activity FOR another user, clear it, see who owns the history.

    WHY THIS IS A DIFFERENT QUESTION FROM THE EARLIER TESTS.
    Those tried four ways of doing ONE thing: writing a History record directly,
    with the attribution fields set. All four were ignored. But writing history
    directly is not how Act! itself creates history -- the product is
    activity-centric. You schedule an activity, you clear it, and a history
    record falls out.

    And the scheduling endpoint takes a USER ID IN THE PATH:

        POST /api/organizers/{userId}/tasks

    with `scheduledFor` / `scheduledForId` in the body. That is as close to an
    "on behalf of" API as exists here without using the words, and if the
    history it produces belongs to the person the activity was scheduled FOR,
    the attribution problem is solved with no vendor involvement.

    Two steps, so two places it can fail: the task may refuse to be scheduled
    for someone else, or it may schedule correctly and still clear to history
    owned by the caller. Both are reported separately -- "it did not work" is
    not an answer anyone can act on.
    """
    us = act.get("api/users")
    us = us if isinstance(us, list) else (us or {}).get("value") or []
    target = find_user(us, target_email)
    tid = target.get("id")
    name = str(target.get("displayName") or "")
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    later = now + datetime.timedelta(minutes=15)

    task = {
        "subject": f"{RUN_TAG} activity-route attribution check",
        "details": ("Scheduled for one Act! user by another to test whether the "
                    "resulting history is attributed to the schedulee. Contains "
                    "no client information. Safe to delete."),
        "startTime": now.isoformat(),
        "endTime": later.isoformat(),
        "scheduledForId": tid,
        "scheduledFor": name,
        "activityTypeId": 0,                 # Call
        "contacts": [{"id": me["id"]}],
        "isPrivate": False,
        "isTimeless": False,
    }
    print(f"\nscheduling for : {name} <{target_email}>  id={tid}")
    print(f"authenticated  : {act.user}")
    print("\n--- WOULD POST to api/organizers/{userId}/tasks ---")
    print(json.dumps(task, indent=2))
    print("--- then PUT api/tasks/{taskId}/clear with result 'Call Completed' ---")
    if not confirm:
        print("\nDRY RUN. Nothing was written. Add --confirm to run it.")
        return

    created = post(act, f"api/organizers/{tid}/tasks", task)
    task_id = created.get("id") if isinstance(created, dict) else created
    print(f"\ntask created: {task_id}")

    # STEP ONE result: did the task actually belong to the other user?
    try:
        back = act.get(f"api/tasks/{task_id}")
        sched_for = (back or {}).get("scheduledFor")
        sched_by = (back or {}).get("scheduledBy")
        print(f"   scheduledFor : {sched_for}")
        print(f"   scheduledBy  : {sched_by}")
        step1 = name and name in str(sched_for)
    except ActError as e:
        print(f"   [could not read the task back: {str(e)[:90]}]")
        step1 = None

    # STEP TWO: clear it, which is what produces the history record.
    # THE MANAGER FIELDS ARE SENT ON THE CLEAR.
    #
    # `PATCH /api/History/{id}` ignores them on an EXISTING record -- proven on
    # 2026-08-13, and it returned success while changing nothing. But a field
    # that cannot be edited afterwards may still be settable at creation, and
    # the clear is the creation. Untested until now because the first run had
    # the caller and the contact as the same person, which hid the problem.
    #
    # PROVEN IGNORED, 2026-08-14. Sent on the clear against a user-contact and
    # the history still came back managed by that contact's user. Kept in the
    # payload so the next person does not have to re-run the experiment to find
    # out, and so the comment above stays checkable.
    clear = {
        "result": {"id": 1, "name": "Call Completed"},
        "history": {
            "startTime": now.isoformat(), "endTime": later.isoformat(),
            "includeDetailsToHistory": True,
            "subject": f"{RUN_TAG} activity-route attribution check",
            "details": "Cleared via PUT api/tasks/{id}/clear.",
            "isPrivate": False,
            "recordManagerID": tid,
            "recordManager": name,
            "manageUserID": tid,
        },
    }
    put(act, f"api/tasks/{task_id}/clear", clear)
    print("   task cleared")

    hist = act.get(f"api/contacts/{me['id']}/history")
    hist = hist if isinstance(hist, list) else (hist or {}).get("value") or []
    mine = [h for h in hist if RUN_TAG in str(h.get("subject") or "")
            or RUN_TAG in str(h.get("regarding") or "")]
    mine.sort(key=lambda h: str(h.get("created") or ""), reverse=True)
    if not mine:
        print("\n[!] clearing produced no history record on this contact.")
        return
    # EVERY history this clear produced, not just the newest one.
    #
    # Reading only `mine[0]` assumed the clear creates exactly one record. When
    # the contact is itself an Act! user that assumption is worth testing, and
    # the answer changes what the fix is: two records means Act! is also filing
    # a copy for the attendee, one record means the manager is simply wrong.
    print(f"\n{len(mine)} history record(s) carry this test's marker:")
    for h in mine:
        print(f"   {h.get('id')}")
        print(f"      recordManager  : {h.get('recordManager')}")
        print(f"      createUserName : {h.get('createUserName')}")

    h = mine[0]
    # ONLY recordManager decides this. It used to accept a match on
    # recordManager OR createUserName -- and createUserName is ALWAYS the
    # authenticating account, so scheduling for yourself passed the test no
    # matter what happened to the manager. A pass condition that a broken run
    # can satisfy is worse than no test: this printed "ATTRIBUTES CORRECTLY"
    # over a record managed by the wrong person.
    # BY ID, not by name substring. `name in mgr` would be satisfied by any
    # colleague whose display name contains the target's -- and this is the one
    # comparison the entire attribution question rests on, so it should not
    # depend on how two people happen to be spelled.
    mgr = str(h.get("recordManager") or "")
    mgr_id = str(h.get("recordManagerID") or "")
    won = bool(tid) and mgr_id == str(tid)
    print("\n" + (
        "VERDICT: THE ACTIVITY ROUTE ATTRIBUTES CORRECTLY.\n"
        f"  -> recordManager is {mgr!r}, the user the activity was scheduled for."
        if won else
        f"VERDICT: MISATTRIBUTED. recordManager is {mgr!r}, but the activity was\n"
        f"     scheduled for {name!r}"
        + (".\n     The task itself scheduled correctly, so the loss happens at "
           "the clearing step." if step1 else
           ", and the task did not schedule for them either.")))
    print(f"\nTo remove {'them' if len(mine) > 1 else 'it'}:")
    for h in mine:
        print(f"  python src/act_write_test.py --user {act.user} "
              f"--db {act.database} --delete {h.get('id')}")


# The eight buttons the rep sees, and what each one means to Act!.
#
# WHY A TABLE AND NOT EIGHT HISTORY TYPES
# ---------------------------------------
# Act!'s Call result picklist has exactly four values, so eight buttons collapse
# onto four results. The collapse is deliberate and must stay VISIBLE: three of
# our outcomes carry a meaning Act! has no field for, and if that meaning lives
# only in the button the rep tapped, it is lost the moment it reaches the CRM.
# So each one states what it writes AND what else it does in our app.
#
#   act    -- the Act! result id used when the task is cleared. None = no Act!
#             write at all (Skip records nothing anywhere; nothing happened).
#   suffix -- appended to the history details, because "Attempted" alone would
#             make a wrong number and an unanswered ring indistinguishable in a
#             CRM report, and the wrong-number signal is the only check we have
#             on the 84,222 numbers labelled "Direct".
#   local  -- what the app does beyond logging.
OUTCOME_MAP = {
    "connected":    dict(label="Connected",    act=1,    suffix="",
                         local=""),
    "attempted":    dict(label="Attempted",    act=0,    suffix="",
                         local=""),
    "voicemail":    dict(label="Voicemail",    act=17,   suffix="",
                         local=""),
    "callback":     dict(label="Call back",    act=0,    suffix="Call back requested.",
                         local="stays in the queue"),
    "received":     dict(label="Call received", act=2,   suffix="",
                         local=""),
    "wrong-number": dict(label="Wrong number", act=0,    suffix="Wrong number.",
                         local="flags the phone number as bad"),
    "skip":         dict(label="Skip",         act=None, suffix="",
                         local="no call was made, so nothing is logged"),
    "do-not-call":  dict(label="Do not call",  act=1,    suffix="Asked not to be called again.",
                         local="firm-wide suppression"),
}


def outcome_map(act: Act, me: dict, target_email: str, confirm: bool) -> None:
    """Prove the eight app buttons land on the right Act! history type.

    WHAT IS ACTUALLY UNKNOWN HERE
    -----------------------------
    The attribution test cleared ONE task with result id 1 and got back a Call
    Completed. That tells us the round trip works; it does NOT tell us which
    field chose the type. Two candidates were sent at once:

        activityTypeId  on the task      (0 = Call)
        result.id       on the clear     (1 = Call Completed)

    If the ACTIVITY type wins, every outcome clears to the same history type and
    the whole vocabulary collapses -- a voicemail and a connect become
    indistinguishable in the CRM, silently, which is this project's signature
    failure. So this schedules ONE task per distinct result, clears each with a
    different result id, and reads back the type that actually landed.

    Four writes, not eight: eight buttons collapse onto four Act! results.

    NOTHING IS DELETED AUTOMATICALLY. The delete commands are printed for you to
    run. An earlier version of this docstring claimed a `--keep` flag existed and
    that cleanup was automatic; neither was true, which would have left someone
    believing the CRM had been tidied when it had not.
    """
    us = act.get("api/users")
    us = us if isinstance(us, list) else (us or {}).get("value") or []
    target = find_user(us, target_email)
    tid, name = target.get("id"), str(target.get("displayName") or "")

    # The distinct results, and which buttons ride on each.
    wanted = {}
    for key, o in OUTCOME_MAP.items():
        if o["act"] is None:
            continue
        wanted.setdefault(o["act"], []).append(o["label"])

    # The four Call results, by the ids --inspect reported from the live server.
    names = {0: "Call Attempted", 1: "Call Completed", 2: "Call Received",
             17: "Call Left Message"}

    print(f"\nscheduling for : {name} <{target_email}>  id={tid}")
    print(f"authenticated  : {act.user}")
    print(f"target contact : {me.get('fullName')}  id={me['id']}\n")
    print("the eight buttons, and what each writes to Act!:\n")
    for key, o in OUTCOME_MAP.items():
        a = "(nothing written)" if o["act"] is None else f"{o['act']:<3} {names.get(o['act'],'?')}"
        extra = f"   [{o['local']}]" if o["local"] else ""
        print(f"   {o['label']:<15} -> {a}{extra}")
        if o["suffix"]:
            print(f"   {'':<15}    details += {o['suffix']!r}")

    print(f"\nwould create {len(wanted)} test activities, one per distinct result:")
    for rid, labels in sorted(wanted.items()):
        print(f"   result {rid:<3} {names.get(rid,'?'):<20} covers {', '.join(labels)}")

    if not confirm:
        print("\nDRY RUN. Nothing was written. Add --confirm to run it.")
        return

    created, problems = [], []
    for rid in sorted(wanted):
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        later = now + datetime.timedelta(minutes=15)
        subject = f"{RUN_TAG} outcome map result={rid}"
        task = {
            "subject": subject,
            "details": "Outcome-map probe. Contains no client information. Safe to delete.",
            "startTime": now.isoformat(), "endTime": later.isoformat(),
            "scheduledForId": tid, "scheduledFor": name,
            # Deliberately left at Call for every one of them. If this is what
            # decides the history type, all four will come back identical and
            # the probe has found the thing it was written to find.
            "activityTypeId": 0,
            "contacts": [{"id": me["id"]}],
            "isPrivate": False, "isTimeless": False,
        }
        t = post(act, f"api/organizers/{tid}/tasks", task)
        task_id = t.get("id") if isinstance(t, dict) else t
        put(act, f"api/tasks/{task_id}/clear", {
            "result": {"id": rid, "name": names.get(rid, "")},
            "history": {"startTime": now.isoformat(), "endTime": later.isoformat(),
                        "includeDetailsToHistory": True, "subject": subject,
                        "details": f"Outcome-map probe, result {rid}.",
                        "isPrivate": False},
        })
        created.append((rid, subject))
        print(f"   cleared result {rid} ({names.get(rid)})")

    hist = act.get(f"api/contacts/{me['id']}/history")
    hist = hist if isinstance(hist, list) else (hist or {}).get("value") or []

    print("\nwhat actually landed:\n")
    print(f"   {'sent':<5} {'expected':<20} {'got back':<20} {'manager':<20} details kept")
    ids_to_clean = []
    for rid, subject in created:
        h = next((x for x in hist if subject in str(x.get("subject") or "")
                  or subject in str(x.get("regarding") or "")), None)
        if not h:
            problems.append(f"result {rid} produced no history record at all")
            print(f"   {rid:<5} {names.get(rid,''):<20} {'-- MISSING --':<20}")
            continue
        ht = h.get("historyType")
        got = (ht or {}).get("name") if isinstance(ht, dict) else str(ht)
        mgr = str(h.get("recordManager") or "")
        kept = "yes" if str(rid) in str(h.get("details") or "") else "NO"
        print(f"   {rid:<5} {names.get(rid,''):<20} {str(got):<20} {mgr:<20} {kept}")
        if names.get(rid) and str(got).strip().lower() != names[rid].lower():
            problems.append(f"result {rid} was sent as {names[rid]!r} but came back {got!r}")
        if name and name not in mgr:
            problems.append(f"result {rid} is managed by {mgr!r}, not {name!r}")
        ids_to_clean.append(h.get("id"))

    print()
    if problems:
        print("VERDICT: THE OUTCOME VOCABULARY DOES NOT SURVIVE THE ROUND TRIP.")
        for p in problems:
            print(f"  - {p}")
        print("\n  -> the app must not ship a distinction the CRM cannot hold.")
    else:
        print("VERDICT: ALL FOUR RESULTS ROUND-TRIP CORRECTLY, ATTRIBUTED TO "
              f"{name.upper()}.\n  -> the clear result, not activityTypeId, "
              "decides the history type.\n     The eight-button vocabulary is safe to build.")

    print("\nTo remove the test records:")
    for hid in ids_to_clean:
        print(f"  python src/act_write_test.py --user {act.user} "
              f"--db {act.database} --delete {hid}")


def dump_history(act: Act, history_id: str) -> None:
    """Every field on one history record, so association can be read not guessed.

    WHY THIS IS NEEDED
    ------------------
    In the Act! UI's history grid the advisor we called lands in the INVITEE
    column, while ordinary history written by a human lands in ASSOCIATED
    CONTACT. Reps browse by the latter, so an entry that only appears as an
    invitee is an entry nobody finds -- and it is a fair bet that Act!'s own
    reporting joins on the association too.

    The `contacts: [{id}]` we send on the task is evidently producing the invitee
    link. Which field produces the other one is not documented anywhere we can
    reach, so read it off a record instead of guessing: this prints the whole
    object, and any list-valued field is expanded, because the difference between
    the two columns will be a list with a name in it.
    """
    h = act.get(f"api/History/{history_id}")
    if isinstance(h, list):
        h = h[0] if h else {}
    if not h:
        print(f"[!] no history record {history_id}")
        return

    print(f"history {history_id}\n")
    scalars, lists = {}, {}
    for k, v in sorted(h.items()):
        (lists if isinstance(v, (list, dict)) else scalars)[k] = v

    for k, v in scalars.items():
        if v not in (None, "", False):
            print(f"   {k:<28} {str(v)[:90]}")
    print("\n   -- populated objects and lists --")
    for k, v in lists.items():
        if not v:
            continue
        if isinstance(v, dict):
            print(f"   {k:<28} {json.dumps(v)[:120]}")
        else:
            print(f"   {k:<28} [{len(v)}]")
            for item in v[:4]:
                if isinstance(item, dict):
                    # displayName / fullName is the whole point: it says WHICH
                    # person this list is linking, which is the question.
                    label = (item.get("displayName") or item.get("fullName")
                             or item.get("name") or "")
                    print(f"   {'':<28}   {label or json.dumps(item)[:90]}"
                          f"{('  id=' + str(item.get('id'))) if label else ''}")
                else:
                    print(f"   {'':<28}   {str(item)[:90]}")

    empty = [k for k, v in lists.items() if not v]
    if empty:
        print(f"\n   empty lists (candidates for the association we are missing):"
              f"\n      {', '.join(empty)}")


def dump_compare(act: Act, contact_id: str) -> None:
    """Dump a HUMAN-created call history beside one of ours, field by field.

    THE QUESTION
    ------------
    In the Act! UI grid our entries put the advisor in INVITEE and leave
    ASSOCIATED CONTACT blank; real entries mostly do the opposite. But the API
    shows only ONE `contacts` array on our record and no association field at
    all, so the difference is not in anything we send.

    The likely cause is provenance: history created by CLEARING AN ACTIVITY
    attaches its contact as an attendee, while history written directly attaches
    it as an association. If that is right it is a real trade-off, because the
    direct route is precisely the one that loses attribution -- so it is worth
    establishing by comparison rather than by argument.

    This finds the newest Call-type history on a contact that we did NOT write,
    and prints the fields that differ from ours. Read only.
    """
    hist = act.get(f"api/contacts/{contact_id}/history")
    hist = hist if isinstance(hist, list) else (hist or {}).get("value") or []
    if not hist:
        print(f"[!] no history on contact {contact_id}")
        return

    def is_call(h):
        ht = h.get("historyType") or {}
        return str((ht.get("activityTypeName") if isinstance(ht, dict) else "")) == "Call"

    ours = [h for h in hist if MARKER in str(h.get("regarding") or "")]
    theirs = [h for h in hist if MARKER not in str(h.get("regarding") or "") and is_call(h)]
    theirs.sort(key=lambda h: str(h.get("created") or ""), reverse=True)

    print(f"{len(hist)} history entries: {len(ours)} written by this script, "
          f"{len(theirs)} human-created calls\n")
    if not theirs:
        print("[!] no human-created Call history on this contact. Pass a contact "
              "id that a rep has actually logged a call against -- that is the "
              "only record that can answer the question.")
        return

    real = act.get(f"api/History/{theirs[0].get('id')}")
    if isinstance(real, list):
        real = real[0] if real else {}
    mine = act.get(f"api/History/{ours[0].get('id')}") if ours else {}
    if isinstance(mine, list):
        mine = mine[0] if mine else {}

    print(f"human record  {real.get('id')}  ({real.get('created')})")
    if mine:
        print(f"ours          {mine.get('id')}  ({mine.get('created')})")
    print()

    keys = sorted(set(real) | set(mine))
    for k in keys:
        a, b = real.get(k), mine.get(k)

        def short(v):
            if isinstance(v, list):
                return f"[{len(v)}] " + ", ".join(
                    str((x.get("displayName") or x.get("fullName") or x.get("name") or x.get("id"))
                        if isinstance(x, dict) else x) for x in v[:3])
            if isinstance(v, dict):
                return json.dumps(v)[:70]
            return "" if v in (None, "") else str(v)[:70]

        sa, sb = short(a), short(b)
        if not sa and not sb:
            continue
        flag = "  <-- DIFFERS" if (bool(sa) != bool(sb)) else ""
        print(f"   {k:<24} human={sa[:52]:<52} ours={sb[:40]}{flag}")

    only_real = [k for k in keys if real.get(k) and not mine.get(k)]
    print("\n   populated on the human record and EMPTY on ours:")
    print(f"      {', '.join(only_real) if only_real else '(none)'}")
    print("\n   -> a field in that list is the association we are missing. If the "
          "list is\n      empty, the difference is not in the record at all and "
          "the UI is\n      rendering by provenance, which we cannot change from "
          "the payload.")


def associate(act: Act, history_id: str, contact_id: str, confirm: bool) -> None:
    """PUT /api/history/{id}/contacts/{contactId} — the documented association.

    WHY THIS MATTERS
    ----------------
    History we create by clearing an activity puts the advisor in the Act! UI's
    INVITEE column and leaves ASSOCIATED CONTACT empty; history a rep records by
    hand does the reverse. Reps browse by the latter, so our entries are filed
    where nobody looks for them.

    Comparing the two records field by field through `GET /api/History/{id}`
    showed them IDENTICAL -- same `contacts` array, nothing populated on one and
    absent on the other -- and the conclusion drawn was that the API cannot
    express the difference at all.

    That conclusion was wrong, and this endpoint was sitting in the same path
    listing at the time. `PUT /api/history/{id}/contacts/{contactId}` associates
    a contact with a history item as an explicit OPERATION rather than a field
    on the model -- which is why reading the model could never have found it.
    Credit for spotting it belongs to the review, not to me.

    Read-only until --confirm.
    """
    def contacts_on(h):
        return [(c.get("id"), c.get("displayName") or c.get("fullName") or "")
                for c in (h.get("contacts") or []) if isinstance(c, dict)]

    before = act.get(f"api/History/{history_id}")
    if isinstance(before, list):
        before = before[0] if before else {}
    if not before:
        raise ActError(f"no history record {history_id}")

    print(f"history {history_id}")
    print(f"   regarding : {before.get('regarding')}")
    print(f"   manager   : {before.get('recordManager')}")
    print(f"   contacts  : {contacts_on(before) or '(none)'}")
    print(f"\n--- WOULD PUT api/history/{history_id}/contacts/{contact_id} ---")
    if not confirm:
        print("\nDRY RUN. Nothing was written. Add --confirm to run it.")
        return

    put(act, f"api/history/{history_id}/contacts/{contact_id}", {})
    after = act.get(f"api/History/{history_id}")
    if isinstance(after, list):
        after = after[0] if after else {}
    print(f"\nafter, contacts : {contacts_on(after) or '(none)'}")

    # The API view may not change even when the UI column does -- that is exactly
    # what the earlier field-by-field comparison found -- so this reports what it
    # can see and hands the real question to a human looking at the grid.
    print("\nThe API showed no difference between the two kinds of association "
          "before, so\nthe answer is in the Act! UI rather than here. Open this "
          "contact's history\ngrid and check whether the advisor now appears "
          "under ASSOCIATED CONTACT\nrather than only under INVITEE.")


def put_manager(act: Act, history_id: str, target_email: str, confirm: bool) -> None:
    """Reassign an existing history's Record Manager with PUT, not PATCH.

    WHY THIS IS A DIFFERENT ATTEMPT
    ------------------------------
    Everything before this used `PATCH /api/History/{id}`, which the published
    Swagger describes as "Partially update an already existing history". It
    returned 200 and changed nothing, and that was reported as "the API cannot
    set Record Manager".

    The same path also exposes **PUT** -- "Update an already existing history" --
    taking the FULL `Histories.History` model, on which `recordManagerID` appears
    as a plain string with no read-only flag.

    IT WAS IGNORED TOO. Tested 2026-08-14: PUT returned success and the manager
    was unchanged, exactly as PATCH had. So `recordManagerID` is SERVER-MANAGED,
    and its presence in the model without a read-only flag says nothing about
    whether a client may set it. That was the mistake in reading the spec this
    way -- schema presence is not write permission, and Swagger records the
    former only.

    Four routes now agree: POST, PATCH, PUT, and the activity clear. This is
    settled, and the function is kept as the evidence.

    That distinction was available in the documentation the whole time. It was
    missed because the earlier search read path NAMES rather than operations.

    A full replace needs the whole record or it will blank what it omits, so
    this GETs the history first and sends it back with two fields changed.
    """
    us = act.get("api/users")
    us = us if isinstance(us, list) else (us or {}).get("value") or []
    target = find_user(us, target_email)
    tid, name = target.get("id"), str(target.get("displayName") or "")

    before = act.get(f"api/History/{history_id}")
    if isinstance(before, list):
        before = before[0] if before else {}
    if not before:
        raise ActError(f"no history record {history_id}")

    print(f"history {history_id}")
    print(f"   recordManager now : {before.get('recordManager')}")
    print(f"   would become      : {name}  ({tid})\n")

    # The whole record back, with only the manager changed. Read-only fields are
    # sent as they came: the server decides what it ignores, and guessing which
    # ones to strip is how a full replace quietly empties a column.
    body = dict(before)
    body["recordManagerID"] = tid
    body["recordManager"] = name

    print("--- WOULD PUT to api/History/{id} ---")
    print(json.dumps({k: body.get(k) for k in
                      ("id", "recordManagerID", "recordManager", "createUserID",
                       "createUserName", "regarding", "historyTypeID")}, indent=2))
    print(f"    ...plus the {len(body)} fields read back unchanged")
    if not confirm:
        print("\nDRY RUN. Nothing was written. Add --confirm to run it.")
        return

    put(act, f"api/History/{history_id}", body)
    after = act.get(f"api/History/{history_id}")
    if isinstance(after, list):
        after = after[0] if after else {}
    got = str((after or {}).get("recordManager") or "")
    print(f"\nafter PUT, recordManager : {got!r}")
    print(f"           createUserName : {(after or {}).get('createUserName')!r}")

    # Only the manager decides this, and the rest of the record must have
    # survived -- a "success" that blanked the details would be a worse outcome
    # than the failure it replaced.
    lost = [k for k in ("regarding", "details", "historyTypeID", "startTime")
            if before.get(k) and not (after or {}).get(k)]
    print("\n" + ("VERDICT: PUT SETS THE RECORD MANAGER.\n"
                  "  -> log the call, then reassign it. Attribution is solvable."
                  if name and name in got else
                  f"VERDICT: PUT ignored it too — still {got!r}."))
    if lost:
        print(f"[!] PUT BLANKED {lost} — a full replace dropped fields that were "
              f"there before. Do not use this route without sending them back.")


def build_entry(contact_id: str, type_id, note: str) -> dict:
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
    body = {
        "regarding": f"{MARKER} connectivity check",
        "details": note or (
            "Written by src/act_write_test.py to confirm the Act! Web API can "
            "create history records. Contains no client information. Safe to delete."),
        "startTime": now,
        "endTime": now,
        # The contact this attaches to. An array because one history entry can
        # span several contacts; here it is deliberately exactly one.
        "contacts": [{"id": contact_id}],
    }
    if type_id is not None:
        body["historyTypeID"] = type_id
    return body


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--user", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--contact-id", help="override the target; defaults to your own record")
    ap.add_argument("--inspect", action="store_true", help="read only: target, history, types")
    ap.add_argument("--test", action="store_true", help="build the entry; prints unless --confirm")
    ap.add_argument("--type-id", type=int, help="historyTypeID (see --inspect)")
    ap.add_argument("--note", default="", help="override the details text")
    ap.add_argument("--confirm", action="store_true",
                    help="ACTUALLY WRITE. Without this nothing is sent.")
    ap.add_argument("--delete", metavar="HISTORY_ID",
                    help="remove a history entry by id; refuses records this "
                         "test did not create unless --force")
    ap.add_argument("--force", action="store_true",
                    help="with --delete, allow removing a record that does not "
                         "carry the test marker")
    ap.add_argument("--users", action="store_true",
                    help="read only: list Act! users with their ids and emails")
    ap.add_argument("--activity-route", metavar="EMAIL",
                    help="schedule an activity FOR another Act! user, clear it, and "
                         "report who owns the resulting history — Act!'s own way of "
                         "creating history, and the one route not yet tested")
    ap.add_argument("--associate", metavar="HISTORY_ID",
                    help="associate a contact with a history item via the "
                         "documented PUT /api/history/{id}/contacts/{contactId}; "
                         "use with --contact-id")
    ap.add_argument("--put-manager", metavar="HISTORY_ID",
                    help="reassign an existing history's Record Manager using "
                         "PUT (a full replace) rather than PATCH; use with "
                         "--attribute EMAIL")
    ap.add_argument("--dump", metavar="HISTORY_ID",
                    help="read only: every field on one history record, with "
                         "lists expanded — use it to see which field carries the "
                         "contact association")
    ap.add_argument("--dump-compare", metavar="CONTACT_ID",
                    help="read only: diff a human-created call history against "
                         "one of ours on the same contact, to find which field "
                         "carries the Associated Contact link")
    ap.add_argument("--outcome-map", metavar="EMAIL",
                    help="prove the eight call-log buttons land on the right Act! "
                         "history type: one activity per distinct result, cleared "
                         "and read back")
    ap.add_argument("--permissions", action="store_true",
                    help="read only: what the signed-in Act! account may do")
    ap.add_argument("--set-manager", metavar="HISTORY_ID",
                    help="reassign an EXISTING history record's Record Manager; "
                         "use with --attribute EMAIL")
    ap.add_argument("--names", action="store_true",
                    help="also send the NAME string fields, not just the ids")
    ap.add_argument("--attribute", metavar="EMAIL",
                    help="post a history record attributed to ANOTHER Act! user "
                         "(by email) and report whose name comes back")
    args = ap.parse_args()

    password = os.environ.get("ACT_PASSWORD", "")
    if not password:
        print("ACT_PASSWORD is not set.\n"
              "  PowerShell:  $env:ACT_PASSWORD = 'your password in SINGLE quotes'")
        sys.exit(2)

    act = Act(args.user, password, args.db, args.base)
    try:
        act.authorize()
        print(f"authorized against {act.base} as {args.user}\n")

        if args.associate:
            if not args.contact_id:
                raise ActError("--associate needs --contact-id CONTACT_GUID.")
            associate(act, args.associate, args.contact_id, args.confirm)
            return

        if args.put_manager:
            if not args.attribute:
                raise ActError("--put-manager needs --attribute EMAIL to say who "
                               "to assign it to.")
            put_manager(act, args.put_manager, args.attribute, args.confirm)
            return

        if args.dump:
            dump_history(act, args.dump)
            return

        if args.dump_compare:
            dump_compare(act, args.dump_compare)
            return

        if args.delete:
            # READ IT FIRST. This used to delete any GUID handed to it, with no
            # check and no confirmation -- in a file whose whole premise is that
            # writes are deliberate and reversible. A mistyped id would have
            # destroyed a real advisor's call history with no way back.
            #
            # So: fetch it, show it, and refuse anything this test did not create
            # unless --force says otherwise.
            try:
                doomed = act.get(f"api/History/{args.delete}")
                if isinstance(doomed, list):
                    doomed = doomed[0] if doomed else {}
            except ActError as e:
                print(f"[!] cannot read {args.delete}: {str(e)[:120]}")
                sys.exit(1)
            if not doomed:
                print(f"[!] no history record {args.delete}")
                sys.exit(1)
            regarding = str(doomed.get("regarding") or doomed.get("subject") or "")
            print(f"   regarding : {regarding[:70]}")
            print(f"   created   : {str(doomed.get('created'))[:19]} "
                  f"by {doomed.get('createUserName')}")
            print(f"   manager   : {doomed.get('recordManager')}")
            if MARKER not in regarding and not args.force:
                print(f"\n[!] REFUSED. This record does not carry {MARKER!r},")
                print("    so it was not created by this test. Add --force if "
                      "you are certain.")
                sys.exit(1)
            code = delete(act, f"api/History/{args.delete}")
            print(f"\ndeleted history {args.delete} -> HTTP {code}")
            return

        if args.permissions:
            permissions(act)
            if not (args.users or args.attribute or args.set_manager):
                return

        if args.users:
            users(act)
            if not args.attribute:
                return

        me, types = inspect(act, args.contact_id)

        if args.activity_route:
            activity_route(act, me, args.activity_route, args.confirm)
            return

        if args.outcome_map:
            outcome_map(act, me, args.outcome_map, args.confirm)
            return

        if args.set_manager:
            if not args.attribute:
                raise ActError("--set-manager needs --attribute EMAIL to say who to assign it to.")
            set_manager(act, args.set_manager, args.attribute, me["id"], args.confirm)
            return

        if args.attribute:
            attribution_test(act, me, args.attribute, args.type_id,
                             args.confirm, args.names)
            return
        if not args.test:
            print("\nRead-only. Add --test to see what a write would send.")
            return

        type_id = args.type_id
        if type_id is None and types:
            # Prefer a note-ish type; otherwise leave it out and let Act! default.
            pick = next((t for t in types
                         if str(t.get("name","")).strip().lower() in ("note", "other")), None)
            type_id = pick.get("id") if pick else None
            if type_id is not None:
                print(f"\nusing historyTypeID={type_id} ({pick.get('name')}) "
                      f"-- override with --type-id")

        body = build_entry(me["id"], type_id, args.note)
        print("\n--- WOULD POST to api/History ---")
        print(json.dumps(body, indent=2))
        print("---------------------------------")

        if not args.confirm:
            print("\nDRY RUN. Nothing was written. Re-run with --confirm to post it.")
            return

        print("\nposting...")
        created = post(act, "api/History", body)
        new_id = created.get("id") if isinstance(created, dict) else created
        print(f"created: {new_id}")

        after = act.get(f"api/contacts/{me['id']}/history")
        after = after if isinstance(after, list) else (after or {}).get("value") or []
        mine = [h for h in after if MARKER in str(h.get("regarding") or "")]
        print(f"history entries now: {len(after)}  ({len(mine)} carrying the test marker)")
        print(f"\nTo remove it:\n  python src/act_write_test.py --user {args.user} "
              f"--db {args.db} --delete {new_id}")
    except ActError as e:
        print(f"[!] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
