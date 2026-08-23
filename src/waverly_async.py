"""Waverly Advisors roster -> data/raw/firm_rosters/waverly_<date>.csv

    everything   GET /people    ONE request, ~2.5 MB, no pagination

The whole roster is server-rendered on a single archive page: name, credentials,
title, city/state, direct phone and email, all inside repeating
`<div class="people-block">` cards. There is no advisor API to find and no
"load more" to defeat -- the cheapest scrape in the set.

THE EMAIL IS NOT A mailto: -- READ THIS BEFORE "FIXING" ANYTHING
----------------------------------------------------------------
A `mailto:` grep over this page returns ZERO. Every address is Cloudflare
obfuscated:

    <a href="/cdn-cgi/l/email-protection#d399a6a0a7babdfd81...">Send Email</a>

First byte is the XOR key, the rest is the address. This is the third firm in
this project where the naive grep said "publishes nothing" and the truth was
"publishes everything" (Mariner, Chevy Chase Trust, Waverly). Both routes are
read, and the run fails loudly below a coverage floor.

TWO EMAIL DOMAINS
-----------------
422 addresses are @waverly-advisors.com; ~36 are @promuscapital.com and its
siblings -- Promus, an acquired firm still on its own domain. Filtering to the
obvious domain drops those people, so `email_domain` is a column and nothing is
filtered on it.

THE PHONES ARE DIRECT LINES
---------------------------
454 distinct numbers across ~465 people. `phone_kind` is still computed by
counting occupants rather than asserted, so the claim stays honest if the site
changes.

465 CARDS, 214 IARs -- NOT THE SAME NUMBER
------------------------------------------
The SEC lists 214 IARs at CRD 115332. This page lists everyone including
operations and client service, so roughly half are not advisors. `title` is the
only thing that separates them and is captured verbatim, never inferred.

Run:  python src/waverly_async.py [--dry-run]
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import pathlib
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

BASE = "https://waverly-advisors.com"
PEOPLE = BASE + "/people"
WAVERLY_CRD = "115332"
HEADERS = {
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
}
RETRIES = 3
PAGE_MARKER = "people-block"
MIN_CARDS = 300          # a real page has ~465; anything near zero is a broken parse
MIN_EMAIL_RATE = 0.85
MIN_PHONE_RATE = 0.85

# The \Z is load-bearing. Without it the lookahead has nothing to match after
# the FINAL card on the page, and that card is dropped silently -- exactly the
# off-by-one that cost Mariner a person.
CARD_RE = re.compile(r'<div class="people-block">.*?(?=<div class="people-block">|\Z)', re.S)
PROFILE_RE = re.compile(r'href="(https://waverly-advisors\.com/people/([^"]+))"')
H5_RE = re.compile(r"<h5>(.*?)</h5>", re.S)
ANCHOR_RE = re.compile(r"<a[^>]*>(.*?)</a>", re.S)
TITLE_RE = re.compile(r"</a>\s*<span>(.*?)</span>", re.S)
SMALL_RE = re.compile(r'<p class="p--small">(.*?)</p>', re.S)
CF_RE = re.compile(r'/cdn-cgi/l/email-protection#([0-9a-fA-F]+)')
MAIL_RE = re.compile(r'href="mailto:([^"?]+)"')
PHONE_RE = re.compile(r"\(?\b\d{3}\)?[.\- ]\d{3}[.\- ]\d{4}\b")
CITY_RE = re.compile(r"^(.*),\s*([A-Z]{2})$")
SVG_RE = re.compile(r"<svg.*?</svg>|<\?xml[^>]*\?>|<!DOCTYPE svg[^>]*>", re.S)
TAGS = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


def text(fragment: str) -> str:
    return WS.sub(" ", htmllib.unescape(TAGS.sub(" ", SVG_RE.sub("", fragment or "")))).strip()


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
            r = session.get(url, headers=HEADERS, timeout=120)
            kind = r.headers.get("content-type", "")
            if r.status_code == 200 and "text/html" in kind:
                # A marker, not just a status code -- a 200 of well-formed HTML
                # with no roster in it is a real failure mode on these sites.
                if not expect or expect in r.text:
                    return r
                print(f"    [-] attempt {attempt + 1}: 200 but no {expect!r}")
            else:
                print(f"    [-] attempt {attempt + 1}: HTTP {r.status_code}")
        except Exception as exc:
            print(f"    [-] attempt {attempt + 1}: {type(exc).__name__} {exc}")
        time.sleep(2 ** attempt)
    return None


COLUMNS = ["name", "designations", "title", "email", "email_domain", "phone",
           "phone_kind", "area_code", "city", "state", "profile_url", "slug"]


def parse_card(card: str) -> dict | None:
    prof = PROFILE_RE.search(card)
    h5 = H5_RE.search(card)
    if not prof or not h5:
        return None
    block = h5.group(1)
    anchor = ANCHOR_RE.search(block)
    # "Justin T. Russell, <span>CIMA</span>" -- the credentials live inside the
    # name anchor, so split on the span rather than on the comma: plenty of
    # people have no credentials and some have several.
    raw_name = anchor.group(1) if anchor else ""
    creds = " ".join(text(x) for x in re.findall(r"<span>(.*?)</span>", raw_name, re.S))
    name = text(re.sub(r"<span>.*?</span>", "", raw_name, flags=re.S)).rstrip(",").strip()
    title_hit = TITLE_RE.search(block)

    # The small paragraphs are location / phone / email, but their ORDER is not
    # guaranteed and people with no phone simply have one fewer. Classify each
    # by what it contains instead of by position.
    city = state = phone = ""
    for frag in SMALL_RE.findall(card):
        value = text(frag)
        if not value or value.lower() == "send email":
            continue
        hit = PHONE_RE.search(value)
        if hit and not phone:
            phone = hit.group(0)
            continue
        loc = CITY_RE.match(value)
        if loc and not city:
            city, state = loc.group(1).strip(), loc.group(2)

    mails = [m.strip() for m in MAIL_RE.findall(card)]
    mails += [e for e in (cf_email(x) for x in CF_RE.findall(card)) if e]
    email = mails[0] if mails else ""

    digits = re.sub(r"\D", "", phone)
    if digits[:1] == "1":
        digits = digits[1:]
    return {
        "name": name,
        "designations": creds,
        "title": text(title_hit.group(1)) if title_hit else "",
        "email": email,
        "email_domain": email.split("@")[-1].lower() if "@" in email else "",
        "phone": phone,
        "phone_kind": "",                 # filled in after the parse
        "area_code": digits[:3] if len(digits) >= 10 else "",
        "city": city,
        "state": state,
        "profile_url": prof.group(1),
        "slug": prof.group(2).strip("/"),
    }


def classify(df: pd.DataFrame):
    """Direct vs shared decided by counting occupants, never by how it looks."""
    counts = Counter(df.loc[df["phone"] != "", "phone"])

    def label(row):
        if not row["phone"]:
            return ""
        n = counts[row["phone"]]
        return "direct" if n == 1 else ("shared" if n <= 5 else "switchboard")

    return df.apply(label, axis=1), counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="fetch and count, write nothing")
    args = ap.parse_args()

    started = time.time()
    session = requests.Session()

    r = request(session, PEOPLE, expect=PAGE_MARKER)
    if r is None:
        raise SystemExit("could not fetch /people -- refusing to write an empty roster")
    markup = r.text
    cards = CARD_RE.findall(markup)
    print(f"[*] /people: {len(markup):,} bytes, {len(cards):,} cards")
    if len(cards) < MIN_CARDS:
        raise SystemExit(f"only {len(cards)} cards (< {MIN_CARDS}) -- the page layout "
                         f"changed; fix CARD_RE rather than lowering the floor")

    rows = [row for row in (parse_card(c) for c in cards) if row and row["name"]]
    if args.dry_run:
        with_mail = sum(1 for x in rows if x["email"])
        print(f"[*] dry run: {len(rows):,} people parsed, {with_mail:,} with an email")
        return
    if not rows:
        raise SystemExit("nothing parsed -- refusing to write an empty roster")

    df = pd.DataFrame(rows, columns=COLUMNS)
    df["phone_kind"], counts = classify(df)
    scratch_path("waverly", "cards", ext="json").write_text(
        json.dumps(rows, indent=1), encoding="utf-8")
    out = roster_path("waverly")
    df.to_csv(out, index=False)

    emails = int((df["email"] != "").sum())
    phones = int((df["phone"] != "").sum())
    e_rate, p_rate = emails / len(df), phones / len(df)
    print(f"\n[*] {len(df):,} people in {time.time() - started:.0f}s -> {out}")
    print(f"    {emails:,} have a personal EMAIL ({e_rate:.1%}), "
          f"{df['email'].str.lower().nunique()} unique, across domains: "
          f"{dict(Counter(df.loc[df['email_domain'] != '', 'email_domain']).most_common(4))}")
    print(f"    {phones:,} have a PHONE ({p_rate:.1%}); "
          f"{df.loc[df['phone'] != '', 'phone'].nunique()} distinct numbers, "
          f"{df['area_code'].replace('', pd.NA).nunique()} area codes")
    print("    phone_kind: " + ", ".join(
        f"{k} {v:,}" for k, v in df["phone_kind"].value_counts().items() if k))
    repeats = [(p, n) for p, n in counts.most_common(3) if n > 1]
    print("    numbers used by more than one person: " +
          (", ".join(f"{p} x{n}" for p, n in repeats) if repeats else "none"))
    print(f"    {int((df['city'] != '').sum()):,} have a city; "
          f"{df['state'].replace('', pd.NA).nunique()} states; "
          f"{int((df['title'] != '').sum()):,} have a title")

    dupes = len(df) - df["slug"].nunique()
    if dupes:
        print(f"    [!] {dupes} duplicate slug(s)")
    if BRANCHES.exists():
        b = pd.read_parquet(BRANCHES, columns=["advisor_crd", "firm_crd"])
        sec = b[b["firm_crd"].astype(str) == WAVERLY_CRD]["advisor_crd"].nunique()
        if sec:
            print(f"    SEC lists {sec:,} IARs at CRD {WAVERLY_CRD}; this page lists "
                  f"{len(df):,} people of all roles ({len(df) / sec:.0%}) -- "
                  f"use `title` to separate advisors from support")

    if e_rate < MIN_EMAIL_RATE or p_rate < MIN_PHONE_RATE:
        raise SystemExit(
            f"\n[!] COVERAGE BELOW FLOOR (email {e_rate:.1%} < {MIN_EMAIL_RATE:.0%} "
            f"or phone {p_rate:.1%} < {MIN_PHONE_RATE:.0%}). The page carried both for "
            f"~98% of cards when built, so suspect the parser -- most likely the "
            f"Cloudflare route. The CSV was written; do not trust it.")


if __name__ == "__main__":
    main()
