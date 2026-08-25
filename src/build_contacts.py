"""CRM + scraped rosters -> webapp/data/contacts.json, keyed on advisor CRD.

Replaces the 16-person trial file from export_contacts.py with the real thing:
~47k CRM records and ~100k scraped roster records, matched to SEC advisor CRDs
so the map can show a phone, an email, a team and that team's assets on the
person a rep just clicked.

WHY THE CRM IS THE SPINE AND THE ROSTERS ARE THE REACH
-----------------------------------------------------
They overlap on only 19% of email addresses, so neither replaces the other:

    CRM only      relationships EIC already owns, with an owner's initials
    roster only   ~80k people EIC has never contacted
    both          the CRM record wins on contact detail, and says who owns it

The CRM is also the ONLY source that knows whose relationship a contact is.
Showing a rep a "new prospect" who is in fact a colleague's client of nine
years is the single most damaging thing this panel could do, so `owner` is
carried on every CRM-sourced record and rendered.

FIRM CRD COMES FROM THE EMAIL DOMAIN, NOT THE COMPANY NAME
----------------------------------------------------------
The CRM has no CRD and its `Company` is free text -- "Wells Fargo Advisors,
LLC" and "Wells Fargo Advisors Financial Network" are two CRDs (19616, 11025)
and three spellings. The email domain is unambiguous and covers 88% of rows.

The map from domain to CRD is DERIVED, not typed: every scraped roster has a
known CRD and a column of that firm's email addresses, so the rosters
themselves say that lpl.com means 6413. That covers 64% of CRM addresses on
its own. EXTRA_DOMAINS below adds only the firms whose scrape returned no
email column at all -- the gap the CRM exists to fill.

MATCHING REUSES THE CALIBRATED MATCHER
--------------------------------------
forbes_match.py already solved "published name -> CRD" and was calibrated
against 1,303 labelled pairs, including the finding that namesake count -- not
score -- predicts false positives. That index and its name scoring are reused
here rather than reimplemented. One thing changes: a contact carries a firm
CRD, which is a far harder signal than the fuzzy firm NAME Forbes gives, so
the firm term is exact-match and weighted accordingly.

Every row keeps its `tier`, and the panel renders it. A `review` contact is
shown as unconfirmed rather than hidden, because a rep who can see "we think
this is them" makes a better decision than one shown nothing.

TEAM ASSETS ARE STORED ON THE TEAM, NEVER COPIED TO THE PERSON
--------------------------------------------------------------
The CRM ascribes a team's assets to every member: 623 asset values are shared
by 1,451 contacts, which overstates the book by $2.87B (10.9%) if summed. A
team is identified as (company, city, asset value) -- value alone is wrong,
because 71 shared values span unrelated firms and cities and are coincidences,
not teams. Members reference a team id; the amount is stored once, on the team.
Nothing downstream can double count it by summing a column.

Run:  python src/build_contacts.py [--limit N] [--report]
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import math
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import pandas as pd

from nicknames import same_person

from firm_rosters import FIRMS
from web_assets import write_json_gz
from forbes_match import (ACCEPT, MARGIN, NICKNAMES, W_SUFFIX, build_index,
                          load_reference, name_score, norm, split_name,
                          suffix_agreement, suffix_of)

ROOT = pathlib.Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
ROSTERS = RAW / "firm_rosters"
OUT = ROOT / "webapp" / "data"
CRM_GLOB = str(RAW / "CRM_Contacts_*.xlsx")

# Firm CRD is exact here (from a domain), not a fuzzy name match, so it earns
# more weight than in forbes_match and city earns less -- a CRM address is the
# advisor's mailing address, which is often a branch the SEC files elsewhere.
W_NAME, W_FIRM, W_CITY, W_STATE = 0.45, 0.30, 0.10, 0.15

# NAMESAKE_CAP is 3 in forbes_match and is deliberately NOT reused here.
#
# contact_calibrate.py measured it against 617 contact rows whose true CRD
# Barron's publishes. Of the rows the cap ALONE rejects -- clearing both the
# score and the margin gate -- 95 of 95 were correct. Precision is 99.8% at cap
# 3 and 99.8% with no cap at all, while recall runs 70.8% -> 86.1%. The cap was
# costing a fifth of the recall and buying nothing.
#
# It is redundant with MARGIN, and the sweep shows why. The cap fires when many
# advisors share a surname. But if those namesakes are genuinely
# indistinguishable they score alike, the runner-up gap collapses, and MARGIN
# already rejects them -- MARGIN at 0.0 is the one setting where precision
# actually falls (97.6%). If instead one of them wins by more than MARGIN, some
# real evidence separated them: 91 of those 95 rows carry an exact firm CRD
# from an email domain, and their minimum gap is 0.143. "Smith" being a common
# name is not a reason to discard an exactly-corroborated match.
#
# Kept as a finite number rather than removed, so a surname with hundreds of
# holders still has to clear something, and so the gate stays visible.
CONTACT_NAMESAKE_CAP = 25

# Only firms whose scrape produced NO email column, so the derived map cannot
# see them. Each CRD was looked up in firms_scored.parquet, not recalled.
EXTRA_DOMAINS = {
    "ubs.com": "8174",
    "wellsfargoadvisors.com": "19616",
    "wellsfargo.com": "19616",
    "wfadvisors.com": "19616",
    "wfafinet.com": "11025",          # FiNet is the independent channel, a separate CRD
    "stifel.com": "793",
    "schwab.com": "5393",
    "hightoweradvisors.com": "145323",
    "rockco.com": "291361",
    "benjaminfedwards.com": "146936",
    "orion.com": "107975",
    "stewardpartners.com": "283004",
    "stephens.com": "3496",
    "ms.com": "149777",               # Morgan Stanley institutional domain
}
# Deliberately NOT mapped: ustrust.com (U.S. Trust folded into BofA Private
# Bank and files no ADV of its own) and kisinvestments.com (280 CRM contacts,
# no matching filer found). Guessing a CRD to raise a coverage number would
# put contacts on the wrong firm's pin.

# PER-ROSTER COLUMN MAP
# ---------------------
# The generic guess-list below (pick("city", "office_city", "branch_city")...)
# works for 22 of 28 files and fails SILENTLY on the rest, because a column
# that is merely absent produces an empty string rather than an error. An audit
# of all 28 files found six rosters -- 36,559 rows, a fifth of the data --
# matching with NO city and NO state, which is 0.25 of the match score
# (W_CITY + W_STATE) thrown away:
#
#   edward_jones  19,657   faCity / faState        (camelCase, never matched)
#   wells_fargo    8,925   parsed from `address`   (no city column at all)
#   ubs            5,589   City ok, state in Region
#   rbc            1,853   no geography published
#   wealthspire      382   no geography published
#   chevy_chase      133   no geography published
#
# So the map is explicit per file and the generic list is only a fallback.
# Anything named here that is missing from the file is reported by
# --audit-columns rather than silently coalescing to "".
ROSTER_COLUMNS = {
    # base_url is not a column: faUrl is site-relative ("/us-en/financial-
    # advisor/paul-harrison") and without a host every profile button on the
    # panel pointed at whatever server was serving the map.
    "edward_jones": {"city": ["faCity"], "state": ["faState"],
                     # practice_name is faBranchTeam's team_name, unpacked by
                     # the scraper. faBranchTeam stays as the fallback so an
                     # older roster written before that column existed still
                     # finds its practices.
                     "name": ["faName"], "team": ["practice_name", "faBranchTeam"],
                     "profile_url": ["faUrl"], "emails": ["emails"],
                     "base_url": "https://www.edwardjones.com"},
    "ubs": {"city": ["City"], "state": ["Region"], "name": ["MarketingName"],
            "title": ["RankTitle", "JobTitle"], "team": ["TeamSiteNames"],
            "team_url": ["TeamSiteUrls"],
            "phone": ["LocalNumber"], "profile_url": ["Url"],
            "linkedin": ["LinkedInUrl"], "emails": ["Emails"]},
    "wells_fargo": {"address": ["address"], "profile_url": ["url"],
                    "emails": ["emails"], "phone": ["phone_numbers"]},
    "rbc": {"team": ["team_name"], "profile_url": ["profile_url"],
            # team_name is NOT a title. It was reaching the panel's title slot
            # through the generic list's `team_name` fallback, so every RBC
            # advisor was captioned with their team's name where a job title
            # belongs. Pinned to nothing so the title stays empty and honest.
            "title": []},
    "wealthspire": {"phone": ["phone_base"], "phone_ext": ["phone_ext"],
                    "profile_url": ["profile_url"]},
    "chevy_chase_trust": {"profile_url": ["profile_url"]},
    "cetera": {"crd": ["advisor_crd"]},
    "morgan_stanley": {"team": ["Team Name"], "profile_url": ["Profile URL"]},
    "merrill": {"team": ["Team Name"], "team_url": ["Team Site"],
                "profile_url": ["Profile URL"]},
    "raymond_james": {"team": ["team_name"], "profile_url": ["advisor_profile_url"],
                      "website": ["website_url"]},
    "ameriprise": {"team": ["team_name"], "profile_url": ["profile_url"]},
    "baird": {"team": ["team_name"], "profile_url": ["website_url"]},
    "janney": {"team": ["team_name"], "profile_url": ["profile_url"],
               "linkedin": ["linkedin"]},
    "citi": {"team": ["team_name"], "profile_url": ["profile_url"]},
    "truist": {"team": ["team_title"], "profile_url": ["profile_url"]},
    "captrust": {"team": ["group_team"], "profile_url": ["profile_url"],
                 "linkedin": ["linkedin"]},
    "mariner": {"profile_url": ["profile_url"], "linkedin": ["linkedin"]},
    # Stifel publishes no team field. Its `website` IS the practice -- 798
    # advisors share 290 sites -- so the domain is the grouping key and, plainly,
    # the label too. See team_from_website() in src/stifel_async.py for why it
    # is not prettified into a name.
    "stifel": {"team": ["team_name"], "team_url": ["website"],
               "profile_url": ["profile_url"], "linkedin": ["linkedin"]},
    "northwestern_mutual": {"profile_url": ["website"], "linkedin": ["linkedin"]},
    "mercer": {"profile_url": ["profile_url"]},
    "lpl": {"profile_url": ["Website URL"]},
    "focus_partners": {"profile_url": ["profile_url"]},
    "ep_wealth": {"profile_url": ["profile_url"]},
    "corient": {},
    "waverly": {"profile_url": ["profile_url"]},
    "red_door": {"profile_url": ["profile_url"]},
    # The old DBA sweep had no name column and a `practice` column holding page
    # titles ("Home"). src/sanctuary_async.py reads Sanctuary's own partner
    # directory instead, so `firm` is the real practice and `name` is populated.
    # `name_confidence` is confirmed or probable -- see that script for what
    # each means. Everything is loaded; filtering on it is a separate decision.
    "sanctuary": {"team": ["firm"], "team_url": ["domain"], "name": ["name"],
                  "email": ["email"],
                  "phone": ["phone"], "city": ["city"], "state": ["state"],
                  "profile_url": ["source_page"]},
    "rj_branches": {},
}

TOLLFREE = {"800", "833", "844", "855", "866", "877", "888"}
# What the panel is allowed to call a direct line. Anything else reaches a
# front desk and must be labelled so.
REACHES_PERSON = {"direct", "extension"}


def digits_only(raw) -> str:
    d = re.sub(r"\D", "", str(raw or ""))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d


def e164(raw) -> str:
    """US 10-digit -> +1XXXXXXXXXX. Anything else returns empty rather than a
    guess: a malformed tel: link dials something, and not the person shown."""
    d = digits_only(raw)
    return f"+1{d}" if len(d) == 10 and d[0] not in "01" else ""


def pretty(raw) -> str:
    d = digits_only(raw)
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else ""


# The lookarounds are load-bearing, and so is the optional country code.
# Without them "+14048422322" matches from the leading 1 and yields
# "1404842232" -- ten digits that look dialable and are WRONG, missing the
# last two. That is Morgan Stanley's exact format on 12,315 rows, so the bug
# would have produced a working call to the wrong person, not an error.
_PHONE = (r"(?<!\d)(?:\+?1[\s.\- ]?)?\(?\d{3}\)?[\s.\- ]*"
          r"\d{3}[\s.\- ]*\d{4}(?!\d)")
LABELLED_PHONE = re.compile(r"([A-Za-z][A-Za-z \-]*?)\s*Phone\s*:\s*(" + _PHONE + ")", re.I)
ANY_PHONE = re.compile(_PHONE)


def first_number(raw) -> str:
    """One dialable number out of whatever the source put in the cell.

    Wells Fargo packs several into one string, labelled and separated by
    semicolons, using non-breaking spaces:

        "(469) 296-0055; Local Phone:(469) 296-0055;
         Toll-Free Phone:(866) 281-7436; (866) 281-7436"

    Handing that to a tel: link dials nothing. A LOCAL number is preferred
    over a toll-free one because toll-free routes to a queue, which is the
    same office-versus-person distinction the panel labels everywhere else.
    """
    text = str(raw or "").replace(" ", " ").strip()
    if not text or text.lower() == "nan":
        return ""
    labelled = {m.group(1).strip().lower(): m.group(2)
                for m in LABELLED_PHONE.finditer(text)}
    for want in ("direct", "local", "office", "main", "branch"):
        if want in labelled:
            return labelled[want]
    # digits_only strips a leading country code first; a raw [:3] slice reads
    # "+18004001177" as area code "180" and misses that it is toll-free.
    unlabelled = [n for n in ANY_PHONE.findall(text)
                  if digits_only(n)[:3] not in TOLLFREE]
    if unlabelled:
        return unlabelled[0]
    hit = ANY_PHONE.search(text)
    return hit.group(0) if hit else ""


# Professional designations that trail a published name. Deliberately a list
# of KNOWN credentials rather than "anything after the first comma": "Smith,
# John" and "Chan, Wai Lin" are real name orders, and a blanket comma rule
# would destroy them.
DESIGNATIONS = {
    "cfa", "cfp", "cpa", "cima", "chfc", "clu", "aif", "aifa", "cdfa", "cpfa",
    "cpwa", "crpc", "crps", "crpsi", "aams", "accredited", "citp", "ckA",
    "qka", "qpa", "qpfc", "cebs", "cfs", "cltc", "cmfc", "cpc", "crc", "csa",
    "caia", "cbe", "cfe", "cfp®", "chsnc", "ea", "jd", "llm", "ll", "m",
    "mba", "ms", "msa", "msf", "mst", "phd", "rICP", "ricp", "wmcp", "bfa",
    "cap", "afc", "shrmscp", "shrmcp", "sphr", "phr", "cpm", "clf", "fpqp",
    "iaccp", "cippus", "cipp", "cams", "frm", "cfs®", "abv", "pfs", "cva",
    "cexp", "cep", "ctfa", "cwsi", "cws", "aep", "rma", "awma", "wms",
    "apma", "crps", "planner", "cpfa", "cima", "chfc", "cfs", "clu",
}
SUFFIX_WORDS = {"jr", "sr", "ii", "iii", "iv", "v", "vi", "esq"}
_DESIG_CLEAN = re.compile(r"[^a-z]")
# The trademark and service-mark symbols do not survive the trip: AWMA(TM)
# arrives as the letters "AWMATM" and CPFA(TM) as "CPFATM". Stripped before the
# membership test rather than enumerated -- listing every marked variant is a
# list that goes stale, and a credential that fails the test becomes the
# SURNAME. "Lynn Shaw, AWMATM" gated on "awmatm", matched nobody, and his
# direct line never reached the map. 168 rows across the rosters did this.
_MARK = re.compile(r"(?:tm|sm|r)$")


def strip_designations(raw: str) -> str:
    """'Cheryl Bicknell, JD, LL.M.' -> 'Cheryl Bicknell'.

    THIS IS LOAD-BEARING FOR MATCHING. split_name() treats the last token as
    the surname, so a trailing credential BECOMES the surname -- "Adam Corder,
    FPQP" gates on "fpqp", which exists nowhere in the SEC index, and the row
    silently matches nobody. It cost Mariner 73% of its roster, Chevy Chase
    54% and Wealthspire 52% before it was found: thousands of contacts dropped
    with no error, because "no match" is a legitimate outcome and looks
    identical to this bug.

    A genuine suffix (Jr, III) is KEPT -- forbes_match already handles those
    and they help disambiguate a father and son at the same firm.
    """
    text = str(raw or "").strip()
    if "," not in text:
        return text
    head, *tail = [part.strip() for part in text.split(",")]
    kept = []
    for part in tail:
        token = _DESIG_CLEAN.sub("", part.lower())
        if not token:
            continue
        # Test the bare token AND the token with a trailing mark removed, so
        # "awmatm" is recognised as "awma". Only strip when it actually helps:
        # bare "sm" or "tm" would otherwise reduce to nothing.
        unmarked = _MARK.sub("", token)
        if token in DESIGNATIONS or (len(unmarked) >= 2 and unmarked in DESIGNATIONS):
            continue                       # a credential: drop it
        # Everything else is KEPT, including tokens we do not recognise. Only
        # a known designation is ever removed. Dropping unknown short tokens
        # looked tidier and silently mangled "Smith, John" into "Smith" -- a
        # real "Last, First" ordering, which some CRM exports use. Keeping an
        # odd trailing word costs nothing; deleting a given name costs the row.
        kept.append(part)
    return ", ".join([head] + kept) if kept else head


def clean_email(raw) -> str:
    e = str(raw or "").strip().lower()
    return e if re.match(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", e) else ""


def derive_domain_map() -> dict[str, str]:
    """Which email domain belongs to which firm, learned from the rosters.

    A roster's CRD is known, so its email column IS the evidence. Only domains
    that point at ONE firm are kept: gmail.com appears under a dozen firms and
    means nothing, and a domain split across two CRDs cannot resolve a contact.
    """
    seen: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for slug, meta in FIRMS.items():
        files = sorted(glob.glob(str(ROSTERS / f"{slug}_*.csv")))
        if not files:
            continue
        frame = pd.read_csv(files[-1], low_memory=False)
        col = next((c for c in frame.columns if c.lower() in ("email", "e-mail")), None)
        if not col:
            continue
        for value in frame[col].dropna().astype(str):
            address = clean_email(value)
            if address:
                seen[address.split("@")[-1]][meta["crds"][0]] += 1

    mapping = {}
    for domain, counts in seen.items():
        crd, n = counts.most_common(1)[0]
        # A domain shared across firms is not evidence -- it is a free mailbox
        # provider or an aggregator, and either way it cannot name one firm.
        if len(counts) == 1 and n >= 2:
            mapping[domain] = crd
    mapping.update(EXTRA_DOMAINS)
    return mapping


# ---------------------------------------------------------------------------
# THE "DEAR" FIELD, which is the greeting a rep already chose.
#
# {{first_name}} in an email was rendered by splitting the DISPLAY name, so
# Christopher Tolman -- who is "Chris" to everybody at UBS and "Chris" in the
# Dear field of his own Act! record -- was greeted as Christopher.
#
# This is not derived and not guessed. `salutation` is populated on 47,419 of
# 47,466 Act! contacts and differs from the first name on 2,550 of them, which
# is exactly the set of people whose greeting we were getting wrong. It is not
# competing with the SEC filing: the filing says who somebody IS, and this says
# what they are called.
#
# The Excel export the rest of this module reads has no Dear column, so it is
# joined from the JSON API pull on email address. Not through the crosswalk --
# act_crosswalk.py imports score_contacts() from here, and depending on it back
# would be circular.
#
# WHAT IS REFUSED, and why the list is short: the field is clean. 111 of 47,419
# values are not a name, and 55 of those are a status somebody typed into the
# greeting -- RETIRED, RETRIED, LEFT INDUSTRY. "Dear RETIRED," is the one
# outcome worth writing code to prevent. Initials are KEPT: A.J., T.J. and J.P.
# are how those people are addressed, and an all-caps rule would have dropped
# them along with the statuses.
SALUTATION_STATUSES = {"retired", "retried", "left industry", "deceased",
                       "inactive", "do not contact", "do not mail", "no mail",
                       "unsubscribed", "none", "n/a", "na"}


def usable_salutation(value: str) -> str:
    """The Dear field when it is a greeting, or "" when it is something else."""
    v = " ".join(str(value or "").split())
    if not v or len(v) > 20 or len(v.split()) > 2:
        return ""
    if v.lower() in SALUTATION_STATUSES:
        return ""
    if re.search(r"[0-9@/\|;:]", v):
        return ""
    # Must contain a letter; "&" joins two people rather than naming one.
    if "&" in v or not re.search(r"[A-Za-z]", v):
        return ""
    return v


def salutation_by_email() -> dict:
    """email -> Dear field, from the newest Act! JSON pull."""
    files = sorted(glob.glob(str(RAW / "act_contacts_*.json")))
    if not files:
        print("[*] no Act! JSON pull; greetings fall back to the first name")
        return {}
    out = {}
    for row in json.loads(pathlib.Path(files[-1]).read_text(encoding="utf-8")):
        email = clean_email(row.get("emailAddress") or "")
        greeting = usable_salutation(row.get("salutation"))
        if email and greeting:
            out[email] = greeting
    return out


def load_crm(domains: dict[str, str]) -> pd.DataFrame:
    files = sorted(glob.glob(CRM_GLOB))
    if not files:
        print("[*] no CRM export found; rosters only")
        return pd.DataFrame()
    path = pathlib.Path(files[-1])
    frame = pd.read_excel(path).rename(columns=lambda c: str(c).strip())
    frame["email"] = frame.get("E-mail", "").map(clean_email)
    greetings = salutation_by_email()
    frame["salutation"] = frame["email"].map(lambda e: greetings.get(e, "") if e else "")
    named = int((frame["salutation"] != "").sum())
    print(f"[*] {named:,} CRM contacts carry a usable Dear field for the greeting")
    frame["firm_crd"] = frame["email"].map(
        lambda e: domains.get(e.split("@")[-1], "") if e else "")

    # A team is (company, city, asset value). Value alone is wrong: 71 shared
    # values span unrelated firms and cities and are coincidences, and $0
    # appears on 82 rows and is not an amount.
    assets = pd.to_numeric(frame.get("Total Assets"), errors="coerce")
    frame["assets"] = assets.where(assets > 0)
    frame["team_key"] = [
        f"{norm(str(c))}|{norm(str(t))}|{int(a)}"
        if pd.notna(a) and str(c).strip() and str(t).strip() else ""
        for c, t, a in zip(frame.get("Company", ""), frame.get("City", ""), frame["assets"])
    ]
    frame["source"] = "CRM"
    frame["source_file"] = path.name
    frame["name"] = ((frame.get("First Name", "").fillna("").astype(str) + " "
                      + frame.get("Last Name", "").fillna("").astype(str))
                     .str.strip().map(strip_designations))
    frame["owner"] = frame.get("EIC Contact", "").fillna("").astype(str).str.strip()
    frame["title"] = frame.get("Title", "").fillna("").astype(str).str.strip()
    frame["company"] = frame.get("Company", "").fillna("").astype(str).str.strip()
    frame["city"] = frame.get("City", "").fillna("").astype(str).str.strip()
    frame["state"] = frame.get("State", "").fillna("").astype(str).str.strip().str.upper()
    frame["phone"] = frame.get("Phone", "").fillna("").astype(str)
    frame["mobile"] = ""
    frame["phone_kind"] = ""        # derived below; the CRM does not say
    # Fields only the rosters publish. Declared here so the concat has one
    # schema and a missing column can never become the string "nan".
    for col in ("team", "profile_url", "linkedin", "phone_ext", "given_crd", "office"):
        frame[col] = ""
    print(f"[*] CRM {path.name}: {len(frame):,} rows, "
          f"{int((frame['email'] != '').sum()):,} emails, "
          f"{int((frame['firm_crd'] != '').sum()):,} resolved to a firm CRD "
          f"({(frame['firm_crd'] != '').mean():.0%})")
    return frame


def parse_email_list(raw) -> list:
    """One cell -> the addresses in it. JSON list, or delimited, or one."""
    text = str(raw or "").strip()
    if not text or text.lower() == "nan":
        return []
    if text.startswith("["):
        try:
            return [str(e).strip() for e in json.loads(text) if str(e).strip()]
        except (ValueError, TypeError):
            pass
    return [e.strip() for e in re.split(r"[;,\s]+", text) if "@" in e]


def own_email(name: str, candidates: list) -> str:
    """The address belonging to THIS person, out of the several a profile page
    may list.

    Edward Jones publishes a branch team on each advisor's page, so the page
    carries the addresses of everyone at that branch. The scraper stored the
    FIRST one, which produced 9,171 records -- 46.7% of the file -- captioned
    with a colleague's email: Paul Harrison's record shipped
    christopher.tavel@edwardjones.com while paul.harrison@edwardjones.com sat
    unused two elements away in the same list. UBS and Wells Fargo carry the
    same multi-address shape.

    This is worse than a blank. A rep who mails that address is writing to the
    wrong human, and nothing on the panel says so.

    SURNAME is the test, not position. Where no candidate carries this person's
    surname the answer is NO EMAIL -- returning a colleague's would be the bug
    this function exists to remove, and 351 Edward Jones rows genuinely have
    only a branch-mate's address on the page.
    """
    cands = [clean_email(e) for e in candidates]
    cands = [e for e in cands if e]
    if not cands:
        return ""
    if len(cands) == 1 and len(candidates) == 1:
        return cands[0]
    given, last = split_name(strip_designations(str(name or "")))
    if not last:
        return ""
    first = given[0] if given else ""
    def tokens(addr):
        return {norm(t) for t in re.split(r"[._\-]+", addr.split("@")[0]) if norm(t)}
    # Surname AND given name is the strongest signal and settles 245 of the 255
    # Edward Jones pages that list two people of the same surname -- because
    # those pages are mostly FAMILY. Patrick and Jordan Desamours, Mike and
    # Tyler Sesan, Bill and Zach Kaiser all share a branch.
    #
    # Where the given name does not match literally, the nickname table decides
    # before surname alone does. Falling straight through to surname-only sent
    # Tom Egan to sloane.egan@ while thomas.egan@ sat in the same list, and Jim
    # Zawacki to alex.zawacki@ over james.zawacki@ -- the Edward Jones bug this
    # function exists to fix, reproduced between siblings.
    forms = {first} | NICKNAMES.get(first, set()) if first else set()
    for addr in cands:
        tok = tokens(addr)
        if last in tok and forms & tok:
            return addr
    # An initial stands for the name: j.mitchell@ is James Mitchell.
    if first:
        for addr in cands:
            tok = tokens(addr)
            if last in tok and any(len(t) == 1 and t == first[0] for t in tok):
                return addr
    surname_only = [a for a in cands if last in tokens(a)]
    # Exactly one person of this surname on the page: it is them. Several, and
    # nothing distinguishes them -- so return NOTHING. Guessing here ships a
    # sibling's address, which is the same defect as shipping a colleague's.
    return surname_only[0] if len(surname_only) == 1 else ""


def absolute_url(raw, base: str = "") -> str:
    """A link the browser can actually follow.

    Three shapes reached the panel and 23,259 of them were broken:

      /us-en/financial-advisor/paul-harrison   Edward Jones, site-relative
      //advisors.ubs.com/john.allanson         UBS, protocol-relative
      www.livewellcapital.com                  Northwestern Mutual, bare host

    A site-relative path resolves against whatever host is serving the map, so
    every Edward Jones profile button pointed at localhost. The failure is
    invisible in the data -- the string is present and looks like a URL -- and
    only shows up when somebody clicks it.
    """
    url = str(raw or "").strip()
    if not url or url.lower() == "nan":
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return (base.rstrip("/") + url) if base else ""
    if re.match(r"https?://", url, re.I):
        return url
    # a bare host, with or without a path
    if re.match(r"[\w-]+(\.[\w-]+)+(/|$)", url):
        return "https://" + url
    return ""


def first_url(raw) -> str:
    """One URL out of a cell that may hold several.

    UBS separates multiple team sites with "!", not a comma or a semicolon, and
    an advisor on two teams is the common case there. The FIRST is the team the
    roster names first, which is the one the team name came from.
    """
    text = str(raw or "").strip()
    if not text or text.lower() == "nan":
        return ""
    for sep in ("!", ";", ","):
        if sep in text:
            text = text.split(sep)[0].strip()
            break
    return text


def team_name(raw) -> str:
    """A team's NAME out of whatever shape the roster stored it in.

    Edward Jones nests an object, and its "no team" value is the literal string
    "[]" -- which is truthy, so a plain read reported a team on all 19,657 rows
    when only 1,000 have one.
    """
    text = str(raw or "").strip()
    if not text or text in ("[]", "{}", "nan"):
        return ""
    if text[0] in "[{":
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return ""
        if isinstance(data, list):
            data = data[0] if data else {}
        if isinstance(data, dict):
            for key in ("team_name", "name", "title"):
                if data.get(key):
                    return str(data[key]).strip()
        return ""
    return text


def area_code_states(people: pd.DataFrame) -> dict:
    """area code -> state, LEARNED from the rows that carry both.

    RBC (1,853), Wealthspire (382) and Chevy Chase (133) publish no city and no
    state, so they were scoring 0 on both geography terms. All three publish a
    phone number, and a US area code sits inside one state almost without
    exception.

    Derived from this project's own data rather than a pasted NANP table, for
    the same reason the domain->CRD map is derived: a hardcoded list goes stale
    silently and nobody notices. 100k+ roster rows carry a phone AND a filed
    state, which is far more evidence than a table would be.

    A code is only used where it is DOMINANT -- 90% of at least 20 observations
    agree. Codes that straddle a border (201, 862) fail that test and are left
    out, so an ambiguous code contributes nothing instead of a coin flip.
    """
    have = people[(people["state"].str.fullmatch(r"[A-Z]{2}").fillna(False))
                  & (people["phone"] != "")]
    codes = have["phone"].map(lambda p: digits_only(p)[:3])
    tally = collections.defaultdict(collections.Counter)
    for code, st in zip(codes, have["state"]):
        if len(code) == 3 and code not in TOLLFREE:
            tally[code][st] += 1
    out = {}
    for code, counter in tally.items():
        total = sum(counter.values())
        state, n = counter.most_common(1)[0]
        if total >= 20 and n / total >= 0.90:
            out[code] = state
    return out


_STATE_ZIP = re.compile(r",?\s*([A-Za-z][A-Za-z .'\-]+?),?\s+([A-Z]{2})\s+\d{5}(?:-\d{4})?\s*$")


def city_state_from_address(raw) -> tuple[str, str]:
    """'880 S PRESTON RD STE 10 PROSPER, TX 75078' -> ('PROSPER', 'TX').

    Wells Fargo publishes no city or state column at all -- only a single
    free-text address -- so its 8,925 advisors were matching on name and firm
    alone. Anchored at the END of the string on the ZIP, which is the only
    reliably-shaped token in a US address; a looser parse turns
    "WARNER CENTER TOWERS; TOWER 1" into a city.
    """
    text = re.sub(r"\s+", " ", str(raw or "").strip())
    hit = _STATE_ZIP.search(text)
    if not hit:
        return "", ""
    city = re.sub(r"^.*?(?:;|\d{4,}\s)", "", hit.group(1)).strip()
    # a street suffix left in the city half means the parse ran into the street
    city = re.sub(r"^(?:STE|SUITE|UNIT|FL|FLOOR)\s+\S+\s+", "", city, flags=re.I)
    return (city.title() if city.isupper() else city).strip(), hit.group(2).upper()


EIC_CRD = "283930"          # Equity Investment Corporation
EIC_FILE = RAW / "EIC_Contacts.xlsx"


def load_eic() -> pd.DataFrame:
    """OUR OWN people.

    EIC_Contacts.xlsx holds the 16 EIC employees with a work line, a cell and
    an address. It was the source for the original 16-person trial file, and
    when this module replaced that trial nothing re-attached it -- so every EIC
    colleague who holds a CRD has been on the map since with no phone and no
    email. Mitchell Gentry (CRD 6502169) is one of them.

    They are colleagues, not prospects, and are labelled as such: `owner` means
    "an EIC rep owns this relationship", which is nonsense for an EIC employee,
    so it stays empty and the source line says where the record came from.
    """
    if not EIC_FILE.exists():
        print("[*] no EIC_Contacts.xlsx; EIC staff will have no contact detail")
        return pd.DataFrame()
    frame = pd.read_excel(EIC_FILE, dtype=str).rename(columns=lambda c: str(c).strip())
    frame = frame.fillna("")
    rec = pd.DataFrame({
        "name": frame.get("Name", "").astype(str).str.strip().map(strip_designations),
        "title": "",
        "team": "",
        "profile_url": "",
        "linkedin": "",
        "email": frame.get("Email", "").map(clean_email),
        "phone": frame.get("Work Number", "").map(first_number),
        "phone_ext": "",
        "mobile": frame.get("Cell Number", "").map(first_number),
        # Stated, not inferred. These are per-person direct dials from our own
        # directory -- the one source in this project where we do not have to
        # reason about what a number reaches.
        "phone_kind": "direct",
        "city": "Atlanta",
        "state": "GA",
        "office": "",
        "company": "Equity Investment Corporation",
        "owner": "",
        "assets": pd.NA,
        "team_key": "",
        "firm_crd": EIC_CRD,
        "given_crd": "",
        "source": "EIC",
        "source_file": EIC_FILE.name,
    })
    rec = rec[rec["name"].str.strip() != ""]
    print(f"[*] EIC {EIC_FILE.name}: {len(rec)} colleagues, "
          f"{int((rec['email'] != '').sum())} emails, "
          f"{int((rec['phone'] != '').sum())} work lines, "
          f"{int((rec['mobile'] != '').sum())} mobiles")
    return rec


def load_rosters() -> pd.DataFrame:
    """Every scraped roster, normalised onto the CRM's column names."""
    out = []
    for slug, meta in FIRMS.items():
        files = sorted(glob.glob(str(ROSTERS / f"{slug}_*.csv")))
        if not files:
            continue
        path = pathlib.Path(files[-1])
        # dtype=str is load-bearing. Without it pandas reads an all-numeric
        # phone column as float64, so Morgan Stanley's "+14048422322" becomes
        # 14048422322.0 and stringifies with a trailing ".0" -- eleven digits
        # that no longer look like a US number, and the row silently loses its
        # phone. Nothing errors; the column is simply empty downstream.
        frame = pd.read_csv(path, low_memory=False, dtype=str).fillna("")
        # Normalise SPACES as well as case. These 26 files come from 26 sites
        # and spell the same field "name", "Display Name", "faName" and
        # "MarketingName". Matching on lowercase alone silently dropped Edward
        # Jones, Merrill, LPL and UBS -- 58,687 people, a third of the data,
        # with no error because an empty name column is not an exception.
        cols = {re.sub(r"[^a-z0-9]", "", c.lower()): c for c in frame.columns}
        overrides = ROSTER_COLUMNS.get(slug, {})

        def pick(*names, field=""):
            """Named columns for this field, then the generic guesses.

            An override wins outright, INCLUDING an empty list, which is how a
            file says "this field is genuinely not published here" and stops the
            generic list from finding something that merely looks similar --
            RBC's team_name landing in the title slot being the case in point.
            """
            named = overrides.get(field)
            if named is not None:
                if not named:
                    return pd.Series([""] * len(frame), index=frame.index, dtype=object)
                names = tuple(named) + tuple(names)
            return _coalesce(*names)

        def _coalesce(*names):
            """First named column that actually HAS DATA on this row.

            Coalescing row-wise, not picking one column for the whole file.
            The earlier version returned the first column that merely EXISTED,
            which is a different and wrong thing: Captrust ships an empty
            `direct_phone` alongside a populated `office_phone`, so all 834 of
            its numbers were dropped, and Mercer kept its 193 direct numbers
            while discarding 990 office ones. Preference order still decides
            -- a direct line beats an office line where both are present -- but
            an empty cell now falls through instead of winning.
            """
            out = pd.Series([""] * len(frame), index=frame.index, dtype=object)
            for n in names:
                key = re.sub(r"[^a-z0-9]", "", n.lower())
                if key not in cols:
                    continue
                col = frame[cols[key]].astype(str).str.strip()
                blank = out.eq("")
                out = out.mask(blank, col.where(col.str.lower().ne("nan"), ""))
                if not out.eq("").any():
                    break
            return out

        name = pick("name", "full", "full_name", "advisor_name", "faName",
                    "display_name", "marketing_name", field="name")
        if not name.str.strip().any():
            first, last = _coalesce("first_name", "first"), _coalesce("last_name", "last")
            name = (first + " " + last).str.strip()
        if not name.str.strip().any():
            # Loud, not silent: a roster that contributes nobody is a bug in
            # this loader, not a firm that employs no one.
            print(f"    [!] {slug}: no name column found in {list(frame.columns)[:8]}")
        # An address list is resolved against the person's own name; a single
        # address column is taken as given. own_email() returns "" rather than
        # a branch-mate's address when nothing matches.
        email_list = pick("emails", field="emails")
        if email_list.str.strip().any():
            email = pd.Series(
                [own_email(n, parse_email_list(raw))
                 for n, raw in zip(name, email_list)],
                index=frame.index, dtype=object)
        else:
            email = pick("email", "e-mail", field="email").map(clean_email)

        city = pick("city", "office_city", "branch_city", field="city").str.strip()
        state = (pick("state", "office_state", "branch_state", field="state")
                 .str.strip().str.upper())
        addr = pick(field="address") if "address" in overrides else None
        if addr is not None and addr.str.strip().any():
            parsed = [city_state_from_address(a) for a in addr]
            city = city.mask(city.eq(""), [c for c, _ in parsed])
            state = state.mask(state.eq(""), [s for _, s in parsed])
        # A two-letter code is a state; "Southeast" or "Great Lakes" is a sales
        # region and must not be scored as one. UBS's Region column holds both.
        state = state.where(state.str.fullmatch(r"[A-Z]{2}"), "")

        rec = pd.DataFrame({
            "name": name.map(strip_designations),
            # Seniority is what the sales team reads off this, so the ranked
            # title wins over the generic job title where a source has both.
            # team_name is NOT in this list any more. It was the last fallback,
            # so every RBC advisor -- whose file publishes no title at all --
            # was captioned with their team's name in the slot the sales team
            # reads for SENIORITY. A team is now carried in its own field.
            "title": pick("primary_title", "rank_title", "title", "job_title",
                          "role", "position", "title_display_option",
                          field="title").str.strip(),
            "team": pick("team_name", "team", "practice", "group_team",
                         field="team").map(team_name),
            # The team's OWN page, where a roster publishes one. Stored on the
            # practice rather than the person -- see the practices block below.
            "team_url": pick("team_site", "team_url", "team_site_urls",
                             field="team_url").map(
                lambda u: absolute_url(first_url(u), overrides.get("base_url", ""))),
            "profile_url": pick("profile_url", "website_url", "website", "url",
                                field="profile_url").map(
                lambda u: absolute_url(u, overrides.get("base_url", ""))),
            "linkedin": pick("linkedin", "linkedin_url", field="linkedin").map(
                absolute_url),
            "email": email,
            # Ordered best-to-worst by what the number REACHES, so a personal
            # line always wins over a branch line when a roster carries both.
            # Morgan Stanley's "Main Phone" is the advisor's own -- 12,271
            # distinct numbers over 12,315 rows -- while "Branch Phone" has
            # only 498 distinct, so the order here is measured, not assumed.
            "phone": pick("phone", "direct_phone", "main_phone",
                          "formatted_phone", "local_number", "phone_base",
                          "office_phone", "branch_phone", "phone_numbers",
                          field="phone").map(first_number),
            # Wealthspire publishes "410.988.9494 ext. 99001" and labels it
            # `extension`, which REACHES_PERSON treats as reaching a human. The
            # extension was being dropped and the bare switchboard number
            # shipped under a "Direct" button -- a call that lands on the front
            # desk while the panel promises otherwise. Carried so the tel: link
            # can dial it, and enforced in infer_phone_kind().
            "phone_ext": pick("phone_ext", "extension", "ext",
                              field="phone_ext").map(
                lambda v: re.sub(r"\D", "", str(v or ""))),
            "mobile": pick("mobile", "cell", "secondary_phone",
                           field="mobile").map(first_number),
            # The scraper that read the site already worked out whether the
            # number reaches a person or a front desk, by counting how many
            # colleagues share it. That verdict is better than anything this
            # loader could re-derive, so it is carried through, not recomputed.
            "phone_kind": pick("phone_kind", "direct_phone_kind",
                               field="phone_kind").str.strip().str.lower(),
            "city": city,
            "state": state,
            # WHICH DESK, not which town. City is too coarse to reason about
            # phones: Edward Jones runs ~19,000 one-advisor branches and several
            # share a city, so "3.8 advisors per city" describes four separate
            # storefronts, not a branch of four. A street address (or Raymond
            # James's branch_id) is the actual office, and infer_phone_kind
            # needs that grain to tell a direct dial from a small branch line.
            "office": (pick("branch_id", "address1", "address", "office_address",
                            "street_line_1", "office_street", "address_line1",
                            field="office").str.strip().str.lower()
                       .str.replace(r"[^a-z0-9]+", " ", regex=True).str.strip()
                       + "|" + _coalesce("postal", "postal_code", "zipcode",
                                         "zip", "fazipcode").str.strip()),
            "company": meta["label"],
            "owner": "",
            "assets": pd.NA,
            "team_key": "",
            "firm_crd": meta["crds"][0],
            "source": meta["label"],
            "source_file": path.name,
        })
        # A roster that publishes the ADVISOR's own CRD needs no matching at
        # all. Cetera does, on 9,767 of 9,785 rows, and we were fuzzy-matching
        # them anyway -- 1,538 of its people landed in the review tier carrying
        # a warning, while the file had stated the answer outright.
        rec["given_crd"] = ""
        crd_cols = overrides.get("crd")
        if crd_cols:
            given = _coalesce(*crd_cols).str.strip()
            rec["given_crd"] = given.where(given.str.fullmatch(r"\d{2,10}"), "")

        # A per-row channel column means the roster spans several CRDs.
        channel = meta.get("channel")
        if channel and channel["column"] in frame.columns:
            rec["firm_crd"] = (frame[channel["column"]].astype(str)
                               .map(channel["map"]).fillna(meta["crds"][0]))
        # IS THAT COLUMN ACTUALLY A TEAM?
        # A "team_name" is not always a practice. Baird's holds 150 values over
        # 1,591 advisors and they are BRANCH labels -- "Milwaukee", "Seattle".
        # Captrust's holds 14 and they are departments -- "Financial Advisors",
        # "Board of Directors". Rendering either as "Team Sarasota" tells a rep
        # something false about how the advisor is organised.
        #
        # Tested rather than listed, so a roster added later is judged the same
        # way. A real practice roster is mostly-distinct (Ameriprise: 2,890
        # names over 7,104 people; UBS 1,693 over 3,664) and does not simply
        # repeat the city.
        filled = rec["team"].str.strip().ne("")
        if filled.sum() >= 50:
            values = rec.loc[filled, "team"]
            variety = values.nunique() / filled.sum()
            echoes_city = (values.map(norm)
                           .eq(rec.loc[filled, "city"].map(norm))).mean()
            if variety < 0.15 or echoes_city > 0.5:
                what = "city labels" if echoes_city > 0.5 else "a department list"
                print(f"    [!] {slug}: 'team' column looks like {what} "
                      f"({values.nunique()} distinct over {int(filled.sum()):,} "
                      f"rows) -- dropped rather than shown as a practice")
                rec["team"] = ""

        rec = rec[rec["name"].str.strip() != ""]
        # Loud per-file accounting. Every defect this audit was written to
        # catch -- lost geography, a colleague's email, a team in the title
        # slot -- produced plausible output and no exception, so the only way
        # they surface is by being counted on every run.
        # base_url is a value, not a column list -- iterating it would audit
        # its individual characters as though each were a column name.
        missing = [f"{f}:{c}" for f, names in overrides.items()
                   if isinstance(names, list) for c in names
                   if re.sub(r"[^a-z0-9]", "", c.lower()) not in cols]
        print(f"    {slug:<22} {len(rec):>6,} rows  "
              f"email {int((rec['email'] != '').sum()):>6,}  "
              f"phone {int((rec['phone'] != '').sum()):>6,}  "
              f"city {int((rec['city'] != '').sum()):>6,}  "
              f"state {int((rec['state'] != '').sum()):>6,}  "
              f"title {int((rec['title'] != '').sum()):>6,}  "
              f"team {int((rec['team'] != '').sum()):>6,}"
              + (f"   [!] named column absent: {missing}" if missing else ""))
        out.append(rec)
    joined = pd.concat(out, ignore_index=True) if out else pd.DataFrame()
    print(f"[*] rosters: {len(joined):,} rows from {len(out)} firms, "
          f"{int((joined['email'] != '').sum()):,} emails")
    return joined


