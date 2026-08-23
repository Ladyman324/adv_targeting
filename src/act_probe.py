"""Find the Act! Web API endpoint that answers for OUR database, and say why the others don't.

WHY THIS EXISTS
---------------
The obvious first attempt -- POST credentials at https://apimta.act.com and read
the status code -- returns 401 and tells you nothing, because apimta.act.com is
Act!'s DOCUMENTATION host. It answers 401 to an unauthenticated request too:

    apimta.act.com/act.web.api/authorize -> 401, no Content-Type, empty body
    apius.act.com/act.web.api/authorize  -> 401, application/json

Our database is not on either of those unless Act! put it there. For Act!
Premium Cloud the pattern is:

    https://{server}/{customer}-api/act.web.api/authorize

...where {server} and {customer} come from the URL you use to log into Act! in a
browser. So this script tries a list of candidate bases and prints everything
that distinguishes one failure from another: status, WWW-Authenticate, body, and
the rate-limit headers that only appear once you are actually talking to a real
Act! instance.

A 200 returns a bare JWT as text -- not JSON, no quotes.

Usage:
    set ACT_PASSWORD=...            (PowerShell: $env:ACT_PASSWORD = '...')
    python src/act_probe.py --user bladyman@eicatlanta.com --db EQUITYINVESTMENT
    python src/act_probe.py --user ... --db ... --base https://xyz-api.actcloud.com

THE PASSWORD IS NEVER PRINTED. Its length and a short fingerprint are, because
a password mangled in transit -- a `$` eaten by shell interpolation, a trailing
newline from a file read -- is invisible in every other way and produces exactly
the same 401 as a wrong one.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import sys

import requests

# Ordered most- to least-likely. The first two are Act!'s own regional hosts;
# the documentation host is included ONLY so its distinctive empty-bodied 401
# shows up next to the others, because that contrast is the whole diagnosis.
CANDIDATE_BASES = [
    "https://apius.act.com/act.web.api",
    "https://my.act.com/act.web.api",
    "https://apimta.act.com/act.web.api",      # documentation host -- expected to fail
]

TIMEOUT = 30


def fingerprint(secret: str) -> str:
    """Enough to tell two passwords apart, not enough to be one."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8]


def describe_password(pw: str) -> None:
    print("password check")
    print(f"  length      {len(pw)}")
    print(f"  fingerprint {fingerprint(pw)}")
    # The failure modes that produce a silent 401 and nothing else.
    if pw != pw.strip():
        print("  [!] LEADING OR TRAILING WHITESPACE -- almost always a stray newline "
              "from a file or a copy-paste")
    if "$" in pw:
        print("  [*] contains '$' -- confirm the length above matches what you typed. "
              "In PowerShell, \"pa$$word\" interpolates; 'pa$$word' does not.")
    if not pw:
        print("  [!] EMPTY. Set ACT_PASSWORD in the environment.")
    print()


def try_base(base: str, user: str, db: str, password: str) -> bool:
    """Attempt /authorize against one base URL. True on success."""
    url = f"{base.rstrip('/')}/authorize"
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    headers = {"Authorization": f"Basic {token}", "Act-Database-Name": db}

    print(f"--- {url}")
    try:
        r = requests.get(url, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as e:
        print(f"    NO RESPONSE  {type(e).__name__}: {e}\n")
        return False

    body = (r.text or "").strip()
    print(f"    status       {r.status_code}")
    print(f"    content-type {r.headers.get('Content-Type', '(none)')}")
    if r.headers.get("WWW-Authenticate"):
        print(f"    www-auth     {r.headers['WWW-Authenticate']}")
    # These only appear on a genuine Act! instance, so their presence says the
    # host is real even when the credentials are refused -- which is exactly the
    # distinction a bare status code hides.
    for h in ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset"):
        if r.headers.get(h):
            print(f"    {h.lower():<12} {r.headers[h]}")
    print(f"    body         {body[:200] or '(empty)'}")

    if r.status_code == 200 and body:
        print("\n    *** SUCCESS ***")
        print(f"    token starts {body[:24]}...")
        print(f"    token length {len(body)}")
        print("\n    Use it as:  Authorization: Bearer <token>")
        print(f"    Base URL:   {base.rstrip('/')}")
        return True

    if r.status_code == 401 and not body and not r.headers.get("Content-Type"):
        print("    -> empty 401 with no content type: this is the documentation host,")
        print("       not an instance holding your database. Not a credentials problem.")
    elif r.status_code == 401:
        print("    -> a real instance refused these credentials, OR does not hold a")
        print(f"       database called {db!r}. Both look like this.")
    elif r.status_code == 404:
        print("    -> wrong path or wrong tenant for this host.")
    print()
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--user", required=True, help="Act! user name (often the email)")
    ap.add_argument("--db", required=True,
                    help="database name EXACTLY as it appears in the top box of the "
                         "Act! Premium Cloud login screen")
    ap.add_argument("--base", action="append", default=[],
                    help="extra base URL to try, e.g. https://xyz-api.actcloud.com/act.web.api "
                         "(repeatable; tried before the built-in candidates)")
    args = ap.parse_args()

    password = os.environ.get("ACT_PASSWORD", "")
    if not password:
        print("ACT_PASSWORD is not set in the environment.\n"
              "  PowerShell:  $env:ACT_PASSWORD = 'your password in SINGLE quotes'\n"
              "  cmd:         set ACT_PASSWORD=your password")
        sys.exit(2)

    describe_password(password)
    print(f"user     {args.user}")
    print(f"database {args.db}\n")

    for base in args.base + CANDIDATE_BASES:
        if try_base(base, args.user, args.db, password):
            sys.exit(0)

    print("No endpoint accepted these credentials.")
    print()
    print("The base URL is the most likely culprit, and it is not guessable from here.")
    print("Log into Act! in a browser and look at the address bar: for Act! Premium")
    print("Cloud the API lives at  https://{server}/{customer}-api/act.web.api  --")
    print("the same server, with '-api' appended to the customer segment. Pass it")
    print("with --base. If that 401s too, the API may simply not be enabled on the")
    print("account, which only Act! support can turn on.")
    sys.exit(1)


if __name__ == "__main__":
    main()
