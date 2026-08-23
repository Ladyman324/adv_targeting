"""Export compact CRD-keyed sales profiles for mapped advisory firms."""
from __future__ import annotations

from collections import defaultdict
import json
import pathlib
import re

import pandas as pd

from adv import ALLOC, CLIENT_TYPES
from export_geojson import display_firm
from firm_names import dedupe, normalize


ROOT = pathlib.Path(__file__).parents[1]
WEB = ROOT / "webapp" / "data"

CLIENT_LABELS = [
    "Individuals (other than high net worth)", "High net worth individuals",
    "Banking or thrift institutions", "Investment companies",
    "Business development companies", "Pooled investment vehicles",
    "Pension and profit-sharing plans", "Charitable organizations",
    "State or municipal government entities", "Other investment advisers",
    "Insurance companies", "Sovereign wealth funds / foreign official institutions",
    "Corporations or other businesses", "Other clients",
]
ASSET_LABELS = [
    "Exchange-traded equity securities", "Non-exchange-traded equity securities",
    "U.S. government/agency bonds", "U.S. state and local bonds",
    "Sovereign bonds", "Investment-grade corporate bonds",
    "Non-investment-grade corporate bonds", "Derivatives",
    "Registered investment company / BDC securities",
    "Pooled investment vehicle securities", "Cash and cash equivalents", "Other",
]


def scalar(value, integer=False):
    if pd.isna(value):
        return None
    return int(round(float(value))) if integer else round(float(value), 1)


def truth(value) -> bool:
    return value is True or str(value).strip().upper() in {"Y", "TRUE", "1"}


# Schedule A/B ownership codes, strongest first, for ranking rather than display.
CODE_RANK = {"F": 6, "E": 5, "D": 4, "C": 3, "B": 2, "A": 1, "NA": 0}


# Tokens that must not be title-cased: entity suffixes, initialisms and the
# roman numerals private funds are numbered with. Without this, CD&R Ferdinand
# Holdings, L.P. renders as "Cd&R Ferdinand Holdings, L.P." and Fund XII as
# "Fund Xii".
_KEEP = {"LLC", "LP", "LLP", "PC", "PA", "INC", "CO", "LTD", "USA", "US", "NA",
         "SA", "AG", "NV", "BV", "GP", "SARL", "SCSP", "RSC", "REIT", "III"}
_ROMAN = re.compile(r"^[IVXLCDM]{1,7}$")


def _title(value: str) -> str:
    text = str(value or "").strip()
    if not text.isupper():
        return text
    words = []
    for token in text.split(" "):
        core = re.sub(r"[^A-Z&]", "", token.upper())
        words.append(token.upper() if core in _KEEP or _ROMAN.match(core) or "&" in token
                     else token.title())
    return " ".join(words)


def load_owners() -> dict:
    """firm CRD -> {"a": [[name, type, title, date, code, ctrl, crd], ...],
                    "chain": [names from immediate parent up to the top]}

    The chain is walked here rather than in the browser: Schedule B names the
    entity each interest is held IN, so following it upward is a graph walk, and
    it needs both a hop cap and a cycle guard. Filers are also inconsistent --
    the same CD&R fund is filed as "CLAYTON, DUBILIER & RICE FUND XII, L.P." by
    one firm and "CLAYTON, DUBILIER, & RICE FUND XII, L.P." by another -- so
    steps are compared on the normalised form and displayed as filed.
    """
    path = ROOT / "data" / "interim" / "firm_owners.parquet"
    if not path.exists():
        return {}
    table = pd.read_parquet(path)
    out: dict = {}
    for crd, group in table.groupby("firm_crd", sort=False):
        direct = group[group["schedule"] == "A"]
        indirect = group[group["schedule"] == "B"]

        rows = sorted(
            (
                [_title(r.owner_name), r.owner_type, _title(r.title_or_status),
                 r.date_acquired, r.ownership_code, 1 if r.control_person == "Y" else 0,
                 str(r.owner_crd).strip()]
                for r in direct.itertuples(index=False)
            ),
            key=lambda row: (-CODE_RANK.get(row[4], 0), -row[5], row[0]),
        )

        # start from the largest entity owner; individuals are the top already
        seeds = [r for r in direct.itertuples(index=False)
                 if r.owner_type in ("DE", "FE")]
        chain: list = []
        if seeds:
            seeds.sort(key=lambda r: CODE_RANK.get(r.ownership_code, 0), reverse=True)
            upward: dict = {}
            for r in indirect.itertuples(index=False):
                upward.setdefault(normalize(r.owned_entity), []).append(r)
            node = seeds[0].owner_name
            chain = [_title(node)]
            seen = {normalize(node)}
            for _ in range(12):
                parents = upward.get(normalize(node))
                if not parents:
                    break
                parents.sort(key=lambda r: CODE_RANK.get(r.ownership_code, 0), reverse=True)
                node = parents[0].owner_name
                key = normalize(node)
                if key in seen:
                    break
                seen.add(key)
                chain.append(_title(node))
        # The group key travels WITH the chain. The webapp previously re-derived
        # it in JavaScript, and a mis-escaped regex there silently disagreed with
        # normalize() here, so every parent lookup missed. One implementation.
        out[str(crd)] = {"a": rows, "chain": chain,
                         "chain_keys": [normalize(step) for step in chain]}
    return out