def email_name(address: str) -> tuple[list[str], str]:
    """('collie.krausnick@raymondjames.com') -> (['collie'], 'krausnick').

    THE FIRM'S OWN STATEMENT OF WHO THIS IS. Edward Carl Krausnick Jr goes by
    Collie: the branch page prints "Collie Krausnick", and "collie" appears in
    none of his SEC fields -- not first_name, not middle_name, not
    used_first_name, not a parenthetical. given_forms() therefore produces
    {carl, ed, eddie, edward, ned, ted} and the name scores 0.0, so his row
    matched nobody and his direct line never reached the map.

    The email local part carries the name he actually uses. It is only trusted
    where its SURNAME half agrees with the filed surname -- that agreement is
    what makes the given half evidence about this person rather than a guess.
    Corporate aliases (info@, service@) have no surname to agree with and fall
    away on their own.
    """
    local = str(address or "").split("@")[0].lower()
    parts = [norm(t) for t in re.split(r"[._\-]+", local) if norm(t)]
    if len(parts) < 2:
        return [], ""
    # trailing digits disambiguate namesakes (john.hammons3) -- not a surname
    parts = [re.sub(r"\d+$", "", t) or t for t in parts]
    return parts[:-1], parts[-1]


def resolve_surname(name: str, index) -> tuple[list[str], str]:
    """Parse a published name, using the SEC index to tell a credential from a
    surname instead of a hand-maintained list.

    DESIGNATIONS has now failed four times -- trailing credentials cost Mariner
    73% of its roster, "AWMATM" became Lynn Shaw's surname, and this pass found
    "cepa" (177 rows), "adpa", "rfc", "cipm", "flmi", "aiaa" doing the same.
    Every list of credentials goes stale, and the failure is silent: an
    unrecognised credential becomes the SURNAME and the row matches nobody.

    The index already knows the answer. Across 436,091 advisors it contains
    shaw, krausnick and smith but none of cepa, rfc, adpa, cipm or cpwa. So
    when a parsed surname is absent from it, drop the trailing comma-part and
    try again -- the data decides, and new credentials need no maintenance.

    Bounded to two retries so a genuinely unlisted surname (a support-staff
    member the SEC does not carry) still resolves to itself rather than being
    eaten one comma at a time.
    """
    given, last = split_name(strip_designations(name))
    if last in index or "," not in str(name):
        return given, last
    parts = [p for p in str(name).split(",") if p.strip()]
    for cut in (1, 2):
        if len(parts) <= cut:
            break
        trial_given, trial_last = split_name(strip_designations(",".join(parts[:-cut])))
        # Accept a trial that the index knows, and also one that merely
        # produces a surname where the original produced none: a stray "(R)"
        # left as its own token makes split_name return "", and an empty
        # surname gates on nothing at all.
        if trial_last and (trial_last in index or not last):
            return trial_given, trial_last
    return given, last


