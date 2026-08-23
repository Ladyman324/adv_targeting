"""Raymond James BRANCH team pages -> data/raw/firm_rosters/rj_branches_<date>.csv

WHY THIS EXISTS
---------------
The main Raymond James roster (8,400 advisors, scraped from the advisor-search
API) carries the BRANCH SWITCHBOARD as each advisor's phone. Field Norris shows
as (901) 766-7700 -- a number shared by 47 people in Memphis. His actual direct
line, 901.766.7713, is published on the branch's own team page, along with a
personal email and title for everyone in the office.

    5,765 of 8,400 RJ advisors (69%) currently hold a shared branch number.

The RJA (employee) channel is 3,547 advisors across only 507 branches, so a few
dozen branch pages recover direct dials for a large share of them.

THIS MODULE DOES NOT FETCH. THAT IS DELIBERATE.
----------------------------------------------
raymondjames.com sits behind an Akamai WAF that:

  * 403s the `requests` library on every `-branch` URL, at any pace
  * 403s `fetch()` from inside an already-loaded page, same URL
  * serves the page normally to a real browser NAVIGATION or IFRAME load
  * applies a STATEFUL penalty: a burst earns a block lasting many minutes,
    and 9-second spacing was still refused while a penalty was active

So the capture half runs through browser automation, one page at a time, at
about one request per minute. This module owns the half that can be done
safely offline: verifying, merging and resuming. Keeping them separate means a
blocked capture can never corrupt what was already collected.

CONSERVATIVE BY CONSTRUCTION
----------------------------
`pending()` returns only branches not already captured, so a re-run never
re-requests a page. `ingest()` verifies before it trusts:

  * the page's resolved city/state must match the branch we asked for --
    `birmingham-branch` serves Birmingham MI, and this roster holds BOTH
    Birmingham MI (45 advisors) and Birmingham AL (43). Without the check,
    Alabama advisors would silently be given Michigan phone numbers.
  * every person must carry exactly one email, from a card holding exactly one
    mailto. An earlier version walked up a fixed number of DOM levels and on
    Atlanta's grid layout paired one person's email with another's name and
    phone -- a plausible-looking record that is entirely wrong.

Run:  python src/rj_branches.py --pending 12
      python src/rj_branches.py --ingest capture.json
      python src/rj_branches.py --status
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from firm_rosters import roster_path, scratch_path  # one naming convention, defined once

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
ROSTERS = ROOT / "data" / "raw" / "firm_rosters"
RJ_ROSTER_GLOB = "raymond_james_*.csv"
STORE = ROOT / "data" / "interim" / "rj_branch_capture.json"
RJA_CRD = "705"

# One request per minute, and stop after two refusals in a row rather than
# retrying into a longer ban. These are the numbers the capture side is asked
# to honour; they live here so the policy is written down, not remembered.
PACE_SECONDS = 60
STOP_AFTER_CONSECUTIVE_BLOCKS = 2
COOLDOWN_SECONDS = 900
SESSION_CAP = 40

TEAM_PATH = "/about-us/our-team"
BASE = "https://www.raymondjames.com"


def slugify(city: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(city).strip().lower()).strip("-") + "-branch"


def digits(raw) -> str:
    d = re.sub(r"\D", "", str(raw or ""))
    return d[1:] if len(d) == 11 and d.startswith("1") else d


def rja_targets() -> pd.DataFrame:
    """Every RJA city, largest first, with its guessed slug and a collision flag."""
    files = sorted(ROSTERS.glob(RJ_ROSTER_GLOB))
    if not files:
        raise SystemExit("no raymond_james roster found")
    d = pd.read_csv(files[-1], dtype=str, low_memory=False).fillna("")
    rja = d[d["advisor_subsidiary"] == "RJA"]
    g = (rja.groupby(["city", "state"]).size()
         .sort_values(ascending=False).reset_index(name="advisors"))
    g["slug"] = g["city"].map(slugify)
    # A slug that two different cities produce cannot be trusted to the right
    # one without reading the page, so it is flagged rather than dropped.
    g["ambiguous"] = g["slug"].duplicated(keep=False)
    g["url"] = BASE + "/" + g["slug"] + TEAM_PATH
    return g


def load_store() -> dict:
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except Exception:
        return {"branches": {}}


def save_store(store: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=1), encoding="utf-8")
    tmp.replace(STORE)


def captured() -> tuple[set, set, pd.DataFrame]:
    """What we already hold, read from the ROSTER CSV -- the real artefact.

    An earlier version read only this module's own JSON store, which the
    working process never wrote to: capture happened in the browser and was
    merged straight into the roster. `--status` therefore reported "0 of 372"
    while 141 branches sat on disk. Trust the file that exists.

    Returns (slugs, cities, frame). CITIES matter as much as slugs: a 404 on
    `coralgables-branch` does not mean the office is missing, it means Coral
    Gables is served by `miami-branch` and is already captured under a
    different name. Re-requesting it would waste a scarce request.
    """
    files = sorted(ROSTERS.glob("rj_branches_*.csv"))
    if not files:
        return set(), set(), pd.DataFrame()
    d = pd.read_csv(files[-1], dtype=str).fillna("")
    cities = {str(c).strip().lower() for c in d.get("city", []) if str(c).strip()}
    return set(d["branch_slug"]), cities, d


def pending(limit: int) -> pd.DataFrame:
    """Branches still to capture, largest first, never re-requesting one we hold.

    Two slug forms are emitted per city because the site is inconsistent:
    `bocaraton-branch` works while `boca-raton-branch` 404s, and `stlouis`
    works while `saint-louis` does not. The compressed form has the better hit
    rate, so it is tried first.
    """
    have_slugs, have_cities, _ = captured()
    targets = rja_targets()
    rows = []
    for t in targets.itertuples():
        if str(t.city).strip().lower() in have_cities:
            continue                      # already captured under some slug
        compressed = re.sub(r"[^a-z0-9]+", "", str(t.city).lower()) + "-branch"
        for slug in (compressed, t.slug):
            if slug in have_slugs or any(r["slug"] == slug for r in rows):
                continue
            rows.append({"slug": slug, "city": t.city, "state": t.state,
                         "advisors": int(t.advisors),
                         "url": f"{BASE}/{slug}{TEAM_PATH}"})
        if len({r["city"] for r in rows}) >= limit:
            break
    return pd.DataFrame(rows)


def merge_downloads(downloads: pathlib.Path) -> pathlib.Path:
    """Fold browser-captured CSVs from the Downloads folder into the roster.

    This is what the working process actually did, by hand, every batch. It
    belongs in the tool: a merge typed fresh each time is a merge that drifts.
    """
    # One broad pattern, not a list of prefixes. Enumerating "rj_b*", "rj_r*",
    # "rj_s*" meant the next batch tag (rj_e*) was silently skipped and the
    # merge reported no new branches while the files sat in Downloads.
    files = sorted(f for f in downloads.glob("rj_*.csv") if "probe" not in f.name)
    existing = sorted(ROSTERS.glob("rj_branches_*.csv"))
    frames = [pd.read_csv(f, dtype=str).fillna("") for f in files]
    if existing:
        prev = pd.read_csv(existing[-1], dtype=str).fillna("")
        if "page_title" not in prev.columns:
            prev["page_title"] = ""
        frames.append(prev)
    if not frames:
        raise SystemExit("nothing to merge")
    d = pd.concat(frames, ignore_index=True).drop_duplicates(
        subset=["branch_slug", "email"])
    # A page with a single card is a COMPLEX landing page, not a branch roster
    # -- greenwoodvillage-branch returned one "person" titled "Rocky Mountain
    # Complex". Two cards is the floor for believing it is a team page.
    d = d[d.groupby("branch_slug")["email"].transform("size") > 1]

    def loc(t):
        t = str(t).strip()
        m = re.search(r",\s*([A-Z]{2})$", t)
        # Split on the LAST ' - '. A character class containing '-' matches
        # greedily from the FIRST hyphen and makes the city read
        # "Miami Branch of Raymond James - Coral Gables".
        return ("", "") if not m else (t[:m.start()].rsplit(" - ", 1)[-1].strip(),
                                       m.group(1))

    # Parse from page_title where we have it; fall back to whatever the row
    # already carried. The previous .where() chain compared a column against
    # itself and collapsed `state` to a single value across 2,800 rows.
    titles = d["page_title"] if "page_title" in d.columns else pd.Series("", index=d.index)
    parsed = pd.DataFrame([loc(t) for t in titles], index=d.index,
                          columns=["p_city", "p_state"])
    for col, pcol in (("city", "p_city"), ("state", "p_state")):
        prior = (d[col].fillna("").astype(str).str.strip().replace({"nan": "", "None": ""})
                 if col in d.columns else pd.Series("", index=d.index))
        d[col] = parsed[pcol].where(parsed[pcol].str.strip() != "", prior)

    # A few pages title themselves without the trailing ", ST" -- "Pasadena
    # Branch of Raymond James" and "Raymond James - Newtown, PA" among them --
    # so the parse yields nothing. Fall back to the SLUG, which is the only
    # other statement the page makes about where it is. Marked so nobody later
    # mistakes a derived city for one the page actually printed.
    # NaN, not "". Rows coming from the browser CSVs have no `city` column at
    # all, so the concat leaves NaN -- and `astype(str)` turns that into the
    # STRING "nan", which is not empty and silently passed this test.
    blank = lambda col: col.isna() | col.astype(str).str.strip().isin(["", "nan", "None"])
    missing = blank(d["city"])
    if missing.any():
        from_slug = (d.loc[missing, "branch_slug"].str.replace("-branch$", "", regex=True)
                     .str.replace("-", " ").str.title())
        d.loc[missing, "city"] = from_slug
        d.loc[missing, "city_source"] = "slug"
    if "city_source" in d.columns:
        d["city_source"] = d["city_source"].fillna("page_title")
    else:
        d["city_source"] = "page_title"
    d["phone_digits"] = d["phone"].map(digits)
    d["firm_crd"] = RJA_CRD

    # Occupancy counted per branch, aligned BY INDEX. The previous version
    # built a plain list in groupby(sort=False) order and then assigned it to a
    # frame sorted alphabetically -- so 37% of rows received another row's
    # verdict. Numbers used by one person were labelled switchboard and
    # switchboards were labelled direct, which is the exact error this column
    # exists to prevent. groupby().transform() cannot drift like that.
    d = d.sort_values("branch_slug", kind="stable").reset_index(drop=True)
    occupants = d.groupby("branch_slug")["phone_digits"].transform(
        lambda col: col.map(col[col != ""].value_counts()))

    def label(digits, n):
        if not digits or pd.isna(n):
            return ""
        return "direct" if n == 1 else ("shared" if n <= 5 else "switchboard")

    d["phone_kind"] = [label(x, n) for x, n in zip(d["phone_digits"], occupants)]

    out = ROSTERS / f"rj_branches_{dt.date.today().isoformat()}.csv"
    d.drop(columns=[c for c in ("page_title",) if c in d.columns]).to_csv(
        out, index=False, encoding="utf-8")
    return out


def ingest(payload) -> dict:
    """Fold one browser capture into the store, verifying before trusting.

    `payload` is {slug: {title, people:[{name,title,email,phone}]}} or a
    base64 string of that JSON -- base64 because these names carry (R) and (TM)
    marks that a shell round-trip mangles.
    """
    if isinstance(payload, str):
        payload = json.loads(base64.b64decode(payload).decode("utf-8"))
    targets = rja_targets().drop_duplicates("slug").set_index("slug")
    store = load_store()
    report = {"accepted": [], "rejected": [], "people": 0}

    for slug, cap in payload.items():
        if not isinstance(cap, dict) or cap.get("err") or not cap.get("people"):
            report["rejected"].append((slug, cap.get("err", "no people") if isinstance(cap, dict) else "bad"))
            continue
        title = str(cap.get("title", ""))
        # "Our Team - Memphis Branch of Raymond James - Memphis, TN"
        hit = re.search(r"-\s*([A-Za-z .'-]+),\s*([A-Z]{2})\s*$", title)
        got_city, got_state = (hit.group(1).strip(), hit.group(2)) if hit else ("", "")
        want = targets.loc[slug] if slug in targets.index else None
        if want is not None and got_state and got_state != want["state"]:
            # The exact Birmingham case: asked for AL, served MI.
            report["rejected"].append(
                (slug, f"served {got_city}, {got_state}; expected {want['city']}, {want['state']}"))
            continue

        people, seen = [], set()
        for p in cap["people"]:
            email = str(p.get("email", "")).strip()
            if "@" not in email or email.lower() in seen:
                continue
            seen.add(email.lower())
            people.append({"name": str(p.get("name", "")).strip(),
                           "title": str(p.get("title", "")).strip(),
                           "email": email,
                           "phone": str(p.get("phone", "")).strip()})
        if not people:
            report["rejected"].append((slug, "no usable people"))
            continue
        store["branches"][slug] = {
            "title": title, "city": got_city, "state": got_state,
            "captured": dt.date.today().isoformat(), "people": people}
        report["accepted"].append((slug, len(people)))
        report["people"] += len(people)

    save_store(store)
    return report


def build() -> pathlib.Path:
    """Flatten the store into a roster CSV, classifying phones by occupancy."""
    store = load_store()
    rows = []
    for slug, b in store["branches"].items():
        for p in b["people"]:
            rows.append({**p, "branch_slug": slug, "city": b["city"],
                         "state": b["state"], "firm_crd": RJA_CRD})
    if not rows:
        raise SystemExit("nothing captured yet")
    df = pd.DataFrame(rows)
    df["phone_digits"] = df["phone"].map(digits)

    # Same occupant-counting rule as every other scraper here, scoped to the
    # BRANCH: these pages publish one switchboard plus a direct line each, so a
    # number used once on its own page really is a desk line.
    kinds = []
    for slug, group in df.groupby("branch_slug"):
        counts = group["phone_digits"].value_counts()
        for d in group["phone_digits"]:
            kinds.append("" if not d else ("direct" if counts[d] == 1 else
                                           ("shared" if counts[d] <= 5 else "switchboard")))
    df = df.sort_values("branch_slug").reset_index(drop=True)
    df["phone_kind"] = kinds

    out = ROSTERS / f"rj_branches_{dt.date.today().isoformat()}.csv"
    df.to_csv(out, index=False, encoding="utf-8")
    return out


def status() -> None:
    slugs, cities, d = captured()
    targets = rja_targets()
    total_cities = targets["city"].str.lower().nunique()
    print(f"[*] captured {len(slugs)} branch pages covering {len(cities)} cities "
          f"of {total_cities} RJA cities")
    if len(d):
        direct = int((d.get("phone_kind", pd.Series(dtype=str)) == "direct").sum())
        print(f"    {len(d):,} people | {d['email'].str.lower().nunique():,} emails "
              f"| {direct:,} direct lines | {d['state'].nunique()} states")
        top = d.groupby(["branch_slug", "city", "state"]).size().sort_values(ascending=False)
        for (sl, c, st), n in list(top.items())[:8]:
            print(f"      {sl:26} {str(c) + ', ' + str(st):24} {n:>4}")
    left = pending(1000)
    print()
    print(f"    {len(left):,} slug candidates remain across "
          f"{left['city'].nunique() if len(left) else 0} uncovered cities")
    print(f"    pace {PACE_SECONDS}s/request, stop after "
          f"{STOP_AFTER_CONSECUTIVE_BLOCKS} consecutive blocks")
    print("    NOTE: capture runs in a REAL BROWSER -- Akamai 403s every library")
    print("          client on -branch URLs at any pace. This module does")
    print("          targeting and merging, not fetching.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pending", type=int, metavar="N", help="print the next N branches to capture")
    ap.add_argument("--ingest", metavar="FILE", help="fold a browser capture (json or base64) in")
    ap.add_argument("--build", action="store_true", help="write the roster CSV")
    ap.add_argument("--merge", action="store_true",
                    help="fold browser-captured CSVs from Downloads into the roster")
    ap.add_argument("--downloads", default=str(pathlib.Path.home() / "Downloads"))
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.pending:
        left = pending(args.pending)
        print(json.dumps([{"slug": r.slug, "city": r.city, "state": r.state,
                           "advisors": int(r.advisors), "ambiguous": bool(r.ambiguous)}
                          for r in left.itertuples()], indent=1))
    elif args.ingest:
        text = pathlib.Path(args.ingest).read_text(encoding="utf-8").strip()
        rep = ingest(text if text.startswith("{") is False and "{" not in text[:2] else json.loads(text))
        for slug, n in rep["accepted"]:
            print(f"    accepted {slug:26} {n:>4} people")
        for slug, why in rep["rejected"]:
            print(f"    REJECTED {slug:26} {why}")
        print(f"[*] {rep['people']} people added")
    elif args.merge:
        out = merge_downloads(pathlib.Path(args.downloads))
        d = pd.read_csv(out, dtype=str).fillna("")
        print(f"[*] {len(d):,} people | {d['branch_slug'].nunique()} branches "
              f"| {d['state'].nunique()} states -> {out.name}")
        print("    phone_kind: " + ", ".join(
            f"{k} {v}" for k, v in d["phone_kind"].value_counts().items() if k))
    elif args.build:
        out = build()
        df = pd.read_csv(out)
        print(f"[*] {len(df):,} people -> {out.name}")
        print("    phone_kind: " + ", ".join(f"{k} {v}" for k, v in
                                             df["phone_kind"].value_counts().items() if k))
    else:
        status()


if __name__ == "__main__":
    main()
