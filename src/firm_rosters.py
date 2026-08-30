"""Where scraped firm rosters live, what they are called, and which CRDs they map to.

ONE convention, defined once:

    data/raw/firm_rosters/<firm_slug>_<YYYY-MM-DD>.<ext>

  firm_slug   lowercase, underscores, from the FIRMS registry below
  YYYY-MM-DD  the date the data was captured -- sortable, unambiguous, and
              readable. Epoch seconds (lpl_advisors_nationwide_1785581673.csv)
              are none of those: two Morgan Stanley files differing only by
              epoch is exactly how the wrong one gets used.
  ext         csv or json, whatever the source produced

NO CRD IN THE FILENAME. Three of these firms are several legal entities --
Raymond James files span CRD 705 and 149018, Wells Fargo spans 19616 and 11025
-- so a single CRD in the name would assert something false. The mapping lives
in FIRMS instead, where it can hold a list and a per-row rule.

Adding a firm later: add one entry to FIRMS. Uploading a file by hand: name it
to the convention, or run `python src/firm_rosters.py --fix` and it will rename
what it recognises.
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
ROSTER_DIR = ROOT / "data" / "raw" / "firm_rosters"
SUPERSEDED = ROSTER_DIR / "superseded"

NAME_RE = re.compile(r"^(?P<slug>[a-z0-9_]+)_(?P<date>\d{4}-\d{2}-\d{2})\.(?P<ext>csv|json|xlsx)$")

# slug -> how it joins to the SEC data.
#   crds     every legal entity this firm's roster may contain
#   channel  optional column that says which entity a given ROW belongs to
FIRMS = {
    "edward_jones":   {"label": "Edward Jones",   "crds": ["250"]},
    # THREE sources merged by src/merrill_async.py: the Yext Answers directory
    # (complete), the office stream (capped by the server at 50,000 of its
    # 65,088 documents), and every team page either one names. "Team Name" is
    # the real team; the Merrill office code that used to occupy that column
    # lives in "Office Name". "Entity Type" is ADVISOR or PROFESSIONAL_STAFF
    # straight from Merrill -- 7,509 of the 18,867 are support staff.
    "merrill":        {"label": "Merrill Lynch",  "crds": ["7691"]},
    "morgan_stanley": {"label": "Morgan Stanley", "crds": ["149777"]},
    # TWO sources merged by src/ubs_async.py: the Broadridge locator API, which
    # lists advisors only, and every team/branch page it names. The pages carry
    # 3,890 people the API does not -- mostly client associates and team
    # administrators, ~1,250 of them advisor-titled -- so JobTitle is the field
    # to filter on if support staff are unwanted. Columns are Broadridge's own
    # names (MarketingName, RankTitle, Emails); build_contacts maps them.
    "ubs":            {"label": "UBS",            "crds": ["8174"]},
    # Cetera's many broker-dealers (Advisor Networks, Financial Specialists,
    # Investment Services...) file advisory business under ONE RIA, CRD 105644
    # -- 10,440 advisors in our data. 165436 and 285648 are also Cetera but
    # report zero RAUM and 11/0 advisors, so they are not join targets.
    "cetera":         {"label": "Cetera",         "crds": ["105644"]},
    "baird":          {"label": "Robert W. Baird", "crds": ["8158"]},
    # Truist Advisory Services is the RIA. Truist Investment Services is the
    # broker-dealer and files no ADV of its own, so 283390 is the only join
    # target even though profiles name both entities.
    "truist":         {"label": "Truist Wealth",  "crds": ["283390"]},
    # RBC Wealth Management-U.S. is a DIVISION of RBC Capital Markets, LLC and
    # files no ADV of its own, so 31194 is the join target. The other RBC
    # entities in the data (Rochdale 117198, Global AM 107173, Private Counsel
    # 109648) are separate businesses, not the retail advisor force.
    "rbc":            {"label": "RBC Wealth Management", "crds": ["31194"]},
    # `team_name` is DERIVED, not copied. Ameriprise's own teamName field is a
    # parent pointer: for a practice lead it holds the practice, for everyone
    # under them it holds the lead's personal name. src/ameriprise_async.py
    # follows that chain to a practice and keeps the raw value in `reports_to`.
    "ameriprise":     {"label": "Ameriprise",   "crds": ["6363"]},
    # Citi Personal Wealth Management publishes only part of CRD 7059:
    # 229 advisors in 9 states, against 2,409 IARs in 27 states in ADV.
    "citi":           {"label": "Citi Wealth Management", "crds": ["7059"]},
    # CRD 140195 is Mariner (Overland Park, KS) -- $125B, 960 advisors, the
    # firm behind marinerwealthadvisors.com. Its siblings are separate ADV
    # filers with their own advisors: Mariner Independent (305418, 120),
    # Mariner Wealth (289886, 63), Mariner Advisor Network (283824, 14),
    # Mariner Institutional (111964, 16). They are NOT folded in, because a
    # roster scraped from the parent brand should not claim them.
    # WATCH OUT: Mariner Investment Group (124744, New York, $149B) is an
    # unrelated firm that merely shares the word. Never match against it.
    "mariner":        {"label": "Mariner",      "crds": ["140195"]},
    "stifel":         {"label": "Stifel Nicolaus", "crds": ["793"]},
    # Corient Private Wealth (319448) is the operating RIA -- $165B, 629
    # advisors. Corient IA LLC (326262) reports zero advisors and is not a
    # join target. Corient is a rollup of CI Financial's US acquisitions,
    # and the roster names the acquired firm in `legacyFirm`.
    "corient":        {"label": "Corient",      "crds": ["319448"]},
    # CRD 2881 is Northwestern Mutual Investment Services, the BD/RIA arm --
    # 2,130 IARs in 48 states. 307865 (NM Investment Management) is the
    # in-house asset manager: $323B, ZERO advisors, not a join target.
    # NOTE both score as is_target=False. NM reps are captive and insurance-
    # led, so this roster is reach data, not a qualified prospect list.
    "northwestern_mutual": {"label": "Northwestern Mutual", "crds": ["2881"]},
    "janney":         {"label": "Janney Montgomery Scott", "crds": ["463"]},
    # Focus Partners Wealth (159289) is the operating RIA -- $182B, 1,023
    # advisors, avg account $824K, scores 88.9 SMA fit. Focus Partners
    # Advisor Solutions (143319, 42 advisors) is the separate TAMP/platform
    # business and is NOT folded in.
    "focus_partners": {"label": "Focus Partners", "crds": ["159289"]},
    # CAPTRUST (175112) is the operating RIA -- $1.24T, 939 advisors, avg
    # account $8.4M, opportunity 96.1. Captrust Wealth Advisors (173736,
    # Holland MI, 19 advisors) is a separate small filer, not folded in.
    "captrust":       {"label": "CAPTRUST",     "crds": ["175112"]},
    # EP Wealth Advisors (111147, Torrance CA) -- $45B, 331 advisors, avg
    # account $2.18M, SMA fit 97.2, one of the highest in the dataset.
    "ep_wealth":      {"label": "EP Wealth Advisors", "crds": ["111147"]},
    # Mercer GLOBAL Advisors (147363, Denver) is the RIA behind
    # merceradvisors.com -- $84B, 729 advisors, avg account $726K.
    # WATCH OUT: Mercer Investments (133449, Boston, $226B) and Mercer
    # Investment Solutions (155919) are the UNRELATED Marsh McLennan
    # consulting business. Same word, different company, zero advisors.
    "mercer":         {"label": "Mercer Advisors", "crds": ["147363"]},
    # Wealthspire Advisors (106181) -- $34B, 232 advisors, avg account
    # $3.44M, 59% equity, SMA fit 99.3, the highest in the dataset.
    # Wealthspire Retirement Advisory (121254) is a separate filer doing
    # retirement-plan work (8% equity) and is NOT folded in.
    "wealthspire":    {"label": "Wealthspire Advisors", "crds": ["106181"]},
    # Chevy Chase Trust Company (110742) -- $44.9B, 39 IARs, avg account
    # $8.81M, SMA fit 92.8. The site publishes ~133 people because it is a
    # TRUST company: most of the roster is trust, operations and client
    # service, NOT the 39 IARs. Use `title` to separate them -- treating all
    # 133 as advisors would overstate the firm's sales surface 3x.
    # Emails span TWO domains: chevychasetrust.com and bfsaulco.com, the
    # B.F. Saul Company parent. A single-domain assumption drops people.
    "chevy_chase_trust": {"label": "Chevy Chase Trust", "crds": ["110742"]},
    # Waverly Advisors (115332, Birmingham AL) -- $29.8B, 214 IARs, avg
    # account $833K, SMA fit 87.5. The whole roster renders on ONE page.
    # ~465 people of all roles against 214 IARs, so `title` separates
    # advisors from support. Emails span waverly-advisors.com AND
    # promuscapital.com (Promus, acquired, still on its own domain).
    "waverly":        {"label": "Waverly Advisors", "crds": ["115332"]},
    # Sanctuary Advisors (226606) -- $34.9B, 398 advisors, SMA fit 88.2.
    # NOT a single-site scrape: Sanctuary's advisors are independent
    # practices, and the firm files 94 separate practice domains on
    # Schedule D. Harvested by src/dba_roster.py, so rows carry a
    # `domain` and an `email_kind`, and `name` is blank by design --
    # 94 unrelated layouts cannot be parsed to a person generically.
    "sanctuary":      {"label": "Sanctuary Wealth", "crds": ["226606"]},
    # Red Door Wealth Management (153235, Memphis TN) -- $3.09B, 10 IARs,
    # 4,471 accounts, avg account $690K, SMA fit 72.1, 5.G(5) = Yes.
    # Small enough to sit past rank 400 by RAUM and still a Tier A profile.
    # The site lists 22 people of all roles; `title` separates the advisors.
    # Every email is personal and first-name based (john@, allen@); the PHONE
    # is one office line shared by all 22, so phone_kind is switchboard
    # throughout. That is the firm, not a parsing defect.
    "red_door":       {"label": "Red Door Wealth Management", "crds": ["153235"]},
    # Raymond James BRANCH team pages (CRD 705, the RJA employee channel).
    # A SUPPLEMENT to the `raymond_james` roster, not a replacement: that one
    # comes from the advisor-search API, which returns the BRANCH SWITCHBOARD
    # as each advisor's phone -- Field Norris shows as (901) 766-7700, a number
    # 47 Memphis advisors share. The branch's own team page publishes his
    # direct line, 901.766.7713, plus a title and personal email.
    # Captured through browser automation because Akamai 403s every library
    # client on `-branch` URLs. Slugs are irregular -- bocaraton-branch works
    # while boca-raton-branch 404s, and miami-branch serves Coral Gables -- so
    # the page's resolved city/state is verified against the branch we asked for.
    "rj_branches":    {"label": "Raymond James (branch pages)", "crds": ["705"]},
    # Alex. Brown, a division of Raymond James -- also CRD 705, so this is a
    # LABEL on part of the RJ book, not a separate firm. It exists because the
    # advisor-search API returns `branch_is_alex_brown` and leaves it 0 on all
    # 8,400 rows; the only usable signal is the @alexbrown.com email domain.
    # Scraped from the legacy alexbrownbranches.com, which is open. Branches
    # that have migrated to raymondjames.com 301 away and are left to
    # rj_branches.py, whose browser automation can get past Akamai.
    # Publishes NO personal phone -- every row is the branch switchboard, so
    # this file must never win a phone contest against rj_branches.
    "alex_brown":     {"label": "Alex. Brown (Raymond James)", "crds": ["705"]},
    # LPL Holdings also owns LPL Enterprise (8733) and Commonwealth (8032).
    # Commonwealth is a distinct brand whose advisors do not consider
    # themselves LPL, so matching an LPL roster against it would let a
    # Commonwealth advisor absorb an LPL contact record. 6413 only.
    "lpl":            {"label": "LPL Financial",  "crds": ["6413"]},
    # Two entities sharing one brand: 19616 is the employee channel, 11025 is
    # FiNet. The scrape covers fa.wellsfargoadvisors.com and carries no channel
    # column, so it likely spans both.
    "wells_fargo":    {"label": "Wells Fargo Advisors", "crds": ["19616", "11025"]},
    # RJA is the employee channel; IMD and FID register through RJFS Advisors.
    # This mapping is inferred from channel structure and should be spot-checked
    # against BrokerCheck before it is trusted.
    "raymond_james":  {"label": "Raymond James", "crds": ["705", "149018"],
                       "channel": {"column": "advisor_subsidiary",
                                   "map": {"RJA": "705", "IMD": "149018", "FID": "149018"}}},
}

def allowed_crds(slug: str, row=None):
    """Return the SEC firm family this authoritative roster row may match."""
    meta = FIRMS[slug]
    family = tuple(str(value) for value in meta["crds"])
    channel = meta.get("channel")
    if not channel or row is None:
        return family
    value = str(row.get(channel["column"], "") or "").strip().upper()
    mapped = channel["map"].get(value)
    return (str(mapped),) if mapped else family

# Filenames seen in the wild -> slug. Extend as new sources arrive.
ALIASES = {
    "ubs_advisors": "ubs", "ubs_advisors_all_fields": "ubs",
    "edward_jones_advisors": "edward_jones", "edward_jones_advisors_enriched": "edward_jones",
    "wells_fargo_advisors": "wells_fargo",
    "raymond_james_advisors": "raymond_james",
    "lpl_advisors_nationwide": "lpl", "lpl_advisors": "lpl",
    "cetera_advisors": "cetera",
    "baird_advisors": "baird", "robert_w_baird": "baird",
    "truist_advisors": "truist", "truist_wealth": "truist",
    "rbc_advisors": "rbc", "rbc_wealth_management": "rbc",
    "ameriprise_advisors": "ameriprise",
    "citi_advisors": "citi", "citigroup": "citi",
    "mariner_advisors": "mariner", "mariner_wealth_advisors": "mariner",
    "stifel_advisors": "stifel", "stifel_nicolaus": "stifel",
    "corient_advisors": "corient", "corient_private_wealth": "corient",
    "northwestern_mutual_advisors": "northwestern_mutual",
    "northwestern_mutual_fr": "northwestern_mutual",
    "janney_advisors": "janney", "janney_montgomery_scott": "janney",
    "focus_partners_advisors": "focus_partners",
    "focus_partners_wealth": "focus_partners",
    "captrust_advisors": "captrust", "captrust_people": "captrust",
    "ep_wealth_advisors": "ep_wealth", "epwealth": "ep_wealth",
    "mercer_advisors": "mercer", "mercer_global_advisors": "mercer",
    "wealthspire_advisors": "wealthspire",
    "chevy_chase_trust_company": "chevy_chase_trust",
    "chevychasetrust": "chevy_chase_trust",
    "waverly_advisors": "waverly",
    "sanctuary_wealth": "sanctuary", "sanctuary_advisors": "sanctuary",
    "red_door_wealth": "red_door", "reddoorwealth": "red_door",
    "raymond_james_branches": "rj_branches",
    "alex_brown_advisors": "alex_brown", "alexbrown": "alex_brown",
    "alex_brown_branches": "alex_brown",
    "merrill_lynch_advisors": "merrill", "merrill_advisors": "merrill",
    "morgan_stanley_advisors": "morgan_stanley", "morgan_stanley_advisors_full": "morgan_stanley",
}


def roster_path(slug: str, when: dt.date | None = None, ext: str = "csv") -> pathlib.Path:
    """The canonical path a scraper should write to."""
    if slug not in FIRMS:
        raise KeyError(f"unknown firm slug {slug!r}; add it to FIRMS in src/firm_rosters.py")
    ROSTER_DIR.mkdir(parents=True, exist_ok=True)
    return ROSTER_DIR / f"{slug}_{(when or dt.date.today()).isoformat()}.{ext}"


def scratch_path(slug: str, stage: str, when: dt.date | None = None,
                 ext: str = "json") -> pathlib.Path:
    """An INTERMEDIATE artefact, e.g. a discovery pass before enrichment.

    Deliberately not in firm_rosters/: that folder means "current rosters", and
    an unfinished discovery file sitting there would audit as OK and could be
    loaded as though it were the finished article.
    """
    out = ROOT / "data" / "interim" / "firm_rosters"
    out.mkdir(parents=True, exist_ok=True)
    return out / f"{slug}_{stage}_{(when or dt.date.today()).isoformat()}.{ext}"


def latest(slug: str) -> pathlib.Path | None:
    """Newest conforming file for a firm. Loaders should call this rather than
    hard-coding a filename, so a fresh scrape is picked up automatically."""
    hits = []
    for path in ROSTER_DIR.glob(f"{slug}_*"):
        m = NAME_RE.match(path.name)
        if m and m.group("slug") == slug:
            hits.append((m.group("date"), path))
    return max(hits)[1] if hits else None


def guess_slug(stem: str) -> str | None:
    """Best-effort slug for a hand-uploaded file."""
    s = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")
    s = re.sub(r"_\d{6,}$", "", s)                     # trailing epoch
    s = re.sub(r"_\d{4}[-_]\d{2}[-_]\d{2}$", "", s)    # trailing date
    s = re.sub(r"_atlanta$|_nationwide$", "", s)
    if s in ALIASES:
        return ALIASES[s]
    if s in FIRMS:
        return s
    for alias, slug in ALIASES.items():
        if s.startswith(alias):
            return slug
    return next((slug for slug in FIRMS if s.startswith(slug)), None)


def guess_date(path: pathlib.Path) -> dt.date:
    """Date from the filename if it carries one, else the file's mtime."""
    stem = path.stem
    m = re.search(r"(\d{4})[-_](\d{2})[-_](\d{2})", stem)
    if m:
        try:
            return dt.date(*map(int, m.groups()))
        except ValueError:
            pass
    m = re.search(r"_(\d{9,11})$", stem)                # epoch seconds
    if m:
        try:
            return dt.datetime.fromtimestamp(int(m.group(1))).date()
        except (ValueError, OSError):
            pass
    return dt.date.fromtimestamp(path.stat().st_mtime)


