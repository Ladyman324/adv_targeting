"""Static server for webapp/ that serves pre-compressed .gz -- but only when it
is provably current.

python -m http.server cannot do this: it ignores Accept-Encoding entirely and
sends contacts.json as 24 MB every time. This adds ~4.7x on the largest files
without moving the project to a real web server.

THE FRESHNESS CHECK IS THE POINT
--------------------------------
Serving a stale .gz means serving YESTERDAY'S DATA with a 200 and no warning,
which is worse than serving nothing. So the .gz is never trusted on its own:
every request re-stats the source JSON and compares it with what
src/web_assets.py recorded when it compressed the file. Mismatch means the
JSON has been rebuilt or edited since, and the request falls back to the
uncompressed original.

The fallback direction is deliberate. A wrong guess here makes the map SLOW,
never WRONG.

Stale hits are counted and printed on exit, because a silent fallback that
happens on every request is a build problem worth knowing about rather than a
performance mystery.

Run:  python serve.py [--port 8781] [--dir webapp]
"""
from __future__ import annotations

import argparse
import datetime
import functools
import http.server
import html
import json
import pathlib
import socketserver
import sys
import time
import uuid
import urllib.parse
import uuid

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from web_assets import is_fresh, load_manifest  # noqa: E402

COMPRESSIBLE = {".json", ".js", ".css", ".html", ".svg", ".geojson"}

# ---------------------------------------------------------------------------
# A LOCAL STAND-IN FOR api/, AND NOTHING MORE
#
# In production /api/* is served by the Static Web App's managed Functions
# against Azure Table Storage, with the caller's identity injected at the edge.
# None of that exists on a laptop, and the dialer is unusable without it -- so
# this reimplements the same four routes over a JSON file.
#
# It is a DEVELOPMENT FIXTURE. It has one hard-coded user, no authentication of
# any kind, and it is never deployed: deploy_swa.py uploads webapp/ and api/,
# and this file is neither. The response shapes match api/shared/store.js so the
# front end cannot tell the difference; if the two ever drift, the symptom is a
# feature that works locally and fails in Azure, which is why they are kept
# side by side and small.
# ---------------------------------------------------------------------------
DEV_STORE = ROOT / "data" / "dev_dialer.json"
DEV_USER = {"id": "dev-local", "name": "local developer", "dev": True}
# Must match api/log/index.js exactly; audit.py checks that it does. The last
# two are RETIRED -- no button offers them, and both servers still accept them
# so that events already in storage, and phones that have not reloaded since the
# vocabulary changed, are not refused.
DISPOSITIONS = {"connected", "attempted", "voicemail", "wrong-number",
                "received", "callback", "do-not-call", "skipped",
                "no-answer", "gatekeeper"}

# Why the call was made. Must match PURPOSES in api/log/index.js; audit.py
# checks that it does. Closed for the same reason dispositions are: it becomes
# the SUBJECT of an Act! history record, which is text the whole firm reads, so
# the client is not the authority on what may go there. Empty is valid, and is
# what every call logged before this existed carries.
PURPOSES = {"meeting", "materials", "check-in", "cold"}

# Per-rep preferences. Must match SETTING_KEYS in api/shared/store.js; audit.py
# checks that it does. Values are the maximum stored length. A CLOSED SET, and
# unknown keys are dropped rather than stored -- this is the one row read on
# every page load, and a settings row that accepts whatever the page had in
# scope becomes a junk drawer nobody dares delete from.
SETTING_KEYS = {
    "defaultScope": 24, "defaultListId": 64,
    # Who to copy on outgoing email. Mirrors api/shared/store.js; src/audit.py
    # compares the two lists, because a key the dev server drops is a preference
    # that saves in production and vanishes locally.
    "copySelf": 8, "copyInternal": 8, "copyInternalTo": 254,
    "homeLabel": 80, "homeLat": 24, "homeLon": 24,
    "emailSignature": 1500,
    "autoDialOn": 8, "autoDialDelay": 8, "autoDialAnnounce": 8,
    "fieldRadius": 8,
}

# Dispositions that write nothing to Act!, mirroring RESULTS in api/shared/act.js.
NO_ACT_WRITE = {"skipped"}


