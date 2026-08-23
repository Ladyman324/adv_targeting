"""Contact harvest across a firm's FILED DBA websites -> data/raw/firm_rosters/<slug>_<date>.csv

Some firms have no corporate advisor directory because their advisors are
independent practices, each with its own site. Those sites are not a mystery:
the firm files them on Schedule D, so `data/output/firm_websites.parquet`
already holds the discovery step. This walks them.

    seed     firm_websites.parquet, filtered to one firm's CRD
    probe    GET <domain>/ + a short list of team/about/contact paths
    harvest  mailto:, Cloudflare-obfuscated mail, tel:, and phone text

WHY THIS IS A DIFFERENT KIND OF SCRAPER
---------------------------------------
Every other scraper here targets ONE site whose structure was read first. This
targets N sites of unknown and unrelated structure, so it cannot promise a
clean name->email->phone row. It promises something narrower and checkable:
which practices are reachable, and what contact points they publish. Attaching
a contact to a PERSON needs per-site parsing and is deliberately not claimed --
`name` is populated only where a page makes it unambiguous.

GENERIC MAILBOXES ARE NOT ADVISOR CONTACTS
------------------------------------------
info@, contact@, hello@, admin@ reach a front desk. They are captured but
labelled `generic`, never mixed into the personal count, because a roster that
counts info@ as an advisor email overstates reach at exactly the moment the
sales team is relying on it.

WHAT COUNTS AS A FAILURE, AND WHAT DOES NOT
-------------------------------------------
A domain that resolves and serves a page with no contact detail is DATA -- that
practice publishes nothing. A domain that times out, NXDOMAINs or 403s is a
FAILURE and is reported separately. Collapsing the two would let a blocked
crawl masquerade as a firm that keeps its advisors private.

Run:  python src/dba_roster.py sanctuary --crd 226606 [--limit N] [--workers 8]
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import html as htmllib
import json
import pathlib
import re
import sys
import time
from collections import Counter
from urllib.parse import urlparse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from firm_rosters import roster_path, scratch_path  # one naming convention, defined once

import pandas as pd
import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEBSITES = ROOT / "data" / "output" / "firm_websites.parquet"

HEADERS = {
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
}
TIMEOUT = 20
MAX_PAGES = 5          # home + up to 4 contact-ish pages, per domain

# Ranked: the earlier a path appears the more likely it names PEOPLE rather
# than a form. Only paths that actually return HTML are followed.
CANDIDATE_PATHS = ["/team", "/our-team", "/about/team", "/meet-the-team",
                   "/our-people", "/people", "/advisors", "/about-us",
                   "/about", "/contact", "/contact-us"]

# Filed URLs that are somebody's social profile, not a practice website.
SOCIAL = re.compile(
    r"(linkedin|facebook|twitter|//x\.com|instagram|youtube|reddit|giphy|indeed|"
    r"glassdoor|threads|bsky|vimeo|slideshare|tiktok|sprout|pinterest|spotify|"
    r"podbean|iheart|apple\.com|play\.google|goo\.gl|bit\.ly|wikipedia)", re.I)

MAIL_RE = re.compile(r'mailto:([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})')
CF_RE = re.compile(r'/cdn-cgi/l/email-protection#([0-9a-fA-F]+)')
VALID_EMAIL = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}")
TEXT_MAIL_RE = re.compile(r'\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b')
TEL_RE = re.compile(r'href="tel:([^"]+)"')
PHONE_RE = re.compile(r"\(?\b\d{3}\)?[.\-\s]\d{3}[.\-\s]\d{4}\b")
LINK_RE = re.compile(r'href="([^"#?]+)"')
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S)
TAGS = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")

GENERIC = ("info", "contact", "hello", "admin", "office", "support", "team",
           "inquiries", "help", "service", "clientservice", "marketing",
           "compliance", "privacy", "webmaster", "noreply", "no-reply", "sales")
# Addresses that belong to the website vendor or a tracking pixel, not the firm.
JUNK_DOMAINS = ("sentry.io", "wixpress.com", "example.com", "sentry-cdn.com",
                "godaddy.com", "squarespace.com", "wordpress.com")


def text(fragment: str) -> str:
    return WS.sub(" ", htmllib.unescape(TAGS.sub(" ", fragment or ""))).strip()


def cf_email(encoded: str) -> str:
    """Cloudflare obfuscation: first byte is the XOR key for the rest.

    Three firms in this project (Mariner, Chevy Chase Trust, Waverly) looked
    like they published no email until this was applied, so it runs everywhere.
    """
    try:
        key = int(encoded[:2], 16)
        out = "".join(chr(int(encoded[i:i + 2], 16) ^ key)
                      for i in range(2, len(encoded), 2))
    except ValueError:
        return ""
    return out if "@" in out and " " not in out else ""


def is_generic(address: str) -> bool:
    return address.split("@")[0].lower().strip(".") in GENERIC


def emails_in(markup: str, host: str) -> set[str]:
    found = set(MAIL_RE.findall(markup))
    found |= {e for e in (cf_email(x) for x in CF_RE.findall(markup)) if e}
    # Plain-text addresses too, but ONLY on the practice's own domain --
    # unrestricted, this regex pulls in vendor and stock-photo addresses.
    root = ".".join(host.split(".")[-2:])
    found |= {e for e in TEXT_MAIL_RE.findall(markup)
              if e.lower().endswith("@" + root) or ("." + root) in e.lower()}
    out = set()
    for e in found:
        e = e.strip().strip(".").lower()
        # Strict FULL match, not a substring test. Across 97 unrelated site
        # layouts the loose checks let `info@auriccapital.com?` through -- a
        # query-string fragment glued to the address, which is not dialable,
        # mailable, or comparable with anything else in the roster.
        if not VALID_EMAIL.fullmatch(e) or any(j in e for j in JUNK_DOMAINS):
            continue
        if len(e) > 120 or e.endswith((".png", ".jpg", ".gif", ".webp")):
            continue
        out.add(e)
    return out


def phones_in(markup: str) -> set[str]:
    found = {WS.sub(" ", t).strip() for t in TEL_RE.findall(markup)}
    found |= set(PHONE_RE.findall(text(markup)))
    out = set()
    for p in found:
        digits = re.sub(r"\D", "", p)
        if digits.startswith("1"):
            digits = digits[1:]
        if len(digits) == 10 and digits[0] not in "01":
            out.add(f"{digits[:3]}.{digits[3:6]}.{digits[6:]}")
    return out


def crawl(domain: str) -> dict:
    """One practice site. Returns a record even when nothing is found -- 'this
    firm publishes nothing' is a finding, and must be distinguishable from
    'we could not reach it'."""
    rec = {"domain": domain, "status": "", "site_name": "", "pages": 0,
           "emails": [], "generic_emails": [], "phones": [], "sampled": []}
    session = requests.Session()
    base = "https://" + domain
    try:
        r = session.get(base, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
            rec["status"] = f"http_{r.status_code}"
            return rec
    except Exception as exc:
        rec["status"] = type(exc).__name__
        return rec

    host = urlparse(r.url).netloc.lower().replace("www.", "")
    title = TITLE_RE.search(r.text)
    rec["site_name"] = text(title.group(1))[:120] if title else ""
    pages = {r.url: r.text}

    # Prefer paths the site's own navigation links to; fall back to guesses.
    linked = {l.lower() for l in LINK_RE.findall(r.text)}
    ordered = [p for p in CANDIDATE_PATHS
               if any(l.rstrip("/").endswith(p) for l in linked)]
    ordered += [p for p in CANDIDATE_PATHS if p not in ordered]
    for path in ordered:
        if len(pages) >= MAX_PAGES:
            break
        try:
            pr = session.get(base + path, headers=HEADERS,
                             timeout=TIMEOUT, allow_redirects=True)
        except Exception:
            continue
        if (pr.status_code == 200
                and "text/html" in pr.headers.get("content-type", "")
                and pr.url not in pages):
            pages[pr.url] = pr.text

    all_mail, all_phone = set(), set()
    for markup in pages.values():
        all_mail |= emails_in(markup, host)
        all_phone |= phones_in(markup)

    personal = sorted(e for e in all_mail if not is_generic(e))
    rec.update({
        "status": "ok",
        "pages": len(pages),
        "emails": personal,
        "generic_emails": sorted(e for e in all_mail if is_generic(e)),
        "phones": sorted(all_phone),
        "sampled": sorted(pages),
    })
    return rec


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("slug", help="roster slug to write, e.g. sanctuary")
    ap.add_argument("--crd", required=True, help="firm CRD whose filed websites to crawl")
    ap.add_argument("--limit", type=int, help="only the first N domains, for a pilot")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true", help="list domains and stop")
    args = ap.parse_args()

    started = time.time()
    w = pd.read_parquet(WEBSITES)
    w["firm_crd"] = w["firm_crd"].astype(str)
    urls = [u for u in w.loc[w["firm_crd"] == str(args.crd), "url"]
            if isinstance(u, str) and not SOCIAL.search(u)]
    domains, seen = [], set()
    for u in urls:
        h = urlparse(u if "//" in u else "http://" + u).netloc.lower().replace("www.", "")
        if h and h not in seen:
            seen.add(h)
            domains.append(h)
    if not domains:
        raise SystemExit(f"no non-social domains filed under CRD {args.crd}")
    print(f"[*] CRD {args.crd}: {len(domains):,} distinct filed domains")
    if args.dry_run:
        for d in domains[:40]:
            print("   ", d)
        return
    if args.limit:
        domains = domains[:args.limit]

    records = []
    with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i, rec in enumerate(pool.map(crawl, domains), 1):
            records.append(rec)
            if i % 20 == 0:
                print(f"    crawled {i:,}/{len(domains):,}")

    scratch_path(args.slug, "dba_crawl", ext="json").write_text(
        json.dumps(records, indent=1), encoding="utf-8")

    rows = []
    for rec in records:
        for kind, bucket in (("personal", rec["emails"]), ("generic", rec["generic_emails"])):
            for address in bucket:
                rows.append({"name": "", "email": address, "email_kind": kind,
                             "phone": "; ".join(rec["phones"][:3]),
                             "practice": rec["site_name"], "domain": rec["domain"],
                             # rec["pages"] is already the COUNT. The previous
                             # expression wrapped an int in a list and measured
                             # that, so every row reported 1 page regardless.
                             "source_pages": rec["pages"]})
    df = pd.DataFrame(rows, columns=["name", "email", "email_kind", "phone",
                                     "practice", "domain", "source_pages"])
    out = roster_path(args.slug)
    df.to_csv(out, index=False)

    ok = [r for r in records if r["status"] == "ok"]
    dead = [r for r in records if r["status"] != "ok"]
    with_personal = [r for r in ok if r["emails"]]
    with_any_mail = [r for r in ok if r["emails"] or r["generic_emails"]]
    with_phone = [r for r in ok if r["phones"]]
    personal = {e for r in ok for e in r["emails"]}

    print(f"\n[*] {len(domains):,} domains in {time.time() - started:.0f}s -> {out}")
    print(f"    reachable: {len(ok):,} ({len(ok) / len(domains):.0%});  "
          f"unreachable: {len(dead):,}")
    print(f"    publish a PERSONAL email: {len(with_personal):,} sites "
          f"({len(with_personal) / max(1, len(ok)):.0%} of reachable) "
          f"-> {len(personal):,} addresses")
    print(f"    only a generic mailbox:   "
          f"{len(with_any_mail) - len(with_personal):,} sites")
    print(f"    publish NO email at all:  {len(ok) - len(with_any_mail):,} sites")
    print(f"    publish a phone:          {len(with_phone):,} sites")
    if dead:
        print("    unreachable reasons: " +
              ", ".join(f"{k} {v}" for k, v in
                        Counter(r["status"] for r in dead).most_common(5)))
    for r in with_personal[:6]:
        print(f"      {r['domain']:38} {r['emails'][:2]} {r['phones'][:1]}")
    print("\n    NOTE: `name` is intentionally blank. These are N unrelated site "
          "layouts;\n    attaching an address to a PERSON needs per-site parsing "
          "and is not claimed here.")


if __name__ == "__main__":
    main()