def score_contacts(people: pd.DataFrame, index) -> pd.DataFrame:
    """Contact -> advisor CRD, with the same refuse-when-unsure discipline as
    forbes_match: below threshold, ambiguous, or too many namesakes means the
    row keeps its contact detail but is not attached to a pin."""
    tiers, crds, scores, namesakes_out, gaps = [], [], [], [], []
    known_crds = {crd for entries in index.values() for crd, _, _ in entries}
    for rec in people.itertuples(index=False):
        # The roster stated this person's CRD. Nothing a name-similarity score
        # produces can be better evidence than the firm naming its own
        # advisor's registration number, so it is taken and not second-guessed
        # -- but only if the SEC actually carries that CRD, so a typo or a
        # retired registration falls through to matching instead of pinning a
        # contact to a CRD that exists nowhere.
        stated = str(getattr(rec, "given_crd", "") or "")
        if stated and stated in known_crds:
            tiers.append("confirmed")
            crds.append(stated)
            scores.append(1.0)
            namesakes_out.append(0)
            gaps.append(1.0)
            continue
        given, last = resolve_surname(rec.name, index)
        # A published nickname the filing never records is invisible to
        # given_forms(); the email local part is the one place the firm spells
        # it out. Used only as a FALLBACK and only when its surname half agrees
        # with the name we are already gating on.
        alt_given, alt_last = email_name(getattr(rec, "email", ""))
        # Scoring the email's given name against the SEC forms does NOT work --
        # that was the first attempt, and "collie" against {carl, ed, edward}
        # scores 0.0 for the same reason the published name did. The email is
        # not evidence that Collie EQUALS Edward. It is evidence that the firm
        # addresses this person as Collie Krausnick, and the SURNAME half is
        # what can be corroborated: it agrees with the filed surname we are
        # already gating on, at a firm we can check.
        use_alt = bool(alt_last) and alt_last == last
        scored = []
        for crd, forms, entry in index.get(last, []):
            n = name_score(given, forms)
            if n <= 0 and use_alt and rec.firm_crd and rec.firm_crd in entry["firms"]:
                # Surname and firm both agree and the filed given name simply
                # never records the nickname. Deliberately BELOW the 0.8 that
                # counts toward CONTACT_NAMESAKE_CAP: identifies a surname at a
                # firm, not which same-named colleague, so where several exist
                # they all score alike, tie, and are rejected as ambiguous.
                n = 0.60
            if n <= 0:
                continue
            f = 1.0 if rec.firm_crd and rec.firm_crd in entry["firms"] else 0.0
            c = 1.0 if rec.city and norm(rec.city) in entry["cities"] else 0.0
            s = 1.0 if rec.state and rec.state in entry["states"] else 0.0
            if not rec.firm_crd:
                # No firm to compare: renormalise so an unknown firm neither
                # helps nor penalises, exactly as forbes_match does.
                total = (W_NAME * n + W_CITY * c + W_STATE * s) / (W_NAME + W_CITY + W_STATE)
            else:
                total = W_NAME * n + W_FIRM * f + W_CITY * c + W_STATE * s
            # Lynn Shaw II and Lynn Shaw are both Raymond James Memphis, so
            # name, firm, city and state all agree perfectly. Without this the
            # son was unreachable and his email and direct line landed on his
            # father's CRD.
            total += W_SUFFIX * suffix_agreement(suffix_of(rec.name),
                                                 entry.get("suffix", ""))
            scored.append((total, crd, n))
        scored.sort(reverse=True)
        best = scored[0] if scored else None
        second = scored[1][0] if len(scored) > 1 else 0.0
        namesakes = sum(1 for row in scored if row[2] >= 0.8)
        ambiguous = bool(best) and (best[0] - second) < MARGIN
        if best and best[0] >= ACCEPT and not ambiguous and namesakes <= CONTACT_NAMESAKE_CAP:
            tier, crd, score = "high", best[1], best[0]
        elif best:
            tier, crd, score = "review", best[1], best[0]
        else:
            tier, crd, score = "none", "", 0.0
        tiers.append(tier)
        crds.append(crd)
        scores.append(round(score, 3))
        namesakes_out.append(namesakes)
        # How far clear of the runner-up. Retained because contact_calibrate
        # showed MARGIN, not ACCEPT, is what actually decides most rejections:
        # sweeping ACCEPT from 0.50 to 0.72 changed nothing at all.
        gaps.append(round(best[0] - second, 3) if best else 0.0)
    people = people.copy()
    people["advisor_crd"] = crds
    people["tier"] = tiers
    people["match_score"] = scores
    people["namesakes"] = namesakes_out
    people["match_gap"] = gaps
    return people


