"""Invariants that must hold across the built artifacts and the app that reads them.

WHY THIS EXISTS
---------------
Every significant bug in this project has been SILENT: plausible output, no
exception, a wrong answer nobody had reason to question. A sample, in the order
they were found -- an email column that took a colleague's address for 9,171
advisors; a branch that hid $4.42B of book value; a tile join that returned zero
rows because two files spelled the same key differently; a name index whose
client threw away the records the builder had carefully filed; a service worker
that cached the sign-in page as the application.

None of those raised an error. Each was found by running a count and noticing
the number was wrong.

So this file is that noticing, written down. Every check here corresponds to a
bug that actually happened, and its job is to make that bug unable to happen
again quietly.

WHAT BELONGS HERE
-----------------
Facts that must be true and have nothing enforcing them:

  * a value must come from a fixed vocabulary
  * two files must agree on a count, a key format, or a field name
  * a constant duplicated across languages must hold the same value
  * a rule the front end applies must match the rule the server enforces

That last group is where this project keeps breaking. Python writes the data,
JavaScript reads it, a Node function validates it and a Python fixture imitates
that function -- four places, one rule, and no compiler in sight.

WHAT DOES NOT BELONG HERE
-------------------------
Anything a human has to judge. This exits non-zero or it says nothing; a check
that needs interpretation belongs in a report, not in a gate.

Run:  python src/audit.py [--verbose]
Exit code is the number of failed checks, so it can gate a deploy.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB = ROOT / "webapp"
DATA = WEB / "data"
SRC = ROOT / "src"
API = ROOT / "api"

sys.path.insert(0, str(SRC))

RESULTS: list[tuple[str, bool, str]] = []
VERBOSE = False


def check(name: str):
    """Register a check. The function returns (ok, detail)."""
    def wrap(fn):
        try:
            ok, detail = fn()
        except FileNotFoundError as e:
            # A missing artifact is a skip, not a failure: someone may be
            # auditing a partial build. Anything else is a real failure.
            RESULTS.append((name, True, f"skipped -- {e.filename} not built"))
            return fn
        except Exception as e:                       # noqa: BLE001
            RESULTS.append((name, False, f"{type(e).__name__}: {e}"))
            return fn
        RESULTS.append((name, bool(ok), detail))
        return fn
    return wrap


def read(path: pathlib.Path):
    return json.loads(path.read_text(encoding="utf-8"))


def text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. The phone-kind taxonomy
#
# `wk` decides whether a button says "Direct" and whether the queue sorts
# someone to the front. A value outside the taxonomy would fall through every
# comparison and silently become "Office" -- a number labelled worse than it is,
# which costs calls rather than making them wrong.
# ---------------------------------------------------------------------------
PHONE_KINDS = {"direct", "extension", "sole-use", "shared", "switchboard",
               "toll-free", "single-occupant", "unverified"}


@check("phone_kind values are all in the taxonomy")
def _kinds():
    # contacts.json, because `wk` -- the phone kind -- lives there and nowhere
    # else. A previous edit of mine pointed this at advisor_index.json, whose
    # rows carry only a name: `seen` came back empty and the check passed
    # unconditionally for every value, which is worse than deleting it.
    adv = read(DATA / "contacts.json")["advisors"]
    seen = {v.get("wk") for v in adv.values() if v.get("wk")}
    unknown = seen - PHONE_KINDS
    return (not unknown,
            f"{len(seen)} kinds in use"
            + (f"; UNKNOWN: {sorted(unknown)}" if unknown else ""))


# ---------------------------------------------------------------------------
# 2. Everyone is either in a tile or counted as unplaced
#
# The name index reports `unplaced` so the UI can say who cannot be searched.
# If that number drifts from reality the app states a falsehood about its own
# coverage -- and states it confidently.
# ---------------------------------------------------------------------------
@check("tile rows + unplaced == contactable advisors")
def _coverage():
    adv = read(DATA / "contacts.json")["advisors"]
    idx = read(DATA / "name_index.json")
    rows = 0
    for p in (DATA / "tiles").glob("*.json"):
        rows += len(read(p)["rows"])
    total = idx["searchable"] + idx["unplaced"]
    return (rows == idx["searchable"] and total == len(adv),
            f"tiles {rows:,} == searchable {idx['searchable']:,}; "
            f"+ unplaced {idx['unplaced']:,} == contacts {len(adv):,}")


# ---------------------------------------------------------------------------
# 3. Team money and individual money are never both present
#
# build_contacts sets `ia` only where no team-mate shares the amount. The card
# renders both unconditionally on the strength of that invariant; if it ever
# broke, one person's book would be added to their team's and shown twice.
# ---------------------------------------------------------------------------
@check("no advisor carries both a team amount and an individual book")
def _double_count():
    c = read(DATA / "contacts.json")
    teams = c.get("teams") or {}
    both = [k for k, v in c["advisors"].items()
            if v.get("ia", 0) > 0 and v.get("tm") and (teams.get(str(v["tm"])) or {}).get("a", 0) > 0]
    return (not both, f"{len(both)} advisors double counted"
            + (f" e.g. {both[:5]}" if both else ""))


# ---------------------------------------------------------------------------
# 4. Every name-index record is findable by the tokens it was filed under
#
# THE BUG THIS ENCODES: William Kaiser was correctly filed in the "bi" shard
# under his nickname, and the client then re-filtered by substring on his
# DISPLAYED name and threw him straight back out. Searching "bill" returned
# Billups and Billeter and no Williams at all, and nothing anywhere errored.
#
# The fix was the `alt` column. This asserts the fix still holds: for every
# record, every token it was filed under is reachable from name + alt.
# ---------------------------------------------------------------------------
@check("every name-index record is reachable from name + alt")
def _index_reachable():
    from build_name_index import tokens_for            # noqa: PLC0415

    idx = read(DATA / "name_index.json")
    C = {c: i for i, c in enumerate(idx["columns"])}
    bad = []
    checked = 0
    for p in sorted((DATA / "names").glob("*.json")):
        shard = read(p)
        prefix = shard["prefix"]
        for r in shard["rows"]:
            checked += 1
            name, alt = r[C["name"]], (r[C["alt"]] or "")
            hay = name.lower()
            alts = alt.split()
            # At least one token starting with this shard's prefix must be
            # findable the way the client looks for it.
            reachable = any(
                t.startswith(prefix) and (t in hay or any(a.startswith(t) for a in alts))
                for t in tokens_for(name))
            if not reachable:
                bad.append((prefix, name))
            if len(bad) > 5:
                break
        if len(bad) > 5:
            break
    return (not bad, f"{checked:,} records checked"
            + (f"; UNREACHABLE e.g. {bad[:5]}" if bad else ""))


# ---------------------------------------------------------------------------
# 5. The call-outcome vocabulary agrees in all three places
#
# dial.js offers the buttons, api/log validates them, and serve.py imitates that
# validation for local development. A disposition present in one and missing
# from another fails only at the moment a rep presses it, in production, having
# just made the call.
# ---------------------------------------------------------------------------
@check("call outcomes agree across dial.js, api/log and serve.py")
def _outcomes():
    dial = text(WEB / "dial.js")
    # SCOPED TO THE OUTCOMES BLOCK. This used to scan the whole file for
    # `key: "..."`, which was correct until a second list of keyed objects
    # appeared -- the purpose chips -- and all four were then reported as
    # buttons the API would reject. A pattern that matches the SHAPE of the
    # thing rather than the thing itself eventually matches something else,
    # and a false alarm is expensive in a guard whose only value is being
    # believed.
    block = dial.split("const OUTCOMES = [", 1)[-1].split("\n  ];", 1)[0]
    js = set(re.findall(r'key:\s*"([a-z-]+)"', block))
    # `skipped` is a real disposition with no button, so it is added rather
    # than being a mismatch.
    js.add("skipped")
    # Retired keys are accepted by both servers ON PURPOSE and offered by
    # neither. Events carrying them are already in Table Storage, and a phone
    # that has not reloaded since the rename can still send one -- refusing it
    # would lose a call outcome at the only moment it cannot be re-entered. So
    # this is no longer an equality; it is containment in a known direction.
    retired = set(re.findall(r'"?([a-z-]+)"?:\s*"',
                             dial.split("const RETIRED = {", 1)[-1].split("};", 1)[0]))
    api = set(re.findall(r'"([a-z-]+)"',
              re.search(r"const DISPOSITIONS = new Set\(\[(.*?)\]\)",
                        text(API / "log" / "index.js"), re.S).group(1)))
    py = set(re.findall(r'"([a-z-]+)"',
             re.search(r"DISPOSITIONS = \{(.*?)\}",
                       text(ROOT / "serve.py"), re.S).group(1)))
    problems = []
    if js - api:
        problems.append(f"buttons the API would reject {sorted(js - api)}")
    if api - js - retired:
        problems.append(f"api accepts {sorted(api - js - retired)}, which is neither "
                        f"a button nor listed as retired")
    if api != py:
        problems.append(f"serve.py differs {sorted(api ^ py)}")
    return (not problems,
            f"{len(js)} live, {len(retired)} retired, {len(api)} accepted"
            + ("; " + "; ".join(problems) if problems else ""))


# ---------------------------------------------------------------------------
# 6. The queue cap is the same number on both sides
#
# The client reports "N did not fit" from its own constant; the server trims to
# its own. If they disagree the client's arithmetic is wrong and the tail of a
# rep's queue disappears while the UI says everything was added.
# ---------------------------------------------------------------------------
@check("MAX_QUEUE matches between dial.js and api/shared/store.js")
def _queue_cap():
    a = re.search(r"const MAX_QUEUE = (\d+)", text(WEB / "dial.js")).group(1)
    b = re.search(r"const MAX_QUEUE = (\d+)", text(API / "shared" / "store.js")).group(1)
    return (a == b, f"dial.js {a}, store.js {b}")


# ---------------------------------------------------------------------------
# 7. The dev fixture and the real store return the same field names
#
# THE BUG THIS ENCODES: listDnc returned {crd, by, at, reason} and serve.py
# returned {crd, advisorName, userName, atUtc, reason}. Identical code path,
# two answers -- the class of bug that works perfectly on a laptop and fails
# only in Azure.
# ---------------------------------------------------------------------------
@check("do-not-call records have identical fields in serve.py and store.js")
def _dnc_shape():
    store = text(API / "shared" / "store.js")
    block = re.search(r"async function listDnc\(\).*?return out;", store, re.S).group(0)
    js = set(re.findall(r"(\w+):\s*e\.", block))
    py_block = re.search(r'dnc = \{(.*?)\}', text(ROOT / "serve.py"), re.S).group(1)
    py = set(re.findall(r'"(\w+)":', py_block))
    return (js == py, f"store.js {sorted(js)} vs serve.py {sorted(py)}")


# ---------------------------------------------------------------------------
# 8. Versioned assets are pinned to what is on disk
#
# An entire session of front-end work once shipped invisibly because index.html
# still pinned the previous hash. web_assets.check_assets() is the authority;
# this simply makes it a gate rather than something to remember to run.
# ---------------------------------------------------------------------------
@check("index.html, field.html and sw.js pin the current assets")
def _asset_pins():
    from web_assets import check_assets                # noqa: PLC0415
    stale = check_assets()
    return (not stale, "all current" if not stale else f"STALE: {stale}")


# ---------------------------------------------------------------------------
# 9. sw.js is part of the field version hash
#
# THE BUG THIS ENCODES: the service worker's cache is named after this tag, and
# the tag ignored sw.js. A fix to the worker's own caching logic therefore left
# the cache name unchanged and every poisoned entry in place -- the fix could
# not reach the devices that needed it.
# ---------------------------------------------------------------------------
@check("editing sw.js changes the field version tag")
def _sw_in_tag():
    from web_assets import field_tag                   # noqa: PLC0415
    sw = WEB / "sw.js"
    before = field_tag()
    original = sw.read_bytes()
    try:
        sw.write_bytes(original + b"\n// audit probe\n")
        after = field_tag()
    finally:
        sw.write_bytes(original)
    restored = field_tag()
    return (before != after and before == restored,
            f"{before} -> {after} -> {restored}")


# ---------------------------------------------------------------------------
# 10. The data layer is never cached by the service worker
#
# The tiles carry names, direct lines and which colleague owns each
# relationship. A worker answers without touching the network, which also means
# without touching authentication -- so caching them would put CRM data at rest
# on the device and make access revocation stop being immediate.
# ---------------------------------------------------------------------------
@check("service worker refuses to cache /data, /api and /.auth")
def _sw_scope():
    sw = text(WEB / "sw.js")
    missing = [p for p in ("/data/", "/api/", "/.auth/") if f'"{p}"' not in sw]
    return (not missing, "all three excluded" if not missing
            else f"NOT excluded: {missing}")


# ---------------------------------------------------------------------------
# 11. Every route serving data requires authentication
#
# deploy_swa.py refuses to publish without this, but the check is worth having
# separately: the site looks identical whether or not it is present.
# ---------------------------------------------------------------------------
@check("staticwebapp.config.json guards /data, /api and /*")
def _routes():
    cfg = read(WEB / "staticwebapp.config.json")
    guarded = {r.get("route") for r in cfg.get("routes", [])
               if "authenticated" in (r.get("allowedRoles") or [])}
    need = {"/data/*", "/api/*", "/*"}
    return (need <= guarded, f"guarded {sorted(guarded)}")


# ---------------------------------------------------------------------------
# 12. Profile and website links are absolute
#
# THE BUG THIS ENCODES: 23,259 advisor profile links were relative or
# protocol-relative, so the map sent reps to localhost. Every one rendered as a
# working button.
# ---------------------------------------------------------------------------
@check("advisor profile and LinkedIn links are absolute")
def _absolute_links():
    adv = read(DATA / "contacts.json")["advisors"]
    bad = [v.get("pu") or v.get("li") for v in adv.values()
           if (v.get("pu") and not str(v["pu"]).startswith("http"))
           or (v.get("li") and not str(v["li"]).startswith("http"))]
    return (not bad, f"{len(bad)} relative links"
            + (f" e.g. {bad[:3]}" if bad else ""))


# ---------------------------------------------------------------------------
# 13. Every name-index record can be hydrated
#
# THE BUG THIS ENCODES: a national search hit is a padded row -- name, city and
# state, nothing else -- and queueing one stored a snapshot with no phone. The
# session card then showed a person with no Call button and no explanation, so
# the queue looked full and was partly undialable.
#
# The fix fetches the record's tile before queueing, which only works if the
# cell it names actually exists. A record pointing at a missing tile would put
# us straight back to an empty snapshot, silently.
# ---------------------------------------------------------------------------
@check("every name-index record points at a tile that exists")
def _hydratable():
    cells = set(read(DATA / "tile_index.json")["cells"])
    idx = read(DATA / "name_index.json")
    C = {c: i for i, c in enumerate(idx["columns"])}
    missing, checked = set(), 0
    for p in sorted((DATA / "names").glob("*.json")):
        for r in read(p)["rows"]:
            checked += 1
            cell = r[C["cell"]]
            if cell not in cells:
                missing.add(cell)
    return (not missing,
            f"{checked:,} records; {len(cells):,} tiles"
            + (f"; MISSING {sorted(missing)[:5]}" if missing else ""))


# ---------------------------------------------------------------------------
# 14. A queue snapshot can carry every field the session card reads
#
# The card renders phone, email, firm, city and state from the stored snapshot.
# If a builder ever drops one of those columns from the tiles, the snapshot
# silently carries an empty string and the card silently omits a button.
# ---------------------------------------------------------------------------
@check("tiles carry every column a queue snapshot needs")
def _snapshot_columns():
    cols = set(read(DATA / "tile_index.json")["columns"])
    need = {"crd", "name", "firm", "phone", "phone_kind", "city", "state", "email"}
    missing = need - cols
    return (not missing, f"{len(need)} needed"
            + (f"; MISSING from tiles: {sorted(missing)}" if missing else ""))


# ---------------------------------------------------------------------------
# 15. The dev shim compares timestamps the way the Azure store does
#
# THE BUG THIS ENCODES: cycle progress asks "my outcomes since this instant".
# The Azure store compares epoch milliseconds; serve.py compared ISO strings.
# The shim writes seconds ("...:45Z") and the browser sends milliseconds
# ("...:45.123Z"), and lexically "Z" sorts after "." -- so every event from the
# same second counted as being after the cutoff, and a brand new cycle opened
# reading "3 of 3 done".
#
# Correct in production, wrong on a laptop. That direction is the dangerous one,
# because the laptop is where behaviour gets judged.
# ---------------------------------------------------------------------------
@check("serve.py compares timestamps numerically, like the Azure store")
def _since_semantics():
    import importlib.util                                # noqa: PLC0415
    spec = importlib.util.spec_from_file_location("_srv", ROOT / "serve.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    same_second = mod._iso_ms("2026-08-13T21:30:45Z")
    cutoff = mod._iso_ms("2026-08-13T21:30:45.123Z")
    later = mod._iso_ms("2026-08-13T21:30:46Z")
    ok = (same_second < cutoff) and (later >= cutoff) and cutoff > 0
    return (ok, f"same-second {same_second < cutoff and 'excluded' or 'INCLUDED'}, "
                f"later {later >= cutoff and 'included' or 'EXCLUDED'}")


# ---------------------------------------------------------------------------
# 16. Saving the queue reconciles the list summary the picker renders
#
# THE BUG THIS ENCODES: the picker labels each list "<name> (<count>)" from the
# summaries in state.lists, which were refetched only on open/create/rename/
# delete. Bulk-adding 40 people wrote the queue and left the picker reading (0).
# Reproduced in the browser: items 3, picker "From map (0)".
#
# Nothing raises. The rep just reads a number that is not true.
# ---------------------------------------------------------------------------
@check("saving the queue updates the count the list picker shows")
def _picker_counts():
    src = (WEB / "dial.js").read_text(encoding="utf-8")
    body = src.split("async function save()", 1)[-1].split("\n  }", 1)[0]
    ok = "state.lists.find" in body and "row.count = state.items.length" in body
    return (ok, "save() reconciles state.lists"
            if ok else "save() writes the queue but leaves the picker summary stale")


# ---------------------------------------------------------------------------
# 17. A bulk add into a SAVED list asks before it lands
#
# THE BUG THIS ENCODES: every bulk-add button dropped its people into whatever
# list happened to be open. A rep with a hand-curated monthly list one click
# away from a 200-person map selection could contaminate it with no undo.
# THE SECOND VERSION OF THE BUG: the guard fired only when the open list was a
# SAVED one, exempting the default scratch list called "current". But a rep who
# has spent ten minutes putting forty people into "current" does not think of
# it as scratch, and the next bulk tap poured two hundred more on top with no
# undo and no way to tell the two groups apart. The test is now whether the
# destination ALREADY HAS PEOPLE IN IT, whatever it is called. An empty list
# still adds silently -- there is nothing there to damage, and a prompt on
# every single bulk add teaches the rep to dismiss it unread.
# ---------------------------------------------------------------------------
@check("bulk add asks where to land when the list already has people in it")
def _bulk_destination():
    src = (WEB / "app.js").read_text(encoding="utf-8")
    body = src.split("async function bulkQueue(", 1)[-1].split("\n}", 1)[0]
    guarded = "Dial.state.items.length" in body and "askDestination(" in body
    # And every bulk path must go through that one function.
    paths = len(re.findall(r"\bawait bulkQueue\(", src))
    return (guarded and paths >= 3,
            f"guarded={guarded}, {paths} bulk paths route through bulkQueue()")


# ---------------------------------------------------------------------------
# 18. Do-not-call fails CLOSED
#
# THE BUG THIS ENCODES: the suppression list was loaded inside a
# `.catch(() => {})` shared with the queue, so a failed /api/dnc left an empty
# Map -- and an empty Map means every suppression test silently passes. The app
# looked healthy, said nothing, and blocked nobody.
#
# A guard that stops guarding without saying so is worse than no guard, because
# the rep has stopped checking.
# ---------------------------------------------------------------------------
@check("the desk and the phone call every advisor the same thing")
def _one_display_name():
    """THE BUG THIS ENCODES: 47,371 advisors -- a third of the overlap -- had a
    different name in the two applications.

    The desktop built a name from the SEC feed; the field view took whatever
    contact record won in contacts.json. Nobody compared them, because they were
    written by different scripts reading different files and each looked
    perfectly reasonable on its own.

    It surfaced as a SEARCH MISS: Cosmo Boyd on the desk, Montague Boyd on the
    phone, and a rep who read the first and typed it into the second found
    nobody. That is the shape of the whole class -- not an error, an absence.

    Checked per CRD across the three artifacts a rep actually reads: the map
    pin, the desktop search index, and the field tile.
    """
    index = {str(r[0]): r[1] for r in read(DATA / "advisor_index.json")["advisors"]}
    if not index:
        return False, "advisor_index.json has no advisors"

    tiles, bad = 0, []
    for path in (DATA / "tiles").glob("*.json"):
        tile = read(path)
        C = {c: i for i, c in enumerate(tile["columns"])}
        for row in tile["rows"]:
            crd = str(row[C["crd"]])
            want = index.get(crd)
            tiles += 1
            if want and row[C["name"]] != want:
                bad.append(f"{crd} desk {want!r} phone {row[C['name']]!r}")
    pins = 0
    for path in DATA.glob("pins_??.json"):
        layer = read(path)
        for pin in layer["pins"]:
            want = index.get(str(pin[6]))
            pins += 1
            if want and pin[7] != want:
                bad.append(f"{pin[6]} search {want!r} pin {pin[7]!r}")

    return (not bad,
            f"{tiles:,} field rows and {pins:,} map pins agree with the search index"
            if not bad else
            f"{len(bad):,} DISAGREE, e.g. {bad[:3]}")


@check("an in-app reply keeps the thread and passes the same gates as a campaign")
def _reply_send_is_a_real_reply():
    """TWO FAILURES THIS PREVENTS, and they are different in kind.

    THE THREAD. Replying by constructing a new message with "RE:" prefixed looks
    right to a human and is a NEW conversation to every mail system involved. It
    would silently cost us the strongest matching route we have: when the
    advisor answers, `conversationId` leads nowhere and the sweep drops to
    references or sender-only. The feature would quietly degrade the feature it
    is built on. Graph's createReply keeps the real conversation.

    THE GATES. A reply is still an outbound email to an advisor. If the composer
    skipped suppression, the quietest path to a suppressed address would be the
    one with no checks on it -- and a hard bounce or an opt-out is a statement
    about the ADDRESS, not about which screen the mail was typed on. The
    compliance blind copy is the same argument.

    Checked together because they are the two ways this feature could look
    finished and be wrong.
    """
    path = API / "shared" / "email-reply-send.js"
    if not path.exists():
        return True, "in-app reply not built yet"
    src = text(path)

    def body_of(name):
        """The text of one function, sliced by index.

        Not a regex: these signatures carry default arguments containing their
        own brackets, and a pattern for the argument list stops inside them --
        which is how an earlier check here searched the wrong text and passed on
        deliberately broken code.
        """
        start = src.find(f"async function {name}(")
        if start < 0:
            return ""
        end = src.find("\nasync function ", start + 1)
        return src[start:end if end > start else len(src)]

    problems = []

    # The gates live in two shared helpers so both send paths get them. Check
    # that the helpers really do the work, AND that each path calls them --
    # either half alone would be satisfied by dead code.
    if "blockedAmong" not in body_of("guard") and "isSuppressed" not in body_of("guard"):
        problems.append("guard() does not check suppression")
    if "complianceBcc" not in body_of("applyCompliance"):
        problems.append("applyCompliance() does not use core.complianceBcc")

    for name in ("reply", "followUp"):
        fn = body_of(name)
        if not fn:
            problems.append(f"{name}() not found")
            continue
        if "guard(" not in fn:
            problems.append(f"{name}() skips the suppression gate")
        if "applyCompliance(" not in fn:
            problems.append(f"{name}() skips the compliance blind copy")

    # A reply must continue the thread; a follow-up must NOT.
    if "createReply" not in body_of("reply"):
        problems.append("reply() does not use Graph createReply -- a fabricated RE: starts a new thread")
    if "createReply" in body_of("followUp"):
        problems.append("followUp() reuses the old thread instead of starting a conversation")

    return (not problems,
            "both send paths pass suppression and compliance; reply continues the thread"
            if not problems else
            "SEND PATH IS A BACK DOOR: " + "; ".join(problems))


@check("no deployed data file is large enough to be fragile")
def _swa_file_limit():
    """WHAT ACTUALLY HAPPENED, rather than the limit I first assumed.

    contacts.json reached 40 MB and started returning a 500 from Static Web
    Apps. I diagnosed a 25 MB per-file limit; that was ASSERTED, not verified,
    and the evidence contradicts it -- the same file had been served for weeks.

    The real sequence was:

      DATA_VERSION had been frozen at 20260803b since 3 August, so every data
      URL was a warm CDN key. Bumping it to 20260822a created cold keys, and
      the first origin fetch of a 40 MB file in three weeks failed. Sharding it
      to ~9.7 MB fixed that -- and then ONE shard came back 500 anyway, from a
      corrupted upload, which a redeploy cleared.

    So the honest lesson is not a number. It is that large single files are
    fragile here in ways smaller ones are not: they fetch serially, they are a
    bigger target for a partial upload, and when one breaks it takes the whole
    dataset with it rather than a quarter of it.

    This threshold is therefore a PRUDENCE limit, not a platform one. It exists
    so nobody discovers the next fragile file the way we discovered this one --
    from a rep looking at a card that never loads.
    """
    # Not a documented platform ceiling -- see the docstring. 25 MB is where a
    # file stops being routinely re-fetchable and starts being a single point of
    # failure worth splitting.
    limit = 25 * 1024 * 1024
    warn_at = 20 * 1024 * 1024

    # Ask the DEPLOYER what ships, rather than keeping a second list here that
    # could disagree with it. contacts.json is excluded there precisely because
    # of this limit, and a check that flagged it anyway would be reporting a
    # solved problem forever.
    sys.path.insert(0, str(SRC))
    from deploy_swa import included                        # noqa: PLC0415

    over, close = [], []
    for path in sorted(WEB.rglob("*")):
        if not path.is_file() or not included(path):
            continue
        size = path.stat().st_size
        if size > limit:
            over.append(f"{path.relative_to(WEB)} {size / 1e6:.1f} MB")
        elif size > warn_at:
            close.append(f"{path.relative_to(WEB)} {size / 1e6:.1f} MB")

    if over:
        return False, (f"FRAGILE ({limit / 1e6:.0f} MB+): {over} -- split these. A file "
                       "this size fetches serially and takes the whole dataset "
                       "down when one upload goes wrong")
    return True, ("every deployed file is servable"
                  + (f"; approaching the limit: {close}" if close else ""))


@check("the page's data build is not older than the data it will load")
def _data_version_current():
    """THE SYMPTOM THIS CATCHES BEFORE A REP DOES: an unfixable warning.

        "This page is running an older build (20260803b) than the data it just
         loaded (generated Aug 22, 2026). Reload with Ctrl+F5."

    Reloading does not help, because DATA_VERSION is a constant in app.js and
    the browser is doing exactly what it was told. The runtime check is right;
    the constant is stale.

    It went stale the first time somebody rebuilt the data without also
    remembering a constant three thousand lines away in another language --
    which is to say, immediately. web_assets.py now bumps it automatically; this
    is the backstop that turns a rep-visible warning into a build-time failure.
    """
    app = text(WEB / "app.js")
    found = re.search(r'const DATA_VERSION = "([^"]+)";', app)
    if not found:
        return False, "DATA_VERSION not found in webapp/app.js"
    shipped = re.sub(r"[^0-9]", "", found.group(1))[:8]

    meta_path = DATA / "metadata.json"
    if not meta_path.exists():
        return True, "no metadata.json to compare against"
    generated = json.loads(text(meta_path)).get("generated_utc", "")
    built = str(generated)[:10].replace("-", "")
    if not built or not shipped:
        return True, "nothing to compare"
    return (shipped >= built,
            f"DATA_VERSION {found.group(1)} covers data built {built}"
            if shipped >= built else
            f"STALE: app.js says {found.group(1)} but the data was built {built} -- "
            f"every rep gets a warning no reload can clear. Run src/web_assets.py")


@check("a partial write never uses Replace")
def _partial_writes_use_merge():
    """THE BUG THIS ENCODES: a failed sweep erased its own watermark.

    Azure Table Storage's Replace mode makes the stored entity BECOME the
    payload -- every property absent from it is deleted. Two writers here build
    partial entities from whatever the caller supplied:

        putSweepState()   the error path writes {lastError} alone, and under
                          Replace that deleted watermarkUtc. One token lapse
                          sent that rep back to a 48-hour window, silently.
        putEngagement()   wrote a folded state with no actedAt and deleted it,
                          so a reply a rep had already handled came back as new.

    186 tests passed while both were live, because a hand-written mock records
    what is WRITTEN and models nothing about what a write destroys. That is why
    test/helpers/fake-table.js exists and why this check does too.

    putConnection() legitimately uses Replace -- it rewrites the whole entity
    deliberately, with a field whitelist, so SDK metadata cannot leak into the
    token cache. It is named here rather than inferred.
    """
    src = text(API / "shared" / "email-store.js")
    deliberate = {"putConnection"}
    offenders = []
    for match in re.finditer(r"async function (\w+)\(", src):
        name = match.group(1)
        end = src.find("\nasync function ", match.end())
        body = src[match.start():end if end > 0 else len(src)]
        if "upsertEntity" not in body or '"Replace"' not in body:
            continue
        if name in deliberate:
            continue
        # A writer that builds its entity conditionally is a PARTIAL writer:
        # some properties are present only when the caller passed them.
        if re.search(r"if \(\w+\[key\] !== undefined\)|if \(entity\[key\] !== undefined\)", body):
            offenders.append(name)
    return (not offenders,
            "partial writers use Merge; only putConnection replaces, deliberately"
            if not offenders else
            f"REPLACE ON A PARTIAL WRITE: {offenders} -- absent fields are DELETED")


@check("the store is tested against real Table Storage semantics")
def _faithful_store_double():
    """WHY THIS IS CHECKED AT ALL: the mocks certified the bugs.

    Two deployment-blocking defects were pure Table Storage semantics -- Replace
    deleting absent properties, and ascending key order deciding which rows a
    truncated read keeps. Neither could be expressed by a mock that stores what
    it is given in a Map, so 186 green tests meant nothing about either.

    The double must therefore actually implement the behaviours that bit, and be
    used. A test helper nobody imports is decoration.
    """
    helper = API / "test" / "helpers" / "fake-table.js"
    if not helper.exists():
        return False, "test/helpers/fake-table.js is missing -- the mocks cannot catch Replace"
    body = text(helper)
    missing = [name for name, needle in (
        ("Replace semantics", 'mode === "Replace"'),
        ("Merge semantics", "...this.rows.get(key)"),
        ("key ordering", "localeCompare"),
        ("illegal key rejection", "illegalKey"),
    ) if needle not in body]
    users = [p.name for p in (API / "test").glob("*.test.js")
             if "fake-table" in text(p)]
    if missing:
        return False, f"the double does not model: {missing}"
    return (bool(users),
            f"modelled and used by {', '.join(sorted(users))}"
            if users else "nothing imports the double, so it proves nothing")


@check("the work queue is built from engagement, never from volume")
def _queue_is_not_a_volume_counter():
    """THE PRODUCT MISTAKE THIS PREVENTS: a dashboard that rewards sending.

    "117 emails sent this week" is the number that makes a screen look busy and
    makes a rep behave worse. It is a measure of effort spent, not of anything
    achieved, and putting it at the top of a queue quietly instructs the team to
    maximise it -- which is the behaviour the 25-a-day limits already exist to
    restrain. The two features would then be pulling against each other, and the
    dashboard would win, because it is the thing a rep looks at every morning.

    So every reason an advisor can appear in the queue must describe the
    RELATIONSHIP moving: they replied, they have gone quiet, a follow-up is due,
    an address broke. None of them may be a count of what we sent.

    The counts ARE computed and stored -- outbound30d is useful context on a
    profile. This checks only that none of them can put somebody in the queue.
    """
    src = text(API / "shared" / "email-engagement.js")
    block = re.search(r"const REASONS = \[(.*?)\];", src, re.S)
    if not block:
        return False, "REASONS not found in email-engagement.js"
    keys = re.findall(r'key:\s*"(\w+)"', block.group(1))
    if not keys:
        return False, "no reasons declared"

    banned = ("sent", "volume", "count", "emails", "activity_count", "quota", "target")
    offending = [k for k in keys if any(word in k.lower() for word in banned)]

    # The reason() function is the only thing that may put somebody in the
    # queue, so a volume field appearing inside it is the real failure.
    #
    # Sliced by index rather than matched by regex: the signature is
    # `function reason(state, now = Date.now())`, and a pattern for the argument
    # list stops at the `)` inside Date.now() -- which silently searched the
    # wrong text and reported a clean pass on deliberately broken code.
    start = src.find("function reason(")
    volume_in_reason = []
    if start >= 0:
        end = src.find("\n}", start)
        body = src[start:end if end > start else len(src)]
        for field in ("outbound30d", "inbound30d", "sentCount", "emailsSent"):
            if re.search(rf"\b{field}\b", body):
                volume_in_reason.append(field)

    problems = ([f"reason key {k!r}" for k in offending]
                + [f"reason() reads {f}" for f in volume_in_reason])
    return (not problems,
            f"{len(keys)} queue reasons, all relationship signals: {', '.join(keys)}"
            if not problems else
            "QUEUE REWARDS VOLUME: " + "; ".join(problems))


@check("the desk and the phone use the server's word for what happened")
def _activity_wording_is_shared():
    """THE DIVERGENCE THIS PREVENTS: two apps disagreeing about a reply.

    This is the same failure as the display-name split that made Cosmo Boyd
    findable on the desk and invisible on the phone: two clients each deciding
    the same thing for themselves, and nothing comparing them.

    Whether an inbound message is a REPLY, an unprompted email, or an
    out-of-office is a judgement with real consequences -- a rep about to dial
    reads it as "they answered us". It is decided once, in
    shared/email-activity.js, and both clients render `entry.label` verbatim.

    So neither client may contain the wording itself. A literal "Reply received"
    in app.js or field.js means somebody has started deciding locally again, and
    the two will drift the moment one is edited.
    """
    phrases = ["Reply received", "Automatic reply", "Delivery failure",
               "Email received", "Email sent"]
    server = text(API / "shared" / "email-activity.js")
    missing = [p for p in phrases if f'"{p}' not in server]
    if missing:
        return False, f"email-activity.js no longer produces {missing}; the check needs updating"

    offenders = []
    for name in ("app.js", "field.js"):
        src = text(WEB / name)
        # Comments strip first: both files EXPLAIN this rule in prose, and a
        # check that reads its own documentation as a violation is a check
        # nobody will keep.
        code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
        code = re.sub(r"<!--.*?-->", "", code, flags=re.S)
        for phrase in phrases:
            if phrase in code:
                offenders.append(f"{name}: {phrase!r}")
    return (not offenders,
            "both clients render the server's label; neither invents its own"
            if not offenders else
            f"WORDING DECIDED LOCALLY: {offenders} -- the desk and the phone will drift")


@check("advisor-authored email is fetched as text, never as HTML")
def _message_body_is_text():
    """THE VULNERABILITY THIS CLOSES: mail from outside the firm, in our own DOM.

    A rep clicks View on a reply and the message is rendered inside the advisor
    map. That text was written by somebody outside the firm and is not trusted.
    Fetching it as HTML would put attacker-controlled markup one innerHTML away
    from executing as script in a page that holds the rep's session.

    Asking Graph for TEXT removes the class of bug rather than defending against
    it: there is no markup to sanitise, so there is nothing for a sanitiser to
    get wrong. The client escapes it again on the way to the DOM; this checks
    the half that a future "let's show formatting" change would quietly undo.

    uniqueBody rather than body is also pinned. `body` is the entire quoted
    chain, so a five-word reply would arrive wrapped in every message before it.
    """
    src = text(API / "shared" / "graph-mail.js")
    fn = re.search(r"async function getMessageContent\b(.*?)\n\}", src, re.S)
    if not fn:
        return True, "getMessageContent not present yet"
    block = fn.group(1)
    problems = []
    if 'outlook.body-content-type="text"' not in block:
        problems.append('does not request text (Prefer: outlook.body-content-type="text")')
    if "uniqueBody" not in block:
        problems.append("does not select uniqueBody")
    if re.search(r"\$select[^\"]*\"[^\"]*\bbody\b(?!-)", block) and "uniqueBody" not in block:
        problems.append("selects the full body")
    return (not problems,
            "message text is requested as plain text, uniqueBody only"
            if not problems else
            "UNTRUSTED HTML COULD REACH THE PAGE: " + "; ".join(problems))


@check("reading a message checks whose mailbox it is first")
def _activity_ownership():
    """THE RULE THIS ENFORCES: the timeline is shared, the content is not.

    Activity rows are deliberately visible to every rep -- knowing a colleague
    contacted this advisor on Tuesday is what stops two people working the same
    person in the same week. The MESSAGE is different: it lives in one mailbox
    and only that rep's delegated token reaches it.

    Microsoft enforces that regardless, so this is not the only guard. But the
    check must happen BEFORE the Graph call, because Graph's refusal is a 404
    that reads as "this email was deleted" -- and a rep chasing a message that
    looks deleted will not conclude that it was simply somebody else's.
    """
    path = API / "shared" / "email-activity.js"
    if not path.exists():
        return True, "activity read side not present yet"
    src = text(path)
    fn = re.search(r"async function messageContent\b(.*?)\n\}", src, re.S)
    if not fn:
        return False, "messageContent not found in email-activity.js"
    block = fn.group(1)
    owner_at = block.find("activityOwner")
    graph_at = block.find("getMessageContent")
    if owner_at < 0:
        return False, "messageContent never checks activityOwner -- any rep could read any mailbox"
    if graph_at >= 0 and owner_at > graph_at:
        return False, "the ownership check runs AFTER the Graph call, so the refusal is Graph's 404"
    return True, "ownership is settled before Graph is asked"


@check("no mailbox reader is scoped to a single folder")
def _mailbox_scope():
    """THE FAILURE THIS PREVENTS: a rep's own Outlook rule ending detection.

    Reply and bounce detection both read a rep's mailbox. If either asks for a
    NAMED FOLDER, then a rule a rep writes for their own convenience -- without
    telling anyone, without knowing this exists -- files advisor mail somewhere
    unpolled, and detection stops. Nothing errors. Every screen goes on saying
    "no reply recorded" with total confidence.

    /me/messages spans every folder, so there is no folder assumption left to
    invalidate. This check exists because the fix is one URL that would be very
    easy to "tidy" back into a folder query later.
    """
    src = text(API / "shared" / "graph-mail.js")
    code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    scoped = re.findall(r"/me/mailFolders/[A-Za-z]+/messages", code)
    return (not scoped,
            "mailbox readers query /me/messages, which spans every folder"
            if not scoped else
            f"FOLDER-SCOPED: {sorted(set(scoped))} -- an Outlook rule silently ends detection")


@check("email timers use deliberate non-overlapping minutes")
def _sweep_schedules():
    """THE BUG THIS PREVENTS: two functions racing one mailbox's token cache.

    putConnection() in email-store.js upserts with "Replace" and NO etag, while
    tokenFor() reads the MSAL cache, refreshes it, and writes the rotated cache
    back. Two sweeps touching the same mailbox in the same minute can interleave
    and lose one process's rotated refresh token. It surfaces later, somewhere
    else, as a spurious needsReconnect for a rep who did nothing wrong -- which
    is close to unfindable from the symptom.

    The crons are offset so the collision cannot happen. That offset looks
    arbitrary, so it is pinned here.
    """
    def minutes(name):
        path = API / name / "function.json"
        if not path.exists():
            return None
        schedule = json.loads(text(path))["bindings"][0].get("schedule", "")
        parts = schedule.split()
        return set(parts[1].split(",")) if len(parts) > 1 else set()

    bounce = minutes("email-bounce-sweep")
    reply = minutes("email-reply-sweep")
    repair = minutes("email-engagement-repair")
    direct = minutes("email-direct-repair")
    if bounce is None or reply is None:
        return True, "reply sweep not deployed yet"
    pairs = [("bounce/reply", bounce & reply)]
    if repair is not None:
        pairs.extend([("bounce/repair", bounce & repair), ("reply/repair", reply & repair)])
    if direct is not None:
        pairs.extend([("bounce/direct", bounce & direct), ("reply/direct", reply & direct)])
        if repair is not None:
            pairs.append(("repair/direct", repair & direct))
    clash = [(name, sorted(values)) for name, values in pairs if values]
    return (not clash,
            f"bounce :{','.join(sorted(bounce))}; reply :{','.join(sorted(reply))}; "
            + (f"repair :{','.join(sorted(repair))}" if repair is not None else "repair not deployed")
            + (f"; direct :{','.join(sorted(direct))}" if direct is not None else "; direct not deployed")
            + " -- no overlap"
            if not clash else
            "TIMER COLLISION: " + "; ".join(f"{name} at {values}" for name, values in clash))


@check("engagement repair is atomic, mailbox-free, and disabled by default")
def _engagement_repair_safety():
    """Projection repair must survive crashes without broadening mailbox access.

    Activity and its dirty marker belong in one same-partition transaction. The
    consumer is separately gated, has a user canary, and must never import Graph
    or auth. Conditional marker acknowledgement is what prevents activity that
    arrives during a fold from being cleared by the older worker.
    """
    repair_path = API / "email-engagement-repair" / "index.js"
    config_path = API / "email-engagement-repair" / "function.json"
    if not repair_path.exists() or not config_path.exists():
        return True, "engagement repair not deployed yet"
    repair = text(repair_path)
    store_src = text(API / "shared" / "email-store.js")
    config = json.loads(text(config_path))
    binding = config.get("bindings", [{}])[0]
    checks = {
        "timer is explicitly gated": "EMAIL_ENGAGEMENT_REPAIR_ENABLED" in repair
            and '!== "1"' in repair,
        "timer has a user canary": "EMAIL_ENGAGEMENT_REPAIR_USER_IDS" in repair,
        "timer never imports Graph/auth": "graph-mail" not in repair and "email-auth" not in repair,
        "schedule is literal": bool(binding.get("schedule")) and "%" not in binding.get("schedule", ""),
        "runOnStartup is false": binding.get("runOnStartup") is False,
        "activity and marker share a transaction": "submitTransaction(actions)" in store_src
            and 'kind: "engagement_dirty"' in store_src,
        "marker acknowledgement is conditional": "ackEngagementDirty" in store_src
            and "{ etag: marker.etag }" in store_src,
        "projection source is user scoped": "listActivityForUser" in store_src,
    }
    failed = [name for name, ok in checks.items() if not ok]
    return (not failed,
            "atomic marker, conditional ack, user canary, literal disabled timer, no mailbox imports"
            if not failed else "ENGAGEMENT REPAIR SAFETY MISSING: " + "; ".join(failed))


@check("direct sends are durable, content-minimal, and never auto-resubmit uncertainty")
def _direct_send_ledger_safety():
    """Pin the boundary that prevents a lost Graph response becoming two emails."""
    direct_path = API / "shared" / "email-direct-send.js"
    store_path = API / "shared" / "email-direct-store.js"
    queue_path = API / "shared" / "email-direct-queue.js"
    worker_path = API / "email-direct-worker" / "function.json"
    repair_path = API / "email-direct-repair" / "index.js"
    repair_config_path = API / "email-direct-repair" / "function.json"
    if not all(path.exists() for path in [direct_path, store_path, queue_path,
                                          worker_path, repair_path, repair_config_path]):
        return True, "durable direct-send package not deployed yet"
    direct = text(direct_path)
    store_src = text(store_path)
    queue_src = text(queue_path)
    repair = text(repair_path)
    route = text(API / "email" / "index.js")
    client = text(WEB / "email.js")
    worker_binding = json.loads(text(worker_path))["bindings"][0]
    repair_binding = json.loads(text(repair_config_path))["bindings"][0]
    ambiguous = re.search(r"if \(ambiguous\) \{(.*?)\n\s*\} else if", direct, re.S)
    remembered = re.search(r"function rememberDirect\b(.*?)\n\s*\}", client, re.S)
    forbidden_store_fields = re.findall(
        r"\b(?:body|bodyText|bodyHtml|recipientEmail|recipientsJson|attachmentsJson|accessToken)\s*:",
        re.sub(r"/\*.*?\*/|^\s*//.*$", "", store_src, flags=re.S | re.M))
    checks = {
        "separate operation table": 'TABLE_NAME = "EmailDirectSendOps"' in store_src,
        "operation and marker are transactional":
            '[["create", operation], ["create", marker]]' in store_src,
        "store contains no content or token fields": not forbidden_store_fields,
        "queue is isolated": worker_binding.get("queueName") == "email-direct-work"
            and 'QUEUE_NAME = "email-direct-work"' in queue_src,
        "queue payload is identifiers only":
            'return { v: 1, kind: String(kind), userId: String(userId)' in queue_src
            and "body" not in re.sub(r"/\*.*?\*/|^\s*//.*$", "", queue_src, flags=re.S | re.M),
        "HTTP is asynchronous with status": 'directSend.start(who, body, "reply"), 202' in route
            and 'op === "direct_send_status"' in route,
        "unsafe synchronous route is gone": "replySend.reply(who, body)" not in route
            and "replySend.followUp(who, body)" not in route,
        "intent uses a dedicated HMAC": "EMAIL_DIRECT_SEND_HMAC_KEY" in direct
            and 'createHmac("sha256"' in direct,
        "send package and canary are default off": "EMAIL_DIRECT_SEND_OPS_ENABLED" in direct
            and "EMAIL_DIRECT_SEND_OPS_USER_IDS" in direct and '!== "1"' in direct,
        "ambiguous work only reconciles": bool(ambiguous)
            and 'state: "ambiguous"' in ambiguous.group(1)
            and '"direct_reconcile"' in ambiguous.group(1)
            and '"direct_send"' not in ambiguous.group(1),
        "expired submitting becomes ambiguous": 'operation.state === "submitting"' in repair
            and 'state: "ambiguous"' in repair,
        "repair is gated, canaried, and mailbox-free": "EMAIL_DIRECT_REPAIR_ENABLED" in repair
            and "EMAIL_DIRECT_REPAIR_USER_IDS" in repair
            and "graph-mail" not in repair and "email-auth" not in repair,
        "repair timer is deliberate": repair_binding.get("runOnStartup") is False
            and bool(repair_binding.get("schedule")) and "%" not in repair_binding.get("schedule", ""),
        "activity waits for canonical sent time": "occurredAt: message.sentDateTime" in direct
            and "actedAt: message.sentDateTime" in direct,
        "browser persistence is metadata-only": bool(remembered)
            and all(name in remembered.group(1) for name in
                    ["operationId", "kind", "crd", "sourceId", "createdUtc"])
            and all(name not in remembered.group(1) for name in
                    ["text:", "body:", "recipient", "attachment"]),
    }
    failed = [name for name, ok in checks.items() if not ok]
    return (not failed,
            "separate ETag ledger/outbox, one-way ambiguity, canonical activity, metadata-only clients"
            if not failed else "DIRECT SEND SAFETY MISSING: " + "; ".join(failed))


@check("the reply sweep stores no message body")
def _reply_sweep_stores_no_body():
    """THE RULE THIS ENFORCES: Exchange stays the system of record.

    The app holds delegated Mail.ReadWrite, so the PERMISSION does not stop it
    reading and keeping message bodies -- only the code does. A sweep that
    quietly persisted bodies would build a second copy of our own reps'
    mailboxes, which is not what anyone agreed to when they connected one.

    So the activity writer is checked for body-shaped fields, and the sweep is
    checked for asking Graph for one. Metadata in Azure, content in Exchange,
    fetched on demand when a rep actually clicks.
    """
    forbidden = ("body", "bodyPreview", "uniqueBody", "attachments")

    store_src = text(API / "shared" / "email-store.js")
    fn = re.search(r"async function recordActivity\b(.*?)\n\}", store_src, re.S)
    if not fn:
        return True, "recordActivity not present yet"
    written = set(re.findall(r"(\w+)\s*:", fn.group(1)))
    leaked = sorted(w for w in written if w in forbidden)

    sweep = API / "email-reply-sweep" / "index.js"
    asked = []
    if sweep.exists():
        code = re.sub(r"/\*.*?\*/", "", text(sweep), flags=re.S)
        code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
        # ACTIVITY_FIELDS is the sweep's $select and is defined in graph-mail.js.
        fields = re.search(r"const ACTIVITY_FIELDS = ([^;]+);", text(API / "shared" / "graph-mail.js"))
        if fields:
            asked = sorted(f for f in forbidden if re.search(rf"\b{f}\b", fields.group(1)))

    bad = leaked + asked
    return (not bad,
            "the activity log holds metadata only; bodies stay in Exchange"
            if not bad else
            f"BODY REACHES STORAGE: stored={leaked} selected={asked}")


@check("the advisor email lookup agrees with the contacts it was built from")
def _advisor_email_lookup():
    """THE FAILURE THIS CATCHES: a reply sweep filtering against a stale universe.

    The sweep decides whether an inbound message is worth recording by looking
    its sender up in advisor_emails.json.gz. An advisor missing from that file is
    not a degraded result -- their reply is discarded as noise, silently, and the
    map goes on saying "no reply recorded" forever.

    Nothing else notices, because the sweep is behaving exactly as designed. So
    the invariant is checked here: every address contacts.json holds must be
    resolvable, or explicitly published as ambiguous.

    Skipped rather than failed when the export has not been run -- it is a
    separate step, and a missing artefact is a "not built yet", not a defect.
    """
    lookup = ROOT / "data" / "output" / "advisor_emails.json.gz"
    if not lookup.exists():
        return True, "not built yet -- run src/export_advisor_emails.py"

    import gzip as _gzip
    payload = json.loads(_gzip.decompress(lookup.read_bytes()))
    known = set(payload.get("byEmail", {}))
    ambiguous = set(payload.get("ambiguous", []))
    # OUR OWN PEOPLE are excluded on purpose, not missing by accident. 18 of
    # EIC's registered reps appear in the SEC feed; the activity timeline is
    # firm-wide, so tracking them would let every rep read when their colleagues
    # emailed each other. Absent from the lookup is the intended state, and this
    # check must not read a deliberate exclusion as a stale export.
    internal = set(payload.get("internalCrds", []))

    advisors = json.loads((DATA / "contacts.json").read_text(encoding="utf-8"))["advisors"]
    # Same normalisation the export applies. Kept deliberately simple: the point
    # is to catch an export that never ran or ran against different data, not to
    # re-implement the repair rules and drift from them.
    missing = []
    for crd, rec in advisors.items():
        address = str(rec.get("e", "") or "").strip().lower()
        if not address or "@" not in address:
            continue
        if address in known or address in ambiguous or str(crd) in internal:
            continue
        missing.append(f"{crd} {address}")

    # The four known-malformed records are repaired or rejected by the export, so
    # their RAW form will not be found here. Anything beyond that handful means
    # the file is out of date with the contacts it claims to describe.
    tolerance = 10
    return (len(missing) <= tolerance,
            f"{len(known):,} resolvable, {len(ambiguous)} ambiguous, "
            f"{len(internal)} internal (never tracked), "
            f"{len(missing)} unmatched (<= {tolerance} expected from repairs)"
            if len(missing) <= tolerance else
            f"{len(missing):,} advisor addresses ARE NOT IN THE LOOKUP -- their replies "
            f"would be discarded. Re-run export_advisor_emails.py. e.g. {missing[:3]}")


@check("every field a service patches is a field the store will write")
def _patch_whitelists_complete():
    """THE FAILURE THIS ENDS, having now happened three times.

    patchMessage() and patchBatch() copy only the field names on a hand-kept
    list. A field written by a caller and missing from that list is accepted,
    returns success, and is silently discarded.

        conversationId      fetched from Graph, dropped on write
        teammateCcJson      a rep ticked a teammate; the tick vanished
        teammatesFull       dropped by the client's own recipient whitelist

    Every one looked like "the feature does not work" rather than a bug,
    because nothing errors: the write succeeds and stores less than it was
    given. In a language with no compiler to notice a missing string, the only
    thing that catches it is a check like this one.

    Scans what the SERVICES actually patch and compares it with what the store
    accepts, so it covers fields nobody has thought of yet.
    """
    store_src = text(API / "shared" / "email-store.js")

    def whitelist(fn_name):
        """Every field name the function will copy through.

        Collected from BOTH shapes it uses: arrays declared as a local
        (`const strings = [...]`, iterated as `for (const k of strings)`) and
        arrays written inline in the loop header. Reading only the inline ones
        misses the largest list and reports most of the codebase as broken.
        """
        start = store_src.find(f"async function {fn_name}(")
        if start < 0:
            return None
        end = store_src.find(chr(10) + "}", start)
        body = store_src[start:end if end > start else len(store_src)]
        allowed = set()
        for block in re.findall(r"for \(const \w+ of \[(.*?)\]", body, re.S):
            allowed |= set(re.findall(r'"(\w+)"', block))
        for block in re.findall(r"const \w+ = \[(.*?)\];", body, re.S):
            allowed |= set(re.findall(r'"(\w+)"', block))
        return allowed

    targets = {"patchMessage": r"patchMessage\([^)]*?,\s*\{([^}]*)\}",
               "patchBatch": r"patchBatch\([^)]*?,\s*\{([^}]*)\}"}
    missing = []
    for source in ("email-service.js", "email-reply-send.js", "email-activity.js"):
        path = API / "shared" / source
        if not path.exists():
            continue
        body = text(path)
        for fn, pattern in targets.items():
            allowed = whitelist(fn)
            if allowed is None:
                continue
            for call in re.findall(pattern, body, re.S):
                for field in re.findall(r"(\w+)\s*:", call):
                    if field not in allowed:
                        missing.append(f"{source} -> {fn}({field})")

    return (not missing,
            "every patched field is on the store's whitelist"
            if not missing else
            f"WRITTEN THEN DISCARDED: {sorted(set(missing))} -- the write succeeds "
            f"and saves nothing")


@check("a Graph message field we select is a field we can store")
def _graph_message_fields():
    """THE BUG THIS PREVENTS: a field fetched from Graph, then silently dropped.

    graph-mail.js names the message properties every lookup asks Microsoft for.
    email-store.js has a hand-written whitelist of the column names patchMessage
    is allowed to write. A property added to the first and forgotten in the
    second is fetched over the wire, assigned in the worker, and thrown away on
    write -- with no error at either end, because the whitelist loop simply does
    not match and Table Storage never sees the column.

    conversationId is why this exists. It ties a reply back to the message it
    answers, it is only ever captured once (at draft time), and losing it on the
    way to storage would not surface until reply matching had already failed
    silently for every message sent in the meantime.

    Only fields the worker actually persists are checked: `isDraft` and
    `parentFolderId` are read and used in flight, never stored.
    """
    graph = text(API / "shared" / "graph-mail.js")
    block = re.search(r"const MESSAGE_FIELDS = \"([^\"]+)\"", graph)
    if not block:
        return False, "MESSAGE_FIELDS not found in api/shared/graph-mail.js"
    selected = [f.strip() for f in block.group(1).split(",") if f.strip()]

    store = text(API / "shared" / "email-store.js")
    # Anchored to patchMessage. email-store.js has TWO `const strings = [...]`
    # whitelists -- patchBatch's comes first in the file, and matching it
    # instead reports every message column as missing.
    fn = re.search(r"async function patchMessage\b(.*?)\n\}", store, re.S)
    strings = re.search(r"const strings = \[(.*?)\];", fn.group(1), re.S) if fn else None
    if not strings:
        return False, "patchMessage's string whitelist not found in email-store.js"
    allowed = set(re.findall(r'"(\w+)"', strings.group(1)))

    worker = text(API / "email-worker" / "index.js")
    stored = set(re.findall(r"(graph[A-Z]\w*)\s*:", worker))

    # graphMessageId <- id, graphConversationId <- conversationId: the worker
    # prefixes and capitalises. Derive the column name rather than keeping a
    # second hand-written map that could drift in its own right.
    missing = []
    for field in selected:
        column = "graph" + field[0].upper() + field[1:]
        if column in stored and column not in allowed:
            missing.append(column)
    return (not missing,
            f"{len(selected)} fields selected, every stored one is writable" if not missing
            else f"FETCHED THEN DROPPED: {missing} -- not in patchMessage's whitelist")


@check("every table() name a store function asks for actually exists")
def _table_names():
    """THE BUG THIS ENCODES: contact flags shipped completely broken because
    TABLES had no `contactflags` entry.

    Everything else was right -- the store functions, the /api/flags route, the
    client, the optimistic UI. table("contactflags") resolved to undefined, the
    Table client was constructed with an undefined name, and every call threw.
    Nothing in the chain could catch it: JavaScript is happy to look up a
    missing key, and the failure only appears at the moment a rep clicks.

    A lookup keyed by string, populated by hand, in a language with no compiler
    to complain -- so it is checked here instead.
    """
    src = text(API / "shared" / "store.js")
    block = re.search(r"const TABLES = \{(.*?)\};", src, re.S)
    if not block:
        return False, "TABLES not found in api/shared/store.js"
    declared = set(re.findall(r"(\w+)\s*:", block.group(1)))
    # Comments stripped first. The comment ABOVE TABLES names a table that does
    # not exist, on purpose, to explain the bug -- and a check that reads prose
    # as code reports its own documentation as a fault.
    code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    asked = set(re.findall(r'table\(\s*"([a-z_]+)"\s*\)', code))
    missing = sorted(asked - declared)
    return (not missing,
            f"{len(asked)} table names requested, all declared" if not missing
            else f"NOT IN TABLES: {missing} -- every call against them throws")


@check("a failed do-not-call load disables calling instead of allowing it")
def _dnc_fails_closed():
    """The load is now issued early and awaited later, which is a new way to
    lose the error.

    init() fires four requests together so they share one Function App cold
    start. A promise awaited later must carry a catch in the meantime or Node
    reports an unhandled rejection -- and A PARKED CATCH IS EXACTLY HOW A GUARD
    STOPS GUARDING: swallow the rejection there and the later `await` sees a
    settled promise, calling stays enabled, and the suppression map is empty.

    So the shape has to hold: the fetch is assigned with no catch on the same
    expression, the parked catch sits on its own statement (observing the
    rejection rather than replacing it), and the promise is awaited inside a try
    whose catch disables calling.
    """
    src = text(WEB / "dial.js")
    body = src.split("async function init()", 1)[-1].split(chr(10) + "  }", 1)[0]
    assigned = re.search(r"const\s+dncP\s*=\s*fetchDnc\(\)\s*;", body)
    parked = re.search(r"^\s*dncP\.catch\(\s*\(\)\s*=>\s*\{\s*\}\s*\)\s*;", body, re.M)
    awaited = re.search(r"try\s*\{\s*await\s+dncP\s*;?\s*\}\s*catch", body)
    sets_problem = bool(awaited) and "The do-not-call list could not be loaded" in body
    ok = bool(assigned) and bool(parked) and sets_problem
    return (ok,
            "the dnc load is awaited in a try that disables calling on failure"
            if ok else
            f"FAILS OPEN: assigned={bool(assigned)}, parked-catch={bool(parked)}, "
            f"awaited-and-guarded={sets_problem}")


# ---------------------------------------------------------------------------
# 19. Nothing offers to dial a suppressed person
#
# THE BUG THIS ENCODES: do-not-call hid the "add to call list" button and left
# every tel: link live. The guard was decorative -- the quickest route to
# calling someone on the list was the button still sitting next to their name.
#
# Every tel: anchor in either view must therefore sit behind a suppression test.
# Counted rather than eyeballed, because the next tel: link somebody adds is
# exactly the one that will not get the check.
# ---------------------------------------------------------------------------
@check("every tel: link is built by Dial.telHref")
def _no_dialable_dnc():
    # STRUCTURAL, not proximity-based. The first version of this check looked
    # for a suppression keyword within 420 characters before each `href="tel:`.
    # When an unguarded link was deliberately inserted next to a guarded one to
    # test the check, it passed -- the neighbour's keyword was inside the
    # window. So the rule is now exact: a literal tel: href may exist in exactly
    # one place, the helper that refuses to build one for a suppressed advisor.
    bad = []
    for name in ("app.js", "field.js"):
        src = text(WEB / name)
        for m in re.finditer(r'href="tel:', src):
            bad.append(f"{name}:{src[:m.start()].count(chr(10)) + 1}")
    helper = text(WEB / "dial.js")
    body = helper.split("function telHref", 1)[-1].split("\n  }", 1)[0]
    refuses = "isDnc(crd)" in body and 'return ""' in body
    return (not bad and refuses,
            "all dialing goes through Dial.telHref, which refuses suppressed advisors"
            if not bad and refuses
            else f"raw tel: hrefs at {bad}" if bad
            else "telHref does not refuse suppressed advisors")


# ---------------------------------------------------------------------------
# 20. Advisor history is sorted before it is truncated
#
# THE BUG THIS ENCODES: "has anyone here called them already?" scanned the log
# cross-partition, stopped after limit * 4 rows, and sorted afterwards. The scan
# arrives in PartitionKey order -- user id order -- so the cut kept whichever
# colleagues sorted first and could drop the most recent call in the firm. The
# one question the function exists to answer, answered wrongly, in silence.
# ---------------------------------------------------------------------------
@check("advisor history sorts before truncating")
def _history_sorts_first():
    src = text(API / "shared" / "store.js")
    body = src.split("async function eventsForCrd", 1)[-1].split("\n}", 1)[0]
    windowed = re.search(r"break", body) and re.search(r"limit\s*\*", body)
    sorts = ".sort(" in body and "slice(0, limit)" in body
    return (sorts and not windowed,
            "reads every match for the advisor, then sorts, then cuts"
            if sorts and not windowed
            else "truncates on a multiple of limit BEFORE sorting")


# ---------------------------------------------------------------------------
# 21. Queue writes are guarded by a version, in both implementations
#
# THE BUG THIS ENCODES: a queue save replaces the whole row. The same rep works
# from a desk and a phone, so the second device to save silently erased whatever
# the first had added. The etag makes that a refusal instead of a loss -- and
# serve.py has to refuse it too, or the conflict path is code no one has ever
# seen run.
# ---------------------------------------------------------------------------
@check("queue writes carry a version, and a stale one is refused")
def _queue_concurrency():
    store = text(API / "shared" / "store.js")
    shim = text(ROOT / "serve.py")
    client = text(WEB / "dial.js")
    azure = "etag" in store and "updateEntity" in store and "412" in store
    dev = "etag" in shim and "409" in shim
    sends = "etag: state.etag" in client
    ok = azure and dev and sends
    return (ok, f"azure={azure}, serve.py={dev}, client sends etag={sends}")


# ---------------------------------------------------------------------------
# 22. The dev server does not listen on the network
#
# THE BUG THIS ENCODES: serve.py has no authentication -- it hard-codes one
# user -- and it serves the entire contact file plus writable /api endpoints. It
# bound to "" , which is every interface, so all of that was readable and
# writable by anything on the office network.
# ---------------------------------------------------------------------------
@check("the dev server binds loopback unless told otherwise")
def _dev_server_loopback():
    src = text(ROOT / "serve.py")
    default = re.search(r'--host["\']\s*,\s*default\s*=\s*["\']127\.0\.0\.1', src)
    binds = re.search(r"Server\(\(\s*args\.host", src)
    wildcard = re.search(r'Server\(\(\s*""', src)
    ok = bool(default and binds and not wildcard)
    return (ok, "binds 127.0.0.1 by default, --host to override"
            if ok else "binds every interface, with no auth in front of it")


# ---------------------------------------------------------------------------
# 23. No third-party code executes in a signed-in page
#
# THE BUG THIS ENCODES: Leaflet and its plugins loaded from unpkg.com by tag, on
# a page holding the contact file and the call log. Whatever that tag pointed at
# ran with full access. They are vendored and hashed now, and the CSP names no
# external script host -- so a reintroduced CDN <script> fails visibly rather
# than working quietly.
# ---------------------------------------------------------------------------
@check("no page loads script from a third-party host")
def _no_cdn_script():
    bad = []
    for name in ("index.html", "field.html"):
        src = text(WEB / name)
        for m in re.finditer(r'<(?:script|link)[^>]+(?:src|href)="(https?://[^"]+)"', src):
            bad.append(f"{name}: {m.group(1)}")
        if "Content-Security-Policy" not in src:
            bad.append(f"{name}: no CSP")
        if re.search(r"script-src[^;\"]*unsafe-inline", src):
            bad.append(f"{name}: CSP allows inline script")
    return (not bad, "both pages are same-origin with a CSP"
            if not bad else f"{bad}")


# ---------------------------------------------------------------------------
# 24. The vendored libraries are the ones we hashed
#
# A local copy is only worth something if it is the copy that was reviewed. This
# re-hashes what is on disk against webapp/vendor/manifest.json and never
# touches the network.
# ---------------------------------------------------------------------------
@check("vendored map libraries match their recorded hashes")
def _vendor_hashes():
    import hashlib                                     # noqa: PLC0415
    man = read(WEB / "vendor" / "manifest.json")
    bad = []
    for local, meta in sorted(man.items()):
        p = WEB / "vendor" / local
        if not p.exists():
            bad.append(f"missing {local}")
        elif hashlib.sha256(p.read_bytes()).hexdigest() != meta["sha256"]:
            bad.append(f"changed {local}")
    return (not bad, f"{len(man)} files"
            + (f"; {bad}" if bad else ""))


# ---------------------------------------------------------------------------
# 25. Missing data is not evidence of a business model
#
# THE BUG THIS ENCODES: `retail_raum_share.fillna(0) < MIN_RETAIL_RAUM_SHARE`
# labelled a firm that reported NO RAUM -- which makes the retail share
# undefined, not zero -- as "institutional_only": an affirmative claim about how
# they run their business, manufactured from a blank field.
#
# The insufficient_data rule must come FIRST, or the rules below it read blanks
# as numbers again.
# ---------------------------------------------------------------------------
@check("firms with no reported RAUM are not classified from the blank")
def _absent_is_not_zero():
    # RUN THE CLASSIFIER, do not read it. The first version of this check
    # grepped the institutional_only lambda for "fillna(0)" -- and when the bug
    # was deliberately re-introduced to test it, it passed anyway, because the
    # regex stopped at the parenthesis inside fillna(0). A check on the text of
    # a rule is a check on a spelling; this one feeds two firms through and
    # looks at what comes out.
    import numpy as np                                 # noqa: PLC0415
    import pandas as pd                                # noqa: PLC0415
    import score as scoring                            # noqa: PLC0415

    base = {                        # a plain distributor, so nothing above fires
        "status": "APPROVED", "is_wrap_pm_only": False, "g_pm_ric": False,
        "g_pm_pooled": False, "g_select_advisers": True, "wrap_pm_amt": 0.0,
        "n_private_funds": 0.0, "pooled_raum_share": 0.0,
        "retail_raum_share": 0.9, "n_advisors": 50.0,
        "n_retail_clients": 900.0, "raum_total": 1e9,
    }
    blank = {**base, "raum_total": np.nan, "retail_raum_share": np.nan}
    inst = {**base, "retail_raum_share": 0.01}
    d = pd.DataFrame([base, blank, inst])
    got = list(scoring.classify(d))
    want = ["distributor", "insufficient_data", "institutional_only"]
    return (got == want, f"got {got}"
            + ("" if got == want else f", wanted {want}"))


# ---------------------------------------------------------------------------
# 26. Every column attach_advisors() owns exists on both of its paths
#
# THE BUG THIS ENCODES: with no advisor feed, the function created n_advisors
# and returned -- but not aum_per_advisor, which score.py reads unconditionally.
# A clean rebuild died with a KeyError, and only there, which is the sort of
# thing that gets discovered at the worst possible moment.
# ---------------------------------------------------------------------------
@check("attach_advisors defines the same columns with or without the feed")
def _attach_advisors_columns():
    src = text(SRC / "adv.py")
    body = src.split("def attach_advisors", 1)[-1].split("\ndef ", 1)[0]
    early, late = body.split("return d", 1)
    made_late = set(re.findall(r'(?:out|d)\["(\w+)"\]\s*=', late))
    made_early = set(re.findall(r'd\["(\w+)"\]\s*=', early))
    missing = made_late - made_early
    return (not missing, f"both paths define {sorted(made_early)}"
            if not missing else f"the no-feed path never defines {sorted(missing)}")


# ---------------------------------------------------------------------------
# 27. Feed selection is not left to the filesystem
#
# THE BUG THIS ENCODES: four parsers read `next(RAW.glob("IA_*_Feed_*"))`, which
# takes whatever the directory happens to yield first. With one feed downloaded
# that is right by accident; with two it is a coin flip, and a build that pairs
# last quarter's advisor feed with this quarter's firm roster looks entirely
# normal from the outside.
# ---------------------------------------------------------------------------
@check("SEC feeds are chosen by date, not by directory order")
def _feed_choice():
    bad = []
    for p in sorted(SRC.glob("*.py")):
        if p.name == "audit.py":
            continue                 # this file quotes the pattern it forbids
        for m in re.finditer(r"next\(\s*\w*\.?glob\(", text(p)):
            bad.append(f"{p.name}:{text(p)[:m.start()].count(chr(10)) + 1}")
    return (not bad, "all feed reads go through config.newest_feed()"
            if not bad else f"arbitrary glob selection at {bad}")


# ---------------------------------------------------------------------------
# 28. The Act! crosswalk is internally sound
#
# It is about to become the thing that decides which advisor a call outcome is
# posted against in the CRM the whole firm reads. Two ways it goes wrong
# quietly: an act_id appearing twice (so one contact maps to two advisors, and
# which one wins depends on row order), and a high-tier row whose score sits
# below the acceptance threshold the matcher claims to enforce.
#
# Drift between runs is checked by act_crosswalk.py itself, at the only moment
# both versions exist. This checks the file that got written.
# ---------------------------------------------------------------------------
@check("the Act! crosswalk has unique ids and honours its own threshold")
def _act_crosswalk():
    import pandas as pd                                  # noqa: PLC0415
    from forbes_match import ACCEPT                      # noqa: PLC0415
    d = pd.read_parquet(DATA.parent.parent / "data" / "interim" / "act_crosswalk.parquet")
    dupe = int(d["act_id"].duplicated().sum())
    blank = int((d["act_id"].astype(str).str.strip() == "").sum())
    high = d[d["tier"] == "high"]
    under = int((high["match_score"] < ACCEPT).sum())
    # A matched row must name an advisor; an unmatched one must not.
    inconsistent = int(((d["tier"] == "none") & (d["advisor_crd"] != "")).sum()
                       + ((d["tier"].isin(["high", "review", "confirmed"]))
                          & (d["advisor_crd"] == "")).sum())
    ok = not (dupe or blank or under or inconsistent)
    return (ok, f"{len(d):,} rows, {len(high):,} high"
            + (f"; DUPLICATE act_ids {dupe}" if dupe else "")
            + (f"; BLANK act_ids {blank}" if blank else "")
            + (f"; {under} high-tier rows below ACCEPT={ACCEPT}" if under else "")
            + (f"; {inconsistent} tier/crd contradictions" if inconsistent else ""))


# ---------------------------------------------------------------------------
# 29. The per-advisor book cannot be summed into a firm total
#
# THE TRAP THIS ENCODES: 40% of the CRM's accounts have more than one holder, so
# act_assets.json deliberately writes an account's FULL value against every
# advisor who holds it -- right for a card, wrong for any sum. Adding up the
# advisors block overstates the book by ~39%, and in a viewport summary the
# figure would MOVE AS THE MAP PANNED as team-mates scrolled into view.
#
# The safe route is to union the `ix` account indices and sum the accounts table
# once per index. This asserts that the two really are different -- if a future
# build made the per-advisor sum equal the deduplicated total, the sharing
# information would have been lost and the card would be understating
# relationships.
# ---------------------------------------------------------------------------
@check("EIC book: per-advisor values are shares, not a summable total")
def _act_assets_shape():
    d = read(DATA / "act_assets.json")
    adv, table, totals = d["advisors"], d["accounts"], d["totals"]
    problems = []

    # Structure first, and WITHOUT raising. A check that dies on a KeyError
    # reports "KeyError: 'ix'" instead of what is wrong, which makes the failure
    # a puzzle rather than an answer.
    missing = sum(1 for a in adv.values() if "ix" not in a)
    if missing:
        problems.append(f"{missing:,} advisors carry no account indices, so the "
                        f"viewport total cannot de-duplicate")
    bad_ix = [i for a in adv.values() for i in a.get("ix", [])
              if not isinstance(i, int) or not (0 <= i < len(table))]
    if bad_ix:
        problems.append(f"{len(bad_ix)} account indices out of range")

    naive = sum(a.get("acv", 0) + a.get("lcv", 0) + a.get("mf", 0) for a in adv.values())
    ix = {i for a in adv.values() for i in a.get("ix", [])
          if isinstance(i, int) and 0 <= i < len(table)}
    union = sum(sum(table[i]) for i in ix)
    stated = totals["acv"] + totals["lcv"] + totals["mf"]

    # The per-advisor figures MUST exceed the de-duplicated union, because
    # shared accounts are written at full value against each holder. If they
    # ever converge, sharing has been lost and the card is understating
    # relationships -- which would look entirely reasonable on screen.
    if naive <= union * 1.05:
        problems.append("per-advisor sum no longer exceeds the de-duplicated union; "
                        "shared-account information appears to have been lost")
    if union > stated + 1:
        problems.append("advisors reference more value than the stated firm total")
    return (not problems,
            f"{len(adv):,} advisors, {len(table):,} accounts; "
            f"naive ${naive:,.0f} vs union ${union:,.0f} vs stated ${stated:,.0f}"
            + ("; " + "; ".join(problems) if problems else ""))


# ---------------------------------------------------------------------------
# The national view's EIC summary is pre-computed, and it must stay that way
#
# The national layer carries firm-office placements, not advisor identities, so
# the browser CANNOT derive EIC's own book there -- not approximately, not at
# all. It therefore reads `totals` straight out of act_assets.json.
#
# That makes every field it reads a contract with no compiler behind it. A
# renamed key in the builder would not raise anything: fmtMoney(undefined) and
# Number(undefined).toLocaleString() both render, so the national view would
# quietly show "$0" and "0 advisors" -- a claim that EIC holds nothing in the
# United States, made confidently, on the first screen anyone opens.
#
# So this reads the field names out of the client and demands the file supply
# them, with real values. It also pins the labelling: these tiles sit in a strip
# where every neighbour is scoped to the viewport, and a figure that ignores
# panning while its neighbours obey it has to say so on its face.
# ---------------------------------------------------------------------------
@check("the national view's EIC book figures exist and are labelled as national")
def _act_assets_national():
    totals = read(DATA / "act_assets.json")["totals"]
    app = text(WEB / "app.js")
    html = text(WEB / "index.html")
    problems = []

    body = re.search(r"function renderEicNational\(\)\{.*?" + chr(10) + r"\}", app, re.S)
    if not body:
        return False, "renderEicNational() is gone; the national view cannot state the book"
    body = body.group(0)

    # Every t.<field> the client reads must be present and non-null.
    wanted = sorted(set(re.findall(r"t\.([a-z_]+)", body)))
    absent = [k for k in wanted if totals.get(k) is None]
    if absent:
        problems.append("client reads " + ", ".join(absent) + " but the file has no such field")
    # The three money figures are the headline. Zero there is not a build.
    for k in ("acv", "lcv", "mf"):
        if not totals.get(k):
            problems.append(f"totals.{k} is empty, so the national view would show $0")
    if not totals.get("advisors"):
        problems.append("totals.advisors is empty, so the summary would read '0 advisors'")

    # The de-duplication claim the copy makes must be true of the file.
    if totals.get("shared_accounts", 0) >= totals.get("accounts", 0):
        problems.append("every account is marked shared, which cannot be right")

    # Labelled, not merely correct.
    if 'id="eicScope"' not in html:
        problems.append("the scope badge is gone; a national figure would sit unlabelled "
                        "among viewport figures")
    if "United States" not in body:
        problems.append("renderEicNational no longer names the scope it is showing")
    # And the badge must come OFF again when a viewport figure replaces it.
    viewport = re.search(r"function renderEicAssets\(ids\)\{.*?const seen = new Set\(\);", app, re.S)
    if not viewport or "eicScope" not in viewport.group(0):
        problems.append("renderEicAssets does not clear the badge, so a state figure "
                        "would keep the 'United States' label")

    return (not problems,
            f"${totals.get('acv', 0):,.0f} / ${totals.get('lcv', 0):,.0f} / "
            f"${totals.get('mf', 0):,.0f} across {totals.get('advisors', 0):,} advisors, "
            f"{len(wanted)} fields read"
            + ("; " + "; ".join(problems) if problems else ""))


# ---------------------------------------------------------------------------
# The phone can copy a teammate without having their tile loaded
#
# WHAT BROKE: a practice member shipped as [crd, name, state], so field.js had
# to find each teammate's ADDRESS by scanning TILE_CACHE -- which holds the
# tiles near where the rep is standing and nothing else. A teammate one city
# over had no address and was dropped. Worse, both callers were written
#
#     row ? await teammatesWithEmail(...) : []
#
# and an advisor reached from the QUEUE has no tile row at all, because a queue
# entry is a stored snapshot. The queue is how a rep works a list in the field,
# so the picker was empty in precisely the case it exists for.
#
# It reported nothing, because the picker hides itself when the list is empty:
# "this advisor has no teammates with an address" and "we did not look" render
# identically. The desk was fine throughout -- it resolves teammates from
# contacts.json, which the phone deliberately never loads -- so the two views
# disagreed about the same advisor with nothing to say so.
#
# This pins the shard's shape AND the client's refusal to depend on a tile.
# ---------------------------------------------------------------------------
@check("the phone can copy a teammate it has not got a tile for")
def _practice_emails():
    shards = sorted((DATA / "practices").glob("*.json"))
    if not shards:
        raise FileNotFoundError(2, "not built", str(DATA / "practices"))
    problems = []

    members = withmail = short = 0
    for shard in shards:
        for rec in read(shard).values():
            for m in rec.get("m", []):
                members += 1
                if len(m) < 4:
                    short += 1
                elif str(m[3] or "").strip():
                    withmail += 1
    if short:
        problems.append(f"{short:,} members still carry no email column, so the "
                        f"phone would fall back to scanning loaded tiles for them")
    # Not every advisor has an address; plenty do. A shard where almost nobody
    # does means the column is being written empty, which looks like success.
    if members and withmail / members < 0.25:
        problems.append(f"only {withmail:,} of {members:,} members carry an address; "
                        f"the column appears to be written but not filled")

    field = text(WEB / "field.js")
    # COMMENTS STRIPPED FIRST. The fix's own comment describes the defect it
    # removed, and the first draft of this check matched that sentence and
    # reported the bug as still present. A check that reads prose is a check
    # that fails on documentation.
    code = re.sub(r"/\*.*?\*/", "", field, flags=re.S)
    code = re.sub(r"(?m)^\s*//.*$", "", code)
    # The exact shape of the defect: a teammate lookup conditioned on a row.
    if re.search(r"row\s*\?\s*await teammatesWithEmail", code):
        problems.append("a teammate lookup is still gated on a loaded tile, so an "
                        "advisor opened from the queue has no teammates")
    body = re.search(r"async function teammatesWithEmail\(.*?" + chr(10) + r"\}", code, re.S)
    if not body:
        problems.append("teammatesWithEmail() is gone")
    else:
        if "m[3]" not in body.group(0):
            problems.append("the address shipped with the practice record is not read")
        if "teamKey ?" not in body.group(0):
            problems.append("a missing team key is not handled, which is every queue entry")

    return (not problems,
            f"{len(shards)} shards, {members:,} members, {withmail:,} with an address"
            + ("; " + "; ".join(problems) if problems else ""))


# ---------------------------------------------------------------------------
# Both views offer the two standing flag lists, under the same names
#
# The star and the shield are firm-wide sales knowledge, saved once and read by
# whoever opens the advisor next. The DESK grew lists off them; the field view
# never did -- it fetched the flags on boot (dial.js does it for both apps) and
# then had nothing that consumed them. So a rep who starred somebody at their
# desk could not find that person again from a car, and no screen anywhere said
# the lists were desk-only.
#
# THE NAMES MUST MATCH, not merely exist. Dial.openList() resolves by name, so
# a field view calling its list "Key contact" would silently create a SECOND
# list beside the desk's "Key contacts" -- two lists, same star, diverging.
# ---------------------------------------------------------------------------
@check("the star and shield lists exist on both the desk and the phone")
def _flag_lists_parity():
    desk, field = text(WEB / "app.js"), text(WEB / "field.js")
    problems = []

    # The labels each view will pass to openList(), taken from the source.
    def labels(src):
        found = re.search(r'kind === "key" \? "([^"]+)" : "([^"]+)"', src)
        return set(found.groups()) if found else set()

    desk_labels, field_labels = labels(desk), labels(field)
    if not desk_labels:
        problems.append("the desk no longer names its flag lists")
    if not field_labels:
        problems.append("the field view has no flag lists at all")
    elif desk_labels and desk_labels != field_labels:
        problems.append(f"the two views name them differently -- desk {sorted(desk_labels)} "
                        f"vs field {sorted(field_labels)}; openList resolves by name, so "
                        f"this makes two lists off one star")

    # Both must actually offer the actions, not just define the strings.
    for name, src in (("desk", desk), ("field", field)):
        for action in ("flag-call", "flag-show"):
            if action not in src:
                problems.append(f"{name} has no {action} control")

    # And the field view must not need a loaded tile to dial one, which is the
    # same defect the teammate picker had.
    if "locateFlagged" not in field:
        problems.append("the phone cannot resolve a flagged advisor it has no tile for")

    # THE STANDING LISTS ARE PERSONAL, in both views.
    #
    # The flags stay firm-wide on a card -- shared sales knowledge -- but the
    # lists build a CALL QUEUE, and one assembled from every rep's flags puts a
    # key contact in another rep's territory on a stranger's screen with a Call
    # button beside it. Both views must filter to the signed-in rep.
    for name, src in (("desk", desk), ("field", field)):
        if "Dial.flaggedByMe" not in src:
            problems.append(f"{name}'s standing lists are not scoped to the signed-in rep")
        # PRESSED MEANS MINE. If the control reads the firm-wide boolean, a rep
        # looking at a colleague's key contact sees a lit star and pressing it
        # clears THEIR mark instead of adding their own -- one rep deleting
        # another's, with no way to join a flag already set.
        marks = re.search(r"function flagMarks(?:Field)?\(crd\)\{.*?" + chr(10) + r"\}", src, re.S)
        if not marks:
            problems.append(f"{name} has no flag pair renderer")
        elif "isKeyContact" in marks.group(0) or "isDueDiligence" in marks.group(0):
            problems.append(f"{name}'s flag control is pressed by ANY rep's mark, so one rep "
                            f"can clear another's and nobody can join one already set")
        elif "flaggedByOthers" not in marks.group(0):
            problems.append(f"{name} does not show that a colleague also marked them")

    # And the click must reach the handler. A flag row carries no data-id, so a
    # `if (!l) return` guard placed above it swallows Call and Show silently --
    # which is exactly what shipped on the desk.
    for name, src in (("desk", desk), ("field", field)):
        body = re.search(r'if \(act === "flag-call"', src)
        # The SHARED guard, at the dispatcher's own indentation. A nested
        # `if (!l) return;` inside a single action's block is correct and must
        # not be mistaken for it -- the first draft of this check matched one
        # and reported working code as broken.
        guard = re.search(r"(?m)^ {4}if \(!l\) return;", src)
        if body and guard and guard.start() < body.start():
            problems.append(f"{name}: the list-id guard sits above the flag handler, "
                            f"so Call and Show do nothing")

    # SETTING a flag, not only reading one. Lists alone left the phone able to
    # act on the star but never to put one there, so a rep standing in a lobby
    # had to remember the fact until they were back at a desk.
    for name, src in (("desk", desk), ("field", field)):
        if 'data-flag="${kind}"' not in src:
            problems.append(f"{name} has no flag toggle, only the lists")
        if 'aria-pressed' not in src:
            problems.append(f"{name}'s flag control does not report its state")

    # THE TWO DRAWINGS MUST MATCH. The paths are duplicated on purpose -- the
    # views share only dial.js, which has no DOM -- and duplicated geometry is
    # exactly what drifts silently. A star that is a slightly different star on
    # the phone is the kind of thing nobody reports and everybody notices.
    for const in ("STAR_PATH", "SHIELD_PATH", "CHECK_PATH"):
        got = []
        for src in (desk, field):
            found = re.search(const + r'\s*=\s*(".*?");', src, re.S)
            got.append(re.sub(r'"\s*\+\s*"', "", found.group(1)) if found else None)
        if got[0] is None or got[1] is None:
            problems.append(f"{const} is missing from "
                            + ("the desk" if got[0] is None else "the field view"))
        elif got[0] != got[1]:
            problems.append(f"{const} differs between the two views")

    return (not problems,
            f"desk {sorted(desk_labels)}, field {sorted(field_labels)}"
            + ("; " + "; ".join(problems) if problems else ""))


# ---------------------------------------------------------------------------
# The questionable Act! matches have actually been looked at
#
# WHY THIS IS A CHECK AND NOT A NOTE: the identity gate was rewritten five times
# in one afternoon, each version producing a different demotion count and a
# confident story, because nothing scored it. docs/gate_evaluation.md fixed the
# RULE. What no rule can fix is the residue -- the matches where the evidence
# genuinely does not settle it -- and a residue nobody is reminded about is a
# residue nobody works.
#
# Two piles need a person:
#   review   demoted, and no mailbox settles whether that was right
#   contra   KEPT at high tier and syncing to Act! today, but the firm-issued
#            mailbox on the record matches no name the SEC has on file
#
# The second is the dangerous one: those rows are writing call history onto a
# contact right now, and they do not announce themselves.
#
# WARNS rather than fails. An unworked queue is a known debt, not a broken
# build, and a check that blocks the pipeline on it would simply be disabled.
# ---------------------------------------------------------------------------
@check("questionable Act! matches are triaged, and the queue is visible")
def _act_review_queue():
    import importlib.util                                   # noqa: PLC0415
    spec = importlib.util.spec_from_file_location("act_review", SRC / "act_review.py")
    if spec is None or spec.loader is None:
        return False, "src/act_review.py is missing -- the residue is not triaged at all"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    df = module.build()
    if df.empty:
        return True, "no questionable matches"
    verdicts = module.load_verdicts()
    settled = stale = 0
    todo = {"review": 0, "contra": 0}
    for r in df.itertuples(index=False):
        stored = verdicts.get(r.act_id)
        if stored and stored["evidence_hash"] == r.evidence_hash:
            settled += 1
            continue
        if stored:
            stale += 1
        if r.pile in todo:
            todo[r.pile] += 1

    outstanding = todo["review"] + todo["contra"]
    detail = (f"{len(df):,} questionable; {settled:,} adjudicated; "
              f"outstanding review {todo['review']:,}, contra {todo['contra']:,}")
    if stale:
        detail += f"; {stale:,} verdicts reopened by changed evidence"
    if outstanding:
        detail += " -- run: python src/act_review.py --queue"
    # Always true: this reports debt, it does not block on it.
    return True, detail


# ---------------------------------------------------------------------------
# 30. The territory map is complete, exclusive, and matches its source
#
# WHY THIS IS THE MOST DANGEROUS FILE IN THE PROJECT: it is small, hand-
# maintained, and authoritative. Every advisor in a state inherits whatever it
# says, and the app states it as fact -- "TN, assigned to Matt Keeter". A stale
# entry does not fail; it confidently names the wrong colleague, on every card
# in that state, until somebody notices socially.
#
# So: every state covered, no state in two territories, every owner code has a
# person, no territory owned by an INACTIVE Act! user, and the shipped JSON
# actually matches the YAML it was built from.
# ---------------------------------------------------------------------------
@check("territories: complete, exclusive, and the shipped file matches the source")
def _territories():
    import yaml                                           # noqa: PLC0415
    US = {"AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS",
          "KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY",
          "NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV",
          "WI","WY","DC","PR","VI"}
    src = yaml.safe_load((ROOT / "territories.yaml").read_text(encoding="utf-8"))
    people, terr = src["people"], src["territories"]

    problems = []
    seen = {}
    for code, states in terr.items():
        if code not in people:
            problems.append(f"territory {code} has no person")
        elif people[code].get("inactive"):
            problems.append(f"{code} ({people[code]['name']}) is an inactive Act! user")
        for s in states:
            if s in seen:
                problems.append(f"{s} in both {seen[s]} and {code}")
            seen[s] = code
    if US - set(seen):
        problems.append(f"uncovered states {sorted(US - set(seen))}")
    if set(seen) - US:
        problems.append(f"unknown codes {sorted(set(seen) - US)}")

    # The app reads the JSON, not the YAML. A build that never ran is a build
    # that silently ships yesterday's territories.
    shipped = read(DATA / "territories.json")
    js = {s: v["c"] for s, v in shipped["states"].items()}
    if js != seen:
        drift = sorted(s for s in set(js) | set(seen) if js.get(s) != seen.get(s))
        problems.append(f"territories.json disagrees with territories.yaml on {drift[:6]}"
                        f" -- re-run src/build_territories.py")
    # TWO COPIES OF THE SAME BUSINESS FACT. app.js has carried a hand-written
    # TERRITORIES map (region name -> states) since long before this, for the
    # scope selector. territories.yaml was recovered independently from the CRM
    # and produced the SAME seven groupings -- which is strong corroboration,
    # and also a drift hazard: redraw a territory in one place and the map will
    # scope one way while naming a different person.
    js = text(WEB / "app.js")
    block = js.split("const TERRITORIES = {", 1)[-1].split("};", 1)[0]
    regions = {m.group(1): set(re.findall(r'"([A-Z]{2})"', m.group(2)))
               for m in re.finditer(r'"([^"]+)":\s*\[([^\]]*)\]', block)}
    mine = {}
    for code, states in terr.items():
        mine[code] = set(states)
    # Match each region to the territory it shares the most states with, then
    # require them to be identical. Names differ ("Southeast" vs "MK") and are
    # not the point; the STATE SETS must be the same.
    for rname, rstates in regions.items():
        if not rstates:
            continue
        best = max(mine, key=lambda c: len(mine[c] & rstates))
        if mine[best] != rstates:
            only_js = sorted(rstates - mine[best])
            only_yaml = sorted(mine[best] - rstates)
            problems.append(
                f"app.js region {rname!r} and territory {best} disagree"
                + (f"; app.js-only {only_js}" if only_js else "")
                + (f"; yaml-only {only_yaml}" if only_yaml else ""))

    return (not problems,
            f"{len(seen)} states, {len(terr)} territories, {len(regions)} app.js regions, "
            f"as_of {src.get('as_of')}"
            + ("; " + "; ".join(problems) if problems else ""))


@check("call outcomes agree across the app, the API and the Act! mapping")
def _outcome_act_map():
    """The vocabulary now lives in three places, which is three chances to drift.

        webapp/dial.js        Dial.OUTCOMES -- the buttons, and each Act! id
        api/log/index.js      DISPOSITIONS  -- the server's closed set
        src/act_write_test.py OUTCOME_MAP   -- what the probe proved against Act!

    Each disagreement has its own silent failure. A button the API rejects loses
    a call outcome at the moment the rep taps it -- the one thing in this app a
    human authored and nothing can regenerate. A button with no Act! id syncs
    nothing and still looks fine. An Act! id we never proved is a guess about
    somebody else's database.

    Retired keys are checked in the OTHER direction: they must still be accepted
    by the API, because events carrying them are already in Table Storage and a
    phone that has not reloaded since the rename can still send one.
    """
    js = text(WEB / "dial.js")
    block = js.split("const OUTCOMES = [", 1)[-1].split("\n  ];", 1)[0]
    buttons = {}
    for m in re.finditer(r'\{\s*key:\s*"([^"]+)"(.*?)\}', block, re.S):
        act = re.search(r"\bact:\s*(\d+)", m.group(2))
        buttons[m.group(1)] = int(act.group(1)) if act else None

    retired = set(re.findall(r'"?([a-z-]+)"?:\s*"',
                             js.split("const RETIRED = {", 1)[-1].split("};", 1)[0]))

    api = set(re.findall(r'"([a-z-]+)"',
                         text(ROOT / "api" / "log" / "index.js")
                         .split("const DISPOSITIONS = new Set([", 1)[-1]
                         .split("]);", 1)[0]))

    pyblock = text(SRC / "act_write_test.py").split("OUTCOME_MAP = {", 1)[-1].split("\n}", 1)[0]
    probe = {m.group(1): (None if m.group(2) == "None" else int(m.group(2)))
             for m in re.finditer(r'"([a-z-]+)":\s*dict\(label="[^"]*",\s*act=(\w+)', pyblock)}

    # The fourth copy, and the only one that actually writes to the CRM. A
    # disagreement here does not fail anything -- it files the call in Act! under
    # the wrong result, correctly attributed, with no error anywhere.
    actblock = text(API / "shared" / "act.js").split("const RESULTS = {", 1)[-1] \
        .split("\n};", 1)[0]
    writer = {}
    for m in re.finditer(r'"?([a-z-]+)"?:\s*(null|\{[^}]*\})', actblock):
        key, val = m.group(1), m.group(2)
        if val == "null":
            writer[key] = None
        else:
            rid = re.search(r"\bid:\s*(\d+)", val)
            writer[key] = int(rid.group(1)) if rid else "?"

    problems = []
    for label, got in (("Dial.OUTCOMES", buttons), ("DISPOSITIONS", api),
                       ("OUTCOME_MAP", probe), ("act.js RESULTS", writer)):
        if not got:
            problems.append(f"could not parse {label}")
    if problems:
        return False, "; ".join(problems)

    for key, act in buttons.items():
        if key not in api:
            problems.append(f"{key!r} is a button the API would reject")
        if act is None:
            problems.append(f"{key!r} has no Act! result id, so it syncs nothing")
        elif key not in probe:
            problems.append(f"{key!r} was never proved against Act! by --outcome-map")
        elif probe[key] != act:
            problems.append(f"{key!r} is act={act} in dial.js but {probe[key]} in the probe")
        # The writer is the copy that reaches the CRM, so a mismatch here is the
        # one that misfiles a real call.
        if key not in writer:
            problems.append(f"{key!r} is a button act.js would not write to Act!")
        elif writer[key] is None:
            problems.append(f"{key!r} is mapped to null in act.js, so it silently "
                            f"never reaches the CRM")
        elif writer[key] != act:
            problems.append(f"{key!r} is act={act} in dial.js but {writer[key]} "
                            f"in act.js -- calls would be filed under the wrong result")

    # "skipped" is real and deliberately writes nothing to Act!. Both views offer
    # it directly rather than through OUTCOMES, so it is checked by name -- and
    # act.js must map it to null explicitly rather than by omission, so that a
    # key going missing is distinguishable from a key meant to write nothing.
    if "skipped" not in api:
        problems.append("the API would reject 'skipped'")
    if "skipped" not in writer:
        problems.append("act.js does not mention 'skipped'; it must map to null "
                        "explicitly so silence is a decision, not an oversight")
    elif writer["skipped"] is not None:
        problems.append(f"act.js would write 'skipped' to Act! as {writer['skipped']}")
    for key in retired:
        if key != "skipped" and key not in writer:
            problems.append(f"retired {key!r} has no act.js mapping -- an unsynced "
                            f"phone's outcome would reach us and not the CRM")

    for key in retired:
        if key in buttons:
            problems.append(f"{key!r} is listed as retired but is still a button")
        if key != "skipped" and key not in api:
            problems.append(f"retired {key!r} is no longer accepted -- stored events "
                            "and unsynced phones would be refused")

    # Only four Call results exist on the Act! side, confirmed live on 2026-08-14.
    # A fifth means someone invented an id rather than reading one off the server.
    ids = {a for a in buttons.values() if a is not None}
    if ids - {0, 1, 2, 17}:
        problems.append(f"Act! result ids {sorted(ids - {0, 1, 2, 17})} are not Call results")

    return (not problems,
            f"{len(buttons)} buttons, {len(api)} accepted by the API, "
            f"{len(retired)} retired, Act! ids {sorted(ids)}"
            + ("; " + "; ".join(problems) if problems else ""))


@check("required ACT correction records survive default report generation")
def _required_act_correction_rows():
    import pandas as pd                                   # noqa: PLC0415
    from export_act_crd_corrections import REQUIRED_ACT_IDS  # noqa: PLC0415
    from identity_schema import (CORRECTIONS_CSV_FILENAME,
                                 IDENTITY_DIRNAME,
                                 MANIFEST_FILENAME)        # noqa: PLC0415

    identity = ROOT / "data" / IDENTITY_DIRNAME
    report = pd.read_csv(identity / CORRECTIONS_CSV_FILENAME,
                         dtype=str).fillna("")
    present = set(report.get("act_id", pd.Series(dtype=str)).astype(str))
    missing = sorted(REQUIRED_ACT_IDS - present)
    manifest = read(identity / MANIFEST_FILENAME)
    expected = {
        "identity_manifest_hash": manifest.get("contentHash", ""),
        "act_source_file": (manifest.get("actSource") or {}).get("file", ""),
        "act_source_sha256": (manifest.get("actSource") or {}).get("sha256", ""),
    }
    drift = [name for name, value in expected.items()
             if name not in report or set(report[name].astype(str)) != {value}]
    return (not missing and not drift,
            f"{len(REQUIRED_ACT_IDS)} required records present; provenance bound"
            + (f"; missing {missing}" if missing else "")
            + (f"; drift {drift}" if drift else ""))


@check("the Act! contact lookup is ledger-approved, hash-bound and unambiguous")
def _act_lookup():
    """This file decides WHICH CONTACT a logged call is written onto.

    Everything else in the Act! path fails loudly or not at all. This one fails
    silently and permanently: a wrong id here files a real rep's real call onto
    a stranger's contact record, correctly attributed, with no error raised and
    nothing to distinguish it afterwards from a call that genuinely happened.

    Every pair must be a unique approved identity-ledger link. The fuzzy
    crosswalk is candidate evidence only and is deliberately not consulted.
    """
    import pandas as pd                                   # noqa: PLC0415
    from collections import Counter                       # noqa: PLC0415
    from contact_provenance import sha256_file             # noqa: PLC0415
    from identity_schema import (IDENTITY_DIRNAME, LINKS_FILENAME,
                                 MANIFEST_FILENAME, content_hash)  # noqa: PLC0415

    artifact = read(API / "shared" / "act_contacts.json")
    shipped = artifact["contacts"]
    identity = ROOT / "data" / IDENTITY_DIRNAME
    manifest = read(identity / MANIFEST_FILENAME)
    core = {k: v for k, v in manifest.items()
            if k not in {"generatedUtc", "contentHash"}}
    links_path = identity / LINKS_FILENAME
    links = pd.read_parquet(links_path).fillna("")
    problems = []
    if manifest.get("contentHash") != content_hash(core):
        problems.append("identity manifest content hash is invalid")
    link_meta = (manifest.get("outputs") or {}).get(LINKS_FILENAME) or {}
    if link_meta.get("sha256") != sha256_file(links_path):
        problems.append("identity links do not match the manifest")
    approved = links[(links.identity_status == "approved")
                     & links.can_sync_act.astype(bool)].copy()
    approved["advisor_crd"] = approved.advisor_crd.astype(str).str.strip()
    approved["source_record_id"] = approved.source_record_id.astype(str).str.strip()
    crd_counts = Counter(approved.advisor_crd)
    guid_counts = Counter(approved.source_record_id)
    safe = approved[approved.advisor_crd.map(crd_counts).eq(1)
                    & approved.source_record_id.map(guid_counts).eq(1)]
    expected = dict(sorted(zip(safe.advisor_crd, safe.source_record_id)))
    if shipped != expected:
        wrong = sorted(set(shipped.items()) ^ set(expected.items()))
        problems.append(f"shipped map differs from approved ledger by "
                        f"{len(wrong)} pairs, e.g. {wrong[:3]}")
    if artifact.get("identity_manifest_hash") != manifest.get("contentHash"):
        problems.append("shipped map names a different identity manifest")
    if artifact.get("act_source") != (manifest.get("actSource") or {}).get("file"):
        problems.append("shipped map names a different Act source")
    if artifact.get("act_source_sha256") != (manifest.get("actSource") or {}).get("sha256"):
        problems.append("shipped map names different Act source bytes")

    return (not problems,
            f"{len(shipped):,} approved Act routes of {len(links):,} ledger rows; "
            f"{len(approved) - len(safe):,} ambiguous approved rows excluded"
            + ("; " + "; ".join(problems) if problems else ""))


@check("the outbound recipient registry is direct-identity only and provenance-bound")
def _approved_recipient_registry():
    """A calibrated fuzzy contact is useful on screen, not send authority."""
    import gzip                                           # noqa: PLC0415
    from contact_provenance import sha256_file             # noqa: PLC0415
    from identity_schema import (IDENTITY_DIRNAME, LINKS_FILENAME,
                                 MANIFEST_FILENAME, content_hash)  # noqa: PLC0415

    identity = ROOT / "data" / IDENTITY_DIRNAME
    registry_path = identity / "approved_recipients.json.gz"
    with gzip.open(registry_path, "rt", encoding="utf-8") as handle:
        registry = json.load(handle)
    manifest = read(identity / MANIFEST_FILENAME)
    contacts_path = WEB / "data" / "contacts.json"
    contacts = read(contacts_path)
    provenance = registry.get("provenance") or {}
    core = {k: registry.get(k) for k in
            ("schemaVersion", "recipients", "ineligible", "provenance")}
    problems = []
    if registry.get("contentHash") != content_hash(core):
        problems.append("content hash is invalid")
    fuzzy = [crd for crd, row in (registry.get("recipients") or {}).items()
             if row.get("tier") != "confirmed"]
    if fuzzy:
        problems.append(f"{len(fuzzy):,} non-confirmed recipients remain")
    expected = {
        "identityManifestHash": manifest.get("contentHash", ""),
        "identityLinksSha256": ((manifest.get("outputs") or {})
                                .get(LINKS_FILENAME, {}).get("sha256", "")),
        "contactsSha256": sha256_file(contacts_path),
        "actSource": (manifest.get("actSource") or {}).get("file", ""),
        "actSourceSha256": (manifest.get("actSource") or {}).get("sha256", ""),
    }
    if provenance != expected:
        problems.append("registry provenance differs from current artifacts")
    contact_provenance = contacts.get("provenance") or {}
    if contact_provenance != {k: expected[k] for k in (
            "identityManifestHash", "identityLinksSha256", "actSource",
            "actSourceSha256")}:
        problems.append("contacts provenance differs from current identity inputs")
    count = len(registry.get("recipients") or {})
    descriptor = read(API / "shared" / "approved-recipient-release.json")
    descriptor_core = {k: descriptor.get(k) for k in (
        "schemaVersion", "registrySchemaVersion", "registryContentHash",
        "recipientCount", "ineligibleCount", "provenance")}
    if descriptor.get("descriptorHash") != content_hash(descriptor_core):
        problems.append("packaged registry descriptor hash is invalid")
    if (descriptor.get("registryContentHash") != registry.get("contentHash") or
            descriptor.get("provenance") != provenance or
            descriptor.get("recipientCount") != count or
            descriptor.get("ineligibleCount") !=
            len(registry.get("ineligible") or {})):
        problems.append("packaged API release pins a different registry")
    return (not problems, f"{count:,} direct-identity email routes"
            + ("; " + "; ".join(problems) if problems else ""))


@check("both views can log an outcome off-queue, from the shared vocabulary")
def _offqueue():
    """THE BUG THIS ENCODES: for months the desktop could only record an outcome
    from inside a dialer session, while the field view could also do it from an
    advisor's card. So the same rep doing the same thing got a CRM entry from
    their phone and silence from their desk.

    Nothing errored. The desktop simply had no button, which reads as an
    unreliable sync rather than a missing feature -- and the instinct that
    follows is to distrust the whole integration.

    Two things are asserted. Each view has an off-queue outcome grid at all, and
    each builds it from Dial.OUTCOMES rather than from a hand-written list --
    because a literal list is how the two drift apart again, quietly, the next
    time a button is added.
    """
    problems = []
    views = {
        # file, the marker for its off-queue grid, and the session-only one it
        # must not be confused with.
        "app.js":   ("data-card-outcome", "dialerSession"),
        "field.js": ("data-crd", "sess-out"),
    }
    for fname, (marker, _session) in views.items():
        src = text(WEB / fname)
        block = src.split("outcomeBlock", 1)[-1] if fname == "app.js" else src
        if marker not in src:
            problems.append(f"{fname} has no off-queue outcome grid ({marker})")
            continue
        # The grid must be generated, not typed out.
        near = src.split(marker, 1)[1][:600]
        if "Dial.OUTCOMES" not in near:
            problems.append(f"{fname}'s off-queue grid does not map Dial.OUTCOMES "
                            f"-- a hand-written list will drift from the buttons")
        if "log-out" not in src:
            problems.append(f"{fname} does not wrap the grid in a collapsible "
                            f"<details class='log-out'>")
        del block

    # An off-queue outcome must not move a queue the rep is not working.
    app = text(WEB / "app.js")
    handler = app.split("[data-card-out]", 1)[-1].split("\n});", 1)[0]
    for bad in ("Dial.advance", "Dial.requeue"):
        if bad in handler:
            problems.append(f"the card outcome handler calls {bad}, which would "
                            f"reorder a list the rep is not working")

    return (not problems,
            "app.js and field.js both log off-queue from Dial.OUTCOMES"
            + ("; " + "; ".join(problems) if problems else ""))


@check("no syncable match disagrees with the SEC record on the first name")
def _first_names():
    """THE BUG THIS ENCODES: the matcher scores on surname, firm and location,
    so two people sharing a surname at the same office score identically. 862
    high-tier pairs disagreed on the FIRST name -- Jeffrey and Victoria
    Thompson, Raymond and Rosemary Abreu, Terri and Darren Hunter -- and 340 of
    them were syncable to Act!.

    A call logged against one of those would have been written onto the wrong
    person's contact: correctly attributed, entirely plausible, and unfindable
    afterwards. The first name sat in both files the whole time and nothing
    compared them.

    Checked against the SHIPPED lookup rather than the parquet, because the
    lookup is what the API actually reads -- a crosswalk rebuilt without
    rebuilding the lookup is the same bug wearing a different hat.
    """
    import pandas as pd                                   # noqa: PLC0415
    from nicknames import same_person                     # noqa: PLC0415

    shipped = read(API / "shared" / "act_contacts.json")["contacts"]
    df = pd.read_parquet(ROOT / "data" / "interim" / "act_crosswalk.parquet")
    # THE SEC FILING, not contacts.json.
    #
    # This read contacts.json `n` and called it "the SEC name". It is the
    # pick_best winner, and for an Act!-sourced contact it IS the Act! name --
    # identical on 26,300 of 26,725 high-tier rows. So the check compared Act!
    # against itself and passed, which is the same defect the gate it guards had
    # in three separate files.
    #
    # advisors.parquet is the SEC's own record: filed first name, middle name,
    # and used_first_name, the last of these parsed from Form U4 <OthrNms> and
    # the reason "Marlyn Campbell" is genuinely WAYNE CAMPBELL.
    filed = pd.read_parquet(ROOT / "data" / "interim" / "advisors.parquet",
                            columns=["advisor_crd", "first_name", "middle_name",
                                     "used_first_name"])
    adv = {}
    for r in filed.itertuples(index=False):
        toks = []
        for t in (r.first_name, r.middle_name, r.used_first_name):
            if isinstance(t, str):
                toks.extend(t.split())
        if toks:
            adv[str(r.advisor_crd)] = {"n": " ".join(toks)}

    # Keyed by ACT_ID, not by CRD. One CRD can appear on several crosswalk rows
    # -- the same person entered twice, or a namesake scored lower -- and taking
    # "the first row for this CRD" compared a name from a row the lookup never
    # shipped. That made the check report hundreds of disagreements that were
    # really the wrong record being read, which is its own quiet wrongness.
    by_act = {str(r.act_id): str(r.name) for r in df.itertuples()}
    # A CRD-STATED MATCH IS NOT A NAME MATCH, so a name test says nothing about
    # it. The CRM names the registration number and the SEC carries it; that the
    # contact is filed as "North Brittany" against a filed BRITTANY is a
    # data-entry curiosity, not evidence of a wrong person.
    stated = {str(r.act_id) for r in df.itertuples() if r.tier == "confirmed"}

    bad = []
    for crd, act_id in shipped.items():
        if str(act_id) in stated:
            continue
        act_name = by_act.get(str(act_id))
        sec = (adv.get(crd) or {}).get("n")
        if not act_name or not sec:
            continue
        # THE SAME QUESTION THE GATE ASKS, including the initialism.
        #
        # Without it this check disagrees with act_crosswalk.py by construction:
        # the gate accepts "DJ" against a filed DENNY JOHN and "JP" against JEAN
        # PAUL, and the check then reports those very rows as disagreements. A
        # guard and its gate that ask different questions will always produce
        # findings, and every one of them is noise.
        toks = sec.split()
        # ABSTAIN ON A BARE INITIAL, exactly as the gate does.
        #
        # "J. Cummings", "T.J Weber", "H.S. Hill" carry no comparable given
        # name. act_crosswalk._given_of() returns "" for these and the gate
        # treats that as no evidence rather than as disagreement -- demoting
        # them would punish a thin Act! record rather than a wrong one. A check
        # without the same abstention reports every one of them, and each is
        # noise the gate has already reasoned about.
        import re as _re                                    # noqa: PLC0415
        _parts = [_re.sub(r"[^a-z]", "", w.lower())
                  for w in _re.split(r"[\s.,]+", str(act_name))]
        _parts = [w for w in _parts if w]
        if len(_parts) < 2 or len(_parts[0]) < 2:
            continue
        agrees = any(same_person(act_name, t) for t in toks)
        if not agrees and len(toks) >= 2:
            import re as _re                                # noqa: PLC0415
            first = _re.sub(r"[^a-z]", "",
                            str(act_name).split()[0].lower()) if act_name.split() else ""
            if len(first) >= 2:
                agrees = first == "".join(t[0].lower() for t in toks[:len(first)])
        if not agrees:
            bad.append(f"{crd} {sec!r} vs Act! {act_name!r}")

    return (not bad,
            f"{len(shipped):,} syncable CRDs checked against the SEC name"
            + (f"; {len(bad)} DISAGREE, e.g. {bad[:3]}" if bad else ""))


# ---------------------------------------------------------------------------
# 35. The call-purpose vocabulary agrees in all FOUR places
#
# The four purpose chips are written down four times: dial.js offers them,
# api/log validates them, serve.py imitates that validation locally, and
# act.js turns the key into the LABEL that becomes an Act! history subject.
#
# That last copy is why this check exists and why it is not simply part of the
# outcomes check. The other three only have to agree on the KEYS; act.js is the
# only place the human-readable label lives, and a key present everywhere but
# missing from act.js does not error -- it silently falls back to the generic
# "Call — <name>" subject. The rep taps Materials, the app says it saved, and
# the CRM's Title column is the one that was supposed to be fixed. A failure
# that looks exactly like success is the kind worth a guard.
# ---------------------------------------------------------------------------
@check("call purposes agree across dial.js, api/log, serve.py and act.js")
def _purposes():
    dial = text(WEB / "dial.js")
    block = dial.split("const PURPOSES = [", 1)[-1].split("\n  ];", 1)[0]
    js = set(re.findall(r'key:\s*"([a-z-]+)"', block))

    api = set(re.findall(r'"([a-z-]+)"',
              re.search(r"const PURPOSES = new Set\(\[(.*?)\]\)",
                        text(API / "log" / "index.js"), re.S).group(1)))
    py = set(re.findall(r'"([a-z-]+)"',
             re.search(r"^PURPOSES = \{(.*?)\}",
                       text(ROOT / "serve.py"), re.S | re.M).group(1)))
    # act.js keys may be quoted or bare -- `meeting:` and `"check-in":` both
    # appear, because a hyphen is not a legal bare JS key.
    act_block = re.search(r"const PURPOSES = \{(.*?)\n\};",
                          text(API / "shared" / "act.js"), re.S).group(1)
    act = set(re.findall(r'^\s*"?([a-z-]+)"?:\s*"', act_block, re.M))

    problems = []
    if js - api:
        problems.append(f"chips the API would reject {sorted(js - api)}")
    if api != py:
        problems.append(f"serve.py differs {sorted(api ^ py)}")
    if js - act:
        problems.append(f"no Act! subject label for {sorted(js - act)}, so those "
                        f"chips would silently write the generic subject")
    if act - js:
        problems.append(f"act.js labels {sorted(act - js)}, which no chip offers")
    return (not problems, f"{len(js)} chips, {len(act)} Act! labels"
            + ("; " + "; ".join(problems) if problems else ""))


# ---------------------------------------------------------------------------
# 36. Logging off-queue in the field view acknowledges the tap BEFORE the write
#
# THE BUG THIS ENCODES: the ad-hoc outcome block closed only once /api/log had
# resolved, and that response is not fast -- the request writes to Table
# Storage and then makes up to five sequential round trips to Act! before it
# answers. So the rep tapped "Attempted" and nothing moved for a second or
# more. The natural reading of a button that does nothing is that the tap
# missed, and the natural response is to tap again, which is how one call
# becomes two rows in the CRM.
#
# The acknowledgement is allowed to be optimistic; the SAVE is not. So this
# checks both halves: the block closes before the request is issued, AND the
# failure path puts it back.
# ---------------------------------------------------------------------------
@check("the field view acknowledges an off-queue outcome before awaiting the write")
def _adhoc_ack():
    src = text(WEB / "field.js")
    body = src.split('const out = e.target.closest("[data-outcome]")', 1)[-1][:3000]
    close_at = body.find("det.open = false")
    log_at = body.find("Dial.log(")
    reopens = "det.open = true" in body
    ok = 0 <= close_at < log_at and reopens
    return (ok, f"closes before the write={0 <= close_at < log_at}, "
                f"reopens on failure={reopens}")


# ---------------------------------------------------------------------------
# 37. The settings vocabulary agrees between the API and the dev server
#
# Same failure mode as the disposition list: a key the client saves and the
# server drops is a preference that silently does not stick, and the rep
# concludes they set it wrong. Both sides drop unknown keys ON PURPOSE -- that
# is what stops the row becoming a junk drawer -- so agreement is the only
# thing keeping "dropped because it is junk" apart from "dropped because the
# two lists drifted".
# ---------------------------------------------------------------------------
@check("per-rep settings keys agree between api/shared/store.js and serve.py")
def _settings_keys():
    js = set(re.findall(r"^\s*(\w+):\s*\d+,",
             re.search(r"const SETTING_KEYS = \{(.*?)\n\};",
                       text(API / "shared" / "store.js"), re.S).group(1), re.M))
    py = set(re.findall(r'"(\w+)":\s*\d+',
             re.search(r"^SETTING_KEYS = \{(.*?)\n\}",
                       text(ROOT / "serve.py"), re.S | re.M).group(1)))
    return (js == py and len(js) >= 5,
            f"{len(js)} keys"
            + (f"; DIFFER {sorted(js ^ py)}" if js != py else ""))


# ---------------------------------------------------------------------------
# 38. CRM history is de-duplicated by OUR OWN record, never by a marker in Act!
#
# THE DESIGN THIS PROTECTS: merging Act!'s history with ours needs one side
# de-duplicated, and there are two ways to pick which rows to drop.
#
#   Ours, on actStatus == "written"   a fact we recorded when we wrote it.
#                                     A miss shows a row TWICE.
#   Theirs, by finding our writes     needs a marker in a payload whose shape
#     inside Act!'s payload           we do not control. A miss DELETES the
#                                     rep's evidence that a call happened.
#
# Those failure directions are not comparable, and the safe one is not the
# obvious one -- filtering the CRM side is what anyone would reach for first.
# So the rule is pinned here rather than left to whoever edits the merge next.
# ---------------------------------------------------------------------------
@check("CRM history de-duplicates on our actStatus, not on a marker in Act!")
def _crm_dedupe():
    src = text(API / "log" / "index.js")
    body = src.split("const wantAct", 1)[-1].split("\n    }", 1)[0]
    ours = 'e.act !== "written"' in body
    # The CRM half must be concatenated whole. Any filter applied to
    # crm.events is the unsafe direction by definition.
    filtered = re.search(r"crm\.events\s*\.\s*filter", body)
    # And act.js must not be quietly doing it either.
    act_src = text(API / "shared" / "act.js")
    hist = act_src.split("async function historyFor", 1)[-1].split("\n}", 1)[0]
    act_filters = bool(re.search(r"rows\s*\.\s*filter", hist))
    ok = ours and not filtered and not act_filters
    return (ok, f"drops our written rows={ours}, "
                f"filters the CRM side={bool(filtered) or act_filters}")


# ---------------------------------------------------------------------------
# 39. An email is never composed from a template the rep did not choose
#
# THE BUG THIS PREVENTS: wording the app supplies on the rep's behalf goes out
# over their name, to a prospect, looking automated. That is why the mailto:
# subject was originally left blank.
#
# THE MECHANISM CHANGED AND THE RULE DID NOT. Mail now goes through Microsoft
# Graph -- server-held approved templates, batches, drafts, an outbound queue --
# so `mailtoFor()` and the client-side template table are gone, and the old
# version of this check was asserting the absence of a function rather than the
# presence of a guarantee. It failed for the right reason and would have gone
# on failing for the wrong one.
#
# Restated against the new design, where the rule is enforced harder than
# before: no approved template means HTTP 400, not a default. Anything that
# introduces a fallback -- `templateId || "meeting"`, a first-template-wins
# lookup -- silently reverses the original decision.
# ---------------------------------------------------------------------------
@check("an email batch cannot be created without an explicitly chosen template")
def _email_optin():
    svc = text(API / "shared" / "email-service.js")
    # The lookup takes whatever the client sent and NOTHING else -- no default
    # id substituted on the way in.
    looks_up = re.search(r"getTemplate\(String\(input\.templateId \|\| \"\"\)\)", svc)
    # Absent template is a refusal, not a fallback.
    refuses = re.search(r"if \(!template\) throw httpError\(400", svc)
    # No id may be defaulted anywhere in the mail path.
    defaulted = [f.name for f in (API / "shared").glob("email-*.js")
                 if re.search(r"templateId\s*\|\|\s*[\"'][a-z]", text(f))]
    """A raw mail link is now offered DELIBERATELY, for the one-off note that
    does not belong in a campaign -- so the rule is no longer "no mailto:".

    What a mailto: skips is the whole in-app path: the batch, the send limits,
    the logging, and the suppression check. The first three are the point of it.
    The fourth is not negotiable: a rep must not be handed a one-click way to
    write to somebody who has asked us to stop, with nothing on screen saying
    the guard was skipped.

    So every file offering one has to gate it on the do-not-call list. The
    address may still be SHOWN -- a rep who can read it decides better than one
    shown nothing -- but not as a link.
    """
    unguarded = []
    for path in WEB.glob("*.js"):
        body = text(path)
        if "mailto:" not in body:
            continue
        if "isDnc" not in body:
            unguarded.append(path.name)
    ok = bool(looks_up) and bool(refuses) and not defaulted and not unguarded
    return (ok, f"no default id={bool(looks_up) and not defaulted}, "
                f"refuses when absent={bool(refuses)}, "
                f"mailto: gated on do-not-call={not unguarded}"
                + (f"; DEFAULTED IN {defaulted}" if defaulted else "")
                + (f"; UNGUARDED MAILTO IN {unguarded}" if unguarded else ""))


# ---------------------------------------------------------------------------
# 40. CRM notes are turned back into text before they are displayed
#
# THE BUG THIS ENCODES: Act! stores history details as rich text and returns
# them as HTML. Both views escape everything before rendering -- correctly, and
# that is exactly why `<span>`, `<p>` and `<br>` appeared on screen as literal
# tags rather than as formatting.
#
# The fix must stay a PARSER, not a regex. A regex over HTML mishandles
# attributes containing `>`, cannot tell `&lt;p&gt;` from a real tag, and
# leaves `&nbsp;` and `&#39;` in the output -- so it half-works, which is the
# version that survives review and then shows up in front of a rep.
#
# It must also keep escaping on the render side. Stripping tags is a
# legibility fix, never a licence to trust the string.
# ---------------------------------------------------------------------------
@check("Act! history notes are parsed to plain text before display")
def _crm_plaintext():
    src = text(WEB / "dial.js")
    body = src.split("function plainText(", 1)[-1].split("\n  }", 1)[0]
    parsed = "DOMParser" in body
    # <style>/<script> hold text that is not content; textContent would return
    # the CSS of a pasted Outlook signature verbatim.
    strips_noise = "style" in body and "script" in body
    # Every field shown from a CRM row has to go through it, not just details.
    desc = src.split("function describeHistory(", 1)[-1].split("\n    });", 1)[0]
    covered = all(f"plainText(e.{f})" in desc for f in ("subject", "note", "who", "type"))
    # And the views must still escape what comes back.
    escaped = all("esc(r.text)" in text(WEB / f) for f in ("field.js", "app.js"))
    ok = parsed and strips_noise and covered and escaped
    return (ok, f"parser={parsed}, drops style/script={strips_noise}, "
                f"all CRM fields cleaned={covered}, still escaped={escaped}")


# ---------------------------------------------------------------------------
# 41. The Graph callback survives the trip back from Microsoft
#
# THE BUG THIS ENCODES: /api/email-auth originally required a signed-in
# principal. Microsoft returns the browser to it as a CROSS-SITE navigation, on
# which the Static Web Apps session cookie is not sent, so the platform answered
# 401, the config's 401 override bounced the request to /.auth/login/aad, and
# the authorization code was discarded. The rep was silently returned to a
# perfectly working app that had connected nothing -- no error, no message, an
# infinite "Connect your Microsoft 365 mailbox" loop.
#
# The route must therefore be reachable anonymously, and it must be listed
# BEFORE /api/* -- Static Web Apps takes the first matching rule, so the order
# is the whole mechanism.
#
# Anonymous here is not unauthenticated in substance. Identity comes from the
# state row (24 random bytes, single-use, ten-minute expiry, written against the
# user when the flow began), and the control that actually matters is the
# equality check in complete(): the Graph profile id must equal that userId
# before any token is stored. Losing THAT check would let one employee attach
# another's mailbox, so it is asserted here too.
# ---------------------------------------------------------------------------
@check("Graph OAuth callback is reachable and still binds to one identity")
def _graph_callback():
    cfg = read(WEB / "staticwebapp.config.json")
    routes = [r.get("route", "") for r in cfg.get("routes", [])]
    rule = next((r for r in cfg.get("routes", []) if r.get("route") == "/api/email-auth"), None)
    anon = bool(rule) and rule.get("allowedRoles") == ["anonymous"]
    # First match wins, so the specific rule is useless after the wildcard.
    ordered = ("/api/email-auth" in routes and "/api/*" in routes
               and routes.index("/api/email-auth") < routes.index("/api/*"))
    # The handler must tolerate a missing principal rather than throwing.
    handler = text(API / "email-auth" / "index.js")
    tolerant = "catch" in handler and "who = null" in handler
    # Identity is taken from the state row, not from the header.
    auth = text(API / "shared" / "email-auth.js")
    from_state = "authState.userId" in auth
    # And the mailbox must still be proven to belong to that user.
    bound = re.search(r"profile\.id\)\.toLowerCase\(\)\s*!==\s*String\(user\.id\)", auth) is not None
    ok = anon and ordered and tolerant and from_state and bound
    return (ok, f"anonymous={anon}, before /api/*={ordered}, "
                f"tolerates no principal={tolerant}, identity from state={from_state}, "
                f"mailbox bound to user={bound}")


# ---------------------------------------------------------------------------
# 42. Queue messages are encoded the way the host expects to read them
#
# THE BUG THIS ENCODES: the producer passed { messageEncoding: "base64" } to
# QueueClient. That is a .NET SDK option. The JavaScript Storage SDK ignores
# unknown options silently, so the code READ as though it encoded and did not.
# Messages went onto the queue as plain text while the Functions host defaults
# to Base64.
#
# The failure mode is the worst kind: the host could not decode the message, so
# it never bound it to a function and never invoked the worker. No exception, no
# invocation, nothing in Application Insights -- just messages appearing in the
# poison queue thirty seconds later. Every diagnostic that looks for a failing
# function finds nothing, because no function ever ran.
#
# So: encode explicitly, where it can be seen, and never reintroduce the option
# that looks like it does the job.
# ---------------------------------------------------------------------------
@check("queue messages are base64-encoded to match the host's decoder")
def _queue_encoding():
    src = text(API / "shared" / "email-service.js")
    # The option that silently does nothing must not come back.
    no_fake_option = "messageEncoding" not in src.split("function enqueue(", 1)[0].split("new QueueClient(", 1)[-1]
    body = src.split("async function enqueue(", 1)[-1].split("\n}", 1)[0]
    encodes = 'toString("base64")' in body and "sendMessage" in body
    # The worker must be able to read what the producer writes.
    worker = text(API / "email-worker" / "index.js")
    decodes = 'from(String(value), "base64")' in worker or '"base64"' in worker
    ok = no_fake_option and encodes and decodes
    return (ok, f"no ignored SDK option={no_fake_option}, producer encodes={encodes}, "
                f"worker decodes={decodes}")


# ---------------------------------------------------------------------------
# 43. Publishing an attachment is admin-only and PDF-verified by content
#
# Reps choose attachments from an approved catalog and cannot attach anything
# else. That is the control which makes "the rep sent last year's fact sheet"
# structurally impossible, so the two things holding it up are asserted here.
#
# The role check must be SERVER-side. The composer hides the panel from
# non-admins, but hiding a button is presentation, not authorisation.
#
# And the PDF check must read the file's own bytes. A browser's claimed MIME
# type and the file extension are both supplied by whoever is uploading, so
# neither is evidence -- a renamed .docx would otherwise reach an advisor as a
# file Outlook cannot open, and we would hear about it from the advisor.
# ---------------------------------------------------------------------------
@check("attachment publishing is admin-gated and verified as real PDF")
def _document_admin():
    api_src = text(API / "email" / "index.js")
    block = api_src.split('op === "put_document"', 1)[-1].split("throw service.httpError(400, \"Unknown", 1)[0]
    gated = "isAdmin(who)" in block and "403" in block
    store_src = text(API / "shared" / "email-store.js")
    # The magic number, not the MIME type the client claimed.
    by_content = '"%PDF-"' in store_src and "assertPdf" in store_src
    called = re.search(r"async function putDocument[\s\S]{0,400}?assertPdf\(bytes\)", store_src) is not None
    # Withdrawing a document must remove the row first: listDocuments() reads the
    # row, so a blob-first order leaves a selectable document with no bytes.
    delete_src = store_src.split("async function deleteDocument", 1)[-1].split("\n}", 1)[0]
    row_first = delete_src.index("deleteEntity") < delete_src.index("deleteIfExists")
    ok = gated and by_content and called and row_first
    return (ok, f"server role check={gated}, checks %PDF- header={by_content}, "
                f"checked before storing={called}, delete removes row first={row_first}")


# ---------------------------------------------------------------------------
# 44. Inline charts can only come from images approved onto that template
#
# Three quarters of the Word template library is built around a chart in the
# body, so the application has to render one -- and rendering an <img> from
# author-supplied text is exactly how an HTML injection gets into an email that
# a rep then sends to two hundred advisors under their own name.
#
# The safety rests on ORDER: the body is escaped first, and the {{image:id}}
# token is matched afterwards. So the only path to a tag is a token naming an
# image already uploaded to that template. Reverse those two steps and pasted
# markup renders.
#
# The scheme allowlist matters too. cid: refers to a part attached to this
# message; http(s) images are deliberately absent, because Outlook blocks remote
# images by default and the chart would simply be missing.
#
# Finally, image tokens legitimately remain in bodyText -- they are resolved in
# bodyHtml, not substituted away -- so the unresolved-merge-field check must
# exclude them, or every chart blocks its own batch.
# ---------------------------------------------------------------------------
@check("inline charts render only from template-approved images")
def _inline_images():
    core = text(API / "shared" / "email-core.js")
    body = core.split("function plainTextToSafeHtml(", 1)[-1].split("\n}", 1)[0]
    # Escape, then match the token -- in that order.
    escapes_first = body.index("&amp;") < body.index("IMAGE_TOKEN")
    # An unrecognised id must fall through as text, never render a tag.
    falls_through = "if (!image) return whole" in body
    cid_only = 'allowedSchemesByTag: { img: ["cid"] }' in core
    no_remote_img = "allowedSchemes:" in core and '"https"' in core  # remote allowed for <a>, not <img>
    service = text(API / "shared" / "email-service.js")
    # Image tokens legitimately survive in bodyText -- they resolve into bodyHtml
    # rather than being substituted out -- so they must not be reported as
    # unresolved merge fields, or every chart blocks its own batch. The rule used
    # to be a bare "starts with image:" test; it now additionally requires the id
    # to exist on the template, which is checked by check 50.
    token_exempt = ('/^image:/i.test(token)' in service
                    and "unresolved.push(token); continue;" in service)
    required = "requiredDocumentIds" in service and "required.includes(x)" in service
    api_src = text(API / "email" / "index.js")
    gated = re.search(r'"put_template".{0,200}?isAdmin\(who\)', api_src, re.S) is not None
    ok = all([escapes_first, falls_through, cid_only, no_remote_img, token_exempt, required, gated])
    return (ok, f"escape before token={escapes_first}, unknown id stays text={falls_through}, "
                f"cid-only img scheme={cid_only}, image tokens exempt from merge check={token_exempt}, "
                f"required attachments enforced={required}, admin-gated={gated}")


# ---------------------------------------------------------------------------
# 45. The unsubscribe link cannot act on a GET.
#
# Corporate mail gateways -- Mimecast, Proofpoint, Defender for Office, and the
# scanners at the wirehouses this application mails -- pre-fetch every link in
# an inbound message to check it for malware. An endpoint that suppressed on GET
# would therefore unsubscribe a large fraction of every batch before a single
# human read a word, and the damage would be invisible: the opt-outs would be
# indistinguishable from genuine ones.
#
# So: GET renders a confirmation page, POST writes.
#
# POST-ONLY WAS NOT ENOUGH. Three Raymond James recipients were suppressed with
# nobody clicking, and the telemetry showed the same shape each time: a GET, a
# ~60 second dwell, a second GET, a POST six seconds later. That is a detonation
# sandbox opening the page and pressing the only control on it. So the form now
# asks for the address the message was sent to -- something the page does not
# contain, so an automated submit arrives empty and does nothing.
#
# The typed address only GATES. What gets suppressed is still the address inside
# the signed token, or the page becomes a way to unsubscribe a third party. And
# the address must never be RENDERED here: printed above the box it would be
# there for a form-filling scanner to copy, and the gate would be theatre.
# ---------------------------------------------------------------------------
@check("the unsubscribe form needs a typed address and suppresses only the token's")
def _unsubscribe_is_post_only():
    src = text(API / "email-preferences" / "index.js")
    suppress_src = text(API / "shared" / "email-suppress.js")
    # The only call to suppress() sits inside the POST branch.
    post_branch = src.split('req.method === "POST"', 1)
    writes_on_post = len(post_branch) == 2 and "suppress.suppress(" in post_branch[1]
    no_write_before = "suppress.suppress(" not in post_branch[0]
    # The token still names the victim: suppress() is handed the token's address,
    # never the typed one. Written as a positive match so that renaming the
    # variable to the submitted value cannot pass silently.
    token_only = ("suppress.readToken(token)" in src
                  and re.search(r"suppress\.suppress\(\s*email\b", src) is not None
                  and re.search(r"suppress\.suppress\(\s*typed\b", src) is None)
    # A submitted address is REQUIRED, and must equal the token's before any
    # write happens. Both halves matter: a comparison that is never reached, or
    # one that lets the empty string through, is the bug this check exists for.
    gate = post_branch[1].split("suppress.suppress(")[0] if len(post_branch) == 2 else ""
    reads_typed = 'fieldFrom(req, "email")' in gate
    compares = re.search(r"typed\s*!==\s*suppress\.norm\(email\)", gate) is not None
    rejects_blank = re.search(r"!typed\s*\|\|", gate) is not None
    typed_gate = reads_typed and compares and rejects_blank
    # The address is never printed back -- not on the form, not on the error, not
    # on the confirmation. esc(email) anywhere in the rendered page is the tell.
    no_address_shown = re.search(r"esc\(\s*email\s*\)", src) is None
    signed = "createHmac" in suppress_src and "timingSafeEqual" in suppress_src
    # The typed-address gate is worthless if the address is readable in the URL:
    # a base64 payload hands a scanner the very value the box asks for. Sealed
    # with AES-GCM, whose tag is also the integrity check. The legacy HMAC path
    # stays -- those links are in inboxes -- which is what `signed` still covers.
    sealed = ("createCipheriv" in suppress_src and "aes-256-gcm" in suppress_src
              and "getAuthTag" in suppress_src and "setAuthTag" in suppress_src
              and re.search(r'return\s+`e\.\$\{b64url', suppress_src) is not None)
    # Who asked. Application Insights masks client_IP and records no user agent
    # for Functions, so without this the next incident is inferred from timestamp
    # shapes again. The REJECTED path must log too: a blank submit is harmless
    # now, which makes it a free scanner detector -- but only if it is recorded.
    logs_agent = ("user-agent" in src and "x-forwarded-for" in src
                  and re.search(r"REJECTED[^\"`]*\$\{who\(req\)\}", src) is not None)
    anon = json.loads(text(API / "email-preferences" / "function.json"))
    anonymous = anon["bindings"][0].get("authLevel") == "anonymous"
    routes = json.loads(text(WEB / "staticwebapp.config.json"))["routes"]
    # SWA validates this file against a strict schema and REFUSES THE WHOLE
    # DEPLOY on an unknown property -- there is no "ignore what you don't
    # recognise". A "comment" key added to explain a route once blocked a
    # release for exactly this reason; JSON has no comments and the schema has
    # no room to pretend otherwise. Reasoning belongs in the docs.
    allowed = {"route", "rewrite", "redirect", "allowedRoles", "headers",
               "methods", "statusCode"}
    unknown = {k for r in routes for k in set(r) - allowed}
    names = [r["route"] for r in routes]
    # First match wins, so the anonymous route must precede /api/*.
    ordered = ("/api/email-preferences" in names
               and names.index("/api/email-preferences") < names.index("/api/*"))
    ok = all([writes_on_post, no_write_before, token_only, typed_gate,
              no_address_shown, signed, sealed, logs_agent, anonymous, ordered,
              not unknown])
    return (ok, f"writes only on POST={writes_on_post and no_write_before}, "
                f"typed address required and must match={typed_gate}, "
                f"suppresses the token's address={token_only}, "
                f"address never rendered={no_address_shown}, "
                f"token sealed, not merely signed={sealed}, "
                f"legacy HMAC still timing-safe={signed}, "
                f"requester logged incl. rejects={logs_agent}, "
                f"anonymous={anonymous}, route precedes /api/*={ordered}, "
                f"unknown SWA route keys={sorted(unknown) or 0}")


# ---------------------------------------------------------------------------
# 46. Opt-outs are honoured at both ends of the batch's life.
#
# Checking only at creation lets a batch that sat half-edited for an hour mail
# someone who unsubscribed in the meantime. Checking only at approval means the
# rep reviews sixty messages and silently sends fifty-seven, with no idea which
# three vanished. Both checks are needed, and they do different jobs.
#
# The suppression list is keyed on the ADDRESS, not the CRD: the advisor is
# asking about the address they were mailed at, and a CRD key would both
# over-suppress (every address for that person) and under-suppress (anyone whose
# address we hold without a CRD match).
# ---------------------------------------------------------------------------
@check("opt-outs and the kill switch are enforced right up to the send call")
def _optout_enforced():
    service = text(API / "shared" / "email-service.js")
    suppress_src = text(API / "shared" / "email-suppress.js")
    at_create = "blockedAmong(recipients)" in service
    at_approve = "nowBlocked" in service and "recipient_opted_out" in service

    # The interval this check used to miss entirely.
    #
    # It described approval as "the other end" of suppression, which is wrong:
    # messages are paced apart by mailboxIntervalSeconds, so at the default of
    # five seconds a 250-recipient batch is still sending twenty minutes after it
    # was approved. Both controls have to be re-read immediately before
    # sendDraft(), or an opt-out made in minute three is ignored in minute
    # twelve, and a kill switch stops only NEW approvals.
    worker = text(API / "email-worker" / "index.js")
    send_fn = worker.split("async function send(")[1].split("async function reconcile(")[0]
    guard = send_fn.split("sendDraft(")[0]
    rechecks_optout = "blockedAmong" in guard
    rechecks_policy = "policy()" in guard
    audits_block = "send_blocked_recipient_opted_out" in send_fn
    audits_kill = "send_halted_by_kill_switch" in send_fn
    # A dropped recipient is named, not silently absent.
    explained = "suppressedNote" in service
    keyed_on_address = "keyFor" in suppress_src and "sha256" in suppress_src
    # Local write first, Act! afterwards -- a CRM outage must not lose an opt-out.
    page = text(API / "email-preferences" / "index.js")
    local_first = page.index("suppress.suppress(") < page.index("act.markDoNotEmail(")
    act_src = text(API / "shared" / "act.js")
    # The Act! push must use the PROVEN activity/history path, not a guessed
    # contact-field write. An earlier version patched a "doNotEmail" flag and a
    # "contactNotes" string that nothing here had ever exercised -- and the note
    # field would have overwritten existing notes rather than appending.
    act_pushes = ("markDoNotEmail" in act_src
                  and "api/organizers/" in act_src.split("async function markDoNotEmail")[-1]
                  and "doNotEmail: true" not in act_src
                  and "contactNotes:" not in act_src)
    ok = all([at_create, at_approve, explained, keyed_on_address, local_first, act_pushes,
              rechecks_optout, rechecks_policy, audits_block, audits_kill])
    return (ok, f"filtered at creation={at_create}, re-checked at approval={at_approve}, "
                f"re-checked before sendDraft: opt-out={rechecks_optout} kill switch={rechecks_policy}, "
                f"both audited={audits_block and audits_kill}, "
                f"dropped recipients named={explained}, keyed on address={keyed_on_address}, "
                f"local write precedes Act!={local_first}, Act! push present={act_pushes}")


# ---------------------------------------------------------------------------
# 47. The approval passcode is enforced on the server, with a lockout.
#
# A client-side check is one refresh away from being no check. And four digits
# is 10,000 guesses -- seconds of scripting -- so the attempt counter is what
# turns the gate into something more than decoration. It lives in table storage
# rather than memory because the Function app scales out and restarts.
#
# The threshold ships to the client so the prompt appears at the right time; the
# code itself must never leave the server.
# ---------------------------------------------------------------------------
@check("the approval passcode is server-enforced and rate-limited")
def _passcode():
    core_src = text(API / "shared" / "email-core.js")
    service = text(API / "shared" / "email-service.js")
    store_src = text(API / "shared" / "email-store.js")
    timing_safe = "timingSafeEqual" in core_src
    checked_in_approve = "passcodeRequired(batch.recipientCount" in service
    locks_out = "lockedOut" in service and "recordPasscodeFailure" in store_src
    persisted = 'table("policy")' in store_src and "PASSCODE_MAX_TRIES" in store_src
    # Threshold travels to the client; the code does not.
    exposes_threshold = "passcodeOver: cfg.passcode ? cfg.passcodeOver : null" in service
    leaks_code = re.search(r"passcode:\s*cfg\.passcode", service) is not None
    # Unset code disables the gate rather than locking everyone out.
    fails_open_unset = "!!cfg.passcode" in core_src
    ok = all([timing_safe, checked_in_approve, locks_out, persisted,
              exposes_threshold, not leaks_code, fails_open_unset])
    return (ok, f"timing-safe compare={timing_safe}, enforced in approve={checked_in_approve}, "
                f"lockout={locks_out and persisted}, threshold exposed without code={exposes_threshold and not leaks_code}, "
                f"unset code disables gate={fails_open_unset}")


# ---------------------------------------------------------------------------
# 48. Cross-module references actually exist, and audit() is called correctly.
#
# Two silent failures of the same shape, both found in production behaviour
# rather than by anything failing loudly:
#
#   * email-store.js used core.IMAGE_TOKEN, which was never exported. Reading a
#     missing export yields undefined rather than an error, and
#     matchAll(undefined) does not throw either -- it matches an empty regex at
#     every position. The result was that EVERY template save was rejected for
#     referencing an image called "undefined", including templates with no
#     charts at all.
#
#   * The four admin routes called audit(who, event, details) against a
#     signature of audit(userId, batchId, event, details). Nothing threw: the
#     user object stringified into the partition key and the event name landed
#     in the batchId slot, so the compliance trail for template and document
#     publishing was written unattributable.
#
# JavaScript will not catch either of these. This check does.
# ---------------------------------------------------------------------------
@check("cross-module core.* references resolve and audit() arity is right")
def _module_contract():
    # Comments are stripped BEFORE the exports block is located: a brace inside
    # a comment (a {{image:x}} example, say) otherwise terminates the capture
    # early and the check silently sees an empty export list.
    core_src = re.sub(r"//.*", "", text(API / "shared" / "email-core.js"))
    m = re.search(r"module\.exports = \{(.*?)\};", core_src, re.S)
    exported = set()
    if m:
        exported = {t.strip() for t in m.group(1).split(",")
                    if t.strip() and t.strip().isidentifier()}
    used = set()
    for f in ("email-store.js", "email-service.js", "email-auth.js"):
        path = API / "shared" / f
        if path.exists():
            used |= set(re.findall(r'\bcore\.([A-Za-z_][A-Za-z0-9_]*)',
                                   re.sub(r'//.*', '', text(path))))
    missing = sorted(used - exported)

    # audit(userId, batchId, event, details): every call passes at least three
    # arguments, and the event slot is a string rather than an object.
    bad_audit = []
    for f in (API / "email" / "index.js", API / "shared" / "email-service.js"):
        for call in re.findall(r'audit\(([^;]*?)\);', text(f), re.S):
            args, depth, cur = [], 0, ""
            for ch in call:
                if ch in "([{":
                    depth += 1
                elif ch in ")]}":
                    depth -= 1
                if ch == "," and depth == 0:
                    args.append(cur.strip()); cur = ""
                else:
                    cur += ch
            args.append(cur.strip())
            third = args[2] if len(args) > 2 else ""
            if len(args) < 3 or not (third.startswith(chr(34)) or third.startswith(chr(96))
                                     or third.startswith("mode ===")):
                bad_audit.append(" ".join(call.split())[:70])

    ok = not missing and not bad_audit
    return (ok, f"unexported core.* used elsewhere={missing or 0}, "
                f"audit() calls with a non-string event slot={bad_audit or 0}, "
                f"core exports seen={len(exported)}, core.* references seen={len(used)}")


# ---------------------------------------------------------------------------
# 49. The Act! do-not-email floor is present and wired into the send path.
#
# Act! carries a Mail Code picklist (customFields.email__y_n) recording years of
# opt-outs made long before this application had a suppression list. On the
# 2026-08-13 export, 2,383 addresses that Act! marks do-not-email were still
# selectable and sendable here -- 701 of them explicit UNSUBSCRIBEs -- because the
# app only knew about clicks on its own footer link.
#
# The floor must be keyed on BOTH the address and the CRM contact id. Act! users
# overwrite the email field with a note ("unsubscribed 3/27/26"), which destroys
# the address but not the opt-out, while we still hold a working address from SEC
# data; 823 people were reachable only via the contact id.
#
# An empty or missing floor is the dangerous state, because it looks like
# "nobody is suppressed" rather than like a fault.
# ---------------------------------------------------------------------------
@check("the Act! do-not-email floor is loaded and enforced")
def _act_floor():
    path = API / "shared" / "act_mail_codes.json"
    data = json.loads(text(path))
    addresses, crds = data.get("addresses", {}), data.get("crds", {})
    # Address coverage is independent of identity. CRD fallback is intentionally
    # limited to ledger-approved Act routes.
    approved_routes = read(API / "shared" / "act_contacts.json").get("contacts", {})
    crds_approved = set(crds) <= set(approved_routes)
    populated = len(addresses) > 2000 and len(crds) > 100
    codes = set(addresses.values()) | set(crds.values())
    expected_codes = codes and codes <= {"U", "N", "NC", "BB"}

    src = text(API / "shared" / "email-suppress.js")
    both_keys = "byAddress" in src and "byCrd" in src
    # A storage failure must not silently mean "nobody is suppressed".
    fails_closed = "no batch can be built" in src

    service = text(API / "shared" / "email-service.js")
    at_create = "suppress.blockedAmong(recipients)" in service
    at_approve = "nowBlocked" in service and "recipient_opted_out" in service
    # The contact id has to reach the check, or the CRD half of the floor is dead.
    passes_crd = "contactId: m.contactId" in service
    explained = "blocked.get(r.email)" in service

    ok = all([populated, crds_approved, expected_codes, both_keys, fails_closed,
              at_create, at_approve, passes_crd, explained])
    return (ok, f"{len(addresses)} addresses / {len(crds)} approved CRDs, "
                f"CRDs subset of Act routes={crds_approved}, codes={sorted(codes)}, "
                f"both keys={both_keys}, fails closed={fails_closed}, "
                f"enforced at create={at_create} and approve={at_approve}, "
                f"contact id reaches the check={passes_crd}, reason shown={explained}")


# ---------------------------------------------------------------------------
# 50. Merge fields and chart placeholders survive a rep's editing.
#
# Individual messages hold ALREADY-RENDERED text, so the risk is concentrated in
# one box: the common template a rep may edit. Two ways it went wrong silently:
#
#   * A mistyped chart id. {{image:performace}} was exempted from the
#     unresolved-field check by its "image:" prefix alone, resolved to nothing in
#     bodyHtml, and was mailed as the literal string "{{image:performace}}".
#   * A single brace. {first_name} is not a token, so it renders as literal text
#     and the unresolved-field check never sees it -- only lintTemplate catches
#     it, and lintTemplate ran when an ADMINISTRATOR published a template and
#     never when a rep edited one.
# ---------------------------------------------------------------------------
@check("rep edits cannot silently break merge fields or chart placeholders")
def _merge_safety():
    service = text(API / "shared" / "email-service.js")

    validate = service.split("async function validateMessage")[1].split("async function validateBatch")[0]
    # Chart ids checked against the template, not merely against a prefix.
    ids_checked = "knownImageIds && !knownImageIds.has(id)" in validate
    reports_it = 'code: "unknown_image"' in validate
    # A failed template lookup must not block the batch.
    fails_open = "knownImageIds = tpl ?" in service

    common = service.split("async function updateCommon")[1].split("async function updateMessage")[0]
    # A rep's edit passes the same lint the approved template had to pass.
    lints_edits = "core.lintTemplate({ subject: subjectTemplate" in common
    blocks = 'code = "common_text_invalid"' in common
    # And a deletion is reported, which no typo check can catch.
    reports_removals = "removedTokens" in common or "const removed" in common

    webapp = text(WEB / "email.js")
    live_lint = "wireCommonLint" in webapp
    restore = 'data-email="restore-common"' in webapp and "approvedText" in webapp

    ok = all([ids_checked, reports_it, fails_open, lints_edits, blocks,
              reports_removals, live_lint, restore])
    return (ok, f"chart ids checked against the template={ids_checked and reports_it}, "
                f"unloadable template fails open={fails_open}, "
                f"rep edits linted and blocked={lints_edits and blocks}, "
                f"removals reported={reports_removals}, live lint={live_lint}, restore={restore}")


# ---------------------------------------------------------------------------
# 51. Bounce handling suppresses dead addresses without ever inventing one.
#
# The failure modes here are wildly asymmetric. A MISSED bounce means we mail a
# dead address again next quarter -- annoying, self-correcting. A FALSE bounce
# permanently and silently stops us contacting a reachable advisor, and nobody
# finds out until they ask why we went quiet. So every rule below biases toward
# doing nothing:
#
#   * hard (5.x.x) only; soft (4.x.x) recovers on its own
#   * permanent failures that are not about the ADDRESS -- mailbox full, message
#     too large, blocked by their policy -- are recorded and never suppressed
#   * the address comes from OUR sent record, never from the report, because an
#     NDR is attacker-supplied text and anyone can send us one
#   * the rep's mailbox is read, never modified
# ---------------------------------------------------------------------------
@check("bounce sweeping suppresses only genuinely dead addresses")
def _bounce_sweep():
    parser = text(API / "shared" / "email-bounce.js")
    sweep = text(API / "email-bounce-sweep" / "index.js")

    # The gate lives in assess(): anything that is not a hard bounce comes back
    # act:false, and the sweeper acts only on act:true.
    # Only a hard bounce sets act:true; soft and policy come back act:false with
    # record:true, so they are observed without ever suppressing an address.
    hard_only = ('verdict.kind === "hard"' in parser
                 and "act: true, record: true" in parser
                 and "act: false, record: true" in parser
                 and "if (!verdict.act)" in sweep)
    soft_ignored = 'return { kind: "soft", code }' in parser
    policy_codes = all(c in parser for c in ['"5.2.2"', '"5.7.1"', '"5.3.4"'])
    # The address is read off our own record.
    address_from_us = "ours.recipientEmail" in parser
    forgery_guard = 'reason: "recipient-mismatch"' in parser
    # And a report naming nothing we sent is dropped.
    unmatched = 'reason: "unmatched"' in parser

    # Never modifies the mailbox.
    graph_src = text(API / "shared" / "graph-mail.js")
    inbox = graph_src.split("async function recentInbox")[1].split(chr(10) + "}")[0]
    read_only = "isRead" not in inbox and "PATCH" not in inbox and "DELETE" not in inbox

    # Local suppression before the CRM, so an Act! outage cannot lose it.
    local_first = sweep.index("suppressEmail(") < sweep.index("markHardBounce(")
    # Off by default.
    gated = 'EMAIL_BOUNCE_SWEEP_ENABLED !== "1"' in sweep
    # A literal cron: an unresolved %SETTING% stops the host loading the function
    # at all, which would take the sending API down with it.
    schedule = json.loads(text(API / "email-bounce-sweep" / "function.json"))["bindings"][0]["schedule"]
    literal_cron = "%" not in schedule

    act_src = text(API / "shared" / "act.js")
    # A bounce writes N and must never overwrite a U somebody set deliberately.
    never_downgrades = "rankNow >= MAIL_CODE_RANK[target]" in act_src
    writes_n = 'MAIL_CODE_UNREACHABLE = "N"' in act_src

    ok = all([hard_only, soft_ignored, policy_codes, address_from_us, forgery_guard,
              unmatched, read_only, local_first, gated, literal_cron,
              never_downgrades, writes_n])
    return (ok, f"hard-only={hard_only and soft_ignored}, policy codes exempt={policy_codes}, "
                f"address from our record={address_from_us and forgery_guard}, "
                f"unmatched dropped={unmatched}, mailbox read-only={read_only}, "
                f"local before Act!={local_first}, disabled by default={gated}, "
                f"literal cron={literal_cron}, Act! N without downgrading U={never_downgrades and writes_n}")


# ---------------------------------------------------------------------------
# 52. The call queue records where an advisor SITS, not where they are licensed.
#
# `rs` on a pin is the pipe-delimited list of states an advisor is registered in
# -- "GA|LA|TX" -- and has nothing to do with their office. The queue snapshot
# took its first entry and printed it beside p.c, the office city, producing
# pairs that do not exist: an advisor at 1776 Peachtree Street NW showed as
# "Atlanta, TX" because Texas was the only state he was licensed in.
#
# It was wrong for 89,991 of 418,916 advisors, and wrong in the most misleading
# way available -- a plausible city/state pair invites no suspicion at all.
#
# `_state` is the pin file the record was loaded from, so it is the office state
# by construction. Other uses of `rs` (the profile panel, the firm aggregate,
# the CSV export) legitimately want the whole list and are unaffected.
# ---------------------------------------------------------------------------
@check("the call queue stores the office state, not a registration state")
def _queue_state():
    src = text(WEB / "app.js")
    builder = src.split("function recipientFor")[0]
    # THE WHOLE FUNCTION, not a fixed byte window.
    #
    # This read 1,200 characters from the `crd:` line, which is fine until
    # somebody adds a comment. Explaining WHY the field must stay _state pushed
    # the field itself to 1,696 characters and the check reported the very
    # regression the comment was warning about. A scanner whose reach depends on
    # how much prose sits above the code is measuring the prose.
    fn_start = src.find("function dialSnapshot(")
    fn_end = src.find(chr(10) + "}", fn_start) if fn_start > -1 else -1
    snippet = src[fn_start:fn_end] if fn_start > -1 and fn_end > -1 else ""
    uses_office = "state: (p && p._state)" in snippet
    no_reg_state = 'state: (p && p.rs' not in src
    # Comments stripped first -- the note explaining this bug quotes the old
    # expression verbatim, and a scanner that reads its own documentation as
    # evidence of the defect is worse than no scanner.
    code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    code = re.sub(r"//.*", "", code)
    # The legitimate uses keep the whole list rather than its first element.
    first_only = re.findall(r"rs\.split\([^)]*\)\[0\]", code)
    ok = uses_office and no_reg_state and not first_only
    return (ok, f"queue uses _state={uses_office}, no rs-derived state={no_reg_state}, "
                f"rs used only as a full list={not first_only}")


# ---------------------------------------------------------------------------
# 53. Starter templates are seeded once, not whenever the catalog is empty.
#
# "Empty means seed" meant an administrator who deleted all four starter
# templates got them back on the next page load, with no way to refuse. These
# are exactly the templates that have NOT been through compliance review, so a
# catalog whose whole purpose is to hold approved wording kept refilling itself
# with unapproved wording.
# ---------------------------------------------------------------------------
@check("deleted starter templates stay deleted")
def _template_seed():
    src = text(API / "shared" / "email-store.js")
    guarded = 'if (!out.length && !(await getOptional("templates", "seed", "builtin"))' in src
    marks_on_seed = 'markTemplatesSeeded("first run")' in src
    # Deleting is an administrator curating the catalog; recording it also covers
    # deployments seeded before the marker existed.
    marks_on_delete = "markTemplatesSeeded(`curated:" in src
    # The marker must not show up as a template.
    hidden = 'partitionKey: "seed", rowKey: "builtin"' in src and 'PartitionKey eq ${"approved"}' in src
    ok = all([guarded, marks_on_seed, marks_on_delete, hidden])
    return (ok, f"seed guarded by a marker={guarded}, marked on first run={marks_on_seed}, "
                f"marked when curated={marks_on_delete}, marker hidden from the list={hidden}")


# ---------------------------------------------------------------------------
# 54. Sender health measures what is actually observable.
#
# Every deliverability guide leads with the spam-complaint rate: under 0.1%,
# never 0.3%. Those are Gmail and Yahoo bulk-sender rules, and of 114,309
# mailable advisor addresses in this application TWENTY are consumer mailboxes.
# Everyone else sits behind a corporate gateway, and corporate tenants do not run
# feedback loops -- there is no "reported as spam" event coming back. A complaint
# dashboard here would read zero forever while implying everything was fine.
#
# So the four signals are the ones a corporate gateway does return: hard bounces,
# deferrals, policy refusals, unsubscribes. Deferrals in particular were being
# classified and discarded, which threw away the earliest evidence that a
# receiving gateway had started throttling us.
#
# And a rate below a floor sample is arithmetic, not evidence.
# ---------------------------------------------------------------------------
@check("sender health reports observable signals, per rep and per domain")
def _sender_health():
    h = text(API / "shared" / "email-health.js")
    sweep = text(API / "email-bounce-sweep" / "index.js")
    store_src = text(API / "shared" / "email-store.js")
    service = text(API / "shared" / "email-service.js")
    api_src = text(API / "email" / "index.js")

    # Soft and policy reports are recorded, not merely classified.
    records_soft = "recordDeliveryEvent" in sweep and "verdict.record" in sweep
    persisted = "recordDeliveryEvent" in store_src and "deliveryEvents" in store_src
    # Telemetry must never be able to undo a suppression.
    telemetry_isolated = "delivery event not recorded" in sweep
    # Per domain as well as per rep -- a firm-wide average hides the one gateway
    # that has started refusing.
    per_domain = "domains" in h and "throttles per domain" in h
    # A floor below which no verdict is given.
    has_floor = "MIN_SAMPLE" in h and "before they mean anything" in h
    # Every number carries what to do about it.
    advises = "function advise" in h and "list-quality problem" in h
    # Only real delivery attempts count.
    denominator = '["submitted", "sent"].includes(m.state)' in service
    # Admin-gated.
    gated = re.search(r'op === "sender_health".{0,160}?isAdmin\(who\)', api_src, re.S) is not None

    ok = all([records_soft, persisted, telemetry_isolated, per_domain,
              has_floor, advises, denominator, gated])
    return (ok, f"soft/policy recorded={records_soft and persisted}, "
                f"telemetry cannot undo a suppression={telemetry_isolated}, "
                f"per-domain breakdown={per_domain}, small-sample floor={has_floor}, "
                f"advice attached={advises}, denominator is real sends={denominator}, "
                f"admin-gated={gated}")


# ---------------------------------------------------------------------------
# 55. An unconfirmed contact match cannot be acted on by reflex.
#
# A review-tier contact was matched to the SEC record on a name similarity, not
# on an identifier. Jennifer Friberg's card carried Scott Friberg's Virginia
# number under an Atlanta address, and on 42% of these matches the CRM state
# contradicts where the SEC says the advisor sits.
#
# Showing the details is right -- a rep who can read them decides better than
# one shown nothing. Offering one-click ACTIONS on them is not, because every
# consequence lands on the wrong person: an outcome logged against her CRD, a
# history saying she was called, and -- the one that cannot be undone -- a
# firm-wide do-not-call that silences her because somebody else asked to be left
# alone.
# ---------------------------------------------------------------------------
@check("unconfirmed contact matches offer no one-click email, call or queueing")
def _unconfirmed_actions():
    app = text(WEB / "app.js")
    dial = text(WEB / "dial.js")
    field = text(WEB / "field.js")

    gated = 'const unconfirmed = c.t === "review"' in app
    # Email, phone and queueing all withheld.
    no_email = "Email unavailable" in app
    confirmed_email = (
        'const emailConfirmed = c.t === "confirmed"' in app and
        "if (!emailConfirmed)" in app and
        "Dial.emailRouteStatus(cur).ok" in app and
        'const emailConfirmed = r[COL.tier] === "confirmed"' in field and
        "|| !emailConfirmed" in field)
    no_phone = 'const href = unconfirmed ? "" : Dial.telHref' in app
    no_queue = "confirm who this is before adding them to a call list" in app.lower()                or "Confirm who this is before adding them to a call list." in app
    # The irreversible one is refused outright, in the shared vocabulary so both
    # shells inherit it.
    dnc_blocked = "item.unconfirmed" in dial and "cannot be added" in dial
    # And the flag rides on the queue entry, because the dialer shows only a
    # name and a number.
    on_snapshot = "unconfirmed: !!(c && c.t" in app and 'unconfirmed: r[COL.tier] === "review"' in field
    # The contradiction that makes the gating comprehensible.
    clash = "contact-clash" in app and "_state" in app

    ok = all([gated, no_email, confirmed_email, no_phone, no_queue,
              dnc_blocked, on_snapshot, clash])
    return (ok, f"actions gated={gated}, email withheld={no_email}, "
                f"confirmed-only email={confirmed_email}, call withheld={no_phone}, "
                f"queueing withheld={no_queue}, do-not-call refused={dnc_blocked}, "
                f"flag on the queue entry={on_snapshot}, state clash shown={clash}")


# ---------------------------------------------------------------------------
# 56. Text controls a phone can focus are at least 16px.
#
# iOS Safari zooms the whole page whenever a focused text control is under 16px.
# The field view's own note boxes were already 16px with a comment saying so,
# but the email composer's fields were 13px -- so every tap into a subject or a
# body line jolted the layout and the rep had to pinch back out.
#
# The rule is only about controls a phone actually focuses: checkboxes and file
# inputs never trigger it, and the desktop is unaffected, which is why the fix
# is scoped to the phone breakpoint rather than applied globally.
# ---------------------------------------------------------------------------
@check("phone text inputs are 16px, so iOS does not zoom on focus")
def _ios_input_zoom():
    css = text(WEB / "email.css")
    # The composer's controls all sit inside .email-label; the rule that sets
    # them must reach 16px inside a phone media query.
    phone_blocks = re.findall(r"@media\s*\(max-width:\s*760px\)\s*\{(.*?)\n\}", css, re.S)
    bumped = any("email-label" in b and "font-size:16px" in b.replace(" ", "")
                 for b in phone_blocks)
    # And the box has to be tall enough to hold the larger text.
    taller = any("height:44px" in b.replace(" ", "") for b in phone_blocks)

    # The other way to stop the zoom is to disable pinch-zoom, which trades an
    # accessibility loss for the fix and is not acceptable here.
    no_lockout = True
    for page in ("field.html", "index.html"):
        vp = text(WEB / page)
        if "maximum-scale" in vp or "user-scalable=no" in vp:
            no_lockout = False

    # The field view's own controls were already correct; keep them that way.
    fcss = text(WEB / "field.css")
    field_ok = "font-size:16px" in fcss.replace(" ", "")

    ok = all([bumped, taller, no_lockout, field_ok])
    return (ok, f"composer inputs 16px on phones={bumped}, box grown to fit={taller}, "
                f"pinch-zoom still allowed={no_lockout}, field note boxes still 16px={field_ok}")


@check("attachment blind copy is computed in one place, forced, and previewed")
def _material_bcc():
    """The failure this guards against is silence.

    A compliance blind copy that the worker adds but the preview does not show
    means the rep approves one recipient list and Exchange sends to another.
    A blind copy the preview shows but the worker does not add means the
    material desk never receives the literature and nobody finds out. Both are
    only avoided by the two sides calling the SAME function, so that is what is
    checked -- not that each independently looks right.
    """
    core = text(API / "shared" / "email-core.js")
    worker = text(API / "email-worker" / "index.js")
    service = text(API / "shared" / "email-service.js")
    graph = text(API / "shared" / "graph-mail.js")
    web = text(WEB / "email.js")

    # complianceBcc() is no longer called directly by either side: both now go
    # through extraRecipients(), which resolves the compliance copy TOGETHER
    # with the rep's own cc/bcc choices. That is the same invariant -- one
    # function, both sides -- with one more thing folded into it, so the check
    # follows the call rather than being relaxed.
    defined = ("function complianceBcc(" in core and "function extraRecipients(" in core
               and "extraRecipients," in core
               and "complianceBcc(message, cfg)" in core)
    on_draft = "core.extraRecipients(claimed," in worker
    on_preview = "core.extraRecipients(m, prefs," in service
    wired = "bccRecipients" in graph and "message.bcc" in graph
    shown = "Bcc:" in web
    # Only the attachment case, and never on an internal recipient.
    scoped = "message.attachments" in core and "isExternal(message.recipientEmail" in core

    ok = all([defined, on_draft, on_preview, wired, shown, scoped])
    return (ok, f"helper defined={defined}, used on draft={on_draft}, used in preview={on_preview}, "
                f"graph wired={wired}, shown to sender={shown}, attachment+external only={scoped}")


@check("signature carries a job title and exactly one phone number")
def _signature_shape():
    """Two separate defects lived here.

    The signature code already read jobTitle and mobilePhone; the STORED profile
    predated both, because it is captured once at connect time and never
    revisited. So the check has to cover the refresh as well as the rendering,
    or it passes while every real signature stays wrong.
    """
    core = text(API / "shared" / "email-core.js")
    auth = text(API / "shared" / "email-auth.js")
    service = text(API / "shared" / "email-service.js")

    order = (core.find("p.jobTitle") < core.find("inline(company)")
             and core.find("inline(p.displayName)") < core.find("p.jobTitle"))
    one_phone = "businessPhone || mobilePhone" in core
    labelled = "phoneLabel" in core
    versioned = "PROFILE_VERSION" in auth and "profileVersion" in auth
    refreshed = "refreshProfile" in auth and "auth.refreshProfile(who.id)" in service

    ok = all([order, one_phone, labelled, versioned, refreshed])
    return (ok, f"title between name and company={order}, single phone={one_phone}, "
                f"labelled T/M={labelled}, profile versioned={versioned}, refreshed in place={refreshed}")


def main() -> None:
    global VERBOSE
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--verbose", action="store_true",
                    help="print the detail line for passing checks too")
    args = ap.parse_args()
    VERBOSE = args.verbose

    failed = [r for r in RESULTS if not r[1]]
    width = max(len(n) for n, _, _ in RESULTS)
    for name, ok, detail in RESULTS:
        mark = "[ok]" if ok else "[!!]"
        if ok and not VERBOSE and "skipped" not in detail:
            print(f"  {mark} {name}")
        else:
            print(f"  {mark} {name:<{width}}  {detail}")

    print()
    if failed:
        print(f"[!] {len(failed)} of {len(RESULTS)} checks FAILED")
    else:
        print(f"[*] all {len(RESULTS)} checks passed")
    raise SystemExit(len(failed))


if __name__ == "__main__":
    main()
