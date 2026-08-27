"""A minimal Act! Web API client, plus a discovery mode that asks the instance what it has.

WHY THIS SHAPE
--------------
The endpoint is https://apius.act.com/act.web.api -- established by probing, see
src/act_probe.py. Everything else about this API has to be discovered rather
than assumed: the Swagger document 403s without a bearer token, so no published
page tells you what your instance exposes. `--discover` fetches it WITH the
token and prints the real paths and the real field names.

That matters more than it sounds. The reason to touch Act! at all is that it
holds 35,165 of the 127,445 advisors we have, and the question is which fields
carry the email addresses and the CRD -- guessed field names produce empty
columns rather than errors, which is the failure mode this project keeps hitting.

TOKENS ARE NOT WRITTEN TO DISK. They live in this process and nowhere else. A
JWT for the CRM in a file beside the code is a credential someone will paste
into a chat window or commit by accident; re-authorizing costs one request.

RATE LIMITS ARE OBSERVED, NOT DISCOVERED THE HARD WAY. The API reports
X-RateLimit-Remaining on every response and this client slows down as that
approaches zero, because the alternative -- being cut off in the middle of a
paged read of 35,000 contacts -- means starting again.

Usage:
    $env:ACT_PASSWORD = 'your password in SINGLE quotes'
    python src/act_client.py --user bladyman@eicatlanta.com --db EQUITYINVESTMENT --discover
    python src/act_client.py --user ... --db ... --sample contacts
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from contact_provenance import (ProvenanceError, atomic_create_json,
                                utc_pull_id)

BASE = "https://apius.act.com/act.web.api"
TIMEOUT = 60


class ActError(RuntimeError):
    pass


@dataclass(frozen=True)
class CensusResult:
    total: int
    complete: bool
    stop_reason: str
    output: str = ""


class Act:
    def __init__(self, user: str, password: str, database: str, base: str = BASE):
        self.user, self.password, self.database = user, password, database
        self.base = base.rstrip("/")
        self._token = ""
        self.limit_seen = None

    # ---- auth ------------------------------------------------------------
    def authorize(self) -> str:
        """Exchange basic credentials for a bearer token.

        The 200 body is a BARE JWT in text/html -- not JSON, not quoted. Calling
        .json() on it raises, which is a confusing way to discover that the
        request actually succeeded.
        """
        cred = base64.b64encode(
            f"{self.user}:{self.password}".encode("utf-8")).decode("ascii")
        r = requests.get(
            f"{self.base}/authorize",
            headers={"Authorization": f"Basic {cred}",
                     "Act-Database-Name": self.database},
            timeout=TIMEOUT)
        if r.status_code != 200 or not r.text.strip():
            raise ActError(
                f"authorize failed: {r.status_code} "
                f"{r.headers.get('Content-Type', '')} {r.text[:200]!r}")
        self._token = r.text.strip()
        return self._token

    def token(self) -> str:
        return self._token or self.authorize()

    # ---- requests --------------------------------------------------------
    def get(self, path: str, **params):
        """One GET, with a single automatic re-auth and rate-limit courtesy."""
        url = f"{self.base}/{path.lstrip('/')}"
        for attempt in (1, 2):
            r = requests.get(url, params=params or None, timeout=TIMEOUT,
                             headers={"Authorization": f"Bearer {self.token()}"})
            # An expired token looks like a permissions problem until you retry.
            if r.status_code == 401 and attempt == 1:
                self._token = ""
                continue
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 60))
                print(f"    [rate limited] sleeping {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            self._note_limits(r)
            if not r.ok:
                raise ActError(f"GET {url} -> {r.status_code} {r.text[:300]}")
            if not r.text.strip():
                return None
            try:
                return r.json()
            except ValueError:
                return r.text
        raise ActError(f"GET {url} failed after a re-authorization")

    def post(self, path: str, body=None, allow_error: bool = False, **params):
        """One POST, with the same re-auth and rate-limit courtesy as get().

        Used only for READ-shaped endpoints -- the dynamic-list previews, which
        query records by criteria and write nothing. Nothing here should POST to
        a resource that creates or modifies an Act! record; those go through the
        narrow, audited path in api/shared/act.js.

        allow_error returns (status, payload) rather than raising. When an
        endpoint's request shape is undocumented the 400 body IS the
        documentation -- Act! names the property it could not bind -- and an
        exception throws that away.
        """
        url = f"{self.base}/{path.lstrip('/')}"
        for attempt in (1, 2):
            r = requests.post(url, params=params or None, json=body, timeout=TIMEOUT,
                              headers={"Authorization": f"Bearer {self.token()}",
                                       "Content-Type": "application/json"})
            if r.status_code == 401 and attempt == 1:
                self._token = ""
                continue
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 60))
                print(f"    [rate limited] sleeping {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            self._note_limits(r)
            payload = None
            if r.text.strip():
                try:
                    payload = r.json()
                except ValueError:
                    payload = r.text
            if allow_error:
                return r.status_code, payload
            if not r.ok:
                raise ActError(f"POST {url} -> {r.status_code} {r.text[:300]}")
            return payload
        raise ActError(f"POST {url} failed after a re-authorization")

    def _note_limits(self, r):
        rem = r.headers.get("X-RateLimit-Remaining")
        if rem is None:
            return
        self.limit_seen = (r.headers.get("X-RateLimit-Limit"), rem)
        # Leave headroom rather than sprinting into the wall. A paged read of
        # tens of thousands of contacts is long enough that being cut off
        # matters more than finishing a minute sooner.
        if rem.isdigit() and int(rem) <= 3:
            print(f"    [{rem} calls left this minute] pausing 20s", file=sys.stderr)
            time.sleep(20)


# ---- discovery -----------------------------------------------------------
def discover(act: Act) -> None:
    """Ask the instance what it exposes, instead of guessing."""
    sysinfo = act.get("api/system")
    print("instance")
    for k in ("apiVersion", "sdkVersion", "databaseName", "update"):
        if isinstance(sysinfo, dict) and k in sysinfo:
            print(f"  {k:<12} {sysinfo[k]}")
    if act.limit_seen:
        print(f"  rate limit   {act.limit_seen[1]} of {act.limit_seen[0]} left this minute")
    print()

    spec = None
    for p in ("swagger/v1/swagger.json", "swagger/docs/v1"):
        try:
            spec = act.get(p)
            if isinstance(spec, dict) and spec.get("paths"):
                print(f"swagger from /{p}")
                break
        except ActError:
            continue

    if not isinstance(spec, dict) or not spec.get("paths"):
        print("No swagger document was readable. Falling back to the documented "
              "resource names; check each against --sample.")
        for name in ("contacts", "companies", "groups", "opportunities", "activities"):
            try:
                act.get(f"api/{name}", **{"$top": 1})
                print(f"  api/{name:<14} responds")
            except ActError as e:
                print(f"  api/{name:<14} {str(e)[:70]}")
        return

    paths = sorted(spec["paths"])
    print(f"  {len(paths)} paths\n")
    groups = {}
    for p in paths:
        head = p.strip("/").split("/")[1] if p.strip("/").count("/") else p.strip("/")
        groups.setdefault(head, []).append(p)
    for head in sorted(groups):
        print(f"  {head:<18} {len(groups[head])} paths   e.g. {groups[head][0]}")


def metadata_grep(xml: str, term: str) -> None:
    """Search an OData CSDL document for a property, and show its entity.

    Where Swagger is unavailable this is the only machine-readable description
    of the model, and it answers the question that matters here: does History
    expose a writable record-manager property, or is the one we have been
    sending simply not part of the contract?
    """
    import re                                              # noqa: PLC0415
    t = term.lower()
    entity, hits = "", 0
    for line in xml.splitlines():
        m = re.search(r'<(?:EntityType|ComplexType)[^>]*Name="([^"]+)"', line)
        if m:
            entity = m.group(1)
        for pm in re.finditer(r'<(?:Property|NavigationProperty)[^>]*Name="([^"]+)"'
                              r'(?:[^>]*Type="([^"]*)")?', line):
            name, typ = pm.group(1), pm.group(2) or ""
            if t in name.lower():
                hits += 1
                print(f"   {entity:<28} {name:<30} {typ}")
    if not hits:
        print(f"   no property matching {term!r} anywhere in the model")


def spec_grep(act: Act, term: str, full: bool = False) -> None:
    """Search the Swagger document itself -- paths, parameters AND models.

    WHY THIS IS NOT `--discover`
    ----------------------------
    `discover` fetches the same spec and prints how many paths sit under each
    resource. That is a map of the front door, and it got mistaken for a search:
    "we checked 254 endpoints for an attribution parameter" was really "we
    listed 254 path names". A field like `recordManagerID` never appears in a
    path -- it lives in a model definition three levels down -- so the thing we
    reported as ruled out had never actually been looked at.

    This reads the whole document: every path, its parameters, and the
    properties of every definition. If a capability is documented, it is
    findable from here.

    Usage:
        python src/act_client.py --user ... --db ... --spec-grep recordManager
        python src/act_client.py --user ... --db ... --spec-grep clear --full
    """
    spec = None
    # EVERY CANDIDATE, WITH ITS FAILURE PRINTED. The first version tried two
    # paths and, on failure, said "no readable swagger document" -- which is how
    # a claim that 254 endpoints had been searched survived without anyone
    # noticing the document had never been fetched. A silent fallback turns a
    # search into an assumption.
    #
    # $metadata is included because this is an OData-flavoured API: where Swagger
    # is absent the CSDL still describes every entity and property, which is
    # precisely what a question about a field needs.
    candidates = [
        "swagger/v1/swagger.json", "swagger/docs/v1", "swagger/docs/V1",
        "api/swagger", "api/swagger/docs/v1", "swagger.json",
        "api/$metadata", "$metadata", "api/metadata",
    ]
    print("[*] looking for a machine-readable spec:")
    for p in candidates:
        try:
            got = act.get(p)
        except ActError as e:
            print(f"      /{p:<24} {str(e).split('->')[-1].strip()[:58]}")
            continue
        if isinstance(got, dict) and got.get("paths"):
            print(f"      /{p:<24} OK — swagger, {len(got['paths'])} paths")
            spec = got
            break
        if isinstance(got, str) and "<edmx" in got.lower():
            print(f"      /{p:<24} OK — OData $metadata, {len(got):,} bytes\n")
            return metadata_grep(got, term)
        print(f"      /{p:<24} responded, but is not a spec ({type(got).__name__})")

    if not isinstance(spec, dict) or not spec.get("paths"):
        print("\n[!] No spec is readable from this instance, so the API surface "
              "cannot be\n    enumerated from documentation. Anything asserted "
              "about what this API does\n    or does not support has to come "
              "from a live request, not from a document.")
        return

    t = term.lower()
    paths = spec.get("paths") or {}
    defs = (spec.get("definitions")
            or (spec.get("components") or {}).get("schemas") or {})
    print(f"[*] {len(paths)} paths, {len(defs)} model definitions; "
          f"searching for {term!r}\n")

    print("PATHS")
    hits = 0
    for path, ops in sorted(paths.items()):
        if t not in json.dumps({path: ops}).lower():
            continue
        hits += 1
        methods = ",".join(sorted(m.upper() for m in ops
                                  if m.lower() in ("get", "post", "put", "patch", "delete")))
        print(f"   {methods:<24} {path}")
        if full:
            for m, op in ops.items():
                for prm in (op.get("parameters") or []):
                    print(f"      {m.upper():<7} param {prm.get('name')} "
                          f"(in {prm.get('in')})")
    if not hits:
        print("   (none)")

    print("\nMODEL PROPERTIES")
    found = 0
    for name, d in sorted(defs.items()):
        props = (d or {}).get("properties") or {}
        match = [k for k in props if t in k.lower()]
        if not match and t not in name.lower():
            continue
        found += 1
        print(f"   {name}")
        for k in (sorted(props) if (full or t in name.lower()) else match):
            p = props[k] or {}
            ro = "   read-only" if p.get("readOnly") else ""
            print(f"      {k:<30} {p.get('type') or p.get('$ref') or ''}{ro}")
    if not found:
        print("   (none)")


def sample(act: Act, resource: str) -> None:
    """One record, so the FIELD NAMES are facts rather than assumptions."""
    data = act.get(f"api/{resource}", **{"$top": 1})
    rows = data if isinstance(data, list) else (data or {}).get("value") or []
    if not rows:
        print(f"api/{resource} returned no rows.")
        return
    row = rows[0]
    print(f"api/{resource}: {len(row)} fields on the first record\n")
    for k in sorted(row):
        v = row[k]
        shown = json.dumps(v)[:60] if not isinstance(v, str) else repr(v[:60])
        print(f"  {k:<32} {shown}")
    print("\nFields worth locating before any bulk read: the email address, the "
          "record id, and anything holding a CRD or an SEC identifier.")


def raw(act: Act, resource: str, top: int) -> None:
    """Full untruncated JSON. The nested objects are where the answers hide.

    customFields carries Act!'s user-defined fields under internal names
    (bname1, bcode6, bcount3...), so the label a user sees in the Act! UI is NOT
    the key returned here. "Total Assets" -- which our contact build already
    reads from the Excel export -- is one of these, and which one can only be
    learned by looking.
    """
    data = act.get(f"api/{resource}", **{"$top": top})
    rows = data if isinstance(data, list) else (data or {}).get("value") or []
    print(json.dumps(rows, indent=2, default=str))


VOCAB_CAP = 200


def census(act: Act, resource: str, page: int, cap: int, save_to=None) -> CensusResult:
    """How often is each field actually populated, and with what values?

    INVENTORY BEFORE ASSUMING -- the same rule src/inventory.py applies to the
    SEC feeds. This was written to find an unused custom field to hold a CRD;
    that question is now moot, because a NEW field can be added instead. Four
    better reasons remain, and each is a number we do not have today:

      * mobilePhone coverage. load_crm() hardcodes mobile to "" for all 47,426
        CRM rows, so whatever is in there is being discarded.
      * aemOptOut / aemBounceBack counts. This is the honest answer to "what
        does an Act! blast actually reach", which no amount of contact-count
        arithmetic can substitute for.
      * contactType and isUser. If some of the 12,261 unmatched contacts are
        staff or non-advisor record types, they can be excluded by RULE rather
        than by manual review.
      * acv_total_assets / lcv_total_assets coverage -- per-strategy assets the
        Excel export never carried.

    It also reports the VALUE VOCABULARY of low-cardinality fields, because two
    of them are already known to disagree with their own names: tier__a_b_c
    holds "LPL1" as well as "B", and email__y_n holds "2" and "P".
    """
    import collections                                  # noqa: PLC0415

    if page <= 0 or cap <= 0:
        raise ValueError("page and cap must both be positive")
    if save_to is not None:
        save_to = Path(save_to)
        if save_to.exists():
            raise ProvenanceError(f"immutable artifact already exists: {save_to}")
    filled = collections.Counter()
    values = collections.defaultdict(collections.Counter)
    capped = set()
    kept = []                       # the records themselves, if --save
    seen_ids = set()
    total = 0
    skip = 0
    complete = False
    stop_reason = "cap_reached"

    while total < cap:
        requested = min(page, cap - total)
        rows = act.get(f"api/{resource}", **{"$top": requested, "$skip": skip})
        rows = rows if isinstance(rows, list) else (rows or {}).get("value") or []
        if not rows:
            complete, stop_reason = True, "empty_page"
            break
        # PAGING IS VERIFIED, NOT ASSUMED. If this instance ignores $skip, every
        # request returns page one -- and the census would report 50,000 records
        # that are the same 500 counted a hundred times. Every percentage in the
        # output would be plausible and wrong. So: if a page contributes no ids
        # we have not already seen, paging is not working and we stop and say so
        # rather than producing a confident answer about nothing.
        if any(not isinstance(r, dict) or not str(r.get("id") or "").strip()
               for r in rows):
            raise ActError(f"api/{resource} returned a row without an Act id")
        fresh = [r for r in rows if r.get("id") not in seen_ids]
        if not fresh:
            print(f"\n[!] PAGING IS NOT WORKING. Page at $skip={skip} returned "
                  f"{len(rows)} records, none of them new.\n"
                  f"    This instance appears to ignore $skip, so only the first "
                  f"{len(seen_ids):,} records can be read this way.\n"
                  f"    Reporting on those alone -- the percentages below cover "
                  f"{len(seen_ids):,} contacts, NOT the whole database.",
                  file=sys.stderr)
            stop_reason = "paging_repeated"
            break
        if len(fresh) < len(rows):
            print(f"    [{len(rows) - len(fresh)} repeats at $skip={skip}]",
                  file=sys.stderr)
            stop_reason = "duplicate_ids"
            break
        for r in fresh:
            seen_ids.add(r.get("id"))
            total += 1
            if save_to is not None:
                kept.append(r)
            flat = {}
            for k, v in r.items():
                if k == "customFields":
                    for ck, cv in (v or {}).items():
                        flat[f"custom.{ck}"] = cv
                elif isinstance(v, dict):
                    # businessAddress and homeAddress are objects that are
                    # ALWAYS present and usually empty. Counting the object as
                    # "populated" reported 100% coverage for addresses that hold
                    # nothing but nulls. Flatten so each part is counted.
                    for sk, sv in v.items():
                        flat[f"{k}.{sk}"] = sv
                else:
                    flat[k] = v
            for k, v in flat.items():
                if v is None or v == "" or v == {}:
                    continue
                # BOOLEANS ARE COUNTED BY TRUTH, NOT BY PRESENCE. `False` is
                # not None and not "", so it used to count as populated -- and
                # the census reported that 100% of contacts were opted out and
                # bounced when every single value was False. A field that is
                # always present and usually False must report how often it is
                # TRUE or it says nothing at all.
                if isinstance(v, bool):
                    if v:
                        filled[k] += 1
                    values[k][str(v)] += 1
                    continue
                filled[k] += 1
                if isinstance(v, (str, int, float)):
                    s = str(v)[:40]
                    # Capped so a 47,000-value email column does not build a
                    # 47,000-entry counter, but the cap is RECORDED rather than
                    # silent: a field that hits it is high-cardinality, which is
                    # itself the finding. Fields that stay under it get their
                    # complete vocabulary reported.
                    if len(values[k]) < VOCAB_CAP or s in values[k]:
                        values[k][s] += 1
                    else:
                        capped.add(k)
        skip += len(rows)
        print(f"    ...{total:,} records", file=sys.stderr)
        if len(rows) < requested:
            complete, stop_reason = True, "short_page"
            break

    if not total:
        print("No records read.")
        result = CensusResult(total=0, complete=complete,
                              stop_reason=stop_reason)
        if save_to is not None:
            if not complete:
                raise ActError(f"refusing to save incomplete {resource} census "
                               f"({stop_reason})")
            atomic_create_json(save_to, [], pretty=True)
            return CensusResult(0, True, stop_reason, str(save_to))
        return result

    print(f"\n{total:,} distinct {resource} examined "
          f"({len(seen_ids):,} distinct ids)\n")

    # The four numbers this was run for, stated plainly rather than left to be
    # found among two hundred rows of field counts.
    print("THE NUMBERS THAT DECIDE SOMETHING")
    def pct(k, label):
        n = filled.get(k, 0)
        print(f"  {n:>7,}  {n / total:>6.1%}  {label}")
    pct("mobilePhone", "have a mobile number (currently DISCARDED by load_crm)")
    pct("altEmailAddress", "have an alternate email")
    pct("personalEmailAddress", "have a personal email")
    pct("emailAddress", "have a business email")
    pct("custom.acv_total_assets", "have All-Cap Value assets recorded")
    pct("custom.lcv_total_assets", "have Large-Cap Value assets recorded")
    pct("custom.has_clients___eic", "have the has-clients-with-EIC flag set")
    pct("custom.bname1", "have at least one account recorded (bname1)")
    # These three are booleans and are counted by TRUTH -- see the loop above.
    pct("aemOptOut", "are TRUE for aemOptOut (opted out of Act! marketing)")
    pct("aemBounceBack", "are TRUE for aemBounceBack (have bounced)")
    pct("isUser", "are TRUE for isUser (EIC staff, not prospects)")
    print()
    print("  NOTE: these are the first records the API returned, in its own")
    print("  order -- not a random sample. Percentages are indicative until the")
    print("  full read is done without --cap.")
    print()

    print("POPULATED FIELDS")
    for k, n in sorted(filled.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>7,}  {n / total:>6.1%}  {k}")

    # A CODED field is one with a small closed vocabulary -- exactly the kind
    # whose meaning has to be asked of a person, and exactly the kind the old
    # 12-value ceiling hid. tier__a_b_c is the reason this changed: it has more
    # than twelve values and so printed nothing at all, which read as "nothing
    # to see" rather than "too much to see".
    print("\nVALUE VOCABULARIES")
    print("  Complete lists for fields with a closed vocabulary; a sample for")
    print("  the rest. These are the fields whose codes someone has to explain.")
    for k in sorted(values):
        if k not in filled:
            continue
        n_distinct = len(values[k])
        if k in capped:
            top = ", ".join(f"{v}({n})" for v, n in values[k].most_common(5))
            print(f"  {k}  [high cardinality, {VOCAB_CAP}+ distinct]\n      {top}, ...")
        elif n_distinct <= 30:
            vocab = ", ".join(f"{v}({n})" for v, n in values[k].most_common(30))
            print(f"  {k}  [{n_distinct} distinct]\n      {vocab}")
        else:
            top = ", ".join(f"{v}({n})" for v, n in values[k].most_common(10))
            print(f"  {k}  [{n_distinct} distinct, top 10]\n      {top}")

    if save_to is not None:
        if not complete:
            raise ActError(f"refusing to save incomplete {resource} census "
                           f"({stop_reason}); raise --cap and retry")
        atomic_create_json(save_to, kept, pretty=True)
        print(f"\n[*] wrote {len(kept):,} records to {save_to}")
        print("    This is the CRM export. data/ is gitignored -- it stays local,")
        print("    like the CRM_Contacts_*.xlsx it replaces.")

    write_reports(resource, total, filled, values, capped)
    return CensusResult(total=total, complete=complete,
                        stop_reason=stop_reason,
                        output=str(save_to) if save_to is not None else "")


def write_reports(resource, total, filled, values, capped) -> None:
    """The same two artifacts src/inventory.py produces for the SEC feeds.

    UNDER data/, NOT docs/. inventory.py writes its report to docs/, which is
    tracked in git -- correct for a public SEC filing, wrong for this. The
    vocabulary section below quotes real values, and in this database those
    include client relationship names, an advisor's email address, brokerage
    account numbers and dollar amounts. That file is not going anywhere near a
    commit, so it lives beside the data it describes.
    """
    import csv                                          # noqa: PLC0415
    import datetime                                     # noqa: PLC0415
    import pathlib                                      # noqa: PLC0415

    out = pathlib.Path(__file__).parents[1] / "data" / "interim"
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.date.today().isoformat()

    # Machine-readable: one row per field, for sorting and diffing between pulls.
    csv_path = out / f"act_{resource}_fields_{stamp}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["field", "populated", "share", "distinct_values", "high_cardinality"])
        for k in sorted(set(filled) | set(values)):
            n = filled.get(k, 0)
            w.writerow([k, n, round(n / total, 4) if total else 0,
                        len(values.get(k, ())), k in capped])

    # Readable: markdown, so it renders if it is ever pasted somewhere.
    md_path = out / f"act_{resource}_census_{stamp}.md"
    lines = [f"# Act! `{resource}` field census — {stamp}", "",
             f"{total:,} records examined.", "",
             "**Contains real client data** — relationship names, email addresses, "
             "account numbers and dollar values. Local only; `data/` is gitignored.",
             "", "## Populated fields", "",
             "| field | populated | share | distinct |", "|---|---:|---:|---:|"]
    for k, n in sorted(filled.items(), key=lambda kv: -kv[1]):
        d = f"{VOCAB_CAP}+" if k in capped else len(values.get(k, ()))
        lines.append(f"| `{k}` | {n:,} | {n / total:.1%} | {d} |")
    lines += ["", "## Value vocabularies", "",
              "Fields with a closed vocabulary are listed in full — these are the "
              "codes that need a human explanation.", ""]
    for k in sorted(values):
        if k not in filled:
            continue
        if k in capped:
            top = ", ".join(f"`{v}` ({n})" for v, n in values[k].most_common(5))
            lines.append(f"- **`{k}`** — high cardinality ({VOCAB_CAP}+): {top}, …")
        elif len(values[k]) <= 30:
            vocab = ", ".join(f"`{v}` ({n})" for v, n in values[k].most_common(30))
            lines.append(f"- **`{k}`** — {len(values[k])} distinct: {vocab}")
        else:
            top = ", ".join(f"`{v}` ({n})" for v, n in values[k].most_common(10))
            lines.append(f"- **`{k}`** — {len(values[k])} distinct, top 10: {top}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\n[*] {md_path}")
    print(f"[*] {csv_path}")
    print("    Both under data/, which is gitignored. Do NOT redirect this to the")
    print("    repo root -- the vocabularies quote client names and account numbers.")


def fields_scan(act: Act) -> None:
    """Which record types actually have field metadata, and how many columns.

    `--fields history` returned "0 field definitions" -- not an error, not a 404,
    just an empty list. That is the worst possible answer to get once and believe:
    it looks like "History has no fields" when it means "history is not a record
    type this endpoint knows". The difference decides whether a field is
    read-only or simply somewhere else.

    So ask for all of them and compare. A type with 0 columns next to one with
    200 is a name that was not recognised, not an empty model.
    """
    names = ["contact", "contacts", "company", "companies", "group", "groups",
             "opportunity", "opportunities", "activity", "activities",
             "history", "histories", "note", "notes", "user", "users",
             "task", "tasks"]
    print(f"{'record type':<16} {'columns':>8}   note")
    for n in names:
        try:
            cols = act.get(f"api/metadata/{n}/fields")
        except ActError as e:
            print(f"{n:<16} {'--':>8}   {str(e).split('->')[-1].strip()[:52]}")
            continue
        cols = cols if isinstance(cols, list) else (cols or {}).get("value") or []
        flag = ""
        if cols:
            # The one question worth answering here, answered in passing.
            mgr = [c for c in cols
                   if "manage" in str(c.get("columnName") or "").lower()
                   or "recordmanager" in str(c.get("columnName") or "").lower()]
            if mgr:
                flag = ("  manager field: "
                        + ", ".join(f"{c.get('columnName')}"
                                    f"{' (read-only)' if c.get('isReadOnly') else ' WRITABLE'}"
                                    for c in mgr))
        print(f"{n:<16} {len(cols):>8}{flag}")
    print("\n0 columns means the NAME was not recognised, not that the type has "
          "no fields.")


def fields(act: Act, record_type: str) -> None:
    """Field definitions and picklist meanings -- the database explaining itself.

    The census found 225 internal keys (`email__y_n`, `tier__a_b_c`, `user5`)
    and a pile of undocumented codes. It could not say what `2` or `P` mean,
    and the conclusion was "ask the person who maintains it".

    That may not be necessary. Act!'s metadata carries `displayName` for every
    column, and drop-down fields carry a picklist whose items have a `value` AND
    a `description`. If whoever built these fields wrote descriptions, the
    translation is already in the database.

    It also reports isReadOnly, which decides whether a CRD can be written at
    all -- the open question from before any of this was built.
    """
    try:
        cols = act.get(f"api/metadata/{record_type}/fields")
    except ActError as e:
        print(f"  api/metadata/{record_type}/fields -> {str(e)[:120]}")
        print("  Trying the unscoped endpoint instead.")
        cols = act.get("api/metadata/fields")
    cols = cols if isinstance(cols, list) else (cols or {}).get("value") or []
    print(f"{len(cols):,} field definitions\n")

    print(f"{'COLUMN':<28} {'DISPLAY NAME':<34} {'TYPE':<14} RO  CUSTOM  PICKLIST")
    picklisted = []
    for c in sorted(cols, key=lambda c: str(c.get("columnName") or "")):
        col = str(c.get("columnName") or c.get("name") or "")
        disp = str(c.get("displayName") or "")
        pl = c.get("picklist")
        pl_name = (pl or {}).get("name") if isinstance(pl, dict) else (pl or "")
        if pl_name:
            picklisted.append((col, disp, pl_name, (pl or {}).get("id")))
        print(f"  {col:<26} {disp:<34} {str(c.get('dataType') or ''):<14}"
              f" {'Y' if c.get('isReadOnly') else '.'}   "
              f"{'Y' if c.get('isCustom') else '.'}      {pl_name or ''}")

    if not picklisted:
        print("\nNo picklists are attached to these fields. The codes are free "
              "text, so their meaning lives with whoever maintains the database.")
        return

    print(f"\n{len(picklisted)} fields have a picklist. Resolving the items --")
    print("value plus description is the translation the census could not give.\n")
    for col, disp, pl_name, pl_id in picklisted:
        key = pl_id or pl_name
        try:
            items = act.get(f"api/metadata/drop-down/lists/{key}/items")
        except ActError as e:
            print(f"  {col} ({pl_name}): could not read -- {str(e)[:80]}")
            continue
        items = items if isinstance(items, list) else (items or {}).get("value") or []
        print(f"  {col}  ({disp})  [{pl_name}] -- {len(items)} items")
        for it in items:
            v = str(it.get("value") or "")
            d = str(it.get("description") or "")
            print(f"      {v:<14} {d}")
        print()


def get_raw(act: Act, target: str) -> None:
    """Hit an arbitrary path with arbitrary query options and print what comes back.

    Built for one specific question. The Excel export carries an "EIC Contact"
    column on 37,904 rows -- the relationship owner, and the only field that
    knows whose relationship a contact is. The API does not return it: the
    system column REFERREDBY has displayName "EIC Contact", but the JSON
    property `referredBy` resolves to the CUSTOM field CUST_ReferredBy instead,
    and the system field is shadowed.

    OData $select names COLUMNS, not JSON properties, so it may reach the field
    the default serialisation hides. If it does, Phase 1 proceeds. If it does
    not, the Excel export has to be kept for that one column.

    Also useful for everything else worth probing before committing to it --
    $filter on `edited` for incremental sync, /api/contacts/{id}/history,
    /api/emarketing/analytics/*.
    """
    path, _, query = target.partition("?")
    params = {}
    for pair in query.split("&"):
        if not pair:
            continue
        k, _, v = pair.partition("=")
        params[k] = v
    print(f"GET {path}" + (f"  {params}" if params else ""))
    data = act.get(path, **params)
    rows = data if isinstance(data, list) else [data] if data else []
    print(f"-> {len(rows)} record(s)\n")
    print(json.dumps(rows[:3], indent=2, default=str)[:6000])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--user", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--discover", action="store_true",
                    help="report version, rate limit, and the real endpoint list")
    ap.add_argument("--fields-scan", action="store_true",
                    help="which record types have field metadata at all, and "
                         "whether any exposes a writable record-manager column")
    ap.add_argument("--spec-grep", metavar="TERM",
                    help="search the Swagger document -- paths, parameters AND "
                         "model properties -- for a term such as recordManager")
    ap.add_argument("--full", action="store_true",
                    help="with --spec-grep, print every property and parameter "
                         "of each match rather than only the matching ones")
    ap.add_argument("--sample", metavar="RESOURCE",
                    help="print the field names of one record, e.g. contacts")
    ap.add_argument("--raw", metavar="RESOURCE",
                    help="full untruncated JSON, e.g. contacts -- this is how to "
                         "read customFields and the nested address objects")
    ap.add_argument("--top", type=int, default=2, help="records for --raw (default 2)")
    ap.add_argument("--census", metavar="RESOURCE",
                    help="page the whole resource and report which fields are "
                         "actually populated, e.g. contacts")
    ap.add_argument("--page", type=int, default=500, help="page size for --census")
    ap.add_argument("--cap", type=int, default=50000,
                    help="stop after this many records (default 50000)")
    ap.add_argument("--fields", metavar="RECORDTYPE", nargs="?", const="contact",
                    help="field definitions and picklist meanings (default: contact) "
                         "-- this is where the email__y_n and tier__a_b_c codes may "
                         "already be documented")
    ap.add_argument("--get", metavar="PATH?QUERY",
                    help="raw GET, e.g. 'api/contacts?$top=1&$select=REFERREDBY'. "
                         "PowerShell: use SINGLE quotes, or $select is eaten as a variable")
    ap.add_argument("--save", action="store_true",
                    help="write the pulled records to data/raw/act_<resource>_<date>.json "
                         "-- one pull, then every later question is answered locally")
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
        if args.discover:
            discover(act)
        if args.fields_scan:
            fields_scan(act)
            return
        if args.spec_grep:
            spec_grep(act, args.spec_grep, args.full)
            return
        if args.sample:
            sample(act, args.sample)
        if args.raw:
            raw(act, args.raw, args.top)
        if args.get:
            get_raw(act, args.get)
        if args.fields:
            fields(act, args.fields)
        if args.census:
            dest = None
            if args.save:
                if args.census == "contacts":
                    print("[!] A production contact snapshot requires its exact "
                          "owner map. Run src/act_pull.py instead; standalone "
                          "contact publication is disabled.")
                    return
                import pathlib                           # noqa: PLC0415
                stamp = utc_pull_id()
                dest = (pathlib.Path(__file__).parents[1] / "data" / "raw"
                        / f"act_{args.census}_{stamp}.json")
            census(act, args.census, args.page, args.cap, dest)
        if not (args.discover or args.sample or args.raw or args.census
                or args.fields or args.get or args.spec_grep
                or args.fields_scan):
            print("Nothing to do. Pass --discover, --spec-grep TERM, --fields, "
                  "--get, --sample contacts, --raw contacts, or --census contacts.")
    except ActError as e:
        print(f"[!] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