def infer_phone_kind(people: pd.DataFrame) -> pd.Series:
    """Fill in phone_kind where the source did not say.

    The CRM has no such column, so its numbers are classified the same way
    every scraper in this project does it -- by counting how many colleagues at
    the SAME FIRM share the number. That is evidence, not a guess about the
    shape of the number.

    Scoped to (firm, number) rather than the number alone. Two advisors at
    different firms sharing a 10-digit string is a data error or a coincidence,
    and pooling them would let one firm's switchboard mislabel another's.

    Whether "nobody else shares this number" is EVIDENCE depends on whether we
    hold the whole firm:

      roster source  a census of everyone the firm publishes, so a number used
                     once really is used once -> `direct`
      CRM source     a sample of the relationships EIC happens to have, so
                     being the only row at a branch means nothing -> `unverified`

    I previously held that uniqueness in a census was NOT enough, on the
    grounds that Edward Jones runs ~19,000 one-advisor branches whose phone a
    branch administrator answers, so the number was the OFFICE's rather than
    the advisor's. That reasoning was wrong, and Edward Jones' own file is what
    disproves it. Grouped by street address:

        14,063 offices, 9,486 of them (67%) holding exactly one advisor
        all 9,486 of those carry a number unique in the whole firm
        1,627 numbers are used by more than one advisor
        ZERO of those 1,627 span more than one address

    Nothing pools. Edward Jones has no switchboard gathering advisors from
    different offices, so a number used once reaches one advisor's office and
    nobody else's. Whether their assistant picks it up is not the distinction
    that matters to a rep: they asked for that advisor's office and that is
    what rings. It is categorically different from a Morgan Stanley branch line
    serving twenty-seven people, and the office-size gate I built around this
    belief was excluding the MOST person-specific numbers in the file.

    So uniqueness in a census is the test, and office size is irrelevant:

      direct      the SOURCE established it, OR nobody else in a census roster
                  uses this number
      unverified  a CRM singleton -- a sample, not a census, so being the only
                  row at a branch is evidence of nothing
      extension   a shared line plus a personal extension, and only where the
                  extension digits actually survived

    Only those are allowed to promise a person. `single-occupant` is NOT
    promoted: Captrust and EP Wealth state it themselves, and spot-checking
    against Captrust's own /locations/ pages confirmed the number is published
    for the OFFICE. Where the source has told us what a number is, that beats
    anything inferred here.
    """
    kind = people["phone_kind"].fillna("").astype(str).str.strip().str.lower()
    digits = people["phone"].map(digits_only)
    firms = people["firm_crd"].astype(str)
    is_census = people["source"].ne("CRM")

    # COUNT PEOPLE, NOT ROWS.
    #
    # An advisor in both the CRM and their firm's roster is TWO rows with ONE
    # number, and counting rows made that look like a line shared with a
    # colleague. Christopher Tolman's (212) 713-9576 is used by exactly one
    # advisor in the whole UBS roster and shipped labelled "shared" -- his own
    # second record was the colleague. Every advisor we hold twice was being
    # told their direct line reaches someone else.
    #
    # Identity is the email where there is one, because that is exact, and the
    # normalised name otherwise. Both are per-firm already, so a namesake at a
    # different firm cannot collide.
    ident = people["email"].astype(str).str.lower().str.strip()
    ident = ident.where(ident.ne(""), people["name"].map(norm))
    seen = collections.defaultdict(set)
    for f, d, who in zip(firms, digits, ident):
        if d:
            seen[(f, d)].add(who)
    counts = {key: len(who) for key, who in seen.items()}

    ext = people.get("phone_ext", pd.Series([""] * len(people), index=people.index))
    ext = ext.fillna("").astype(str)

    # WHICH OFFICES ARE REAL BRANCHES, from the data rather than from a belief
    # about how a firm is organised. An office qualifies when we hold at least
    # three of its people AND they do not all share one number -- that is the
    # shape of a staffed branch. A one-advisor Edward Jones storefront fails on
    # the first test and a small firm where everyone answers the same line
    # fails on the second, so neither gets its numbers promoted.
    offices = people.get("office", pd.Series([""] * len(people), index=people.index))
    offices = offices.fillna("").astype(str).str.strip("| ")
    seats = collections.defaultdict(set)
    heads = collections.Counter()
    for firm, office, d, census in zip(firms, offices, digits, is_census):
        if office and census:
            heads[(firm, office)] += 1
            if d:
                seats[(firm, office)].add(d)
    office_is_shared = {key: heads[key] >= 3 and len(seats[key]) >= 2
                        for key in heads}

    out = []
    for k, d, firm, census, x, office in zip(kind, digits, firms, is_census,
                                             ext, offices):
        if not d:
            out.append("")
        elif k == "extension" and not x:
            # The source said "extension" and the extension digits are gone.
            # What is left is the switchboard, and `extension` is in
            # REACHES_PERSON -- so this would print "Direct" over a number that
            # rings the front desk. Demoted to what it actually is.
            out.append("switchboard")
        elif k:
            out.append(k)                       # the scraper already worked it out
        elif d[:3] in TOLLFREE:
            out.append("toll-free")
        else:
            n = counts[(firm, d)]
            if n > 1:
                out.append("shared" if n <= 5 else "switchboard")
            elif not census:
                out.append("unverified")
            else:
                # Unique in a census roster. That is a direct line, and the
                # office it sits in is irrelevant -- see the note above.
                out.append("direct")
    return pd.Series(out, index=people.index)


