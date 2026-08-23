"""Sanctuary Wealth partner firms and their people -> data/raw/firm_rosters/sanctuary_<date>.csv

    GET https://sanctuarywealth.com/wp-json/wp/v2/partner_firms?per_page=100
    GET https://sanctuarywealth.com/partner_firms/<slug>/     for the firm's own site
    GET <firm site>/ + whatever team page it links to

WHY THIS REPLACES THE DBA SWEEP
-------------------------------
src/dba_roster.py walked Sanctuary's SEC-filed websites and produced 183 rows
with NO name on any of them, a `practice` column holding whatever the page
<title> happened to be -- "Home", "Conversant Wealth Management :: Home" -- and
a `phone` column holding every number found anywhere on the page pasted
together. It was doing what a firm-agnostic sweep can do, which for this firm
is not much.

Sanctuary publishes its own partner directory, and it is a plain WordPress REST
collection needing no key: 97 partner firms, each with a page carrying the
firm's real name, its website and its city. The DBA sweep worked from 72
domains derived from SEC filings, so it was missing 25 firms outright as well
as every firm name.

NAMES ARE HEURISTIC HERE, AND SAY SO
------------------------------------
The 97 partner sites are 97 independent firms on 97 unrelated platforms. There
is no shared CMS as at Northwestern Mutual and no JSON-LD Person markup on any
team page sampled, so people are read out of the markup by shape: name-like
text with a title-like phrase near it. That finds real people -- and it also
finds "Our Team" and "Client Login" if nothing filters them.

So every person carries `name_confidence`:

    confirmed   the surname appears in an email address on that same domain,
                so two independent signals agree
    probable    the page structure is clean but nothing corroborates the name

Nothing is silently dropped for being merely probable. Filtering is the
caller's decision; inventing certainty is not available to either of us.

Run:  python src/sanctuary_async.py
      python src/sanctuary_async.py --dry-run     list firms, crawl nothing
      python src/sanctuary_async.py --firms-only  stage 1 only
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import pathlib
import re
import sys
import time
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from firm_rosters import roster_path, scratch_path  # one naming convention, defined once

import requests
from bs4 import BeautifulSoup

ROOT = pathlib.Path(__file__).resolve().parents[1]
BRANCHES = ROOT / "data" / "output" / "advisor_branches.parquet"

SANCTUARY_CRD = "226606"
DIRECTORY = "https://sanctuarywealth.com/wp-json/wp/v2/partner_firms"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
HEADERS = {"user-agent": UA, "accept": "text/html,application/xhtml+xml,application/json"}

COLUMNS = ["name", "title", "email", "email_kind", "phone", "name_confidence",
           "firm", "firm_slug", "domain", "city", "state", "source_page",
           "firm_url", "source"]

# Text that is shaped like a name and is not one. Everything here was produced
# by the extractor on a real page.
NOT_A_NAME = {
    "our team", "our teams", "the team", "meet the team", "about us", "who we are",
    "our mission", "our commitment", "our story", "our approach", "our people",
    "client login", "contact us", "get started", "learn more", "read more",
    "privacy policy", "terms of use", "form crs", "site map", "quick links",
    "wealth management", "financial planning", "investment management",
    "sanctuary wealth", "schedule a call", "book a meeting", "our services",
    "meet our team", "our advisors", "the process", "our values", "case studies",
}
TITLE_WORDS = re.compile(
    r"advisor|adviser|partner|president|principal|founder|manager|associate|"
    r"director|planner|analyst|officer|counsel|administrat|operations|"
    r"client service|wealth|portfolio|chief|cfp|cfa|cpa|chfc|clu|cima",
    re.I)
# Capital THEN a lowercase letter, every word. Without the lowercase, banner
# text in caps -- "ADVANTAGES YOU GAIN", "FREE ASSESSMENT" -- reads as a
# perfectly good three-word name.
# A word is either normal-cased (Baker) or a short all-caps initialism (TJ,
# J.D., A.) -- and at least one word in the name must be normal-cased. That
# last clause is what keeps banner text out: "ADVANTAGES YOU GAIN" is all
# initialism-shaped words and no real one, while "TJ Lenz" and "A. Baker
# Woolworth" each have a lowercase word to anchor on. Requiring EVERY word to
# be normal-cased cost 5 people whose own email address carried their surname.
_WORD = r"(?:[A-Z][a-z][a-zA-Z'’.\-]*|[A-Z]\.?[A-Z]?\.?)"
NAME_SHAPE = re.compile(r"^%s(?: %s){1,3}$" % (_WORD, _WORD))
HAS_REAL_WORD = re.compile(r"(^| )[A-Z][a-z]")
# Trailing furniture that rides along in a card's text and is not part of
# anyone's job title.
TITLE_TAIL = re.compile(
    r"\s*(\[email\s*protected\]|view bio|read bio|read more|learn more|linkedin|"
    r"contact|email|\(?\d{3}\)?[ .\-]\d{3}[ .\-]\d{4}).*$", re.I)
GENERIC_LOCAL = re.compile(
    r"^(info|contact|hello|admin|office|team|support|sales|help|inquiries|"
    r"clientservice|clientservices|service|mail|general|main|welcome)$", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+1[ .\-]?)?\(?\d{3}\)?[ .\-]\d{3}[ .\-]\d{4}")
# `contact` earns its place: several of these sites keep every address on the
# contact page and none on the team page, and without it the surname check has
# nothing to corroborate against -- every name comes back "probable".
TEAM_LINK = re.compile(
    r"team|about|our-people|advisors|who-we-are|leadership|staff|contact", re.I)
# The last word of a person's name is not their job. "Jeremy Niederstadt
# Founder" matched the name shape and sailed through until this existed.
TRAILING_TITLE = re.compile(
    r"(founder|advisor|adviser|partner|president|principal|manager|associate|"
    r"director|planner|analyst|officer|cfp|cfa|cpa|chfc|clu|cima|aif|jd|mba)\.?$",
    re.I)
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "cfp", "cfa", "cpa", "chfc", "clu",
            "aams", "crpc", "cima", "aif", "jd", "mba", "ricp", "cepa", "cpwa"}


# --------------------------------------------------------------------------
# Stage 1 -- Sanctuary's own partner directory
# --------------------------------------------------------------------------
def get(session, url, tries=3, **kw):
    for attempt in range(tries):
        try:
            r = session.get(url, headers=HEADERS, timeout=45, allow_redirects=True, **kw)
            if r.status_code == 200:
                return r
            if r.status_code in (404, 410):
                return None
        except Exception:
            pass
        time.sleep(1.5 * (2 ** attempt))
    return None


def partner_firms(session) -> list:
    """The firms Sanctuary publishes, with name and page link."""
    out, page = [], 1
    while True:
        r = get(session, DIRECTORY, params={"per_page": 100, "page": page})
        if r is None:
            break
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        for item in batch:
            title = ((item.get("title") or {}).get("rendered") or "").strip()
            # WordPress renders &amp; and friends into the title.
            title = BeautifulSoup(title, "lxml").get_text(" ", strip=True)
            if title:
                out.append({"firm": title, "firm_slug": item.get("slug") or "",
                            "firm_url": item.get("link") or ""})
        if page >= int(r.headers.get("X-WP-TotalPages") or 1):
            break
        page += 1
    return out


def firm_detail(session, firm: dict) -> dict:
    """The firm's own website and location, off its Sanctuary page."""
    r = get(session, firm["firm_url"])
    if r is None:
        return firm
    soup = BeautifulSoup(r.text, "lxml")
    skip = ("sanctuarywealth.com", "facebook.", "twitter.", "x.com", "youtube.",
            "linkedin.", "instagram.", "finra.org", "sec.gov", "google.",
            "gstatic.", "jsdelivr.", "w3.org", "adviserinfo")
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("http") and not any(s in href.lower() for s in skip):
            firm["site"] = href
            break
    text = soup.get_text(" ", strip=True)
    where = re.search(r"\b([A-Z][a-zA-Z.\- ]{2,24}),\s*([A-Z]{2})\b", text)
    if where:
        firm["city"], firm["state"] = where.group(1).strip(), where.group(2)
    return firm