def audit(fix: bool = False) -> int:
    if not ROSTER_DIR.exists():
        print(f"{ROSTER_DIR} does not exist")
        return 1
    ok, problems = [], []
    for path in sorted(ROSTER_DIR.iterdir()):
        if path.is_dir():
            continue
        m = NAME_RE.match(path.name)
        if m and m.group("slug") in FIRMS:
            ok.append(path)
            continue
        slug = guess_slug(path.stem)
        target = (roster_path(slug, guess_date(path), path.suffix.lstrip("."))
                  if slug else None)
        problems.append((path, target))

    print(f"{ROSTER_DIR}")
    for path in ok:
        m = NAME_RE.match(path.name)
        crds = ", ".join(FIRMS[m.group("slug")]["crds"])
        print(f"  OK   {path.name:<34}{m.group('date')}  CRD {crds}")
    for path, target in problems:
        if target is None:
            print(f"  ??   {path.name:<34}unrecognised -- add an alias to FIRMS/ALIASES")
        elif fix:
            if target.exists():
                print(f"  SKIP {path.name:<34}-> {target.name} already exists")
            else:
                path.rename(target)
                print(f"  FIX  {path.name:<34}-> {target.name}")
        else:
            print(f"  REN  {path.name:<34}-> {target.name}   (run with --fix)")

    print()
    for slug, meta in FIRMS.items():
        current = latest(slug)
        print(f"  {meta['label']:<24}{current.name if current else 'MISSING':<34}"
              f"CRD {', '.join(meta['crds'])}")
    return 0 if not any(t is None for _, t in problems) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fix", action="store_true", help="rename recognised files in place")
    sys.exit(audit(ap.parse_args().fix))