# What a number actually reaches, best first. Used to decide which of two
# numbers for the same person wins.
REACH_RANK = {"direct": 0, "extension": 1, "single-occupant": 2, "sole-use": 3,
              "shared": 4, "switchboard": 5, "toll-free": 6, "unverified": 7,
              "": 8}


def donate_by_email(people: pd.DataFrame) -> pd.DataFrame:
    """Move the best number each PERSON has onto every row that is them.

    A corporate email address is a unique key for one human, and it is the only
    such key we hold that does not depend on the CRD match succeeding. Raymond
    James is the clear case: 2,303 advisors appear in both the main roster and
    a branch page, and on 1,527 of them the branch page published a direct dial
    while the main roster row carried a switchboard. Both rows are the same
    person and one of them knew the better number.

    This runs BEFORE matching, deliberately. `pick_best` already donates across
    rows that resolved to the same CRD, but that only helps where both rows
    matched -- and a row whose only useful fact is a direct line is exactly the
    kind that fails to match. Keying on the email instead moves the number onto
    the row with the better name, city and title, which is the row most likely
    to match at all.

    Generic mailboxes are excluded: info@ and service@ are not a person, and
    pooling their numbers would spread one switchboard across a whole firm.
    """
    email = people["email"].astype(str).str.lower().str.strip()
    local = email.str.split("@").str[0]
    personal = email.ne("") & people["phone"].ne("") & local.str.contains(r"[._\-]")
    if not personal.any():
        return people

    rank = people["phone_kind"].map(lambda k: REACH_RANK.get(str(k), 8))
    best: dict = {}
    for em, r, ph, kd, ex, src in zip(email[personal], rank[personal],
                                      people.loc[personal, "phone"],
                                      people.loc[personal, "phone_kind"],
                                      people.loc[personal, "phone_ext"],
                                      people.loc[personal, "source"]):
        if em not in best or r < best[em][0]:
            best[em] = (r, ph, kd, ex, src)

    moved = 0
    phone, kind, ext, frm = (people["phone"].copy(), people["phone_kind"].copy(),
                             people["phone_ext"].copy(),
                             pd.Series([""] * len(people), index=people.index))
    for i, em, r in zip(people.index[personal], email[personal], rank[personal]):
        hit = best.get(em)
        if hit and hit[0] < r:
            phone.at[i], kind.at[i], ext.at[i] = hit[1], hit[2], hit[3]
            frm.at[i] = hit[4]
            moved += 1
    people = people.copy()
    people["phone"], people["phone_kind"], people["phone_ext"] = phone, kind, ext
    # Where the number came from, so the panel can say so rather than implying
    # the roster it is displaying published it.
    people["phone_from"] = frm
    print(f"[*] email-keyed phone donation: {moved:,} rows given a better number "
          f"held under the same address by another source")
    return people


