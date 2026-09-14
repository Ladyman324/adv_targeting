"""Capture Morgan Stanley branch and complex entities separately from people.

The public search vertical contains 451 branches and 66 complexes today. They
are useful office hierarchy, manager and switchboard evidence, but they are not
people and must never enter the CRD contact matcher.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import uuid
from typing import Dict, List, Mapping

import httpx
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from firm_rosters import scratch_path
from morgan_stanley_async import BASE_URL, DEFAULT_API_KEY
from morgan_team_pages import canonical_page_url


CENTER = {"lat": 39.5, "lng": -98.35, "radius": 5_000_000}
PROFILE_TYPES = ("Branch", "Complex")


def params(profile_type: str, offset: int, session_id: str,
           limit: int = 50) -> dict:
    filters = {
        "builtin.location": {"$near": CENTER},
        "c_profileType": {"$eq": profile_type},
    }
    return {
        "experienceKey": "ms-search-locator", "api_key": DEFAULT_API_KEY,
        "v": "20220511", "version": "PRODUCTION", "locale": "en",
        "verticalKey": "locations", "filters": json.dumps(filters),
        "limit": str(limit), "offset": str(offset), "retrieveFacets": "false",
        "skipSpellCheck": "false",
        "session_id": session_id,
        "sessionTrackingEnabled": "false", "source": "STANDARD",
    }


async def fetch(client: httpx.AsyncClient, profile_type: str, offset: int,
                session_id: str) -> dict:
    response = await client.get(
        BASE_URL, params=params(profile_type, offset, session_id))
    response.raise_for_status()
    return response.json().get("response", {})


def association_team_record(item: Mapping[str, object],
                            parent: Mapping[str, object]) -> dict | None:
    if str(item.get("c_profileType") or "") != "Team":
        return None
    url = canonical_page_url(item.get("c_pagesURL"))
    if not url:
        return None
    address = item.get("address") or {}
    emails = sorted({
        str(value).strip().lower()
        for value in (item.get("emails") or [])
        if str(value).strip()
    })
    return {
        "Team Name": str(item.get("c_pagesName") or item.get("name") or ""),
        "Team Page URL": url,
        "Published Team Emails": " | ".join(emails),
        "Published Email Count": len(emails),
        "Branch Name": str(
            item.get("c_branchName") or parent.get("c_branchName")
            or parent.get("c_pagesName") or ""),
        "Office Number": str(parent.get("c_officeNumber") or ""),
        "Complex ID": str(parent.get("c_complexID") or ""),
        "Main Phone": str(item.get("mainPhone") or ""),
        "Street Line 1": str(address.get("line1") or ""),
        "Street Line 2": str(address.get("line2") or ""),
        "City": str(address.get("city") or ""),
        "State": str(address.get("region") or ""),
        "Postal Code": str(address.get("postalCode") or ""),
        "Source": "branch_association",
    }


async def collect() -> tuple[List[dict], List[dict]]:
    headers = {
        "User-Agent": "Mozilla/5.0", "Accept": "*/*",
        "Referer": "https://advisor.morganstanley.com/",
    }
    output = []
    teams = {}
    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        for profile_type in PROFILE_TYPES:
            records = {}
            expected = 0
            barren = 0
            for sweep in range(1, 13):
                session_id = "01" + uuid.uuid4().hex[:24].upper()
                first = await fetch(client, profile_type, 0, session_id)
                total = int(first.get("resultsCount") or 0)
                expected = max(expected, total)
                pages = [first]
                if total > 50:
                    pages.extend(await asyncio.gather(*[
                        fetch(client, profile_type, offset, session_id)
                        for offset in range(50, total, 50)
                    ]))
                before = len(records)
                for page in pages:
                    for result in page.get("results") or []:
                        data = result.get("data") or {}
                        key = str(data.get("uid") or data.get("id") or "")
                        if key:
                            records.setdefault(key, data)
                added = len(records) - before
                print(f"    {profile_type} sweep {sweep}: +{added:,}, "
                      f"{len(records):,}/{expected:,}")
                if len(records) >= expected:
                    break
                barren = barren + 1 if added == 0 else 0
                if barren >= 3:
                    break

            for key, data in records.items():
                address = data.get("address") or {}
                emails = data.get("emails") or []
                associated = data.get("c_branchAssociatedEntities") or []
                for item in associated:
                    team = association_team_record(item, data)
                    if not team:
                        continue
                    existing = teams.get(team["Team Page URL"])
                    if existing:
                        known = set(existing["Published Team Emails"].split(" | "))
                        known.update(team["Published Team Emails"].split(" | "))
                        known.discard("")
                        existing["Published Team Emails"] = " | ".join(sorted(known))
                        existing["Published Email Count"] = len(known)
                    else:
                        teams[team["Team Page URL"]] = team
                output.append({
                    "profile_type": profile_type,
                    "source_id": key,
                    "directory_id": str(data.get("id") or ""),
                    "name": str(data.get("c_pagesName") or data.get("name") or ""),
                    "branch_name": str(data.get("c_branchName") or ""),
                    "branch_manager": str(data.get("c_branchManagerName") or ""),
                    "branch_id": str(data.get("c_branchID") or ""),
                    "complex_id": str(data.get("c_complexID") or ""),
                    "phone": str(data.get("mainPhone") or ""),
                    "email": str(emails[0] if emails else ""),
                    "page_url": str(data.get("c_pagesURL") or ""),
                    "street1": str(address.get("line1") or ""),
                    "street2": str(address.get("line2") or ""),
                    "city": str(address.get("city") or ""),
                    "state": str(address.get("region") or ""),
                    "postal": str(address.get("postalCode") or ""),
                    "latitude": str(
                        (data.get("yextDisplayCoordinate") or {}).get("latitude") or ""),
                    "longitude": str(
                        (data.get("yextDisplayCoordinate") or {}).get("longitude") or ""),
                    "associated_entities": len(associated),
                    "associated_fas": sum(
                        str(item.get("c_profileType") or "") == "FA"
                        for item in associated),
                    "associated_teams": sum(
                        str(item.get("c_profileType") or "") == "Team"
                        for item in associated),
                })
            print(f"[*] {profile_type}: {len(records):,} of {expected:,} records")
            if len(records) < expected:
                raise RuntimeError(
                    f"{profile_type}: expected {expected} unique records, "
                    f"got {len(records)} after repeated stable sweeps")
    return output, [teams[key] for key in sorted(teams)]

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    rows, teams = asyncio.run(collect())
    counts: Dict[str, int] = {
        kind: sum(row["profile_type"] == kind for row in rows)
        for kind in PROFILE_TYPES
    }
    print("[*] total: " + ", ".join(f"{key} {value:,}"
                                     for key, value in counts.items()))
    print(f"[*] branch-associated teams: {len(teams):,}")
    if args.dry_run:
        print("[*] dry run: no files written")
        return
    target = scratch_path("morgan_stanley", "offices", ext="csv")
    pd.DataFrame(rows).to_csv(target, index=False)
    team_target = scratch_path("morgan_stanley", "teams", ext="csv")
    pd.DataFrame(teams).to_csv(team_target, index=False)
    print(f"    offices -> {target}")
    print(f"    team index -> {team_target}")


if __name__ == "__main__":
    main()
