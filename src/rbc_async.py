"""RBC Wealth Management advisor roster -> data/raw/firm_rosters/rbc_<date>.csv

    POST https://www.rbcwealthmanagement.com/en-us/wp-admin/admin-ajax.php
         action=rbcwm_get_advisors_by_name&nonce=<nonce>
         &location_string=<state>&name=&data_source=us

A WordPress admin-ajax endpoint that returns rendered HTML rather than JSON, so
this is the only scraper here that parses markup. It takes a location STRING, so
the crawl is simply the 50 states plus DC -- no geographic mesh, no paging, and
no radius games. `count` matches the number of cards returned exactly, in every
state tested including New York's 575, so there is no evidence of a cap.

THE NONCE
---------
The page carries TWO ten-hex-digit nonces and only one of them works:

    "ajax_url":"...admin-ajax.php","nonce":"757b788ac7"   <- this one
    "rest_url":"...wp-json/v1/","rest_nonce":"48997e2d4b" <- 403s

So the nonce is read from its position next to `ajax_url`, not by pattern alone.
WordPress nonces expire (usually 12-24h), which is why it is fetched fresh at
the start of every run rather than hard-coded.

location_string IS NOT A STATE FILTER
------------------------------------
It is a proximity search AROUND the named state, and it crosses state lines
freely. Measured: "West Virginia" returns 223 cards whose area codes are mostly
703 (northern Virginia), 301 (Maryland), 412 and 724 (Pittsburgh) and 202 (DC).
"New Hampshire" returns 255, led by 860 and 203 (Connecticut) and 617 and 781
(Boston).

So the 51 queries return heavily OVERLAPPING sets -- 6,071 cards for 1,852
distinct people, some appearing in eight different state queries. Deduplication
must therefore be on the ADVISOR, never on (advisor, query state): keying on the
state inflated an earlier run of this script 3.3x and produced a roster larger
than the firm.

WHAT IS AND IS NOT HERE
-----------------------
Present: name, designations, direct phone, work EMAIL, team name, profile URL.
Absent: street, city, ZIP, lat/lon, CRD, and -- per the above -- any trustworthy
state. RBC has the weakest geography of any source we hold. The phone AREA CODE
is the best location signal in the feed and is extracted for that reason; the
query states are kept only as a coarse region hint, explicitly named
`found_via_states` so nobody reads them as the advisor's address.

Run:  python src/rbc_async.py [--dry-run]
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import pathlib
import random
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from firm_rosters import roster_path, scratch_path  # one naming convention, defined once

import pandas as pd
import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
BRANCHES = ROOT / "data" / "output" / "advisor_branches.parquet"

FINDER = "https://www.rbcwealthmanagement.com/en-us/find-an-advisor"
AJAX = "https://www.rbcwealthmanagement.com/en-us/wp-admin/admin-ajax.php"
RBC_CRD = "31194"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")
PAUSE = 1.0
RETRIES = 3

STATES = [
    ("Alabama", "AL"), ("Alaska", "AK"), ("Arizona", "AZ"), ("Arkansas", "AR"),
    ("California", "CA"), ("Colorado", "CO"), ("Connecticut", "CT"), ("Delaware", "DE"),
    ("District of Columbia", "DC"), ("Florida", "FL"), ("Georgia", "GA"), ("Hawaii", "HI"),
    ("Idaho", "ID"), ("Illinois", "IL"), ("Indiana", "IN"), ("Iowa", "IA"),
    ("Kansas", "KS"), ("Kentucky", "KY"), ("Louisiana", "LA"), ("Maine", "ME"),
    ("Maryland", "MD"), ("Massachusetts", "MA"), ("Michigan", "MI"), ("Minnesota", "MN"),
    ("Mississippi", "MS"), ("Missouri", "MO"), ("Montana", "MT"), ("Nebraska", "NE"),
    ("Nevada", "NV"), ("New Hampshire", "NH"), ("New Jersey", "NJ"), ("New Mexico", "NM"),
    ("New York", "NY"), ("North Carolina", "NC"), ("North Dakota", "ND"), ("Ohio", "OH"),
    ("Oklahoma", "OK"), ("Oregon", "OR"), ("Pennsylvania", "PA"), ("Rhode Island", "RI"),
    ("South Carolina", "SC"), ("South Dakota", "SD"), ("Tennessee", "TN"), ("Texas", "TX"),
    ("Utah", "UT"), ("Vermont", "VT"), ("Virginia", "VA"), ("Washington", "WA"),
    ("West Virginia", "WV"), ("Wisconsin", "WI"), ("Wyoming", "WY"),
]

NONCE_RE = re.compile(r'admin-ajax\.php","nonce":"([0-9a-f]+)"')
CARD_RE = re.compile(r'<div class="col-lg-4 col-md-6 rbc-card-col-my.*?(?=<div class="col-lg-4 '
                     r'col-md-6 rbc-card-col-my|\Z)', re.S)
NAME_RE = re.compile(r'<h3[^>]*>(.*?)</h3>', re.S)
TEL_RE = re.compile(r'href="tel:([^"]+)"')
MAIL_RE = re.compile(r'href="mailto:([^"]+)"')
SITE_RE = re.compile(r'href="(https?://[^"]*rbcwealthmanagement\.com/web/[^"]+)"')
# Deliberately NOT re.S and deliberately [^<]: the team name is a bare text node
# between two tags. A dot-matches-newline version runs past the end of the card
# and swallows the next three cards' markup as a "team name".
VISIT_RE = re.compile(r'Visit\s+([^<]*?)\s*website')
TAGS = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")
# Trailing letters after the last comma that are a credential, not a surname.
DESIG_RE = re.compile(r",\s*((?:[A-Z][A-Za-z]*\.?\s*)?[A-Z]{2,}[A-Za-z]*®?"
                      r"(?:\s*,\s*[A-Z][A-Za-z®.]*)*)\s*$")


def text(fragment: str) -> str:
    return WS.sub(" ", htmllib.unescape(TAGS.sub(" ", fragment or ""))).strip()


def nonce(session: requests.Session) -> str:
    """Fresh nonce, read by its position beside ajax_url.

    Matching any ten hex digits would find rest_nonce too, and rest_nonce 403s
    on this action -- an error that looks like a block rather than a bad key."""
    page = session.get(FINDER, headers={"user-agent": UA}, timeout=60)
    page.raise_for_status()
    found = NONCE_RE.search(page.text)
    if not found:
        raise SystemExit("no ajax nonce on the finder page -- the markup has changed")
    return found.group(1)


def fetch(session: requests.Session, token: str, state: str):
    headers = {"user-agent": UA, "accept": "*/*", "x-requested-with": "XMLHttpRequest",
               "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
               "referer": FINDER}
    body = {"action": "rbcwm_get_advisors_by_name", "nonce": token,
            "location_string": state, "name": "", "data_source": "us"}
    for attempt in range(RETRIES):
        try:
            r = session.post(AJAX, headers=headers, data=body, timeout=180)
            if r.status_code == 200:
                payload = r.json()
                if payload.get("success"):
                    data = payload.get("data") or {}
                    return data.get("html", ""), data.get("count"), True
                print(f"    [-] success=false for {state}")
            elif r.status_code == 403:
                print(f"    [-] 403 for {state} -- nonce rejected or expired")
            else:
                print(f"    [-] HTTP {r.status_code} for {state}")
        except Exception as exc:
            print(f"    [-] {type(exc).__name__} for {state}: {exc}")
        time.sleep(PAUSE * (2 ** attempt) + random.random())
    return "", None, False


def parse(markup: str, state_code: str):
    """Cards -> records, plus the set of team names this page advertises.

    Team names are harvested from 'Visit <X> website' links, which is the one
    authoritative statement the markup makes about which entries are teams. A
    card is then a team if its own heading is one of those names -- rather than
    guessing from words like 'Group', which would misclassify a person named
    Wealth or a solo practice called 'The Pedersen Investment Group'."""
    cards = CARD_RE.findall(markup)
    # "Visit website" (no name) is a personal site, so the capture is empty --
    # drop those rather than letting "" become a team everything matches.
    teams = {n for n in (WS.sub(" ", htmllib.unescape(m)).strip()
                         for m in VISIT_RE.findall(markup)) if n}
    rows = []
    for card in cards:
        heading = NAME_RE.search(card)
        if not heading:
            continue
        full = text(heading.group(1))
        if not full:
            continue
        phone = TEL_RE.search(card)
        email = MAIL_RE.search(card)
        site = SITE_RE.search(card)
        visit = VISIT_RE.search(card)
        # the team a PERSON belongs to, named in their own "Visit X website" link
        team = WS.sub(" ", htmllib.unescape(visit.group(1))).strip() if visit else ""
        rows.append({"full": full, "phone": phone.group(1) if phone else "",
                     "email": email.group(1) if email else "",
                     "site": site.group(1) if site else "",
                     "team_link": team, "state": state_code})
    return rows, teams


def split_name(full: str):
    """'Joe Bennick, CIMA®, CPWA®' -> ('Joe Bennick', 'CIMA®, CPWA®')."""
    found = DESIG_RE.search(full)
    if found:
        return full[:found.start()].strip(), found.group(1).strip()
    return full.strip(), ""


COLUMNS = ["name", "first_name", "last_name", "designations", "email", "phone",
           "area_code", "found_via_states", "team_name", "profile_url", "record_type"]


def to_row(rec: dict, is_team: bool) -> dict:
    name, designations = split_name(rec["full"])
    parts = name.split()
    digits = re.sub(r"\D", "", rec["phone"])
    return {
        "name": name,
        "first_name": "" if is_team else (parts[0] if parts else ""),
        "last_name": "" if is_team else (parts[-1] if len(parts) > 1 else ""),
        "designations": designations,
        "email": rec["email"],
        "phone": rec["phone"],
        # the one real geographic signal in this feed
        "area_code": digits[:3] if len(digits) >= 10 else "",
        # which state SEARCHES surfaced this person -- a region hint, not an
        # address. See the module docstring: the search crosses state lines.
        "found_via_states": ";".join(sorted(rec["states"])),
        # for a person this is their team; for a team card it is the team itself
        "team_name": rec["full"] if is_team else rec["team_link"],
        "profile_url": rec["site"],
        "record_type": "team" if is_team else "individual",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="fetch the nonce only")
    args = ap.parse_args()

    session = requests.Session()
    token = nonce(session)
    print(f"[*] ajax nonce {token}")
    if args.dry_run:
        print(f"[*] dry run: would query {len(STATES)} states")
        return

    started = time.time()
    seen: dict = {}
    team_names: set = set()
    failures, counts = [], {}

    for i, (state, code) in enumerate(STATES, 1):
        markup, count, ok = fetch(session, token, state)
        if not ok:
            failures.append(state)
            continue
        rows, teams = parse(markup, code)
        team_names |= teams
        counts[code] = count
        new = 0
        for rec in rows:
            # Key on NAME + EMAIL, and deliberately not on email alone: a team
            # card often carries a member's address (the Schweda-Ford Wealth
            # Management Team card shows patrick.ford@rbc.com), so an
            # email-only key lets the team and the person collide and one of
            # them disappears. The query state must not be in the key either --
            # see the docstring.
            key = (rec["full"].lower(), rec["email"].lower(), rec["phone"])
            if key in seen:
                seen[key]["states"].add(rec["state"])
            else:
                rec["states"] = {rec["state"]}
                seen[key] = rec
                new += 1
        # count is the page's own tally; a mismatch means the parser missed cards
        if count is not None and count != len(rows):
            print(f"    [!] {state}: API said {count}, parsed {len(rows)}")
        print(f"[{i}/{len(STATES)}] {state:<22}{count:>5} cards, {new:>4} new  "
              f"|  total {len(seen):,}")
        time.sleep(PAUSE)

    if not seen:
        raise SystemExit("nothing collected -- refusing to write an empty roster")

    records = list(seen.values())
    scratch_path("rbc", "cards", ext="json").write_text(
        json.dumps([dict(r, states=sorted(r["states"])) for r in records], indent=1),
        encoding="utf-8")

    people = [r for r in records if r["full"] not in team_names]
    teams = [r for r in records if r["full"] in team_names]

    df = pd.DataFrame([to_row(r, False) for r in people], columns=COLUMNS)
    out = roster_path("rbc")
    df.to_csv(out, index=False)

    team_out = scratch_path("rbc", "teams", ext="csv")
    pd.DataFrame([to_row(r, True) for r in teams], columns=COLUMNS).to_csv(
        team_out, index=False)

    emails = int((df["email"] != "").sum())
    print(f"\n[*] {len(df):,} advisors + {len(teams):,} team profiles in "
          f"{time.time() - started:.0f}s")
    print(f"    roster -> {out}")
    print(f"    teams  -> {team_out}")
    print(f"    {emails:,} have an email ({emails / len(df):.1%}); "
          f"{int((df['phone'] != '').sum()):,} have a phone; "
          f"{df['area_code'].replace('', pd.NA).nunique()} distinct area codes")
    print(f"    {int((df['team_name'] != '').sum()):,} advisors name a team")
    print(f"    NOTE: no street, city or coordinates in this feed. area_code is "
          f"the only real location signal; found_via_states is a search region, "
          f"NOT an address -- the state search crosses state lines")

    cards = sum(c for c in counts.values() if c)
    spread = df["found_via_states"].str.count(";").add(1)
    print(f"    {cards:,} cards over {len(counts)} queries collapsed to "
          f"{len(df) + len(teams):,} distinct records "
          f"({spread.max()} state searches surfaced the most-repeated advisor)")

    if BRANCHES.exists():
        b = pd.read_parquet(BRANCHES, columns=["advisor_crd", "firm_crd"])
        sec = b[b["firm_crd"].astype(str) == RBC_CRD]["advisor_crd"].nunique()
        # COUNT comparison only -- no CRD here. 31194 is RBC Capital Markets in
        # full, which includes institutional and capital-markets staff who are
        # not in a retail advisor finder, so a shortfall is expected.
        print(f"    SEC lists {sec:,} IARs at CRD {RBC_CRD} (all of RBC Capital "
              f"Markets); the finder publishes {len(df):,} ({len(df) / sec:.0%})")
    if failures:
        print(f"    {len(failures)} state(s) FAILED after {RETRIES} attempts: {failures}")


if __name__ == "__main__":
    main()
