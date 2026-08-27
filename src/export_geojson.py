"""Export a geocoded state's advisor pins as a compact JSON layer for the map.

Not GeoJSON: positional arrays + string dictionaries, with firm-level fields
(motion, score, RAUM, 5.G(7), fit/size) held once per firm. The webapp loader
rehydrates it into the same {geometry, properties} shape. ~5x smaller and much
faster to parse; carries the same fields the popup needs (identity, firm,
address, IAPD link, dual BD/RIA, disclosures, office density).
"""
from __future__ import annotations

import outside_managers

import json
import pathlib
import re
import sys

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
# ONE definition of what an advisor is called, shared with export_national.py
# and build_field_tiles.py.
from display_name import display_name

ROOT = pathlib.Path(__file__).parents[1]
INTERIM = ROOT / "data" / "interim"
WEBAPP = ROOT / "webapp" / "data"

_SUFFIX = re.compile(
    r"\s*\((?:(?P<state>[A-Z]{2}),\s*)?CRD\s*(?P<crd>\d+)\)\s*$",
    re.I,
)

# How well we know where this person actually works. A single uncertain flag
# could not say "we know the town but not the street", which is a different
# claim from "we do not know" -- and they imply different actions for a rep:
# drive there, telephone, or verify first.
LOCATION_CODE = {"office": 0, "remote": 1, "uncertain": 2}

# DRP flag -> human label
DRP = {
    "hasCriminal": "Criminal",
    "hasRegAction": "Regulatory action",
    "hasCivilJudc": "Civil judicial",
    "hasCustComp": "Customer complaint",
    "hasTermination": "Termination",
    "hasJudgment": "Judgment / lien",
    "hasBankrupt": "Bankruptcy",
    "hasBond": "Bond",
    "hasInvstgn": "Investigation",
}


# Legal-form tokens that are written upper by convention. INC, CORP, CO and LTD
# are deliberately NOT here -- convention title-cases those ("Acme Capital,
# Inc."), and the default path already does it.
_UPPER_SUFFIX = {"LLC", "LLP", "LP", "PC", "PLLC", "PA", "NA", "AG", "SA",
                 "NV", "BV", "GP", "PLC"}
# Initialisms that are firm BRANDS. Curated, and deliberately short.
#
# The tempting rule -- "a 2-4 letter token in an all-caps name is an
# initialism" -- is wrong, and the data says so plainly. Counting short tokens
# across the 9,755 distinct firm names, the common ones are ordinary words:
# OAK (46 firms), BLUE (35), HILL (30), NEW (29), FUND (28), PEAK (25),
# WEST (23), PARK, ONE, LIFE, SAGE, BAY, ROCK, TRUE, CITY, EDGE, COVE, CORE.
# That rule would render "Oak Hill Capital" as "OAK HILL Capital" -- far more
# damage than the one case it fixes.
#
# So the list is explicit. An acronym missing from it renders as "Bmo" rather
# than "BMO", which is cosmetic and fixed by adding one word; the inverse error
# shouts a common noun on hundreds of labels.
_BRANDS = {"UBS", "RBC", "LPL", "BNY", "PNC", "SEI", "TIAA", "HSBC", "BMO",
           "AXA", "ING", "TPG", "USA", "US", "JP", "DBA", "RIA", "CFA", "ETF",
           "REIT", "IRA", "BOK", "MUFG", "BBVA", "CIBC", "TD", "AIG", "GE"}
# A VALID Roman numeral, not merely a word spelled from Roman letters. The
# loose character-class version kept MILL, DIVVI, VIVID and MCMILL upper --
# every one of them is a real word whose letters all happen to be numerals.
_ROMAN_TOKEN = re.compile(
    r"^M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})[.,]?$")
_AMP_INITIALS = re.compile(r"^[A-Z]{1,3}&[A-Z]{1,3}[.,]?$")
_ORDINAL = re.compile(r"(\d)(St|Nd|Rd|Th)\b")
_MINOR = {"of", "the", "and", "for", "in", "on", "at", "to", "by", "a", "an"}


