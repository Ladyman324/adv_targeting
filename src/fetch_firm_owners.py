"""Form ADV Schedule A (direct owners) and Schedule B (indirect owners) per firm.

Neither schedule is published in bulk. They are absent from the IAPD compilation
XML, from the FOIA roster CSV, and from the IAPD firm API; and the per-firm ADV
PDF renders them as blank form instructions, because Schedules A and B are filed
on initial application and amended through Schedule C rather than restated.

They ARE served by the IAPD section viewer, which needs an encrypted filing key
that appears in no API. IAPD's own bundle builds the link for its "View Form ADV
By Section" menu item as:

    {name:"View Form ADV By Section",
     urlFn: e => `${e.iapd_url}/IAPD/crd_iapd_AdvVersionSelector.aspx?ORG_PK=${e.firmId}`}

so the version selector takes the CRD alone and hands back the FLNG_PK. Two
requests per firm:

    1. crd_iapd_AdvVersionSelector.aspx?ORG_PK=<crd>            -> FLNG_PK
    2. iapd_AdvSchedule{A,B}Section.aspx?ORG_PK=&FLNG_PK=       -> owner rows

Note that omitting FLNG_PK does not error: it returns a ~12 KB empty shell, so a
missing key looks exactly like a firm with no owners. `parse_rows` therefore
keys off the real header row and the caller records `ok` explicitly.

This is HTML, not a feed. The markup can change without notice, so the parser is
header-driven rather than positional and every run reports its own coverage.

Scope: firms reporting Item 5.G(7), i.e. those that select outside managers.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
import html as html_lib

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
CACHE = ROOT / "data" / "firm_owners"
INTERIM = ROOT / "data" / "interim"

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "From": "bladyman@eicatlanta.com",
    "Accept": "text/html,*/*",
}
SELECTOR = "https://files.adviserinfo.sec.gov/IAPD/crd_iapd_AdvVersionSelector.aspx?ORG_PK={crd}"
SECTION = ("https://files.adviserinfo.sec.gov/IAPD/content/viewform/adv/Sections/"
           "iapd_AdvSchedule{sched}Section.aspx?ORG_PK={crd}&FLNG_PK={pk}")
THROTTLE = 0.35

FLNG = re.compile(r"FLNG_PK=([0-9A-F]{40})")
ROW = re.compile(r"(?s)<tr[^>]*>(.*?)</tr>")
CELL = re.compile(r"(?s)<t[dh][^>]*>(.*?)</t[dh]>")
TAG = re.compile(r"<[^>]+>")

# Form ADV Schedule A/B ownership codes. Bands, never exact percentages.
OWNERSHIP_BANDS = {
    "NA": "less than 5%",
    "A": "5% but less than 10%",
    "B": "10% but less than 25%",
    "C": "25% but less than 50%",
    "D": "50% but less than 75%",
    "E": "75% or more",
    "F": "75% or more",
}

# header label -> our column name. Schedule A has "Title or Status"; Schedule B
# adds "Entity in Which Interest is Owned" and renames the title column.
FIELDS = {
    "full legal name": "owner_name",
    "de/fe/i": "owner_type",
    "entity in which interest is owned": "owned_entity",
    "title or status": "title_or_status",
    "status": "title_or_status",
    "date title or status acquired": "date_acquired",
    "date status acquired": "date_acquired",
    "ownership code": "ownership_code",
    "control person": "control_person",
    "pr": "public_reporting",
    "crd no": "owner_crd",
}


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(TAG.sub("", fragment))).strip()


def _get(url: str) -> str:
    time.sleep(THROTTLE)
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=UA), timeout=90).read().decode("utf-8", "replace")


def _field(label: str) -> str | None:
    key = label.lower().strip()
    for prefix, name in FIELDS.items():
        if key.startswith(prefix):
            return name
    return None


def parse_rows(html: str) -> list:
    """Owner rows keyed off the table's own header, so a column reorder in the
    markup cannot silently shift values into the wrong field."""
    header, out = None, []
    for fragment in ROW.finditer(html):
        cells = [_text(c) for c in CELL.findall(fragment.group(1))]
        if not cells:
            continue
        if any(c.upper().startswith("FULL LEGAL NAME") for c in cells):
            header = [_field(c) for c in cells]
            continue
        if header is None or len(cells) != len(header):
            continue
        row = {name: value for name, value in zip(header, cells) if name}
        if row.get("owner_name"):
            out.append(row)
    return out


def fetch_firm(crd: str, refresh: bool = False) -> dict:
    """Cached {ok, flng_pk, A[], B[]} for one firm."""
    path = CACHE / f"{crd}.json"
    if path.exists() and not refresh:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            path.unlink()
    record = {"crd": crd, "ok": False, "flng_pk": None, "A": [], "B": []}
    try:
        match = FLNG.search(_get(SELECTOR.format(crd=crd)))
        if match:
            record["flng_pk"] = match.group(1)
            for sched in ("A", "B"):
                record[sched] = parse_rows(
                    _get(SECTION.format(sched=sched, crd=crd, pk=record["flng_pk"])))
            record["ok"] = True
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as err:
        record["error"] = type(err).__name__
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
    return record


def target_crds(everyone: bool = False) -> list:
    """Firms to fetch. By default only those reporting Item 5.G(7), because they
    are the ones that hire outside managers; --all covers the whole roster."""
    firms = pd.read_parquet(ROOT / "data" / "output" / "firms.parquet",
                            columns=["crd", "g_select_advisers"])
    if not everyone:
        firms = firms[firms["g_select_advisers"].fillna(False)]
    return firms["crd"].astype(str).tolist()


def main(limit: int | None = None, everyone: bool = False) -> None:
    crds = target_crds(everyone)
    if limit:
        crds = crds[:limit]
    scope = "the full roster" if everyone else "firms reporting 5.G(7)"
    cached = sum(1 for crd in crds if (CACHE / f"{crd}.json").exists())
    print(f"Schedule A/B for {len(crds):,} firms -- {scope}", flush=True)
    print(f"  {cached:,} already cached and will not be refetched; "
          f"{len(crds) - cached:,} to fetch "
          f"(~{(len(crds) - cached) * 2 * THROTTLE / 3600:.1f} h)", flush=True)

    rows, ok, empty, failed = [], 0, 0, 0
    for i, crd in enumerate(crds, 1):
        record = fetch_firm(crd)
        if not record["ok"]:
            failed += 1
        elif not record["A"] and not record["B"]:
            empty += 1
        else:
            ok += 1
        for sched in ("A", "B"):
            for row in record[sched]:
                rows.append({"firm_crd": crd, "schedule": sched, **row})
        if i % 100 == 0 or i == len(crds):
            print(f"  {i:>6,}/{len(crds):,}  rows {len(rows):>7,}  "
                  f"with-owners {ok:,}  empty {empty:,}  failed {failed:,}", flush=True)

    columns = ["firm_crd", "schedule", "owner_name", "owner_type", "owned_entity",
               "title_or_status", "date_acquired", "ownership_code",
               "control_person", "public_reporting", "owner_crd"]
    table = pd.DataFrame(rows)
    for column in columns:
        if column not in table:
            table[column] = ""
    table = table[columns]
    table["ownership_band"] = table["ownership_code"].map(OWNERSHIP_BANDS).fillna("")
    INTERIM.mkdir(parents=True, exist_ok=True)
    table.to_parquet(INTERIM / "firm_owners.parquet", index=False)

    named = (table["owner_crd"].astype(str).str.strip() != "").sum()
    print(f"\nfirm_owners.parquet  {len(table):,} rows for {table['firm_crd'].nunique():,} firms")
    print(f"  Schedule A {int((table['schedule'] == 'A').sum()):,} · "
          f"Schedule B {int((table['schedule'] == 'B').sum()):,}")
    print(f"  {named:,} rows carry an owner CRD (individuals; entities file none)")
    print(f"  {ok:,} firms with owners · {empty:,} filed none · {failed:,} unresolved")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    everyone = "--all" in args
    numbers = [a for a in args if a.isdigit()]
    main(int(numbers[0]) if numbers else None, everyone)