def team_url_of(group: pd.DataFrame) -> str:
    """The team's own page, from whichever row in this CRD's group has one."""
    if "team_url" not in group.columns:
        return ""
    for value in group["team_url"]:
        text = str(value or "").strip()
        if text and text.lower() != "nan":
            return text[:200]
    return ""


def filed_given_names() -> dict:
    """CRD -> every given name the SEC has on file, lower-cased.

    Used to CORROBORATE the CRM's Dear field before it is allowed to become a
    greeting. See salutation_of() for why that check is not optional.
    """
    try:
        adv = pd.read_parquet(ROOT / "data" / "interim" / "advisors.parquet",
                              columns=["advisor_crd", "first_name", "middle_name",
                                       "used_first_name"])
    except FileNotFoundError:
        return {}
    out = {}
    for r in adv.itertuples(index=False):
        toks = []
        for t in (r.first_name, r.middle_name, r.used_first_name):
            if isinstance(t, str):
                toks.extend(w.lower() for w in t.split() if w.strip())
        if toks:
            out[str(r.advisor_crd)] = toks
    return out


def salutation_of(group: pd.DataFrame) -> str:
    """The Dear field from whichever row in this CRD's group carries one.

    Read from the GROUP, not from pick_best's winner, for the same reason
    team_url_of exists: when a roster row wins on contact detail it has no Dear
    field at all, and reading it off the winner would lose the greeting for
    exactly the advisors the CRM knows best.
    """
    if "salutation" not in group.columns:
        return ""
    for value in group["salutation"]:
        text = str(value or "").strip()
        if text and text.lower() != "nan":
            return text[:20]
    return ""