def _cased(text: str) -> str:
    """Title-case a firm name without inventing words that are not its name.

    `.title()` was applied unconditionally, which damaged the 9,755 firm names
    in this dataset -- every one of which the SEC files in ALL CAPS. "UBS
    FINANCIAL SERVICES INC." became "Ubs Financial Services Inc." on the map
    label, in the details header, and in every generated email subject.
    """
    text = str(text or "").strip()
    if not text.isupper():
        # Already cased by whoever filed it. No filing in the current data
        # reaches this branch, but a future source with mixed-case names must
        # not be re-cased on the way through.
        return text
    out = []
    for i, token in enumerate(text.split(" ")):
        core = re.sub(r"[^A-Z]", "", token)
        keep = (core in _UPPER_SUFFIX or core in _BRANDS
                or (core and _ROMAN_TOKEN.match(core))
                # An ampersand INSIDE a token joins initials (S&P, AT&T). A
                # bare "&" between words, and "&PARTNERS", are not that -- the
                # old rule kept both upper and shouted "&WEALTH PARTNERS".
                or _AMP_INITIALS.match(token))
        word = token if keep else token.title()
        # `.title()` capitalises after a digit, so "10TH" became "10Th".
        word = _ORDINAL.sub(lambda m: m.group(1) + m.group(2).lower(), word)
        # Minor words read lower inside a name, but never as the first word.
        if i and not keep and word.lower().strip(",.") in _MINOR:
            word = word.lower()
        out.append(word)
    return " ".join(out)


def display_firm(name: str) -> str:
    """Title-case a display name without discarding its identity qualifier."""
    if not isinstance(name, str):
        return "Unknown firm"
    match = _SUFFIX.search(name)
    base = _cased(_SUFFIX.sub("", name).strip())
    if not match:
        return base
    state = match.group("state")
    qualifier = f"{state.upper()}, " if state else ""
    return f"{base} ({qualifier}CRD {match.group('crd')})"


def apply_placement(p: pd.DataFrame) -> pd.DataFrame:
    """One pin per advisor-firm relationship, chosen in placement.py.

    Without this the map drew a pin per advisor-BRANCH pairing, so an advisor
    registered at a home office and working from a branch appeared at both --
    3,737 advisors at One Bryant Park, 466 in a single Stamford suite. The
    duplicates landed on exactly the addresses a rep would investigate first.
    Rows carry `uncertain` when the employment record names a city the advisor
    has no branch in, so the panel can say the address is not corroborated.
    """
    path = INTERIM / "advisor_placement.parquet"
    if not path.exists():
        print("  (no advisor_placement.parquet -- keeping every branch row)")
        p["uncertain"], p["home_label"] = False, ""
        return p
    pl = pd.read_parquet(path)
    pl["advisor_crd"] = pl["advisor_crd"].astype(str)
    pl["firm_crd"] = pl["firm_crd"].astype(str)
    p["firm_crd"] = p["firm_crd"].astype(str)
    p["addr_key"] = (p["branch_street1"].fillna("").astype(str).str.strip().str.upper() + "|"
                     + p["branch_city"].fillna("").astype(str).str.strip().str.upper() + "|"
                     + p["branch_postal"].fillna("").astype(str).str.strip().str[:5])
    # City-level rows keep the key they were built with. Recomputing it here
    # gives "|SPRINGFIELD|" -- identical for Illinois, Massachusetts and
    # Missouri -- so their key carries the state where a postcode would go.
    if "city_addr_key" in p.columns:
        is_city = p["city_level"].fillna(False).astype(bool)
        p.loc[is_city, "addr_key"] = p.loc[is_city, "city_addr_key"]
    before = len(p)
    p = p.merge(pl, on=["advisor_crd", "firm_crd", "addr_key"], how="inner")
    # A handful of advisors are filed twice at one address for one firm --
    # usually a differing suite line on street2 -- which the join would turn
    # back into two pins. One placement means one pin.
    p = p.drop_duplicates(["advisor_crd", "firm_crd"], keep="first")
    p["uncertain"] = p["uncertain"].fillna(False)
    p["home_label"] = p["home_label"].fillna("")
    p["location_type"] = p["location_type"].fillna("office")
    kinds = p["location_type"].value_counts().to_dict()
    print(f"  placement: {before:,} branch rows -> {len(p):,} pins  "
          + ", ".join(f"{k} {v:,}" for k, v in sorted(kinds.items())))
    return p