def _dev_crm_history(crd):
    """Stand-in for Act!'s own history on one contact, for local development.

    Returns None for "this advisor is not in Act!", which is a different answer
    from "Act! has nothing on them" and has to stay distinguishable -- on
    screen the two look identical and mean opposite things to a rep deciding
    whether to make a cold call.

    Read from data/dev_crm_history.json if it exists, so a developer can put a
    realistic record in front of the UI without inventing one here. Absent, no
    advisor is in Act! locally, which is also the honest default: this laptop
    has no CRM.
    """
    path = ROOT / "data" / "dev_crm_history.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if str(crd) not in data:
        return None
    return data[str(crd)]


def _dev_act_status(body, disposition):
    """What the deployed API would report -- WITHOUT touching Act!.

    Local development has no Act! credentials and must never acquire them: a
    laptop writing into the firm's CRM while someone tries a UI change is a
    category of accident worth making impossible rather than unlikely. So this
    returns "off", the same value the real path returns when ACT_SYNC is not 1,
    except for the cases that are decided before any network call and are worth
    being able to see locally.
    """
    if str(body.get("kind") or "outcome") != "outcome":
        return "not-an-outcome"
    if not disposition or disposition in NO_ACT_WRITE:
        return "no-write"
    return "off"


def _iso_ms(value: str) -> float:
    """An ISO-8601 instant as epoch milliseconds, tolerant of both the
    second-resolution stamps this shim writes and the millisecond stamps the
    browser sends."""
    try:
        return datetime.datetime.strptime(
            (value or "").replace("Z", "+0000").replace(".", ".")[:32],
            "%Y-%m-%dT%H:%M:%S.%f%z").timestamp() * 1000
    except ValueError:
        pass
    try:
        return datetime.datetime.strptime(
            (value or "").replace("Z", "+0000"),
            "%Y-%m-%dT%H:%M:%S%z").timestamp() * 1000
    except ValueError:
        return 0.0


def _dev_load() -> dict:
    try:
        return json.loads(DEV_STORE.read_text(encoding="utf-8"))
    except Exception:
        return {"events": [], "queues": {}, "dnc": {}}


def _dev_save(state: dict) -> None:
    DEV_STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DEV_STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(DEV_STORE)