def load_aliases() -> dict:
    """firm CRD -> raw other-name list; empty when the fetch has not run yet."""
    path = ROOT / "data" / "output" / "firm_other_names.parquet"
    if not path.exists():
        return {}
    table = pd.read_parquet(path)
    out: dict = {}
    for row in table.itertuples(index=False):
        out.setdefault(str(row.firm_crd), []).append(str(row.other_name))
    return out


def main() -> None:
    firms = pd.read_parquet(ROOT / "data" / "output" / "firms.parquet")
    firms["crd"] = firms["crd"].astype(str)
    aliases = load_aliases()
    owners = load_owners()
    national = json.loads((WEB / "offices_national.json").read_text(encoding="utf-8"))
    mapped_crds = [str(row[1]) for row in national["firms"]]
    index_to_crd = mapped_crds
    office_ids: dict[str, set[int]] = defaultdict(set)
    office_states: dict[str, set[str]] = defaultdict(set)
    placements: dict[str, int] = defaultdict(int)
    for office in national["offices"]:
        crd = index_to_crd[office[3]]
        placements[crd] += int(office[2])
        office_ids[crd].add(int(office[7]))
        office_states[crd].add(national["states"][office[6]])

    rows = firms.set_index("crd").reindex(mapped_crds)
    profiles = {}
    letters = list(CLIENT_TYPES)
    romans = list(ALLOC)
    for crd, row in rows.iterrows():
        clients = []
        for letter, label in CLIENT_TYPES.items():
            fewer = str(row.get(f"5D({letter})(2)", "")).lower().startswith("fewer")
            clients.append([
                scalar(row.get(f"n_{label}"), integer=True),
                1 if fewer else 0,
                scalar(row.get(f"raum_{label}"), integer=True),
            ])
        assets = [scalar(row.get(f"pct_{ALLOC[roman]}")) for roman in romans]
        name = display_firm(row.get("firm_display", row.get("name", "")))
        profiles[crd] = {
            "name": name,
            "legal": str(row.get("legal_name", "")).title(),
            "filed": str(row.get("filed", "")),
            "city": str(row.get("city", "")).title(),
            "state": str(row.get("state", "")),
            "website": str(row.get("website", "")) if pd.notna(row.get("website")) else "",
            "product": str(row.get("motion", "low_fit")),
            "sma_score": scalar(row.get("sma_fit_score")),
            "fund_score": scalar(row.get("fund_fit_score")),
            "opportunity": scalar(row.get("opportunity_score")),
            "platform": 1 if truth(row.get("platform_access")) else 0,
            "selects": 1 if truth(row.get("g_select_advisers")) else 0,
            "firm_type": str(row.get("firm_type", "")),
            "review": "" if pd.isna(row.get("review_reason")) else str(row.get("review_reason")),
            # former/other names from the IAPD firm record -- Bear Stearns for
            # CRD 79, Smith Barney for Morgan Stanley. Trimmed for display; the
            # full list stays in the search aliases.
            "aka": dedupe(name, aliases.get(crd, []))[:3],
            # Form ADV Schedule A/B, scraped per firm from the IAPD section
            # viewer; absent for firms outside the 5.G(7) fetch scope.
            "own": owners.get(crd, {}),
            "raum": scalar(row.get("raum_total"), integer=True),
            "disc": scalar(row.get("raum_disc"), integer=True),
            "nondisc": scalar(row.get("raum_nondisc"), integer=True),
            "disc_accounts": scalar(row.get("acct_disc"), integer=True),
            "nondisc_accounts": scalar(row.get("acct_nondisc"), integer=True),
            "accounts": scalar(row.get("acct_total"), integer=True),
            "non_pooled": scalar(row.get("raum_non_pooled"), integer=True),
            "equity_implied": scalar(row.get("raum_equity_exchange_implied"), integer=True),
            "fund_implied": scalar(row.get("raum_fund_shares_ric_implied"), integer=True),
            "clients": clients,
            "assets": assets,
            "advisors": scalar(row.get("n_advisors"), integer=True) or 0,
            "mapped_placements": placements[crd],
            "mapped_offices": len(office_ids[crd]),
            "mapped_states": len(office_states[crd]),
            "related_control": 1 if truth(row.get("Control/Controlled by Related Person")) else 0,
            "common_control": 1 if truth(row.get("Under Common Control")) else 0,
        }

    # Advisor-side view of the same Schedule A rows: advisor CRD -> the roles
    # they hold. 9,992 mapped advisors are named as an owner or officer
    # somewhere, so this stays small enough to load with the base data and is
    # what lets the advisor panel show a title and the map filter on it.
    # Restricted to advisors the map can actually show, and titles are interned:
    # "Chief Compliance Officer" alone appears on 917 rows, so repeating the
    # strings tripled the file for no benefit.
    mapped_advisors = set(
        pd.read_parquet(ROOT / "data" / "output" / "advisor_branches.parquet",
                        columns=["advisor_crd"])["advisor_crd"].astype(str))
    titles: list = []
    title_ix: dict = {}
    roles: dict = {}
    for firm_crd, record in owners.items():
        for row in record.get("a", []):
            owner_crd = str(row[6]).strip()
            if not owner_crd or owner_crd not in mapped_advisors:
                continue
            title = row[2] or ""
            if title not in title_ix:
                title_ix[title] = len(titles)
                titles.append(title)
            roles.setdefault(owner_crd, []).append(
                [firm_crd, title_ix[title], row[4], row[5]])
    roles_path = WEB / "owner_roles.json"
    roles_path.write_text(
        json.dumps({"titles": titles, "roles": roles}, separators=(",", ":")),
        encoding="utf-8")
    print(f"{len(roles):,} advisors named as an owner or officer -> "
          f"{roles_path.name} ({roles_path.stat().st_size / 1024:.0f} KB)")

    # Parent rollup. Every entity in a firm's ownership chain becomes a group, so
    # a salesperson can pick the level they think in: "Focus Operating" (42
    # firms) and "Clayton, Dubilier & Rice Fund XII" (27) are the same money at
    # different altitudes, and which one is useful depends on the conversation.
    # Keyed on the normalised name because filers punctuate inconsistently --
    # the same CD&R fund is filed with and without a comma after "Dubilier".
    group_members: dict = {}
    group_label: dict = {}
    for crd, record in owners.items():
        if crd not in profiles:
            continue
        for step in record.get("chain") or []:
            key = normalize(step)
            if not key:
                continue
            group_members.setdefault(key, set()).add(crd)
            group_label.setdefault(key, step)
    groups = {
        key: [group_label[key], sorted(members)]
        for key, members in group_members.items() if len(members) > 1
    }
    print(f"{len(groups):,} parent entities own more than one mapped firm")

    payload = {
        "groups": groups,
        "client_labels": CLIENT_LABELS,
        "asset_labels": ASSET_LABELS,
        "profiles": profiles,
    }
    path = WEB / "firm_profiles.json"
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"{len(profiles):,} mapped firm sales profiles -> {path.name} ({path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