def load_state_branches(state: str) -> pd.DataFrame:
    """Street-addressed branches plus the city-only ones the geocoder skipped.

    123,931 branch records name a city and state but no street, so the geocoder
    dropped them and placement.py never saw them. That silently favoured head
    offices: a firm's own filing that someone works in Villanova could not
    compete with its headquarters, because only one of the two existed.
    """
    p = pd.read_parquet(INTERIM / f"branch_geocoded_{state}.parquet")
    p["city_level"] = False
    city_path = INTERIM / "branch_city_level.parquet"
    if city_path.exists():
        city = pd.read_parquet(city_path)
        city = city[city["branch_state"].astype(str).str.upper().str.strip() == state]
        if len(city):
            p = pd.concat([p, city], ignore_index=True, sort=False)
    return p


def export(state: str) -> None:
    p = load_state_branches(state)
    p = p[p["lat"].notna() & p["lon"].notna()].copy()
    p["advisor_crd"] = p["advisor_crd"].astype(str)
    p = apply_placement(p)

    adv = pd.read_parquet(ROOT / "data" / "output" / "advisors.parquet")
    keep = ["advisor_crd", "iapd_url", "active_ag_reg", "designations",
            "n_prior_firms", "n_exams", "used_first_name"] + list(DRP)
    adv = adv[[c for c in keep if c in adv.columns]].copy()
    adv["advisor_crd"] = adv["advisor_crd"].astype(str)
    p = p.merge(adv, on="advisor_crd", how="left", suffixes=("", "_a"))

    # IAR-side state registrations for this advisor AT THIS FIRM.
    # NOTE: this is the advisory footprint only. IAPD's headline "Licenses"
    # count also includes broker-dealer agent + SRO registrations, which live
    # in BrokerCheck, not this feed -- so this number is legitimately smaller.
    emp = pd.read_parquet(ROOT / "data" / "output" / "advisor_employments.parquet")
    emp = emp[["advisor_crd", "firm_crd", "n_registrations", "reg_states"]].copy()
    emp["advisor_crd"] = emp["advisor_crd"].astype(str)
    emp["firm_crd"] = emp["firm_crd"].astype(str)
    p["firm_crd"] = p["firm_crd"].astype(str)
    p = p.merge(emp, on=["advisor_crd", "firm_crd"], how="left")

    exp_path = ROOT / "data" / "output" / "advisor_experience.parquet"
    if exp_path.exists():
        ex = pd.read_parquet(exp_path)[["advisor_crd", "years_experience", "experience_band"]]
        ex["advisor_crd"] = ex["advisor_crd"].astype(str)
        p = p.merge(ex, on="advisor_crd", how="left")
    else:
        p["years_experience"], p["experience_band"] = pd.NA, pd.NA

    # Firm-level targeting attributes for the AUM range + 5.G(7) filter and the
    # fit/size split shown in each firm row. Sourced from firms.parquet (the score
    # source of truth) rather than the branch parquet, so they never drift from it.
    fm = pd.read_parquet(ROOT / "data" / "output" / "firms.parquet")[
        ["crd", "motion", "raum_total", "g_select_advisers", "is_wrap_sponsor",
         "wrap_both_amt", "wrap_sponsor_amt", "wrap_n_programs",
         "opportunity_score_fit", "opportunity_score_size"]].rename(columns={"crd": "_fmcrd"})
    fm = fm.rename(columns={"motion": "_current_motion"})
    fm["_fmcrd"] = fm["_fmcrd"].astype(str)
    p = p.merge(fm, left_on="firm_crd", right_on="_fmcrd", how="left")
    # 5.I(2)(a) sponsor-only + 5.I(2)(c) sponsor-and-manager. Not (2)(b),
    # which is managing money inside somebody else's programme.
    p["wrap_amount"] = (p.get("wrap_sponsor_amt").fillna(0)
                        + p.get("wrap_both_amt").fillna(0))

    # Show the name the person actually goes by. Form U4 <OthrNms> says Edison
    # Tate Lambeth goes by Tate and Thomas Tolleson goes by Tom; 22.8% of
    # individuals declare a used first name that differs from the filed one, and
    # a rep would never say "Edison". The filed name is retained in the national
    # search index so both still match.
    # THE THIRD COPY OF THIS RULE, now deleted.
    #
    # The map pin, the desktop search index and the field tile each built a
    # display name their own way, and 47,371 advisors ended up with a different
    # name depending on which one a rep was looking at. This one also lost the
    # McKay/Mckay distinction, because .title() lowercases anything after the
    # first letter of a word.
    #
    # src/display_name.py holds the rule now. All three call it.
    used = (p["used_first_name"].fillna("") if "used_first_name" in p
            else pd.Series("", index=p.index))
    p["name"] = [
        display_name(f, l, u) or "Name unavailable"
        for f, l, u in zip(p["first_name"].fillna(""),
                           p["last_name"].fillna(""), used)
    ]
    # firm_display is unique by CRD when legal entities share a name. Keep that
    # qualifier in the label and also carry firm_crd as the canonical key.
    p["firm"] = p["firm_display"].map(display_firm)
    p["motion"] = p["_current_motion"].fillna(p["motion"]).fillna("unclassified")

    # advisors sharing this exact office (same address + firm)
    p["office_key"] = p["addr_key"] + "::" + p["firm_crd"].astype(str)
    dens = p.groupby("office_key")["advisor_crd"].nunique().rename("office_adv")
    p = p.join(dens, on="office_key")

    # ---- compact wire format --------------------------------------------
    # GeoJSON repeats every property NAME on every pin and re-states firm-level
    # facts (motion, score, RAUM, 5.G(7), fit/size) once per advisor. This drops
    # both: positional arrays instead of named objects, string dictionaries for
    # the heavy repeaters (firm, address, city, designations, reg-states), and
    # firm-level fields moved into the firm dictionary. The loader rehydrates it
    # into the exact same {geometry, properties} shape, so nothing downstream
    # changes. Measured ~5x smaller and much faster to parse than GeoJSON.
    def interner():
        idx, order = {}, []
        def add(v):
            i = idx.get(v)
            if i is None:
                i = idx[v] = len(order); order.append(v)
            return i
        return add, order

    mot_add, motions = interner()
    addr_add, addrs = interner()
    city_add, cities = interner()
    des_add, desig = interner()
    reg_add, regs = interner()
    gp_add, gps = interner()
    xb_add, xbs = interner()
    drp_cols = list(DRP)                      # bit order for the disclosure mask

    # CRD, not display name, is the identity key. Same-named legal entities can
    # have different scores, motions, and assets.
    firm_idx, firms = {}, []      # crd -> idx; [name, mIdx, s, ra, sg, sf, sz, crd, why, wrapM]
    def firm_add(r):
        crd = str(r["firm_crd"])
        name = r["firm"]
        i = firm_idx.get(crd)
        if i is None:
            i = firm_idx[crd] = len(firms)
            firms.append([
                name, mot_add(r["motion"]),
                None if pd.isna(r["opportunity_score"]) else round(float(r["opportunity_score"]), 1),
                None if pd.isna(r.get("raum_total")) else int(round(float(r["raum_total"]) / 1e6)),
                # Either signal -- see src/outside_managers.py. 5.G(7) alone hid
                # LPL and 588 other wrap sponsors behind a filter that is on by
                # default.
                1 if outside_managers.hires_outside_managers(r) else 0,
                None if pd.isna(r.get("opportunity_score_fit")) else round(float(r["opportunity_score_fit"]), 1),
                None if pd.isna(r.get("opportunity_score_size")) else round(float(r["opportunity_score_size"]), 1),
                crd,
                # WHICH signal fired. Appended last so every existing positional
                # reader of this row is untouched: "No outside-manager selection
                # reported" was literally true of LPL's 5.G(7) and useless to a
                # rep looking at a $598.6B wrap sponsor.
                outside_managers.reason(r),
                None if pd.isna(r.get("wrap_amount")) else int(round(float(r["wrap_amount"]) / 1e6)),
            ])
        return i

    pins = []
    for _, r in p.iterrows():
        street = r["branch_street1"] if isinstance(r["branch_street1"], str) else ""
        s2 = r.get("branch_street2")
        if isinstance(s2, str) and s2.strip() and s2.strip().lower() != "none":
            street = f"{street}, {s2.strip()}"
        bits = 0
        for bi, col in enumerate(drp_cols):
            if r.get(col) is True or str(r.get(col)).upper() == "Y":
                bits |= (1 << bi)
        band = r["experience_band"] if isinstance(r.get("experience_band"), str) else ""
        rs   = r["reg_states"] if isinstance(r.get("reg_states"), str) else ""
        gval = r["designations"] if isinstance(r.get("designations"), str) and r["designations"] else ""
        city = r["branch_city"].title() if isinstance(r["branch_city"], str) else ""
        pins.append([
            round(r["lon"], 5), round(r["lat"], 5),          # 0,1
            firm_add(r),                                     # 2
            addr_add(street.title()),                        # 3
            city_add(city),                                  # 4
            str(r["branch_postal"]) if pd.notna(r["branch_postal"]) else "",  # 5
            r["advisor_crd"],                               # 6
            r["name"],                                       # 7
            None if pd.isna(r.get("years_experience")) else round(float(r["years_experience"]), 1),  # 8
            xb_add(band) if band else -1,                    # 9
            None if pd.isna(r.get("n_registrations")) else int(r["n_registrations"]),  # 10
            reg_add(rs) if rs else -1,                       # 11
            gp_add(r["geocode_precision"] if isinstance(r.get("geocode_precision"), str) else ""),  # 12
            1 if str(r.get("active_ag_reg")) == "Y" else 0,  # 13 dually
            des_add(gval) if gval else -1,                   # 14
            None if pd.isna(r.get("n_prior_firms")) else int(r["n_prior_firms"]),  # 15
            int(r["office_adv"]),                            # 16
            bits,                                            # 17 disclosure mask
            1 if isinstance(r.get("iapd_url"), str) and r["iapd_url"] else 0,      # 18 has IAPD url
            # APPEND new fields here. The indices above are read positionally by
            # the webapp's rehydrate() and by the summary below, so inserting
            # mid-array silently shifts every field after it.
            1 if r.get("uncertain") else 0,                  # 19 address not corroborated
            str(r.get("home_label") or ""),                  # 20 where employment says they are
            LOCATION_CODE.get(r.get("location_type"), 0),    # 21 how the location is known
        ])

    out_obj = {
        "iapd": "https://adviserinfo.sec.gov/individual/summary/",
        "firms": firms, "motions": motions, "addrs": addrs, "cities": cities,
        "desig": desig, "regs": regs, "gp": gps, "xb": xbs, "pins": pins,
    }
    WEBAPP.mkdir(parents=True, exist_ok=True)
    out = WEBAPP / f"pins_{state}.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(out_obj, fh, separators=(",", ":"))

    dual = sum(1 for pn in pins if pn[13])
    withu = sum(1 for pn in pins if pn[18])
    ids = {pn[6] for pn in pins}
    mb = out.stat().st_size / 1e6
    print(f"{state}: {len(pins):,} pins -> {out.name}  {mb:.1f} MB "
          f"({len(firms)} firms, {len(addrs)} addrs, {len(cities)} cities)")
    print(f"  dually registered {dual:,} | IAPD links {withu:,} | distinct advisors {len(ids):,}")


if __name__ == "__main__":
    export(sys.argv[1] if len(sys.argv) > 1 else "GA")
