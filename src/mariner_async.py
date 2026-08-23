"""Mariner team roster -> data/raw/firm_rosters/mariner_<date>.csv

    GET https://www.marinerwealthadvisors.com/our-team/

The whole team is server-rendered into one 2.1 MB archive page -- 893 cards, no
paging, no search parameters. So this is an HTML parse like RBC, but a single
request rather than 51.

WHY NOT THE WORDPRESS REST API
------------------------------
There is one, and it works: /wp-json/wp/v2/people reports 894 people. But its
`acf` block -- where a WordPress site keeps custom fields -- comes back EMPTY,
so the API gives only name, slug, permalink and two taxonomy term IDs. The
phone, job title and office live in the rendered card markup and nowhere else.
The API is therefore useful only as a cross-check on the count, which is what
it is used for here: 893 cards parsed against 894 people reported.

THE ENDPOINT IS INTERMITTENTLY BLOCKED
--------------------------------------
Roughly every other cold request returns HTML instead of JSON (and /wp-json/
itself 403s), apparently an edge/WAF rule rather than rate limiting -- it
recovers within seconds and a Referer header helps. Every request here goes
through a retry that checks the CONTENT TYPE, not just the status code: a 200
carrying an HTML error page would otherwise parse to zero cards and look like
an empty team.

EMAILS ARE THERE, BUT NOT AS mailto:
------------------------------------
Grepping for "mailto:" returns ZERO on this page, and concluding from that that
Mariner publishes no email is wrong -- it publishes 889. Every address is behind
Cloudflare's email obfuscation:

    <a href="/cdn-cgi/l/email-protection#1d77787b7b33766f68706d78717...">

The fragment is hex: the first byte is an XOR key and every later byte is a
character XORed with it. Decoded, that example is ben.jones@mariner.com. The
browser shows the real address because Cloudflare's JavaScript rewrites the link
after load; a scraper that only looks for mailto: sees nothing.

Four domains appear, which is the acquisition history showing through:
mariner.com (486), marinerwealthadvisors.com (392), adviceperiod.com (9),
marinerwealth.com (2).

WHAT IS AND IS NOT HERE
-----------------------
Present: name, job title, work EMAIL, direct phone, office city + state,
LinkedIn, profile URL.
Absent: street address and lat/lon, so placement goes through the city centroid.

Run:  python src/mariner_async.py [--dry-run]
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

TEAM_URL = "https://www.marinerwealthadvisors.com/our-team/"
API_COUNT = "https://www.marinerwealthadvisors.com/wp-json/wp/v2/people?per_page=1"
MARINER_CRD = "140195"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")
HEADERS = {"user-agent": UA, "accept-language": "en-US,en;q=0.9",
           "referer": TEAM_URL}
RETRIES = 5
PAUSE = 1.5

# The \Z is load-bearing. Without it the lookahead has nothing to match after
# the FINAL card -- the lazy .*? can never be satisfied, and that card is
# dropped silently. It cost one person (Mohit Sandhu) and showed up only as an
# off-by-one against the REST headcount.
CARD_RE = re.compile(
    r'<div class="person card".*?'
    r'(?=<div class="person card"|<div class="pagination|</section|\Z)', re.S)
FILTER_RE = re.compile(r'data-filter="([^"]*)"')
LINK_RE = re.compile(r'<a class="permalink" href="([^"]+)"')
NAME_RE = re.compile(r"<h3>(.*?)</h3>", re.S)
ROLES_RE = re.compile(r'<div class="roles">(.*?)</div>', re.S)
OFFICE_RE = re.compile(r'<div class="offices">(.*?)</div>', re.S)
TEL_RE = re.compile(r'href="tel:([^"]+)"')
LINKEDIN_RE = re.compile(r'href="(https://[^"]*linkedin\.com/[^"]+)"')
CFMAIL_RE = re.compile(r'/cdn-cgi/l/email-protection#([0-9a-fA-F]+)')
TAGS = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


def text(fragment: str) -> str:
    return WS.sub(" ", htmllib.unescape(TAGS.sub(" ", fragment or ""))).strip()


def cf_email(encoded: str) -> str:
    """Undo Cloudflare's email obfuscation.

    The fragment is hex. Byte one is an XOR key; each following byte is one
    character of the address XORed with it. Anything that does not decode to
    something containing '@' is returned empty rather than guessed at."""
    try:
        key = int(encoded[:2], 16)
        out = "".join(chr(int(encoded[i:i + 2], 16) ^ key)
                      for i in range(2, len(encoded), 2))
    except ValueError:
        return ""
    return out if "@" in out and " " not in out else ""


def get(session: requests.Session, url: str, want_json: bool = False):
    """Fetch with retries that verify the CONTENT TYPE.

    Checking only the status code is not enough here: the edge sometimes returns
    HTTP 200 with an HTML block page, which would parse to zero cards and be
    reported as 'Mariner has no team'."""
    for attempt in range(RETRIES):
        try:
            headers = dict(HEADERS)
            if want_json:
                headers["accept"] = "application/json"
            r = session.get(url, headers=headers, timeout=90)
            kind = r.headers.get("content-type", "")
            if r.status_code == 200 and (kind.startswith("application/json")
                                         if want_json else "text/html" in kind):
                return r
            print(f"    [-] attempt {attempt + 1}: HTTP {r.status_code} {kind[:30]}")
        except Exception as exc:
            print(f"    [-] attempt {attempt + 1}: {type(exc).__name__} {exc}")
        time.sleep(PAUSE * (attempt + 1) + random.random())
    return None


def expected_count(session: requests.Session):
    """The REST API's own headcount, used purely to check the parse."""
    r = get(session, API_COUNT, want_json=True)
    return int(r.headers.get("X-WP-Total", 0)) if r else None


