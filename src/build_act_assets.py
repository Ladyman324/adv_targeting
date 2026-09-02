"""Per-advisor product split of EIC's book, keyed on SEC advisor CRD.

WHAT THIS ANSWERS
-----------------
The advisor card shows one blended "assets with EIC" figure. The blend hides the
thing a rep is standing there to decide: WHICH product this relationship holds,
and therefore which one it does not. At firm level the asymmetry is stark --
Merrill Lynch holds $1.06B of All-Cap Value and exactly zero Large-Cap; Raymond
James is the mirror image. The same gaps exist advisor by advisor and nothing
surfaces them.

So this emits, per CRD: All-Cap Value SMA, Large-Cap Value SMA, EICIX, and
Mid-Cap/unallocated value.

FULL ATTRIBUTION PER ADVISOR, DELIBERATELY
------------------------------------------
An account shared by four advisors is written at FULL value against all four.
That is right for a card -- the rep needs the size of the relationship they are
walking into, not a quarter of it -- and it is wrong for any total. 40% of
accounts have more than one holder, so summing this file across advisors
overstates the book by about 42%.

Rather than leave that as a trap, the file carries de-duplicated totals for the
same approved, on-map account set in its `totals` block, and the complete source
book separately in `source_totals`. Nothing should ever recompute totals by
summing `advisors`.

Only approved ACT economic links are published. Economic-only inference never
authorizes an email, call, preferred name, ACT sync, or CRD write-back.

Run:  python src/build_act_assets.py
"""
from __future__ import annotations

import collections
import datetime
import json
import pathlib
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
from act_book import build, slots                        # noqa: E402
from act_economic_links import approved_link_map         # noqa: E402
from contact_provenance import sha256_file               # noqa: E402
from identity_schema import content_hash                 # noqa: E402
from identity_normalize import normalize_crd             # noqa: E402

OUT = ROOT / "webapp" / "data" / "act_assets.json"
IDENTITY = ROOT / "data" / "identity"
ECONOMIC = IDENTITY / "act_economic_links.parquet"
ECONOMIC_MANIFEST = IDENTITY / "act_economic_manifest.json"
IDENTITY_MANIFEST = IDENTITY / "identity_manifest.json"
ADVISOR_INDEX = ROOT / "webapp" / "data" / "advisor_index.json"