def pick_best(group: pd.DataFrame) -> pd.Series:
    """Which record wins when several contacts resolve to the same CRD.

    SCORE FIRST, source second. An earlier version preferred a CRM row and
    otherwise took whichever row happened to sort first, and that is wrong in
    a way that is easy to miss: CRD 7225800 is Christopher Williamson of Red
    Door, matched at 1.00, but the card showed Casey Williamson of Northwestern
    Mutual, matched at 0.203, purely because that row came first. A weak match
    must never displace a strong one -- it puts a real name, a real email and a
    real phone on the wrong person's pin, which is worse than showing nothing.

    Source is the TIEBREAK, applied only among rows within MARGIN of the best
    score, because a CRM record is worth preferring for its ownership data --
    not for being a CRM record.
    """
    ranked = group.sort_values("match_score", ascending=False)
    top = float(ranked["match_score"].iloc[0])
    close = ranked[ranked["match_score"] >= top - MARGIN]
    crm = close[close["source"] == "CRM"]
    pool = crm if len(crm) else close
    if len(pool) > 1:
        # Among equally-good matches, prefer the record that actually reaches a
        # PERSON. Two sources can describe the same advisor with different
        # quality: the Raymond James advisor-search API gives Field Norris the
        # branch switchboard, while the branch team page gives his direct line.
        # Without this, whichever sorted first won, and the better number was
        # discarded silently.
        better = pool[pool["phone_kind"].isin(REACHES_PERSON)]
        if len(better):
            pool = better
    best = pool.iloc[0].copy()

    # A record can lose the identity contest and still hold the better PHONE.
    # Collie Krausnick is filed as Edward: the CRM row matches his filed name
    # at 1.00 and carries a SHARED number, while the branch-page row matches at
    # 0.82 via the email fallback and carries his direct line. The CRM row wins
    # outright on score -- correctly, it is the same person -- and the direct
    # line was thrown away with the row.
    #
    # Every row in `group` already resolved to THIS CRD, so borrowing a phone
    # across them is not a cross-person merge. Identity fields stay with the
    # winner; only the number is upgraded.
    if str(best.get("phone_kind", "")) not in REACHES_PERSON:
        reachable = group[group["phone_kind"].isin(REACHES_PERSON)
                          & (group["match_score"] >= ACCEPT)]
        if len(reachable):
            donor = reachable.sort_values("match_score", ascending=False).iloc[0]
            best["phone"] = donor["phone"]
            best["phone_kind"] = donor["phone_kind"]
            best["phone_ext"] = donor.get("phone_ext", "")
            best["phone_from"] = donor["source"]

    # AND EVERY OTHER EMPTY FIELD, for the same reason the phone is borrowed.
    #
    # Only the phone was being filled in, so a winning row's blanks stayed
    # blank while the answer sat on a losing row for the same CRD. John Pettey
    # (1469162) shipped with NO email: his CRM record won on score and the CRM
    # has no address for him, while both Raymond James rows that matched the
    # same CRD carry one.
    #
    # Safe for the same reason: every row in `group` already resolved to THIS
    # CRD. Only genuinely EMPTY fields are filled, so the winner's own account
    # of itself is never overwritten -- this adds facts, it does not merge
    # conflicting ones. Name, city, state, owner and assets are deliberately
    # excluded: those are identity and provenance, and blending them across
    # sources is how a record stops describing one person.
    borrowable = ["email", "title", "team", "profile_url", "linkedin", "mobile"]
    donors = group[group["match_score"] >= ACCEPT].sort_values(
        "match_score", ascending=False)
    for field in borrowable:
        if field not in group.columns or str(best.get(field, "") or "").strip():
            continue
        have = donors[donors[field].astype(str).str.strip().ne("")]
        if len(have):
            best[field] = have.iloc[0][field]
    return best


def load_people(limit: int | None = None) -> pd.DataFrame:
    """CRM + every roster, on one set of column names, ready to score.

    Extracted from main() so the calibration harness measures the SAME rows the
    build ships. A harness that rebuilds the frame itself would be measuring its
    own reconstruction, and would keep scoring clean rows after a loader bug had
    already broken production -- which is exactly how the Captrust phone loss
    and the Morgan Stanley zero-phone bug both stayed silent.
    """
    domains = derive_domain_map()
    print(f"[*] domain -> firm CRD: {len(domains):,} domains "
          f"({len(domains) - len(EXTRA_DOMAINS):,} derived from rosters, "
          f"{len(EXTRA_DOMAINS)} added by hand)")

    crm = load_crm(domains)
    rosters = load_rosters()
    eic = load_eic()
    people = pd.concat([f for f in (crm, rosters, eic) if len(f)], ignore_index=True)
    # A WHITELIST. A column produced upstream and not named here is dropped
    # silently -- which is exactly what happened to team_url: the roster loader
    # populated it on 39,167 rows, the frame kept it, and this line removed it
    # before anything could read it. The practices all shipped with an empty
    # team URL and nothing anywhere said so.
    keep = ["name", "title", "team", "team_url", "profile_url", "linkedin", "email",
            "phone", "phone_ext", "mobile", "city", "state",
            "office", "company", "owner", "assets", "team_key", "firm_crd",
            "given_crd", "phone_kind", "source", "source_file",
            # The CRM's Dear field. Added here the moment it was added to
            # load_crm -- it was not, the first time, and the greeting silently
            # never reached a single record. Exactly the team_url failure this
            # comment already describes, repeated four lines below it.
            "salutation"]
    # Every column EXCEPT assets, which is numeric and whose NaN means "no
    # figure". Blanket-filling it with "" would make the team logic read an
    # empty string as a value and int() it.
    people = people[keep]
    text = [c for c in keep if c != "assets"]
    people[text] = people[text].fillna("")
    # Learned from rows that publish both, then applied only where state is
    # missing. Never overwrites a filed state -- this is a fallback, not a
    # correction, and a firm's own address beats an inference from a phone.
    codes = area_code_states(people)
    blank = people["state"].eq("") & people["phone"].ne("")
    if blank.any():
        filled = people.loc[blank, "phone"].map(
            lambda p: codes.get(digits_only(p)[:3], ""))
        people.loc[blank, "state"] = filled
        print(f"[*] area code -> state: {len(codes)} unambiguous codes learned, "
              f"{int((filled != '').sum()):,} rows given a state they lacked")

    people["phone_kind"] = infer_phone_kind(people)
    people = donate_by_email(people)
    people = people[people["name"].astype(str).str.strip() != ""]
    if limit:
        people = people.head(limit)
    print(f"[*] {len(people):,} contact rows to match")
    return people


def load_index():
    advisors, branches, employment, _, _ = load_reference()
    index = build_index(advisors, branches, employment)
    print(f"[*] index: {len(index):,} surnames over "
          f"{sum(len(v) for v in index.values()):,} advisor records")
    return index


# Azure Static Web Apps refuses to serve a file over 25 MB -- not a slow
# response, not a truncated one: its own 500 page, with no Content-Length and
# nothing in any log that names the file. contacts.json passed that line and the
# map went quiet.
#
# 12 MB gives real headroom. The file grows with every roster we add, and a
# limit discovered by deploying is a limit discovered by a rep.
SWA_FILE_LIMIT = 25 * 1024 * 1024
SHARD_TARGET = 12 * 1024 * 1024


def write_contact_shards(contacts: dict, payload: dict) -> list:
    """contacts.json, split small enough for Static Web Apps to serve.

    WHY SHARDS AND NOT SOMEWHERE ELSE
    ---------------------------------
    Blob storage was the obvious alternative and would need `connect-src 'self'`
    relaxed in the CSP -- trading a hosting problem for a weaker security
    posture on the page that holds the contact file and the call log. Sharding
    keeps everything same-origin and changes nothing anybody has to reason about
    later.

    Split on ADVISORS only. Teams and practices are small and stay in the base
    file, which the client reads first; the shards carry nothing but advisor
    records and are merged back into one object on arrival, so every consumer
    sees exactly what it saw before.

    The manifest lives in the base file rather than being derived from a naming
    convention, so a client can never guess at a shard that is not there or miss
    one that is.
    """
    ids = sorted(contacts)
    if not ids:
        return []
    # Size the split from the real serialised size rather than a record count:
    # a contact with a team, a title and a profile URL is several times the size
    # of one with a phone number.
    whole = len(json.dumps(contacts, separators=(",", ":")).encode("utf-8"))
    count = max(1, math.ceil(whole / SHARD_TARGET))
    per = math.ceil(len(ids) / count)

    names = []
    for index in range(count):
        chunk = ids[index * per:(index + 1) * per]
        if not chunk:
            continue
        name = f"contacts_{index}.json"
        write_json_gz(OUT / name, {"advisors": {k: contacts[k] for k in chunk}},
                      separators=(",", ":"))
        names.append(name)

    base = {**payload, "advisors": {}, "shards": names,
            "note": payload["note"] + f" Advisor records are in {len(names)} shards; "
                    "contacts.json carries the manifest, the teams and the practices."}
    write_json_gz(OUT / "contacts_base.json", base, separators=(",", ":"))

    largest = max((OUT / n).stat().st_size for n in names)
    print(f"[*] contact shards  {len(names)} files, largest {largest / 1e6:.1f} MB "
          f"(Static Web Apps refuses anything over {SWA_FILE_LIMIT / 1e6:.0f} MB)")
    return names


