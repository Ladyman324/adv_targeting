"""Red Door Wealth Management roster -> data/raw/firm_rosters/red_door_<date>.csv

    discovery  GET /teammember-sitemap.xml   -> 22 /team/<slug>/ profiles
    enrich     GET <profile>                 -> name, designations, title, email, bio

WordPress, one sitemap, 22 people. The smallest scrape in the project and the
only one where the whole firm fits on one screen.

WHY A $3.1B FIRM IS WORTH ITS OWN SCRAPER
-----------------------------------------
CRD 153235, Memphis TN. $3.09B RAUM across 4,471 accounts -- a $690K average
account, 10 IARs, and Item 5.G(5) = Yes, so it hires outside managers. That is
a Tier A profile on every axis except size, and it is invisible to a ranking
sorted by RAUM: it would sit somewhere past rank 400.

EMAIL IS PERSONAL, PHONE IS NOT -- AND THE DIFFERENCE IS THE POINT
------------------------------------------------------------------
Every profile publishes a first-name address (john@, allen@, nancy@). Every
profile publishes the SAME phone, 901.681.0018, which is the main office line.

So `phone_kind` is "switchboard" for all 22 rows, and that is the honest
answer rather than a defect. The count-the-occupants rule used on Captrust,
Wealthspire and Waverly produces it automatically here; nothing is asserted.
`hello@reddoorwealth.com` appears on every page and is dropped as generic --
counting a front-desk mailbox as an advisor's address is exactly the
overstatement this project keeps refusing to make.

FIRST-NAME ADDRESSES ARE A REAL PATTERN, NOT A GUESS
----------------------------------------------------
Because the local part is a bare first name, this roster looks like it could
be extrapolated -- and it must not be. Every address here is read from that
person's own page. No address is constructed from a pattern.

Two facts prove why extrapolating would be wrong. Hannah W. Knight's profile
carries an EMPTY contact div, so she has no published address at all and a
pattern would have invented one. And two people -- Doug Wright and Jud Cannon
-- are on @cannonwrightblount.com, the affiliated CPA firm, not on
@reddoorwealth.com. 21 of 22 is the honest number.

22 PROFILES, 10 IARs
--------------------
The SEC lists 10 IARs. The site lists 22 people including operations and
client service, so `title` is what separates them and is captured verbatim.

Run:  python src/red_door_async.py [--dry-run] [--refresh]
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
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from firm_rosters import roster_path, scratch_path  # one naming convention, defined once

import pandas as pd
import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
BRANCHES = ROOT / "data" / "output" / "advisor_branches.parquet"

BASE = "https://reddoorwealth.com"
SITEMAP = BASE + "/teammember-sitemap.xml"
RED_DOOR_CRD = "153235"
HEADERS = {
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
}
PAUSE = 0.4
RETRIES = 3
PROFILE_MARKER = "info-card"
MIN_EMAIL_RATE = 0.85

# Front-desk mailboxes. Present on every page; never an advisor's address.
GENERIC = ("hello@", "info@", "contact@", "admin@", "office@")

LOC_RE = re.compile(r"<loc>([^<]+)</loc>")
TEAM_RE = re.compile(r"^https?://(?:www\.)?reddoorwealth\.com/team/([^/]+)/$")
H1_RE = re.compile(r"<h1>(.*?)</h1>", re.S)
TITLE_RE = re.compile(r"</h1>\s*<p>(.*?)</p>", re.S)
MAIL_RE = re.compile(r'href="mailto:([^"?]+)"')
CF_RE = re.compile(r'/cdn-cgi/l/email-protection#([0-9a-fA-F]+)')
TEL_RE = re.compile(r'href="tel:([^"]+)"')
BIO_RE = re.compile(r'<div class="bio">(.*?)</div>', re.S)
TAGS = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")
# "John Phillips V, CFA, CFP" -- credentials trail the name after a comma.
CREDS = {"cfa", "cfp", "cpa", "cima", "chfc", "clu", "aif", "cdfa", "cpfa",
         "cpwa", "crpc", "ea", "jd", "mba", "ms", "cap", "afc", "qka", "aams",
         "citp"}


def text(fragment: str) -> str:
    # The page is valid UTF-8 and the registered mark (C2 AE) survives intact;
    # U+FFFD is stripped only as a guard against a future encoding slip, not
    # because this site has one. A Windows console renders the mark as "?" --
    # that is the terminal, not the data, and the CSV holds the real bytes.
    cleaned = htmllib.unescape(TAGS.sub(" ", fragment or "")).replace("�", "")
    return WS.sub(" ", cleaned).strip()


def cf_email(encoded: str) -> str:
    """Cloudflare obfuscation, checked even though this site does not use it --
    three firms here published addresses only in this form."""
    try:
        key = int(encoded[:2], 16)
        out = "".join(chr(int(encoded[i:i + 2], 16) ^ key)
                      for i in range(2, len(encoded), 2))
    except ValueError:
        return ""
    return out if "@" in out and " " not in out else ""


def request(session, url, expect: str = ""):
    for attempt in range(RETRIES):
        try:
            r = session.get(url, headers=HEADERS, timeout=60)
            kind = r.headers.get("content-type", "")
            if r.status_code == 200 and ("text/html" in kind or "xml" in kind):
                if not expect or expect in r.text:
                    return r
                print(f"    [-] attempt {attempt + 1}: 200 but no {expect!r} {url}")
            elif r.status_code == 404:
                return "gone"
            else:
                print(f"    [-] attempt {attempt + 1}: HTTP {r.status_code} {url}")
        except Exception as exc:
            print(f"    [-] attempt {attempt + 1}: {type(exc).__name__} {exc}")
        time.sleep(PAUSE * (2 ** attempt) + random.random())
    return None


COLUMNS = ["name", "designations", "title", "email", "phone", "phone_kind",
           "area_code", "city", "state", "bio", "profile_url", "slug"]


def split_name(raw: str) -> tuple[str, str]:
    """'John Phillips V, CFA, CFP' -> ('John Phillips V', 'CFA, CFP').

    Split on the comma and keep only trailing parts that look like credentials,
    so a genuine suffix stays attached to the name.
    """
    parts = [p.strip() for p in raw.split(",")]
    name, creds = parts[0], []
    for part in parts[1:]:
        if part and part.lower().strip(".®") in CREDS:
            creds.append(part)
        elif part:
            name = f"{name}, {part}"
    return name.strip(), ", ".join(creds)


def parse_profile(markup: str, url: str, slug: str) -> dict:
    h1 = H1_RE.search(markup)
    title = TITLE_RE.search(markup)
    bio = BIO_RE.search(markup)

    raw_name = text(h1.group(1)) if h1 else ""
    name, creds = split_name(raw_name)

    mails = [m.strip().lower() for m in MAIL_RE.findall(markup)]
    mails += [e for e in (cf_email(x) for x in CF_RE.findall(markup)) if e]
    personal = [m for m in mails if not m.startswith(GENERIC)]
    email = personal[0] if personal else ""

    tel = TEL_RE.search(markup)
    phone = WS.sub("", htmllib.unescape(tel.group(1))) if tel else ""
    digits = re.sub(r"\D", "", phone)
    if digits[:1] == "1":
        digits = digits[1:]
    return {
        "name": name,
        "designations": creds,
        "title": text(title.group(1)) if title else "",
        "email": email,
        "phone": phone,
        "phone_kind": "",                  # filled in after the run
        "area_code": digits[:3] if len(digits) >= 10 else "",
        # One office, filed in ADV and printed on every page. Constant rather
        # than parsed: there is nothing per-person to read.
        "city": "Memphis",
        "state": "TN",
        "bio": text(bio.group(1))[:2000] if bio else "",
        "profile_url": url,
        "slug": slug,
    }


def classify(df: pd.DataFrame):
    """Occupant count decides, as everywhere else. Here it will say every
    number is a switchboard, which is the correct answer for this firm."""
    counts = Counter(df.loc[df["phone"] != "", "phone"])

    def label(row):
        if not row["phone"]:
            return ""
        n = counts[row["phone"]]
        return "direct" if n == 1 else ("shared" if n <= 5 else "switchboard")

    return df.apply(label, axis=1), counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="discovery only")
    ap.add_argument("--refresh", action="store_true", help="ignore the cached discovery")
    args = ap.parse_args()

    started = time.time()
    session = requests.Session()
    failures, gone = [], []

    cache = scratch_path("red_door", "discovery", ext="json")
    if cache.exists() and not args.refresh:
        people = [tuple(x) for x in json.loads(cache.read_text(encoding="utf-8"))]
        print(f"[*] {len(people)} profiles from cache; --refresh to re-discover")
    else:
        r = request(session, SITEMAP)
        if r is None:
            raise SystemExit("could not fetch the team sitemap")
        # /team/ itself is in the sitemap: it is the index page, not a person.
        people = sorted({(loc.strip(), TEAM_RE.match(loc.strip()).group(1))
                         for loc in LOC_RE.findall(r.text)
                         if TEAM_RE.match(loc.strip())
                         and TEAM_RE.match(loc.strip()).group(1) != "team"})
        print(f"[*] sitemap: {len(people)} person profiles")
        cache.write_text(json.dumps(people, indent=1), encoding="utf-8")

    if not people:
        raise SystemExit("no profiles found -- refusing to write an empty roster")
    if args.dry_run:
        print(f"[*] dry run: would fetch {len(people)} profiles")
        return

    rows = []
    for url, slug in people:
        r = request(session, url, expect=PROFILE_MARKER)
        if r == "gone":
            gone.append(slug)
            continue
        if r is None:
            failures.append(slug)
            continue
        rows.append(parse_profile(r.text, url, slug))
        time.sleep(PAUSE)

    if not rows:
        raise SystemExit("nothing collected -- refusing to write an empty roster")

    df = pd.DataFrame(rows, columns=COLUMNS)
    df["phone_kind"], counts = classify(df)
    scratch_path("red_door", "profiles", ext="json").write_text(
        json.dumps(rows, indent=1), encoding="utf-8")
    out = roster_path("red_door")
    df.to_csv(out, index=False)

    emails = int((df["email"] != "").sum())
    rate = emails / len(df)
    print(f"\n[*] {len(df)} people in {time.time() - started:.0f}s -> {out}")
    print(f"    {emails} have a personal EMAIL ({rate:.0%}), "
          f"{df['email'].nunique()} unique")
    print("    phone_kind: " + ", ".join(
        f"{k} {v}" for k, v in df["phone_kind"].value_counts().items() if k))
    for phone, n in counts.most_common(2):
        print(f"    {phone} is used by {n} people -- the main office line, "
              f"not a direct dial")
    print(f"    {int((df['title'] != '').sum())} have a title, "
          f"{int((df['designations'] != '').sum())} carry designations")

    dupes = len(df) - df["slug"].nunique()
    if dupes:
        print(f"    [!] {dupes} duplicate slug(s)")
    if BRANCHES.exists():
        b = pd.read_parquet(BRANCHES, columns=["advisor_crd", "firm_crd"])
        sec = b[b["firm_crd"].astype(str) == RED_DOOR_CRD]["advisor_crd"].nunique()
        if sec:
            print(f"    SEC lists {sec} IARs at CRD {RED_DOOR_CRD}; this site lists "
                  f"{len(df)} people of all roles ({len(df) / sec:.0%})")
    if gone:
        print(f"    {len(gone)} profile(s) 404: {gone}")
    if failures:
        print(f"    {len(failures)} FAILED: {failures}")

    if rate < MIN_EMAIL_RATE:
        raise SystemExit(
            f"\n[!] EMAIL COVERAGE {rate:.0%} < {MIN_EMAIL_RATE:.0%}. Every profile "
            f"sampled by hand carried one, so suspect the parser or the generic "
            f"filter. The CSV was written; do not trust it.")


if __name__ == "__main__":
    main()