class GzipHandler(http.server.SimpleHTTPRequestHandler):
    served_gz = 0
    served_raw = 0
    stale = []

    # ---- local /api/* -----------------------------------------------------
    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def _api(self, method: str) -> bool:
        """Handle /api/*; return True if the request was answered here."""
        path = self.path.split("?", 1)[0].rstrip("/")
        if not path.startswith("/api/"):
            return False
        # DECODED. Without this, `since=2026-08-13T08%3A45%3A12.926Z` arrived
        # with its colons still escaped, failed to parse as a timestamp, and
        # the cutoff silently became zero -- so a fresh cycle counted every
        # historical call as already done and opened at "3 of 3".
        query = {}
        if "?" in self.path:
            for part in self.path.split("?", 1)[1].split("&"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    query[k] = urllib.parse.unquote_plus(v)
        route = path[len("/api/"):]
        state = _dev_load()
        uid = DEV_USER["id"]

        if route == "email":
            # A complete local façade with NO Graph boundary. It exercises the
            # composer and persistence contract, but direct send is impossible.
            op = query.get("op", "catalog")
            batches = state.setdefault("email_batches", {}).setdefault(uid, {})
            templates = [
                {"id": "meeting", "name": "Meeting introduction",
                 "subject": "Time to meet — {{first_name}}",
                 "bodyText": "Hi {{first_name}},\n\nWould you have 20 minutes for a short introduction to how we run our value strategies?",
                 "defaultAttachmentIds": [], "version": 1},
                {"id": "materials", "name": "Materials follow-up",
                 "subject": "The material I mentioned",
                 "bodyText": "Hi {{first_name}},\n\nFollowing up on our conversation — I’m sending the overview we discussed.",
                 "defaultAttachmentIds": [], "version": 1},
                {"id": "check-in", "name": "Check-in", "subject": "Checking in",
                 "bodyText": "Hi {{first_name}},\n\nI wanted to see how things are going at {{company_name}}.",
                 "defaultAttachmentIds": [], "version": 1},
                {"id": "cold", "name": "Value equity introduction",
                 "subject": "Value equity SMAs — a short introduction",
                 "bodyText": "Hi {{first_name}},\n\nIf outside managers are something {{company_name}} considers, I would welcome a short conversation.",
                 "defaultAttachmentIds": [], "version": 1}]

            def render(text, recipient):
                name = str(recipient.get("name") or "").strip()
                if "," in name:
                    first = name.split(",", 1)[1].strip().split(" ")[0]
                    last = name.split(",", 1)[0].strip()
                else:
                    words = name.split()
                    first = words[0] if words else ""
                    last = words[-1] if len(words) > 1 else ""
                return (str(text or "").replace("{{first_name}}", first)
                        .replace("{{last_name}}", last)
                        .replace("{{company_name}}", str(recipient.get("firm") or "")))

            def packed(batch):
                messages = batch["messages"]
                errors, warnings, counts = [], [], {}
                seen = {}
                for message in messages:
                    seen[message["recipientEmail"]] = seen.get(message["recipientEmail"], 0) + 1
                for message in messages:
                    problems = []
                    email = message["recipientEmail"]
                    if "@" not in email or "." not in email.split("@")[-1]:
                        problems.append({"code": "invalid_recipient", "message": "Recipient email is missing or invalid."})
                    if seen[email] > 1:
                        problems.append({"code": "duplicate_recipient", "message": "This recipient appears more than once in the batch."})
                    if not message["subject"].strip():
                        problems.append({"code": "missing_subject", "message": "Subject is required."})
                    if not message["bodyText"].strip():
                        problems.append({"code": "missing_body", "message": "Body is required."})
                    if "{{" in message["subject"] + message["bodyText"]:
                        problems.append({"code": "unresolved_merge_fields", "message": "Resolve all template fields."})
                    notes = [] if message.get("reviewed") else [{"code": "not_reviewed", "message": "Final recipient preview has not been approved."}]
                    message["validation"] = {"errors": problems, "warnings": notes, "estimatedBytes": len(message["bodyText"]) + 4096}
                    errors.extend([dict(item, messageId=message["id"], recipient=email) for item in problems])
                    warnings.extend([dict(item, messageId=message["id"], recipient=email) for item in notes])
                    counts[message["state"]] = counts.get(message["state"], 0) + 1
                return {"batch": {k: v for k, v in batch.items() if k != "messages"},
                        "messages": messages, "counts": counts, "valid": not errors,
                        "errors": errors, "warnings": warnings}

            if method == "GET" and op == "catalog":
                self._json(200, {"connection": {"connected": True, "needsReconnect": False,
                    "mailbox": "local.developer@example.test", "profile": {"id": uid,
                    "displayName": "Local Developer", "mail": "local.developer@example.test",
                    "jobTitle": "Development only", "businessPhones": []}},
                    "templates": templates, "documents": [],
                    "signatureHtml": "<div><b>Local Developer</b><br>Development only<br>Graph mock — no mail can be sent</div>",
                    "policy": {"directSendAvailable": False, "killed": True,
                               "reason": "Direct sending is disabled in local development."},
                    "limits": {"directBatchMax": 250, "rollingExternalLimit": 5000,
                        "cancellationSeconds": 30, "mailboxIntervalSeconds": 5,
                        "maxMessageBytes": 20971520, "maxAttachmentBytes": 15728640,
                        "absoluteBatchStop": 15000}})
            elif method == "GET" and op == "batches":
                summaries = [{k: v for k, v in b.items() if k != "messages"} for b in batches.values()]
                summaries.sort(key=lambda b: b.get("createdUtc", ""), reverse=True)
                self._json(200, {"batches": summaries})
            elif method == "GET" and op == "batch":
                batch = batches.get(query.get("id", ""))
                self._json(200, packed(batch)) if batch else self._json(404, {"error": "Email batch not found."})
            elif method == "POST":
                body = self._read_json()
                if op == "create_batch":
                    if len(body.get("recipients") or []) >= 15000:
                        self._json(400, {"error": "15,000 recipients is a campaign-sized use case and is blocked."})
                        return True
                    selected = next((x for x in templates if x["id"] == body.get("templateId")), None)
                    if not selected:
                        self._json(400, {"error": "Choose an approved email template."})
                        return True
                    batch_id = str(uuid.uuid4())
                    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    messages = []
                    for index, raw in enumerate(body.get("recipients") or []):
                        recipient = {"name": str(raw.get("name") or ""),
                                     "email": str(raw.get("email") or "").strip().lower(),
                                     "firm": str(raw.get("firm") or raw.get("companyName") or "")}
                        text = render(selected["bodyText"], recipient)
                        messages.append({"id": str(uuid.uuid4()), "batchId": batch_id, "userId": uid,
                            "ordinal": index, "contactId": str(raw.get("contactId") or raw.get("crd") or ""),
                            "recipientName": recipient["name"], "recipientEmail": recipient["email"],
                            "companyName": recipient["firm"], "subject": render(selected["subject"], recipient),
                            "bodyText": text, "bodyHtml": "".join(f"<p>{html.escape(p)}</p>" for p in text.split("\n\n")),
                            "signatureHtml": "<div><b>Local Developer</b><br>Graph mock — no mail can be sent</div>",
                            "state": "editing", "subjectOverridden": False, "bodyOverridden": False,
                            "baseRevision": 1, "reviewed": False, "attachments": [], "validation": {}})
                    batch = {"id": batch_id, "userId": uid, "userName": DEV_USER["name"], "status": "editing",
                        "mode": "", "name": f'{selected["name"]} — local mock', "templateId": selected["id"],
                        "templateName": selected["name"], "commonSubject": selected["subject"],
                        "commonBodyText": selected["bodyText"], "commonRevision": 1, "attachmentIds": [],
                        "attachmentSummary": [], "recipientCount": len(messages), "externalCount": len(messages),
                        "warningLevel": "normal", "warningMessage": "", "signatureHtml": messages[0]["signatureHtml"] if messages else "",
                        "graphMailboxId": uid, "graphMailbox": "local.developer@example.test", "reviewedUtc": "",
                        "approvedUtc": "", "sendNotBeforeUtc": "", "pausedUtc": "", "canceledUtc": "",
                        "createdUtc": now, "updatedUtc": now, "messages": messages}
                    batches[batch_id] = batch
                    _dev_save(state)
                    self._json(201, packed(batch))
                else:
                    batch = batches.get(str(body.get("batchId") or ""))
                    if not batch:
                        self._json(404, {"error": "Email batch not found."})
                        return True
                    if op == "update_common":
                        batch["commonSubject"] = str(body.get("subject") or "")[:500]
                        batch["commonBodyText"] = str(body.get("bodyText") or "")[:50000]
                        batch["commonRevision"] += 1
                        for message in batch["messages"]:
                            recipient = {"name": message["recipientName"], "firm": message["companyName"]}
                            if not message["subjectOverridden"]:
                                message["subject"] = render(batch["commonSubject"], recipient)
                            if not message["bodyOverridden"]:
                                message["bodyText"] = render(batch["commonBodyText"], recipient)
                                message["bodyHtml"] = "".join(f"<p>{html.escape(p)}</p>" for p in message["bodyText"].split("\n\n"))
                            message["reviewed"] = False
                    elif op == "update_message":
                        message = next((m for m in batch["messages"] if m["id"] == body.get("messageId")), None)
                        if not message:
                            self._json(404, {"error": "Email message not found."})
                            return True
                        if "subject" in body:
                            message["subject"], message["subjectOverridden"] = str(body.get("subject") or "")[:500], True
                        if "bodyText" in body:
                            message["bodyText"], message["bodyOverridden"] = str(body.get("bodyText") or "")[:50000], True
                            message["bodyHtml"] = "".join(f"<p>{html.escape(p)}</p>" for p in message["bodyText"].split("\n\n"))
                        if body.get("resetSubject") or body.get("resetBody"):
                            recipient = {"name": message["recipientName"], "firm": message["companyName"]}
                            if body.get("resetSubject"):
                                message["subject"], message["subjectOverridden"] = render(batch["commonSubject"], recipient), False
                            if body.get("resetBody"):
                                message["bodyText"], message["bodyOverridden"] = render(batch["commonBodyText"], recipient), False
                                message["bodyHtml"] = "".join(f"<p>{html.escape(p)}</p>" for p in message["bodyText"].split("\n\n"))
                        message["reviewed"] = bool(body.get("reviewed"))
                    elif op == "approve":
                        if body.get("mode") == "send":
                            self._json(403, {"error": "Direct sending is disabled in local development."})
                            return True
                        for message in batch["messages"]:
                            message["reviewed"] = bool(body.get("reviewed"))
                        result = packed(batch)
                        if result["errors"]:
                            self._json(400, {"error": f'The batch has {len(result["errors"])} validation problem(s).'})
                            return True
                        batch["mode"], batch["status"] = "drafts", "drafts_ready"
                        for message in batch["messages"]:
                            message["state"] = "draft_ready"
                    elif op == "cancel":
                        batch["status"] = "canceled"
                        for message in batch["messages"]:
                            if message["state"] != "sent":
                                message["state"] = "canceled"
                    elif op not in {"validate"}:
                        self._json(400, {"error": "Unsupported local email operation."})
                        return True
                    batch["updatedUtc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    _dev_save(state)
                    self._json(200, packed(batch))
            else:
                self._json(404, {"error": f"No local email handler for {method}."})
        elif route == "health":
            self._json(200, {"user": DEV_USER, "configured": True,
                             "storageOk": True,
                             "detail": "local development store (serve.py)"})
        elif route == "queue" and method == "GET":
            lists = state["queues"].setdefault(uid, {})
            lid = query.get("id", "")
            if lid:
                q = lists.get(lid) or {
                    "id": lid, "name": "Call list" if lid == "current" else lid,
                    "items": [], "count": 0, "cursor": 0, "cycle": 1,
                    "cycleStartedUtc": "", "updatedUtc": "", "etag": ""}
                self._json(200, q)
            else:
                summaries = [{k: v for k, v in q.items() if k != "items"}
                             for q in lists.values()]
                summaries.sort(key=lambda s: s.get("updatedUtc", ""), reverse=True)
                self._json(200, {"lists": summaries, "defaultId": "current"})
        elif route == "queue" and method == "DELETE":
            lid = query.get("id", "")
            if not lid:
                self._json(400, {"error": "id is required to delete a list."})
                return True
            state["queues"].setdefault(uid, {}).pop(lid, None)
            _dev_save(state)
            self._json(200, {"id": lid, "deleted": True})
        elif route == "queue" and method == "PUT":
            body = self._read_json()
            keep = ("crd", "name", "firm", "phone", "phoneKind", "city",
                    "state", "email")
            items = [{k: str(it.get(k) or "") for k in keep}
                     for it in (body.get("items") or []) if it.get("crd")][:250]
            cursor = max(0, min(int(body.get("cursor") or 0), len(items)))
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            lid = str(body.get("id") or "current").lower()
            lid = "".join(ch for ch in lid if ch.isalnum() or ch == "-")[:40] or "current"
            # Optimistic concurrency, imitating Azure Table's If-Match. A stale
            # etag is refused with 409 rather than silently overwriting the row
            # -- the behaviour the browser has to be developed against, because
            # a conflict that only ever appears in production is a conflict
            # nobody has ever seen handled.
            sent = str(body.get("etag") or "")
            have = str((lists_put := state["queues"].setdefault(uid, {}))
                       .get(lid, {}).get("etag") or "")
            if sent and sent != have:
                self._json(409, {"error": "This list changed on another device. "
                                          "Reload to pick up the newer version "
                                          "— nothing was overwritten."})
                return True
            q = {"id": lid,
                 "name": str(body.get("name") or
                             ("Call list" if lid == "current" else lid))[:60],
                 "items": items, "count": len(items), "cursor": cursor,
                 "cycle": max(1, int(body.get("cycle") or 1)),
                 "cycleStartedUtc": body.get("cycleStartedUtc") or now,
                 "updatedUtc": now, "dropped": 0, "max": 250,
                 "etag": uuid.uuid4().hex}
            lists_put[lid] = q
            _dev_save(state)
            self._json(200, q)
        elif route == "settings" and method == "GET":
            self._json(200, {"settings": state.setdefault("settings", {}).get(uid, {})})
        elif route == "settings" and method == "PUT":
            body = self._read_json()
            cur = state.setdefault("settings", {}).setdefault(uid, {})
            # A MERGE of the keys sent, and unknown keys are dropped -- the same
            # two rules as api/shared/store.js. The field view saves a radius
            # and the desktop saves a scope, and a replace would mean whichever
            # page saved last silently cleared the other's preference.
            for k, mx in SETTING_KEYS.items():
                if k in body:
                    cur[k] = str(body.get(k) if body.get(k) is not None else "")[:mx]
            _dev_save(state)
            self._json(200, {"ok": True, "settings": cur,
                             "accepts": list(SETTING_KEYS)})
        elif route == "dnc" and method == "GET":
            entries = list(state["dnc"].values())
            self._json(200, {"entries": entries, "count": len(entries)})
        elif route == "log" and method == "GET":
            crd = query.get("crd", "")
            since = query.get("since", "")
            limit = min(int(query.get("limit", 25) or 25), 1000 if since else 100)
            events = state["events"]
            if crd:
                events = [e for e in events if e.get("crd") == crd]
            if since:
                # PARSED, not string-compared. This shim writes timestamps to
                # the second ("...:45Z") while the client sends a cycle start
                # with milliseconds ("...:45.123Z"), and lexically "Z" sorts
                # after "." -- so every event from the same second counted as
                # being AFTER the cutoff. A brand new cycle opened reading
                # "3 of 3 done".
                #
                # The Azure store compares epoch milliseconds and never had the
                # bug, which is exactly what makes this kind of divergence
                # dangerous: correct in production, wrong on a laptop, and the
                # laptop is where the behaviour gets judged.
                cut = _iso_ms(since)
                if not cut:
                    # Refuse rather than return everything. An unparseable
                    # cutoff silently means "no filter", which reports a list
                    # as fully worked when none of it has been -- the failure
                    # direction that looks like success.
                    self._json(400, {"error": f"Unparseable since: {since!r}"})
                    return True
                events = [e for e in events if _iso_ms(e.get("at", "")) >= cut]
            mine = [dict(e, source="app") for e in reversed(events)][:limit]
            if not (crd and query.get("act") == "1"):
                self._json(200, {"events": mine})
                return True
            # THE CRM HALF, faked. There are no Act! credentials on a laptop and
            # there must never be any -- but the merge rule is the part most
            # likely to be wrong, and a rule that can only be exercised in
            # production is a rule nobody has watched work. So this shim serves
            # a fixture and applies the REAL de-duplication: local rows whose
            # actStatus is "written" are dropped, because Act! is returning
            # them; everything else is ours alone and stays.
            crm_rows = _dev_crm_history(crd)
            have_crm = crm_rows is not None
            kept = [e for e in mine if e.get("act") != "written"] if have_crm else mine
            merged = kept + [
                {"crd": crd, "at": h["at"], "who": h["who"], "kind": "crm",
                 "disposition": "", "purpose": "", "note": h.get("details", ""),
                 "name": "", "act": "", "source": "act",
                 "subject": h.get("subject", ""), "type": h.get("type", "")}
                for h in (crm_rows or [])]
            merged.sort(key=lambda e: str(e.get("at") or ""), reverse=True)
            self._json(200, {
                "events": merged,
                "crm": {"ok": have_crm,
                        "why": "" if have_crm else "no-contact",
                        "count": len(crm_rows or []),
                        "hidden": len(mine) - len(kept) if have_crm else 0}})
        elif route == "log" and method == "POST":
            body = self._read_json()
            if not body.get("crd"):
                self._json(400, {"error": "crd is required."})
                return True
            disposition = str(body.get("disposition") or "").lower()
            if disposition and disposition not in DISPOSITIONS:
                self._json(400, {"error": f'Unknown disposition "{disposition}".'})
                return True
            purpose = str(body.get("purpose") or "").lower()
            if purpose and purpose not in PURPOSES:
                self._json(400, {"error": f'Unknown purpose "{purpose}".'})
                return True
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            ev = {"id": uuid.uuid4().hex[:12], "at": now, "who": DEV_USER["name"],
                  "crd": str(body.get("crd")), "name": body.get("name", ""),
                  "firm": body.get("firm", ""), "phone": body.get("phone", ""),
                  "kind": body.get("kind", "outcome"), "disposition": disposition,
                  "purpose": purpose,
                  "note": str(body.get("note") or "")[:4000],
                  "sessionId": body.get("sessionId", ""),
                  # The dev fixture NEVER writes to the real CRM. It reports the
                  # status the deployed API would report for the same input, so
                  # the "no-contact" and "no-write" paths can be exercised on a
                  # laptop -- but a local test must never put a row in the firm's
                  # Act! database, so there is no code path here that could.
                  "act": _dev_act_status(body, disposition)}
            state["events"].append(ev)
            dnc = None
            if disposition == "do-not-call":
                # Same field names as api/shared/store.js listDnc/addDnc. These
                # two implementations drifting is the one way this fixture can
                # do real harm, so the shapes are kept literally identical.
                dnc = {"crd": ev["crd"], "by": DEV_USER["name"], "at": now,
                       "reason": ev["note"]}
                state["dnc"][ev["crd"]] = dnc
            _dev_save(state)
            self._json(200, {"ok": True, "id": ev["id"], "at": now,
                             "dnc": dnc, "by": DEV_USER["name"],
                             "act": ev["act"]})
        else:
            self._json(404, {"error": f"No local handler for {method} {path}"})
        return True

    def do_GET(self):
        if self._api("GET"):
            return
        return super().do_GET()

    def do_POST(self):
        if not self._api("POST"):
            self.send_error(405, "POST is only supported on /api/*")

    def do_PUT(self):
        if not self._api("PUT"):
            self.send_error(405, "PUT is only supported on /api/*")

    def do_DELETE(self):
        if not self._api("DELETE"):
            self.send_error(405, "DELETE is only supported on /api/*")

    def _accepts_gzip(self) -> bool:
        return "gzip" in self.headers.get("Accept-Encoding", "").lower()

    def send_head(self):
        path = pathlib.Path(self.translate_path(self.path))
        if (path.is_file() and path.suffix in COMPRESSIBLE and self._accepts_gzip()):
            gz = path.with_suffix(path.suffix + ".gz")
            if gz.exists():
                # Re-stat on EVERY request. The manifest is cheap to read and
                # the whole guarantee rests on this comparison being current,
                # not on something checked once at startup.
                if is_fresh(path, load_manifest()):
                    return self._send_gz(path, gz)
                if path.name not in GzipHandler.stale:
                    GzipHandler.stale.append(path.name)
                    print(f"  [!] {path.name}: .gz is stale, serving the raw JSON. "
                          f"Run `python src/web_assets.py` to refresh it.")
        GzipHandler.served_raw += 1
        return super().send_head()

    def _send_gz(self, path: pathlib.Path, gz: pathlib.Path):
        try:
            fh = open(gz, "rb")
        except OSError:
            return super().send_head()
        GzipHandler.served_gz += 1
        self.send_response(200)
        self.send_header("Content-type", self.guess_type(str(path)))
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(gz.stat().st_size))
        # Without Vary, a shared cache can hand a gzipped body to a client that
        # did not ask for one.
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Last-Modified", self.date_time_string(int(path.stat().st_mtime)))
        self.end_headers()
        return fh

    def log_message(self, fmt, *args):
        pass                      # one line per asset is noise; the summary is enough


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8781)
    ap.add_argument("--dir", default="webapp")
    # LOOPBACK BY DEFAULT. This server has no authentication -- it hard-codes a
    # single user -- and it serves the whole contact file plus writable /api
    # endpoints. Binding "" put all of that on every interface, so anyone on the
    # office network could read the contact data and write to the call log.
    # Exposing it is now a deliberate, typed-out act.
    ap.add_argument("--host", default="127.0.0.1",
                    help="interface to bind (default 127.0.0.1; "
                         "use 0.0.0.0 to expose on the LAN — no auth, so don't)")
    args = ap.parse_args()

    directory = str(ROOT / args.dir)
    handler = functools.partial(GzipHandler, directory=directory)
    # THREADED, and it has to be. A single-threaded server was fine while this
    # only handed out files, because the browser finished one request before
    # asking for the next. Now the page fetches /api/queue and /api/dnc while a
    # tile request is still in flight, and one connection waiting on another on
    # the same thread is a deadlock -- which presents as the whole app hanging
    # rather than as anything resembling a server problem.
    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with Server((args.host, args.port), handler) as httpd:
        print(f"[*] serving {directory} on http://localhost:{args.port} "
              f"(bound to {args.host})")
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            print(f"[!] bound to {args.host} — this server has NO AUTH and "
                  f"serves contact data plus writable /api endpoints")
        print(f"[*] pre-compressed assets are verified against "
              f"src/web_assets.py's manifest on every request")
        print(f"[*] /api/* answered locally from {DEV_STORE.name} as "
              f"'{DEV_USER['name']}' -- DEV FIXTURE, no auth, never deployed")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print(f"\n[*] {GzipHandler.served_gz} gzipped, "
                  f"{GzipHandler.served_raw} uncompressed")
            if GzipHandler.stale:
                print(f"    [!] stale .gz seen for: {', '.join(GzipHandler.stale)}")


if __name__ == "__main__":
    main()