def atomic_json(path: pathlib.Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def load_deployed_advisor_crds() -> tuple[set[str], dict]:
    """Load the exact CRD universe addressable by the deployed map."""
    payload = json.loads(ADVISOR_INDEX.read_text(encoding="utf-8"))
    rows = payload.get("advisors")
    if not isinstance(rows, list) or not rows:
        raise SystemExit("advisor_index.json has no advisor rows")
    crds = set()
    for position, row in enumerate(rows, start=1):
        if not isinstance(row, list) or not row:
            raise SystemExit(f"advisor_index.json row {position} is malformed")
        crd = normalize_crd(row[0])
        if not crd:
            raise SystemExit(f"advisor_index.json row {position} has no CRD")
        if crd in crds:
            raise SystemExit(f"advisor_index.json repeats CRD {crd}")
        crds.add(crd)
    return crds, {"file": ADVISOR_INDEX.name,
                  "sha256": sha256_file(ADVISOR_INDEX), "rows": len(rows)}


def totals_for(accounts: dict, codes) -> dict:
    selected = [accounts[code] for code in codes]
    return {
        "acv": round(sum(a["acv_sma"] for a in selected), 2),
        "lcv": round(sum(a["large"] for a in selected), 2),
        "mf": round(sum(a["fund"] for a in selected), 2),
        "midcap": round(sum(a["midcap"] for a in selected), 2),
        "accounts": len(selected),
    }


def load_economic_links() -> tuple[pathlib.Path, list[dict], pd.DataFrame, dict]:
    """Validate provenance and return the exact ACT snapshot plus approved links."""
    economic = json.loads(ECONOMIC_MANIFEST.read_text(encoding="utf-8"))
    core = {k: v for k, v in economic.items()
            if k not in {"generatedUtc", "contentHash"}}
    if economic.get("contentHash") != content_hash(core):
        raise SystemExit("ACT economic manifest content hash is invalid")
    identity = json.loads(IDENTITY_MANIFEST.read_text(encoding="utf-8"))
    if economic.get("identityManifestHash") != identity.get("contentHash"):
        raise SystemExit("ACT economic links are stale; rebuild them after identity")
    fact = economic.get("actSource") or {}
    source = ROOT / "data" / "raw" / pathlib.Path(str(fact.get("file", ""))).name
    if (not source.is_file() or sha256_file(source) != fact.get("sha256")):
        raise SystemExit("ACT economic source hash differs from its manifest")
    rows = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or len(rows) != int(fact.get("rows") or -1):
        raise SystemExit("ACT economic source row count differs from its manifest")
    ledger_fact = economic.get("ledger") or {}
    if (not ECONOMIC.is_file() or
            sha256_file(ECONOMIC) != ledger_fact.get("sha256")):
        raise SystemExit("ACT economic ledger hash differs from its manifest")
    links = pd.read_parquet(ECONOMIC).fillna("")
    if len(links) != int(ledger_fact.get("rows") or -1):
        raise SystemExit("ACT economic ledger row count differs from its manifest")
    try:
        approved_link_map(links.to_dict("records"))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return source, rows, links, economic


def main() -> None:
    src, rows, links, economic_manifest = load_economic_links()
    accounts, conflicts, unresolved = build(rows)
    if unresolved:
        raise SystemExit(
            "ACT account-value conflicts remain unresolved; refusing asset publication")
    print(f"[*] {src.name}: {len(accounts):,} distinct accounts")
    crd_of = approved_link_map(links.to_dict("records"))
    print(f"[*] economic ledger: {len(crd_of):,} approved ACT contacts")
    deployed_crds, advisor_index_fact = load_deployed_advisor_crds()

    # Which Act! contacts hold each account.
    holders = collections.defaultdict(set)
    for r in rows:
        for code, *_ in slots(r.get("customFields") or {}):
            if code:
                holders[code].add(str(r.get("id")))

    # ACCOUNTS ARE EMITTED AS A TABLE, and advisors reference them by index.
    #
    # Without this a viewport summary is impossible to get right. 40% of
    # accounts have several holders, so adding up the visible advisors' totals
    # counts a team account once per member -- and the figure would then CHANGE
    # AS THE MAP PANS, because a second team-mate scrolling into view would add
    # their share again. Referencing accounts by index lets the client union the
    # set first and sum each account exactly once, at any zoom, under any filter.
    approved_accounts = {
        code for code in accounts
        if any(crd_of.get(aid) for aid in holders[code])}
    placed_accounts = {
        code for code in approved_accounts
        if any(crd_of[aid][0] in deployed_crds
               for aid in holders[code] if aid in crd_of)}
    off_map_accounts = approved_accounts - placed_accounts
    approved_crds = {crd for crd, _tier in crd_of.values()}
    off_map_crds = approved_crds - deployed_crds

    # Publish only accounts reachable from an advisor actually present in the
    # deployed advisor index. Unresolved and off-map vectors stay server-side.
    index_of = {code: i for i, code in enumerate(sorted(placed_accounts))}
    table = [None] * len(index_of)
    for code, i in index_of.items():
        a = accounts[code]
        table[i] = [round(a["acv_sma"], 2), round(a["large"], 2),
                    round(a["fund"], 2), round(a["midcap"], 2)]

    RANK = {"confirmed": 0, "high": 1, "review": 2}
    per = collections.defaultdict(lambda: {"acv": 0.0, "lcv": 0.0, "mf": 0.0,
                                           "midcap": 0.0,
                                           "n": 0, "shared": 0, "t": "review",
                                           "ix": []})
    for code in sorted(placed_accounts):
        a = accounts[code]
        for aid in holders[code]:
            hit = crd_of.get(aid)
            if not hit or hit[0] not in deployed_crds:
                continue
            crd, tier = hit
            e = per[crd]
            e["acv"] += a["acv_sma"]
            e["lcv"] += a["large"]
            e["mf"] += a["fund"]
            e["midcap"] += a["midcap"]
            e["n"] += 1
            e["ix"].append(index_of[code])
            if a["holders"] > 1:
                e["shared"] += 1
            if RANK.get(tier, 9) < RANK.get(e["t"], 9):
                e["t"] = tier

    # The headline and the EIC-assets filter now answer the same question:
    # approved economic links only, with every reached account counted once.
    mapped_accounts = {code: accounts[code] for code in placed_accounts}
    totals = totals_for(accounts, placed_accounts)
    totals.update({
        "accounts_on_map": len(placed_accounts),
        # The shape of the book, not just its size. These were computed here
        # already and printed to a terminal nobody keeps -- so the national view
        # had no way to say anything about EIC's own assets and showed three em
        # dashes, which reads as "we hold nothing" rather than "ask a smaller
        # question". Every figure below is de-duplicated: each account is
        # counted once, whoever holds it.
        "advisors": len(per),
        "advisors_review": sum(1 for e in per.values() if e["t"] == "review"),
        "advisors_economic_only": sum(
            1 for e in per.values() if e["t"] == "high"),
        "shared_accounts": sum(
            1 for a in mapped_accounts.values() if a["holders"] > 1),
        # The asymmetry the whole file exists to surface, at national scale.
        "gap_acv": sum(1 for e in per.values() if e["acv"] > 0 and e["lcv"] == 0),
        "gap_lcv": sum(1 for e in per.values() if e["lcv"] > 0 and e["acv"] == 0),
        "both_smas": sum(1 for e in per.values() if e["acv"] > 0 and e["lcv"] > 0),
    })
    off_map_totals = totals_for(accounts, off_map_accounts)
    off_map_totals["advisors"] = len(off_map_crds)
    off_map_totals["crds"] = len(off_map_crds)
    unresolved_codes = set(accounts) - approved_accounts
    unresolved_totals = totals_for(accounts, unresolved_codes)
    source_totals = totals_for(accounts, accounts)
    source_totals.update({
        "unmapped_accounts": len(accounts) - len(mapped_accounts),
        "approved_accounts": len(approved_accounts),
        "approved_off_map": off_map_totals,
        "unapproved_or_unresolved": unresolved_totals,
    })
    deployment_reconciliation = {
        "approved_on_map": totals_for(accounts, placed_accounts),
        "approved_off_map": off_map_totals,
        "unapproved_or_unresolved": unresolved_totals,
    }

    out = {}
    for crd, e in per.items():
        rec = {"t": e["t"], "n": e["n"], "ix": sorted(set(e["ix"]))}
        for k in ("acv", "lcv", "mf", "midcap"):
            if e[k] > 0:
                rec[k] = round(e[k], 2)
        if e["shared"]:
            rec["sh"] = e["shared"]
        out[str(crd)] = rec

    payload = {
        "note": ("EIC assets by product from approved economic links. "
                 "Headline totals and filtered advisors use the same approved "
                 "account set. acv/lcv/mf are FULL "
                 "account values, so an account shared by several advisors appears "
                 "at full value on each -- correct for a card, WRONG for any sum. "
                 "To total across advisors, union their `ix` account indices and "
                 "sum `accounts` once per index. `totals` is the approved, "
                 "map-addressable figure; `source_totals` reconciles the full book."),
        "as_of": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "source": src.name,
        "economic_manifest_hash": economic_manifest["contentHash"],
        "advisor_index": advisor_index_fact,
        "reconciliation": economic_manifest.get("reconciliation", {}),
        "deployment_reconciliation": deployment_reconciliation,
        "columns": ["acv", "lcv", "mf", "midcap"],
        "accounts": table,
        "totals": totals,
        "source_totals": source_totals,
        "advisors": out,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(OUT, payload)

    naive = sum(e["acv"] + e["lcv"] + e["mf"] + e["midcap"]
                for e in per.values())
    ded = totals["acv"] + totals["lcv"] + totals["mf"] + totals["midcap"]
    tiers = collections.Counter(e["t"] for e in per.values())
    # Read back from `totals` rather than recomputed, so the console and the
    # national view cannot quote different numbers for the same question.
    gap_acv, gap_lcv = totals["gap_acv"], totals["gap_lcv"]

    print(f"[*] {len(out):,} advisors carry a book  ({dict(tiers)})")
    print(f"[*] accounts reaching an advisor on the map: "
          f"{totals['accounts_on_map']:,} of {source_totals['accounts']:,}")
    print(f"[*] approved but off-map: {len(off_map_crds):,} advisors, "
          f"{off_map_totals['accounts']:,} exclusively off-map accounts")
    print(f"[*] per-advisor sum ${naive:,.0f} vs de-duplicated ${ded:,.0f} "
          f"({(naive-ded)/naive:.0%} overlap -- do not sum the advisors block)")
    print(f"[*] PRODUCT GAPS: {gap_acv:,} advisors hold All-Cap but no Large-Cap; "
          f"{gap_lcv:,} hold Large-Cap but no All-Cap")
    print(f"[*] wrote {OUT}")


if __name__ == "__main__":
    main()
