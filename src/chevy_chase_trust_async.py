"""Chevy Chase Trust roster -> data/raw/firm_rosters/chevy_chase_trust_<date>.csv

    discovery  GET /people-sitemap.xml   -> ~133 /people/<slug>/ profiles
    enrich     GET <profile>             -> name, title, email, direct phone, bio

WordPress, and the sitemap is the entire list in one request, so nothing needs
a browser and there is no pagination to get wrong.

THE EMAIL IS THERE -- IT IS JUST NOT A mailto:
----------------------------------------------
Every profile carries a personal address, and a `mailto:` grep returns ZERO of
them. They are Cloudflare-obfuscated:

    <a class="email btn" href="/cdn-cgi/l/email-protection#076d656e...">

First byte is the XOR key, the rest is the address. This is the same scheme
Mariner used, and on that firm the naive grep led to a flat -- and wrong --
"they publish no email". Both routes are read here, and the run FAILS LOUDLY
below a coverage floor rather than quietly shipping a column of blanks.

TWO EMAIL DOMAINS, ON PURPOSE
-----------------------------
Addresses land on chevychasetrust.com AND bfsaulco.com, the B.F. Saul Company
parent. Filtering to the obvious domain would silently drop the parent-company
people, so `email_domain` is a column and nothing is filtered on it.

THE PHONE IS A DIRECT DIAL, AND THAT IS CHECKABLE
-------------------------------------------------
Numbers sit in the title block as `Tel: 240.497.5067`. They are sequential
extensions off the 240.497.5000 switchboard -- 5031, 5067, 5083 -- so they are
personal desk lines, not the main number repeated. `phone_kind` is still
computed by counting occupants per number rather than assumed, because that is
the only way the claim stays true if the site changes. 240.497.5000 and
571.622.1200 are the published switchboards and are labelled as such.

133 PEOPLE, 39 IARs -- NOT THE SAME NUMBER
------------------------------------------
This is a trust company. The SEC lists 39 IARs at CRD 110742; the site lists
everyone, and the first profile in the sitemap is the Chief Technology Officer.
`title` is the only thing separating an advisor from an operations manager, so
it is captured verbatim and never inferred.

Run:  python src/chevy_chase_trust_async.py [--dry-run] [--refresh] [--limit N]
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

BASE = "https://chevychasetrust.com"
SITEMAP = BASE + "/people-sitemap.xml"
CCT_CRD = "110742"
HEADERS = {
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
}
PAUSE = 0.9      # 0.35 tripped this host's rate limiter into 403s at ~row 100
RETRIES = 3
PROFILE_MARKER = "bio-container"

# The two numbers printed in the site header/footer on every page. Anything
# else in a title block is somebody's own line.
SWITCHBOARDS = {"240.497.5000", "571.622.1200"}
# Coverage floors. Below these the parse is broken, not the data -- 30 of 30
# profiles sampled by hand carried both an email and a phone.
MIN_EMAIL_RATE = 0.80
MIN_PHONE_RATE = 0.70

LOC_RE = re.compile(r"<loc>([^<]+)</loc>")
PEOPLE_RE = re.compile(r"^https?://(?:www\.)?chevychasetrust\.com/people/([^/]+)/$")
NAME_RE = re.compile(r'<div class="entry-title">(.*?)</div>', re.S)
TITLE_RE = re.compile(r'<div class="professional-title">(.*?)</div>', re.S)
TEL_RE = re.compile(r"Tel:\s*([\d][\d.\-() ]{8,})", re.I)
MAIL_RE = re.compile(r'href="mailto:([^"?]+)"')
CF_RE = re.compile(r'/cdn-cgi/l/email-protection#([0-9a-fA-F]+)')
BIO_RE = re.compile(r'<div class="entry-content".*?<div class="row">\s*<div class="col-md-12">'
                    r'(.*?)</div>', re.S)
TAGS = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


def text(fragment: str) -> str:
    return WS.sub(" ", htmllib.unescape(TAGS.sub(" ", fragment or ""))).strip()


def cf_email(encoded: str) -> str:
    """Cloudflare obfuscation: first byte is the XOR key for the rest."""
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
            r = session.get(url, headers=HEADERS, timeout=90)
            kind = r.headers.get("content-type", "")
            if r.status_code == 200 and ("text/html" in kind or "xml" in kind):
                # A marker, not just a status code: a 200 with well-formed HTML
                # and no profile in it is the failure mode that cost Janney 74
                # blank rows before it was checked for.
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


COLUMNS = ["name", "title", "email", "email_domain", "phone", "phone_kind",
           "area_code", "bio", "profile_url", "slug"]


def parse_profile(markup: str, url: str, slug: str) -> dict:
    name = NAME_RE.search(markup)
    title_block = TITLE_RE.search(markup)
    bio = BIO_RE.search(markup)

    raw_title = title_block.group(1) if title_block else ""
    tel = TEL_RE.search(text(raw_title))
    phone = WS.sub("", tel.group(1)) if tel else ""
    # The title block holds the phone too; strip it so `title` is only a title.
    title = WS.sub(" ", TEL_RE.sub("", text(raw_title))).strip()

    # Both routes, always. Plain mailto first because it needs no decoding;
    # the Cloudflare form is what actually carries every address here.
    mails = [m.strip() for m in MAIL_RE.findall(markup)]
    mails += [e for e in (cf_email(x) for x in CF_RE.findall(markup)) if e]
    email = mails[0] if mails else ""

    digits = re.sub(r"\D", "", phone)
    if digits[:1] == "1":
        digits = digits[1:]
    return {
        "name": text(name.group(1)) if name else "",
        "title": title,
        "email": email,
        "email_domain": email.split("@")[-1].lower() if "@" in email else "",
        "phone": phone,
        "phone_kind": "",                     # filled in after the run
        "area_code": digits[:3] if len(digits) >= 10 else "",
        "bio": text(bio.group(1))[:2000] if bio else "",
        "profile_url": url,
        "slug": slug,
    }


def classify(df: pd.DataFrame):
    """Label by how many people share the number, not by how it looks.

    The published switchboards are known and named, but everything else is
    decided by counting occupants -- if the site ever starts printing the main
    line on every profile, this notices instead of continuing to call it direct.
    """
    counts = Counter(df.loc[df["phone"] != "", "phone"])

    def label(row):
        phone = row["phone"]
        if not phone:
            return ""
        if phone in SWITCHBOARDS:
            return "switchboard"
        n = counts[phone]
        return "direct" if n == 1 else ("shared" if n <= 5 else "switchboard")

    return df.apply(label, axis=1), counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="discovery only")
    ap.add_argument("--refresh", action="store_true", help="ignore the cached discovery")
    ap.add_argument("--limit", type=int, help="only the first N profiles, for a trial")
    args = ap.parse_args()

    started = time.time()
    session = requests.Session()
    failures, gone = [], []

    cache = scratch_path("chevy_chase_trust", "discovery", ext="json")
    if cache.exists() and not args.refresh:
        people = [tuple(x) for x in json.loads(cache.read_text(encoding="utf-8"))]
        print(f"[*] {len(people):,} profiles from cache; --refresh to re-discover")
    else:
        r = request(session, SITEMAP)
        if r is None:
            raise SystemExit("could not fetch the people sitemap")
        # The sitemap includes /people/ itself -- the index page, not a person.
        people = sorted({(loc.strip(), PEOPLE_RE.match(loc.strip()).group(1))
                         for loc in LOC_RE.findall(r.text)
                         if PEOPLE_RE.match(loc.strip())
                         and PEOPLE_RE.match(loc.strip()).group(1) != "people"})
        print(f"[*] sitemap: {len(people):,} person profiles")
        cache.write_text(json.dumps(people, indent=1), encoding="utf-8")

    if not people:
        raise SystemExit("no profiles found -- refusing to write an empty roster")
    if args.dry_run:
        print(f"[*] dry run: would fetch {len(people):,} profiles")
        return
    if args.limit:
        people = people[:args.limit]

    # RESUME, don't restart. This host's 403 is a WAF block that accumulates
    # across runs on the same IP -- slowing from 0.35s to 0.9s made a later run
    # WORSE (117 rows then 109), which is the signature of a cumulative block
    # rather than a rate problem. So each run keeps whatever the last one got
    # and asks only for what is still missing; two or three passes converge on
    # the full roster instead of trading one subset for another.
    rows, done = [], set()
    prior = roster_path("chevy_chase_trust")
    if prior.exists() and not args.refresh:
        kept = pd.read_csv(prior).fillna("")
        rows = kept.to_dict("records")
        done = {str(r["slug"]) for r in rows if str(r.get("name", "")).strip()}
        print(f"[*] resuming: {len(done):,} profiles already captured, "
              f"{len(people) - len(done):,} to fetch")
    people = [(url, slug) for url, slug in people if slug not in done]
    if not people:
        print("[*] nothing missing -- roster is already complete")


    for i, (url, slug) in enumerate(people, 1):
        r = request(session, url, expect=PROFILE_MARKER)
        if r == "gone":
            gone.append(slug)
            continue
        if r is None:
            failures.append(slug)
            continue
        rows.append(parse_profile(r.text, url, slug))
        if i % 50 == 0:
            print(f"    enriched {i:,}/{len(people):,}")
        time.sleep(PAUSE)

    # This host starts returning 403 -- not 404, not a slow response -- once it
    # decides a client is moving too fast, and the 3 in-loop retries back off
    # over ~4s, which is nowhere near long enough. A first full run lost 16 of
    # 133 people this way. They are not gone, so ask again slowly.
    if failures:
        print(f"[*] retrying {len(failures)} rate-limited profile(s) slowly")
        by_slug = {slug: url for url, slug in people}
        recovered = []
        for slug in failures:
            time.sleep(4.0 + random.random() * 2)
            rr = request(session, by_slug[slug], expect=PROFILE_MARKER)
            if rr not in (None, "gone"):
                rows.append(parse_profile(rr.text, by_slug[slug], slug))
                recovered.append(slug)
        failures = [s for s in failures if s not in recovered]
        print(f"    {len(recovered)} recovered, {len(failures)} still failing")

    blanks = [r for r in rows if not r["name"]]
    if blanks:
        print(f"[*] repairing {len(blanks)} blank profile(s)")
        for row in blanks:
            time.sleep(PAUSE * 3)
            rr = request(session, row["profile_url"], expect=PROFILE_MARKER)
            if rr not in (None, "gone"):
                row.update(parse_profile(rr.text, row["profile_url"], row["slug"]))
        still = sum(1 for r in rows if not r["name"])
        print(f"    {len(blanks) - still} recovered, {still} still blank")

    if not rows:
        raise SystemExit("nothing collected -- refusing to write an empty roster")

    # A resumed run merges old and new; slug is the identity, last write wins.
    rows = list({str(r["slug"]): r for r in rows}.values())

    df = pd.DataFrame(rows, columns=COLUMNS)
    df["phone_kind"], counts = classify(df)
    scratch_path("chevy_chase_trust", "profiles", ext="json").write_text(
        json.dumps(rows, indent=1), encoding="utf-8")
    out = roster_path("chevy_chase_trust")
    df.to_csv(out, index=False)

    emails = int((df["email"] != "").sum())
    phones = int((df["phone"] != "").sum())
    e_rate, p_rate = emails / len(df), phones / len(df)
    print(f"\n[*] {len(df):,} people in {time.time() - started:.0f}s -> {out}")
    print(f"    {emails:,} have a personal EMAIL ({e_rate:.1%}) across "
          f"{df.loc[df['email_domain'] != '', 'email_domain'].nunique()} domains: "
          f"{dict(Counter(df.loc[df['email_domain'] != '', 'email_domain']))}")
    print(f"    {phones:,} have a PHONE ({p_rate:.1%}); "
          f"{df.loc[df['phone'] != '', 'phone'].nunique()} distinct numbers")
    print("    phone_kind: " + ", ".join(
        f"{k} {v:,}" for k, v in df["phone_kind"].value_counts().items() if k))
    repeats = [(p, n) for p, n in counts.most_common(3) if n > 1]
    print("    numbers used by more than one person: " +
          (", ".join(f"{p} x{n}" for p, n in repeats) if repeats else "none"))
    print(f"    {int((df['title'] != '').sum()):,} have a title, "
          f"{int((df['bio'] != '').sum()):,} a bio")

    dupes = len(df) - df["slug"].nunique()
    if dupes:
        print(f"    [!] {dupes} duplicate slug(s)")
    if BRANCHES.exists():
        b = pd.read_parquet(BRANCHES, columns=["advisor_crd", "firm_crd"])
        sec = b[b["firm_crd"].astype(str) == CCT_CRD]["advisor_crd"].nunique()
        if sec:
            print(f"    SEC lists {sec:,} IARs at CRD {CCT_CRD}; this site lists "
                  f"{len(df):,} people of all roles ({len(df) / sec:.0%}) -- "
                  f"a trust company, so most of these are NOT advisors")
    if gone:
        print(f"    {len(gone)} profile(s) 404: {gone[:4]}")
    if failures:
        print(f"    {len(failures)} FAILED: {failures[:5]}")

    # Loud, not silent. A parse that lost the Cloudflare route would still
    # produce a well-formed CSV; only a floor catches that.
    if e_rate < MIN_EMAIL_RATE or p_rate < MIN_PHONE_RATE:
        raise SystemExit(
            f"\n[!] COVERAGE BELOW FLOOR (email {e_rate:.1%} < {MIN_EMAIL_RATE:.0%} "
            f"or phone {p_rate:.1%} < {MIN_PHONE_RATE:.0%}). Hand-sampled profiles "
            f"carried both, so suspect the parser -- the CSV was written, but do "
            f"not trust it.")


if __name__ == "__main__":
    main()