# --------------------------------------------------------------------------
# Stage 2 -- the partner firms' own sites
# --------------------------------------------------------------------------
def cf_emails(html: str) -> list:
    """Addresses Cloudflare has hidden behind data-cfemail.

    Cloudflare rewrites mailto: links into a hex blob XOR-ed with its first
    byte, so a plain regex over the page finds nothing and the site looks like
    it publishes no contact detail at all. Several partner firms sit behind it.
    """
    out = []
    for blob in re.findall(r'data-cfemail="([0-9a-fA-F]+)"', html):
        try:
            raw = bytes.fromhex(blob)
            key = raw[0]
            out.append("".join(chr(b ^ key) for b in raw[1:]).lower())
        except (ValueError, IndexError):
            continue
    return [e for e in out if EMAIL_RE.fullmatch(e)]


def surname(name: str) -> str:
    parts = [p for p in re.sub(r"[^A-Za-z' \-]", " ", name).split()
             if p.lower().strip(".") not in SUFFIXES]
    return parts[-1].lower() if len(parts) > 1 else ""


def container_text(node, levels=3):
    """The card a name sits in, so its title and email travel with it."""
    up = node
    for _ in range(levels):
        if up.parent is None:
            break
        up = up.parent
        text = up.get_text(" ", strip=True)
        if len(text) > 40:
            return up, " ".join(text.split())
    return up, " ".join(up.get_text(" ", strip=True).split())