def main() -> None:
    # Used to corroborate the CRM's Dear field before it becomes a greeting.
    filed_names = filed_given_names()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--limit", type=int, help="only the first N contacts, for a trial")
    ap.add_argument("--report", action="store_true", help="print diagnostics and stop")
    args = ap.parse_args()

    people = load_people(args.limit)
    index = load_index()
    people = score_contacts(people, index)
    dist = people["tier"].value_counts()
    print("[*] match tiers: " + ", ".join(f"{k} {v:,}" for k, v in dist.items()))

    if args.report:
        matched = people[people["tier"].isin(["confirmed", "high", "review"])]
        print(matched.groupby(["source", "tier"]).size().unstack(fill_value=0).to_string())
        return

    # Teams, stored ONCE. Only real ones: two or more people sharing a company,
    # a city and an amount.
    teams = {}
    sized = people[people["team_key"] != ""].groupby("team_key")
    for key, group in sized:
        if len(group) < 2:
            continue
        teams[key] = {
            "n": group["company"].iloc[0],
            "c": group["city"].iloc[0],
            "a": float(group["assets"].iloc[0]),
            "sz": int(len(group)),
        }
    solo = people[(people["team_key"] != "") & (~people["team_key"].isin(teams))]

    usable = people[(people["tier"].isin(["confirmed", "high", "review"]))
                    & (people["advisor_crd"] != "")].copy()
    contacts = {}
    member_state: dict = {}
    for crd, group in usable.groupby("advisor_crd"):
        row = pick_best(group)
        team_key = row["team_key"]
        entry = {
            "n": str(row["name"]).strip(),
            "e": row["email"],
            "w": e164(row["phone"]), "wd": pretty(row["phone"]),
            # set only when the number came from a different source than the
            # rest of the record, so the panel can say where it came from
            "wf": str(row.get("phone_from", "") or ""),
            # What that number actually reaches. The panel labels the button
            # from this, so "Direct" is never printed over a switchboard.
            "wk": str(row["phone_kind"] or ""),
            # The extension is part of the number, not a note. A tel: link
            # dials "+15551234567,,99001" -- the commas are pauses the phone
            # honours, so the rep reaches the person rather than the switchboard.
            "wx": str(row.get("phone_ext", "") or ""),
            "c": e164(row["mobile"]), "cd": pretty(row["mobile"]),
            "ti": str(row["title"])[:80],
            # The advisor's team as their own FIRM names it. Distinct from the
            # CRM team below, which additionally carries an asset figure -- this
            # one is a name only, and is all we have for roster-sourced people.
            "tn": str(row.get("team", "") or "")[:80],
            "pu": str(row.get("profile_url", "") or "")[:200],
            # Consumed when practices are assembled below, then dropped: the
            # URL belongs to the team, and repeating it on every member would
            # add it to the payload once per person instead of once per team.
            #
            # Taken from the GROUP, not from the winning row. pick_best prefers
            # a CRM record where the scores are close, and a CRM record has no
            # opinion about the firm's team page -- so reading it off `row`
            # loses the URL for exactly the advisors we know most about.
            "_tu": team_url_of(group),
            "li": str(row.get("linkedin", "") or "")[:200],
            "src": row["source"],
            "t": row["tier"],
            "ms": float(row["match_score"]),
        }
        # The state the CONTACT record gives, kept so the panel can compare it
        # with where the SEC says this advisor sits. On an unconfirmed match the
        # two contradict 42% of the time, and that contradiction is the clearest
        # evidence available that the record belongs to somebody else -- Scott
        # Friberg's Virginia number was showing on Jennifer Friberg's Atlanta
        # card with nothing on screen to suggest a problem.
        # THE GREETING, only when the CRM supplied one. Absent on roster-sourced
        # advisors, and the emailer falls back to splitting the display name --
        # which is what it did for everybody before this existed.
        # CORROBORATED BY THE FILING, OR NOT USED AT ALL.
        #
        # The Dear field is usually the greeting a rep chose -- Chris for
        # Christopher Tolman, Scott for HENRY SCOTT KRUSE, who goes by his
        # middle name. But 1,834 of the 4,226 that differ from the display name
        # name somebody ELSE: Michael Myers is "Deborah", Joseph Pena is
        # "Melanie", Mitchell Stillman is "Kristin" -- a spouse, an assistant,
        # or a household record. audit.py already cites Mitchell/Kristin
        # Stillman as a known wrong pairing.
        #
        # "Hi Deborah," to Michael Myers is far worse than "Hi Michael," to a
        # man who goes by Mike, so the greeting has to agree with a name the SEC
        # has on file -- first, middle or used, allowing for nicknames.
        #
        # The cost is real and accepted: Chip for JOHN FREDERICK JOHNSON is
        # almost certainly right and is refused, because nothing distinguishes
        # it from Deborah except knowing the person.
        sal = salutation_of(group)
        if sal:
            filed = filed_names.get(str(crd)) or []
            first = sal.split()[0]
            if any(same_person(first, tok) for tok in filed):
                entry["sal"] = sal
        cs = str(row.get("state", "") or "").strip().upper()[:2]
        if cs:
            entry["cs"] = cs
        if row["owner"]:
            entry["o"] = row["owner"]
        # The firm the CONTACT record says this person works for, kept so the
        # panel can compare it with the firm the SEC currently files them
        # under. A disagreement is not an error to be reconciled away -- it is
        # usually a MOVE, and an advisor who has just changed firms is the best
        # prospecting signal in the dataset. Storing only the reconciled answer
        # would destroy the very thing worth surfacing.
        if row["firm_crd"]:
            entry["fc"] = str(row["firm_crd"])
        if str(row["company"]).strip():
            entry["cn"] = str(row["company"]).strip()[:60]
        if team_key and team_key in teams:
            entry["tm"] = team_key
        elif pd.notna(row["assets"]):
            # An amount with no team is this person's own book, and is labelled
            # as such rather than silently shown as team assets.
            entry["ia"] = float(row["assets"])
        # Where two sources disagree the OTHER one is kept, not discarded: a
        # second firm for the same person is usually a move, which is a
        # prospecting signal, not noise.
        others = sorted(set(group["source"]) - {row["source"]})
        if others:
            entry["also"] = others[:3]
        contacts[crd] = {k: v for k, v in entry.items() if v not in ("", None)}
        # Not shipped on the advisor entry -- only used to make a teammate
        # clickable from the national view, where the map holds no per-state
        # features yet and the panel has to know which scope to switch to.
        member_state[crd] = str(row["state"] or "")

    # WHO ELSE IS ON THAT TEAM.
    #
    # A team name on its own tells a rep almost nothing. The useful question is
    # who else is in it -- an SMA conversation is usually with a practice, not
    # an individual, and the other names are the rest of the buying unit.
    # Membership is stored ONCE here, as a list of CRDs, so the panel can
    # resolve names from the advisors it already holds and nothing is
    # duplicated per member.
    #
    # Keyed on (firm CRD, normalised team name): the same practice name at two
    # different firms is two different teams, and "The Jessup Group" at Merrill
    # must never absorb a namesake elsewhere.
    practices: dict = {}
    for crd, entry in contacts.items():
        name = entry.get("tn", "")
        if not name:
            continue
        key = f"{entry.get('fc', '')}|{norm(name)}"
        rec = practices.setdefault(key, {"n": name, "m": [], "u": ""})
        rec["m"].append(crd)
        # One URL per practice, not per person: the team page is the team's.
        # First non-empty wins -- members of one practice either agree or one
        # of them simply has no site recorded.
        if not rec["u"]:
            rec["u"] = entry.get("_tu", "")
    # A "team" of one is just a person with a team name -- usually an advisor
    # whose colleagues we have not matched. Kept off the panel rather than
    # rendered as a team with nobody in it.
    practices = {k: v for k, v in practices.items() if len(v["m"]) > 1}
    for entry in contacts.values():
        entry.pop("_tu", None)
    for key, rec in practices.items():
        rec["m"].sort()
        rec["sz"] = len(rec["m"])
        for crd in rec["m"]:
            contacts[crd]["pk"] = key
        rec["m"] = [[c, member_state.get(c, "")] for c in rec["m"]]

    # The CRM teams already carry a size and an asset figure; they were missing
    # only the roster of who is in them, for the same reason.
    for key, rec in teams.items():
        rec["m"] = [[c, member_state.get(c, "")]
                    for c in sorted(c for c, e in contacts.items()
                                    if e.get("tm") == key)]

    payload = {
        "advisors": contacts,
        "teams": teams,
        "practices": practices,
        "note": (f"{len(contacts):,} advisors with contact detail from the CRM "
                 f"and {len(FIRMS)} scraped rosters. Team assets are stored on "
                 f"the team, never on the person."),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    # JSON and .gz written together, never as two steps -- a .gz that lags the
    # JSON serves yesterday's contacts with a 200 and no warning anywhere.
    write_json_gz(OUT / "contacts.json", payload, separators=(",", ":"))
    write_contact_shards(contacts, payload)

    # Counted per tier, not as "high and everything else". Lumping the
    # roster-stated CRDs in with review reported 8,283 of the most certain
    # records in the file as the least certain ones.
    high = sum(1 for v in contacts.values() if v.get("t") == "high")
    stated = sum(1 for v in contacts.values() if v.get("t") == "confirmed")
    unsure = sum(1 for v in contacts.values() if v.get("t") == "review")
    owned = sum(1 for v in contacts.values() if v.get("o"))
    with_team = sum(1 for v in contacts.values() if v.get("tm"))
    print(f"\n[*] contacts.json  {len(contacts):,} advisors "
          f"({stated:,} CRD stated by the firm, {high:,} high-confidence, "
          f"{unsure:,} review)")
    print(f"    {sum(1 for v in contacts.values() if v.get('e')):,} with email, "
          f"{sum(1 for v in contacts.values() if v.get('w')):,} with a phone")
    kinds = collections.Counter(v.get("wk", "") for v in contacts.values() if v.get("w"))
    print("    phone reaches: " + ", ".join(f"{k or 'unlabelled'} {n:,}"
                                            for k, n in kinds.most_common()))
    reach = sum(n for k, n in kinds.items() if k in REACHES_PERSON)
    print(f"    {reach:,} numbers reach a PERSON; "
          f"{sum(kinds.values()) - reach:,} reach a front desk or are unverified")
    print(f"    {owned:,} carry an EIC relationship owner")
    print(f"    {len(teams):,} teams, {with_team:,} advisors on one; "
          f"team assets total ${sum(t['a'] for t in teams.values()) / 1e9:.2f}B "
          f"counted ONCE")
    if len(solo):
        print(f"    {len(solo):,} contact(s) had an asset value but no team-mate "
              f"-- kept as an individual book, not a team")
    unmatched = people[people["tier"] == "none"]
    print(f"    {len(unmatched):,} contact rows matched no advisor and are not "
          f"in the file")


if __name__ == "__main__":
    main()