OFFICE_PREFIX = re.compile(r"^Offices?:\s*", re.I)
MORE_RE = re.compile(r"\s*\+\s*(\d+)\s*more\s*$", re.I)


def split_office(raw: str):
    """'Offices: Novi, MI + 1 more' -> ('Novi', 'MI', 2).

    The card shows one office and a count; the extras are not named anywhere on
    this page, so the count is kept and the named office is the one we place
    them at."""
    value = OFFICE_PREFIX.sub("", text(raw))
    extra = MORE_RE.search(value)
    n = int(extra.group(1)) + 1 if extra else (1 if value else 0)
    value = MORE_RE.sub("", value).strip()
    if "," in value:
        city, _, state = value.rpartition(",")
        return city.strip(), state.strip(), n
    return value, "", n


COLUMNS = ["name", "title", "email", "phone", "office_city", "office_state",
           "n_offices", "linkedin", "profile_url", "slug"]


def parse(markup: str):
    rows = []
    for card in CARD_RE.findall(markup):
        name = text(NAME_RE.search(card).group(1)) if NAME_RE.search(card) else ""
        if not name:
            continue
        roles = ROLES_RE.search(card)
        office = OFFICE_RE.search(card)
        tel = TEL_RE.search(card)
        li = LINKEDIN_RE.search(card)
        link = LINK_RE.search(card)
        mail = CFMAIL_RE.search(card)
        city, state, n = split_office(office.group(1) if office else "")
        url = link.group(1) if link else ""
        rows.append({
            "name": name,
            "title": text(roles.group(1)) if roles else "",
            "email": cf_email(mail.group(1)) if mail else "",
            "phone": tel.group(1) if tel else "",
            "office_city": city,
            "office_state": state,
            "n_offices": n,
            "linkedin": li.group(1) if li else "",
            "profile_url": url,
            "slug": url.rstrip("/").rsplit("/", 1)[-1] if url else "",
        })
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    args = ap.parse_args()

    started = time.time()
    session = requests.Session()

    page = get(session, TEAM_URL)
    if page is None:
        raise SystemExit(f"could not fetch {TEAM_URL} after {RETRIES} attempts")
    rows = parse(page.text)
    if not rows:
        raise SystemExit("no cards parsed -- the markup has changed, or this was "
                         "a block page; refusing to write an empty roster")

    df = pd.DataFrame(rows, columns=COLUMNS)
    reported = expected_count(session)
    print(f"[*] {len(df):,} cards parsed in {time.time() - started:.0f}s")
    if reported:
        gap = reported - len(df)
        note = "" if abs(gap) <= 2 else "   <-- CHECK THE PARSER"
        print(f"    REST API reports {reported:,} people; parsed {len(df):,} "
              f"(difference {gap}){note}")
    else:
        print("    [!] could not read the REST headcount -- parse is unverified")

    if args.dry_run:
        print("[*] dry run: nothing written")
        return

    scratch_path("mariner", "cards", ext="json").write_text(
        json.dumps(rows, indent=1), encoding="utf-8")
    out = roster_path("mariner")
    df.to_csv(out, index=False)

    print(f"    roster -> {out}")
    emails = int((df["email"] != "").sum())
    print(f"    {emails:,} have an email ({emails / len(df):.1%}, decoded from "
          f"Cloudflare obfuscation); {int((df['phone'] != '').sum()):,} a phone; "
          f"{int((df['office_city'] != '').sum()):,} an office; "
          f"{int((df['linkedin'] != '').sum()):,} a LinkedIn profile")
    print(f"    email domains: "
          f"{df.loc[df['email'] != '', 'email'].str.split('@').str[-1].value_counts().to_dict()}")
    print(f"    no street address or coordinates in this feed")
    encoded = len(CFMAIL_RE.findall(page.text))
    if encoded != emails:
        # A decode that silently drops addresses is worse than none at all.
        print(f"    [!] {encoded} obfuscated link(s) on the page but only "
              f"{emails} decoded -- check cf_email()")
    print(f"    {df['office_state'].replace('', pd.NA).nunique()} states, "
          f"{int((df['n_offices'] > 1).sum()):,} list more than one office")

    # Not every card links to a profile, so a blank slug is normal and must be
    # excluded before testing uniqueness -- otherwise two people who merely lack
    # a profile page get reported as a duplicate.
    linked = df.loc[df["slug"] != "", "slug"]
    unlinked = len(df) - len(linked)
    if unlinked:
        print(f"    {unlinked} card(s) have no profile page (blank slug)")
    dupes = len(linked) - linked.nunique()
    if dupes:
        print(f"    [!] {dupes} duplicate slug(s) -- linked cards should be unique")

    if BRANCHES.exists():
        b = pd.read_parquet(BRANCHES, columns=["advisor_crd", "firm_crd"])
        sec = b[b["firm_crd"].astype(str) == MARINER_CRD]["advisor_crd"].nunique()
        # A COUNT comparison, and a loose one: this page is EVERYONE at Mariner,
        # advisors and support staff alike, so it is not the same population as
        # the ADV register. Expect the roster to exceed the IAR count.
        print(f"    SEC lists {sec:,} IARs at CRD {MARINER_CRD}; this page lists "
              f"{len(df):,} people of all roles ({len(df) / sec:.0%}) -- the page "
              f"is not restricted to advisors, so >100% is expected")


if __name__ == "__main__":
    main()