def people_on(html: str, url: str) -> list:
    """Everyone the page appears to name, with whatever travels alongside."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    found, seen = [], set()
    for node in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "strong", "b",
                               "span", "p", "div", "a"]):
        text = " ".join(node.get_text(" ", strip=True).split())
        if not (4 < len(text) < 44) or not NAME_SHAPE.match(text):
            continue
        if not HAS_REAL_WORD.search(text):
            continue
        if TRAILING_TITLE.search(text):
            continue
        # "Wealth Associate" and "Private Wealth" are job titles standing on
        # their own in a card, not the person the card is about.
        if TITLE_WORDS.search(text):
            continue
        if text.lower() in NOT_A_NAME or text.lower() in seen:
            continue
        box, blob = container_text(node)
        # A name with no title-ish word anywhere near it is a heading, a
        # location or a product, not a person.
        after = blob.split(text, 1)[-1] if text in blob else blob
        title = ""
        for chunk in re.split(r"[|•\n]", after):
            chunk = chunk.strip(" ,-–—")
            if 3 < len(chunk) < 90 and TITLE_WORDS.search(chunk):
                # Some cards repeat the name inside the title block.
                if chunk.lower().startswith(text.lower()):
                    chunk = chunk[len(text):].strip(" ,-–—")
                title = TITLE_TAIL.sub("", chunk).strip(" ,-–—")
                if not title:
                    continue
                break
        emails = [e.lower() for e in EMAIL_RE.findall(blob)] + cf_emails(str(box))
        for a in box.find_all("a", href=True):
            if a["href"].lower().startswith("mailto:"):
                emails.append(a["href"][7:].split("?")[0].strip().lower())
        # No title is not the same as not a person. Trimming "[email protected]
        # VIEW BIO" off a card can leave nothing behind, and requiring a title
        # regardless cost 89 people -- 14 of them CONFIRMED, meaning an address
        # on the page carried their surname. That corroboration is stronger
        # evidence than a job title, so it is accepted in its place.
        if not title:
            last = surname(text)
            corroborated = last and any(
                last in re.split(r"[._\-]+", e.split("@")[0].lower()) or
                last in e.split("@")[0].lower() for e in emails)
            if not corroborated:
                continue
        phones = PHONE_RE.findall(blob)
        found.append({"name": text, "title": title,
                      "emails": list(dict.fromkeys(emails)),
                      "phone": phones[0] if phones else "",
                      "source_page": url})
    return found


def crawl_firm(firm: dict, pause: float) -> dict:
    """One partner firm: its home page and whatever team page it links to."""
    site = firm.get("site")
    if not site:
        return {**firm, "people": [], "emails": [], "reached": False}
    session = requests.Session()
    root = get(session, site)
    if root is None:
        return {**firm, "people": [], "emails": [], "reached": False}
    host = urlparse(root.url).netloc.lower().replace("www.", "")
    pages = {root.url: root.text}

    soup = BeautifulSoup(root.text, "lxml")
    targets = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not TEAM_LINK.search(href) or href.startswith(("mailto:", "tel:", "#")):
            continue
        full = urljoin(root.url, href)
        if urlparse(full).netloc.lower().replace("www.", "") != host:
            continue
        targets.append(full.split("#")[0])
    for target in list(dict.fromkeys(targets))[:4]:
        if target in pages:
            continue
        page = get(session, target, tries=2)
        if page is not None:
            pages[target] = page.text
        time.sleep(pause)

    people, emails = [], []
    for url, html in pages.items():
        people += people_on(html, url)
        emails += [e.lower() for e in EMAIL_RE.findall(html)] + cf_emails(html)
    # Same person on the home page and the team page is one person.
    # "Jeremy Niederstadt" and "Jeremy Niederstadt Founder Marina Bauk" can both
    # survive the shape test on a densely packed card. Where one candidate
    # starts with another, the shorter is the person and the longer is two
    # people run together.
    unique = {}
    for person in sorted(people, key=lambda p: len(p["name"])):
        key = person["name"].lower()
        if any(key.startswith(k + " ") for k in unique):
            continue
        unique.setdefault(key, person)
    return {**firm, "domain": host, "reached": True,
            "people": list(unique.values()),
            "emails": list(dict.fromkeys(emails))}


def rows_for(firm: dict) -> list:
    """One firm's crawl -> roster rows, with the confidence flag."""
    domain = firm.get("domain", "")
    on_domain = [e for e in firm.get("emails", []) if domain and e.endswith("@" + domain)]
    locals_ = [e.split("@")[0].lower() for e in on_domain]
    base = {"firm": firm.get("firm", ""), "firm_slug": firm.get("firm_slug", ""),
            "domain": domain, "city": firm.get("city", ""),
            "state": firm.get("state", ""), "firm_url": firm.get("firm_url", "")}
    def match(person):
        last = surname(person["name"])
        if not last:
            return ""
        for address in person["emails"] + on_domain:
            local = address.split("@")[0].lower()
            if last in re.split(r"[._\-]+", local) or last in local:
                return address
        return ""

    people = firm.get("people", [])
    picked = {p["name"]: match(p) for p in people}
    # A shared family mailbox is not one person's address. Marlen Lopez and
    # Roberto Lopez both matched lopezgroup@ -- assigning it to either would
    # caption one of them with the other's mailbox, which is the Edward Jones
    # fault this project spent the morning removing. Where two people claim one
    # address, neither gets it and it stays in the unclaimed list below.
    counts = {}
    for address in picked.values():
        if address:
            counts[address] = counts.get(address, 0) + 1

    rows, claimed = [], set()
    for person in people:
        own = picked[person["name"]]
        if own and counts[own] > 1:
            own = ""
        # Two independent signals agreeing is what "confirmed" means: the page
        # said this name, and an address on the same domain carries the
        # surname. Either alone is a guess.
        confidence = "confirmed" if own else "probable"
        if own:
            claimed.add(own)
        rows.append({**base, "name": person["name"], "title": person["title"],
                     "email": own, "email_kind": "personal" if own else "",
                     "phone": person["phone"], "name_confidence": confidence,
                     "source_page": person["source_page"], "source": "team_page"})
    # Addresses nobody claimed are still contact points. Carried without a name
    # rather than attached to whoever happened to be nearest on the page.
    for address in on_domain:
        if address in claimed:
            continue
        local = address.split("@")[0]
        rows.append({**base, "name": "", "title": "", "email": address,
                     "email_kind": "generic" if GENERIC_LOCAL.match(local) else "personal",
                     "phone": "", "name_confidence": "",
                     "source_page": firm.get("site", ""), "source": "unclaimed_email"})
    return rows


# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="list firms, crawl nothing")
    ap.add_argument("--firms-only", action="store_true", help="stage 1 only")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--pause", type=float, default=0.4)
    args = ap.parse_args()

    import pandas as pd
    started = time.time()
    session = requests.Session()

    print("[*] stage 1: Sanctuary's partner directory")
    firms = partner_firms(session)
    if not firms:
        raise SystemExit("the partner directory returned nothing -- refusing to write")
    print(f"    {len(firms)} partner firms")
    for i, firm in enumerate(firms, 1):
        firm_detail(session, firm)
        if i % 25 == 0 or i == len(firms):
            print(f"    {i}/{len(firms)} firm pages read")
        time.sleep(args.pause / 2)
    with_site = [f for f in firms if f.get("site")]
    print(f"    {len(with_site)} name their own website; "
          f"{sum(1 for f in firms if f.get('city'))} give a location")

    if args.dry_run:
        for f in firms[:10]:
            print(f"      {f['firm'][:34]:34} {f.get('site','-')[:40]:40} "
                  f"{f.get('city','')}, {f.get('state','')}")
        print("[*] dry run: nothing crawled, nothing written")
        return

    crawled = []
    if not args.firms_only:
        print(f"[*] stage 2: crawling {len(with_site)} partner sites")
        done = 0
        with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            jobs = [pool.submit(crawl_firm, f, args.pause) for f in with_site]
            for job in futures.as_completed(jobs):
                try:
                    crawled.append(job.result())
                except Exception as exc:
                    print(f"    [-] {type(exc).__name__} on a firm site")
                done += 1
                if done % 20 == 0 or done == len(with_site):
                    print(f"    {done}/{len(with_site)} sites  "
                          f"{sum(len(c['people']) for c in crawled):,} people")

    rows = []
    for firm in crawled:
        rows += rows_for(firm)
    df = pd.DataFrame(rows, columns=COLUMNS).fillna("")
    scratch_path("sanctuary", "firms").write_text(
        json.dumps(firms, indent=1), encoding="utf-8")
    out = roster_path("sanctuary")
    df.to_csv(out, index=False)

    named = df[df["name"] != ""]
    unreachable = [c["firm"] for c in crawled if not c.get("reached")]
    print(f"\n[*] {len(df):,} rows in {time.time() - started:.0f}s -> {out}")
    print(f"    {len(named):,} carry a NAME (the old sweep carried none), "
          f"{int((named['email'] != '').sum()):,} of them with their own email")
    if len(named):
        print("    name confidence: " + ", ".join(
            f"{k} {v:,}" for k, v in named["name_confidence"].value_counts().items()))
    print(f"    {df['firm'].nunique()} firms represented; "
          f"{int((df['phone'] != '').sum()):,} rows carry a phone")
    if unreachable:
        print(f"    [!] {len(unreachable)} site(s) did not answer: {unreachable[:6]}")

    if BRANCHES.exists():
        b = pd.read_parquet(BRANCHES, columns=["advisor_crd", "firm_crd"])
        sec = b[b["firm_crd"].astype(str) == SANCTUARY_CRD]["advisor_crd"].nunique()
        if sec:
            print(f"    SEC lists {sec:,} IARs at CRD {SANCTUARY_CRD}; this roster "
                  f"names {len(named):,} people ({len(named) / sec:.0%})")


if __name__ == "__main__":
    main()
