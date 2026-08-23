"""Match Forbes-ranked advisors to CRDs, and measure how often it is right.

THE PROBLEM
Forbes publishes no CRD, so every match is inferred. Naive name matching is
not merely imprecise here, it is dangerous: a false positive puts a "FORBES
RANKED" badge on the wrong person, and a rep opening a call by congratulating
someone on a ranking they do not hold costs more credibility than showing
nothing at all. The design goal is therefore PRECISION, and the tool for it is
refusing to answer.

WHY IT IS TRACTABLE
Display names diverge from SEC filed names about 41% of the time, usually
because the person goes by a middle name -- SEC "Shannon Sullivan" is Forbes
"Andrew Sullivan". Reading only the filed name, that looks unmatchable. But we
also hold middle_name and used_first_name, and between them they carry the
display name in 11 of 13 sampled divergences. The remaining two are a standard
nickname (Andrew -> Andy) and a middle initial (John R. -> Rod).

THE METHOD
  1. Hard gate: normalised surname AND state must agree. Surname held in
     13 of 13 divergent cases; it is the one stable component of a name.
  2. Expand each SEC advisor into every given name they might go by --
     first, middle, used, parentheticals, nicknames, initials.
  3. Score name / firm / city agreement.
  4. AMBIGUITY REJECTION. If the runner-up scores close to the winner, emit
     nothing. Two Sullivans at the same firm in the same metro is exactly the
     case that produces a confident, wrong answer, so it must produce silence.
  5. Calibrate on ground truth. Advisors on BOTH Barron's and Forbes have a
     known CRD from the BrokerCheck link Barron's provides, which gives a
     labelled set to measure precision and recall against rather than guess.

Writes data/interim/forbes_matches.parquet with a tier per row:
    confirmed  -- CRD known from the Barron's bridge, not inferred
    high       -- inferred, above threshold and unambiguous
    review     -- plausible but ambiguous or weak; NEVER shown as a fact
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
from collections import defaultdict

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
INTERIM = ROOT / "data" / "interim"
WEB = ROOT / "webapp" / "data"

# Scoring weights. Name agreement is necessary but not sufficient: the firm is
# what separates two people with the same name in the same state.
W_NAME, W_FIRM, W_CITY, W_STATE = 0.42, 0.30, 0.13, 0.15
# Large enough to break a father/son tie (MARGIN is 0.12), small enough that it
# cannot rescue an otherwise-poor match on its own.
W_SUFFIX = 0.18
ACCEPT = 0.72          # minimum total score for tier "high"
MARGIN = 0.12          # winner must beat runner-up by this much, else ambiguous
UNKNOWN_FIRM = 0.30    # weak prior when we hold no firm name for a candidate

# How many advisors nationally share this published name. The single strongest
# predictor of a wrong match, measured on the 1,303 labelled pairs:
#     1 namesake  99.7% precision      5-9 namesakes  94.5%
#     2           98.5%                10+            79.2%
# The 10+ bucket alone produces most of the errors, and no score threshold
# catches them -- they arrive at a perfect 1.0, because a same-named colleague
# at the same firm in the same city genuinely agrees on every signal we hold.
# Commonness is the only thing that separates them, so it is gated, not scored.
NAMESAKE_CAP = 3

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v", "md", "cfp", "cfa", "cpa", "phd"}

# Bidirectional nickname groups. Deliberately conservative -- a wrong pair here
# manufactures false positives, which is the failure this whole module exists
# to avoid.
NICKNAME_GROUPS = [
    {"andrew", "andy", "drew"}, {"james", "jim", "jimmy", "jamie", "jay"},
    {"robert", "rob", "bob", "bobby", "robbie"}, {"william", "bill", "will", "billy", "willie"},
    {"richard", "rick", "dick", "rich", "richie"}, {"michael", "mike", "mickey"},
    {"thomas", "tom", "tommy"}, {"charles", "charlie", "chuck", "chip"},
    {"joseph", "joe", "joey"}, {"edward", "ed", "eddie", "ted", "ned"},
    {"john", "jack", "johnny", "jon"}, {"daniel", "dan", "danny"},
    {"matthew", "matt"}, {"christopher", "chris"}, {"anthony", "tony"},
    {"kenneth", "ken", "kenny"}, {"stephen", "steven", "steve"},
    {"gregory", "greg"}, {"benjamin", "ben", "benny"}, {"samuel", "sam", "sammy"},
    {"nicholas", "nick"}, {"alexander", "alex"}, {"patrick", "pat"},
    {"timothy", "tim"}, {"frederick", "fred", "freddie"}, {"lawrence", "larry"},
    {"gerald", "jerry"}, {"donald", "don", "donnie"}, {"ronald", "ron", "ronnie"},
    {"jeffrey", "jeff"}, {"douglas", "doug"}, {"peter", "pete"},
    {"harold", "hal", "harry"}, {"walter", "walt"}, {"albert", "al"},
    {"arthur", "art"}, {"raymond", "ray"}, {"eugene", "gene"},
    {"francis", "frank", "frankie"}, {"theodore", "ted", "teddy"},
    {"vincent", "vince"}, {"philip", "phillip", "phil"}, {"martin", "marty"},
    {"leonard", "len", "lenny"}, {"russell", "russ"}, {"stanley", "stan"},
    {"curtis", "curt"}, {"rodney", "rod"}, {"roderick", "rod"},
    {"bradley", "brad"}, {"jonathan", "jon"}, {"joshua", "josh"},
    {"zachary", "zach"}, {"jacob", "jake"}, {"nathaniel", "nathan", "nate"},
    {"elizabeth", "liz", "beth", "betsy", "libby"}, {"katherine", "catherine", "kathy", "kate", "katie", "cathy"},
    {"margaret", "maggie", "meg", "peggy"}, {"patricia", "patty", "trish", "tricia"},
    {"barbara", "barb"}, {"jennifer", "jen", "jenny"}, {"deborah", "debra", "deb", "debbie"},
    {"rebecca", "becky", "becca"}, {"susan", "sue", "susie"}, {"cynthia", "cindy"},
    {"victoria", "vicki", "vicky"}, {"stephanie", "steph"}, {"kimberly", "kim"},
    {"melissa", "mel"}, {"amanda", "mandy"}, {"christine", "christina", "christy", "chrissy"},
    {"theresa", "teresa", "terry", "terri"}, {"virginia", "ginny"},
    {"charlotte", "charlie"}, {"frances", "fran"}, {"eleanor", "ellie"},
    {"alexandra", "alexandria", "allie"}, {"danielle", "dani"}, {"samantha"},
]
NICKNAMES: dict[str, set[str]] = defaultdict(set)
for group in NICKNAME_GROUPS:
    for name in group:
        NICKNAMES[name] |= group


def norm(text) -> str:
    return re.sub(r"[^a-z]", "", str(text or "").lower())


def edit1(a: str, b: str) -> bool:
    """True when a and b are within one edit. Catches the SEC filing typo
    'ADNY' for 'ANDY' -- real, and it cost a match in the sample."""
    if abs(len(a) - len(b)) > 1:
        return False
    if a == b:
        return True
    if len(a) > len(b):
        a, b = b, a
    i = j = 0
    seen = False
    while i < len(a) and j < len(b):
        if a[i] != b[j]:
            if seen:
                return False
            seen = True
            if len(a) == len(b):
                i += 1
            j += 1
        else:
            i += 1
            j += 1
    return True


GENERATIONAL = {"jr", "sr", "ii", "iii", "iv", "v", "vi"}


def suffix_of(text) -> str:
    """The generational suffix a name carries, or "".

    Read from BOTH sides because the two disagree about where it lives: a
    published name puts it after the surname ("Lynn Shaw II"), while the SEC
    bakes it into last_name ("SHAW II"). Dropping it from one side and keeping
    it in the other is what let a father and son collide.
    """
    tokens = [norm(t) for t in re.split(r"[\s.,]+", str(text or "")) if t]
    for token in reversed(tokens):
        if token in GENERATIONAL:
            return token
        if token:
            break
    return ""


def suffix_agreement(a: str, b: str) -> float:
    """+1 both carry the same suffix, -1 they conflict, 0 uninformative.

    Deliberately three-valued. An advisor with no suffix is not evidence
    AGAINST being the Jr -- filers are inconsistent -- so only an explicit
    disagreement is penalised.
    """
    if a and b:
        return 1.0 if a == b else -1.0
    return 0.0


def split_name(full: str) -> tuple[list[str], str]:
    """'P. Schuyler Quackenbush, Jr.' -> (['p','schuyler'], 'quackenbush')."""
    text = re.sub(r"[.,]", " ", str(full or ""))
    tokens = [t for t in text.split() if t]
    tokens = [t for t in tokens if norm(t) not in SUFFIXES]
    if not tokens:
        return [], ""
    if len(tokens) == 1:
        return [], norm(tokens[0])
    return [norm(t) for t in tokens[:-1] if norm(t)], norm(tokens[-1])


def given_forms(row) -> set[str]:
    """Every given name this SEC advisor might be published under.

    first_name can hold two tokens ('CHRISTOPHER ANDREW') and used_first_name
    can hold a parenthetical ('JAMES (JAY)'), so both are tokenised rather than
    taken whole.
    """
    forms: set[str] = set()
    # Filers put the nickname wherever they like, including the SURNAME field:
    # CRD 862872 is filed as last_name "Westmoreland (Rod)" and Forbes prints
    # him as Rod Westmoreland. Take ONLY the parenthetical from the surname --
    # adding the surname itself as a given name would invent matches.
    for bracket in re.findall(r"\(([^)]*)\)", str(getattr(row, "last_name", "") or "")):
        for token in re.split(r"[\s,/]+", bracket):
            token = norm(token)
            if token:
                forms.add(token)
    for field in ("first_name", "middle_name", "used_first_name"):
        value = getattr(row, field, None)
        if not value or pd.isna(value):
            continue
        for token in re.split(r"[\s()/,]+", str(value)):
            token = norm(token)
            if token:
                forms.add(token)
    for token in list(forms):
        forms |= NICKNAMES.get(token, set())
    return forms


def name_score(forbes_given: list[str], sec_forms: set[str]) -> float:
    """Best agreement between any published given name and any known form."""
    if not forbes_given or not sec_forms:
        return 0.0
    best = 0.0
    for token in forbes_given:
        for form in sec_forms:
            if token == form:
                best = max(best, 1.0)
            elif len(token) > 1 and len(form) > 1 and edit1(token, form):
                best = max(best, 0.80)
            elif (len(token) == 1 or len(form) == 1) and token[0] == form[0]:
                # a bare initial is weak evidence and must not carry a match by
                # itself -- firm and city have to do the work
                best = max(best, 0.45)
    return best


_FIRM_NOISE = re.compile(
    r"\b(llc|l\.l\.c|inc|incorporated|corp|corporation|co|company|lp|llp|ltd|"
    r"the|and|of|group|wealth|management|advisors|advisers|advisory|financial|"
    r"services|capital|partners|private|investment|investments|securities|"
    r"planning|associates|network|consulting)\b", re.I)


def firm_key(name: str) -> set[str]:
    """Distinctive tokens of a firm name, with the boilerplate stripped.

    'Merrill Private Wealth Management' -> {'merrill'}
    'Smith Capital Advisors | Northwestern Mutual' is split by the caller.
    """
    text = _FIRM_NOISE.sub(" ", str(name or "").lower())
    return {t for t in re.split(r"[^a-z]+", text) if len(t) >= 2}


def firm_score(forbes_firm: str, sec_firm_names: list[str]) -> float | None:
    """Forbes prints marketing names and often 'Team Name | Broker-Dealer',
    so each side is compared part by part and the best pairing wins.

    Two different kinds of "cannot compare", which must NOT be treated alike:

    None  -- the FORBES name is uninformative ("Capital Investment Advisors"
             reduces to an empty key once boilerplate is stripped). This
             affects every candidate equally, so the caller redistributes the
             weight. Scoring it 0 buried four correct Georgia matches.

    UNKNOWN_FIRM -- we hold no firm name for THIS candidate. That is specific
             to one candidate, so redistributing would promote a person we
             know nothing about to tie with one whose firm positively agrees.
             Exactly that produced the only false-positive risk in the sample:
             two Matthew Williams in Georgia, one confirmed at Merrill, the
             other at a firm with no profile. A weak prior keeps the confirmed
             candidate ahead.
    """
    if not forbes_firm:
        return None
    parts = [p for p in (firm_key(p) for p in str(forbes_firm).split("|")) if p]
    if not parts:
        return None
    sec_keys = [k for k in (firm_key(n) for n in (sec_firm_names or [])) if k]
    if not sec_keys:
        return UNKNOWN_FIRM
    best = 0.0
    for sec_tokens in sec_keys:
        for part in parts:
            overlap = part & sec_tokens
            if overlap:
                best = max(best, len(overlap) / max(1, min(len(part), len(sec_tokens))))
    return min(1.0, best)


def load_reference():
    advisors = pd.read_parquet(
        INTERIM / "advisors.parquet",
        columns=["advisor_crd", "first_name", "middle_name", "last_name",
                 "suffix", "used_first_name"])
    advisors["advisor_crd"] = advisors["advisor_crd"].astype(str)
    # strip any parenthetical before keying, or "Westmoreland (Rod)" indexes
    # under "westmorelandrod" and can never be found
    advisors["last_key"] = advisors["last_name"].map(
        lambda v: norm(re.sub(r"\([^)]*\)", " ", str(v or ""))))

    branches = pd.read_parquet(
        ROOT / "data" / "output" / "advisor_branches.parquet",
        columns=["advisor_crd", "firm_crd", "branch_city", "branch_state"])
    branches["advisor_crd"] = branches["advisor_crd"].astype(str)
    branches["firm_crd"] = branches["firm_crd"].astype(str)

    # current employment only: to_date null means the person is still there,
    # and a former office in another state is not evidence about today
    employment = pd.read_parquet(
        INTERIM / "advisor_employment_history.parquet",
        columns=["advisor_crd", "firm_name_on_record", "city", "state", "to_date"])
    employment = employment[employment["to_date"].isna()].copy()
    employment["advisor_crd"] = employment["advisor_crd"].astype(str)

    profiles = json.loads((WEB / "firm_profiles.json").read_text(encoding="utf-8"))
    # the profile field is "name" (with "legal" and "aka" alongside); reading a
    # key that does not exist silently zeroed the firm signal on every row
    firm_name = {}
    for crd, p in profiles["profiles"].items():
        names = [p.get("name"), p.get("legal"), *(p.get("aka") or [])]
        firm_name[crd] = [n for n in names if n]
    try:
        aliases = json.loads((WEB / "firm_aliases.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        aliases = {}
    return advisors, branches, employment, firm_name, aliases


def build_index(advisors, branches, employment):
    """(surname, state) -> candidate rows. The hard gate lives here: anything
    not sharing a surname AND a state is never even scored."""
    where = defaultdict(lambda: {"states": set(), "cities": set(),
                                 "firms": set(), "names": set()})
    for row in branches.itertuples(index=False):
        entry = where[row.advisor_crd]
        if row.branch_state:
            entry["states"].add(str(row.branch_state).upper())
        if row.branch_city:
            entry["cities"].add(norm(row.branch_city))
        entry["firms"].add(row.firm_crd)

    # Current employment, folded in as a SECOND opinion on location.
    #
    # Branch registrations say where an advisor's office is filed, which is not
    # always where they work -- this project already measures 10.4% of advisors
    # with an employment state matching no branch state, and validated
    # employment history 7/7 against branch records 3/7 on EIC's own staff.
    # That gap is not academic here: of 35 false positives, 31 were cases where
    # the CORRECT person had no branch in the state Forbes places them, scored
    # zero on location, and lost to a same-named colleague who did.
    for row in employment.itertuples(index=False):
        entry = where.get(row.advisor_crd)
        if entry is None:
            continue
        if row.state:
            entry["states"].add(str(row.state).upper())
        if row.city:
            entry["cities"].add(norm(row.city))
        if row.firm_name_on_record:
            entry["names"].add(str(row.firm_name_on_record))

    # Keyed on SURNAME ALONE, deliberately. Gating on (surname, state) as well
    # looked safer and was the opposite: on the full harvest it produced 35
    # false positives, and in 34 of them the correct person was not in the
    # candidate pool at all. Their SEC branch state simply differs from where
    # Forbes says they work -- the same remote-worker divergence this project
    # already measures at 10.4% nationally. Excluded from the pool, the right
    # advisor cannot compete, and a same-named colleague at the same firm and
    # city wins uncontested with a perfect score. State is now scored, not
    # gated, so the true candidate is present to win or to force a tie -- and
    # a tie is rejected, which is safe.
    index = defaultdict(list)
    for row in advisors.itertuples(index=False):
        entry = where.get(row.advisor_crd)
        if not entry:
            continue
        forms = given_forms(row)
        if not forms or not row.last_key:
            continue
        # The SEC bakes a generational suffix into last_name, so CRD 5281870
        # ("LYNN TRUSTY SHAW II") is keyed under "shawii" -- while split_name
        # STRIPS the suffix from published names, so every query for that man
        # asks for "shaw" and can never reach him. His father, CRD 856092
        # ("SHAW"), was the only candidate and absorbed the son's email and
        # direct line. Key suffixed advisors under BOTH forms so both are
        # reachable, and carry the suffix so scoring can tell them apart.
        suffix = suffix_of(getattr(row, "last_name", ""))
        entry = {**entry, "suffix": suffix}
        keys = {row.last_key}
        if suffix:
            stripped = norm(re.sub(r"\s*(?:jr|sr|ii|iii|iv|v|vi)\.?\s*$", " ",
                                   str(row.last_name or ""), flags=re.I))
            if stripped:
                keys.add(stripped)
        for key in keys:
            index[key].append((row.advisor_crd, forms, entry))
    return index


def match(forbes: pd.DataFrame, index, firm_name, aliases, truth=None):
    rows = []
    for rec in forbes.itertuples(index=False):
        given, last = split_name(rec.advisor_name)
        state = rec.rank_state
        known = truth.get(rec.forbes_uri) if truth else None

        scored = []
        for crd, forms, entry in index.get(last, []):
            n = name_score(given, forms)
            if n <= 0:
                continue
            names = []
            for fc in entry["firms"]:
                names.extend(firm_name.get(fc, []))
                names.extend(aliases.get(fc, []))
            # the firm name as the advisor's own employment record states it,
            # which is often closer to Forbes' marketing name than the legal one
            names.extend(entry["names"])
            f = firm_score(rec.firm_name_forbes, names)
            c = 1.0 if norm(rec.city) in entry["cities"] else 0.0
            st = 1.0 if state and state in entry["states"] else 0.0
            # Renormalise over the signals actually available, so an
            # uncomparable firm name neither helps nor penalises.
            if f is None:
                total = ((W_NAME * n + W_CITY * c + W_STATE * st)
                         / (W_NAME + W_CITY + W_STATE))
            else:
                total = W_NAME * n + W_FIRM * f + W_CITY * c + W_STATE * st
            # A father and son at the same firm, city and state agree on every
            # other signal, so the suffix is the only thing that separates
            # them. Applied as an adjustment rather than a weighted term
            # because it is usually 0 -- most names carry no suffix at all,
            # and an absent suffix is not evidence against being the Jr.
            total += W_SUFFIX * suffix_agreement(suffix_of(rec.advisor_name),
                                                 entry.get("suffix", ""))
            scored.append((total, crd, n, -1.0 if f is None else f, c))

        scored.sort(reverse=True)
        best = scored[0] if scored else None
        namesakes = sum(1 for row in scored if row[2] >= 0.8)
        second = scored[1][0] if len(scored) > 1 else 0.0
        ambiguous = bool(best) and (best[0] - second) < MARGIN

        if known:
            tier, crd, score = "confirmed", known, 1.0
        elif best and best[0] >= ACCEPT and not ambiguous and namesakes <= NAMESAKE_CAP:
            tier, crd, score = "high", best[1], best[0]
        elif best:
            tier, crd, score = "review", best[1], best[0]
        else:
            tier, crd, score = "none", None, 0.0

        rows.append({
            "forbes_uri": rec.forbes_uri, "advisor_name": rec.advisor_name,
            "firm_name_forbes": rec.firm_name_forbes, "city": rec.city,
            "rank_state": state, "category": rec.category, "rank": rec.rank,
            "rank_market": rec.rank_market, "rank_segment": rec.rank_segment,
            "team_assets_usd": rec.team_assets_usd,
            "advisor_crd": crd, "match_tier": tier, "match_score": round(score, 3),
            "candidates": len(scored), "ambiguous": ambiguous,
            "namesakes": namesakes,
            "name_score": round(best[2], 2) if best else 0.0,
            "firm_score": round(best[3], 2) if best else 0.0,
            "city_score": round(best[4], 2) if best else 0.0,
            # What the matcher would have concluded on its own. Kept separate
            # from match_score because a confirmed row is handed its CRD and
            # scores 1.0 by fiat -- calibrating on that would grade the
            # matcher against its own answer key.
            "inferred_crd": best[1] if best else None,
            "inferred_score": round(best[0], 3) if best else 0.0,
        })
    return pd.DataFrame(rows)


def barrons_truth() -> dict[str, str]:
    """Forbes uri -> CRD, for advisors on both lists. Barron's slugs and Forbes
    uris are usually but not always identical, so names are used as the bridge
    and the slug only as a tiebreak."""
    path = WEB / "barrons.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for crd, entries in data["advisors"].items():
        for entry in entries:
            slug = str(entry[4]).rstrip("/").rsplit("/", 1)[-1]
            out.setdefault(re.sub(r"-\d+$", "", slug), crd)
    return out


def calibrate(matched: pd.DataFrame, truth: dict[str, str]) -> None:
    """Measure the inferred matcher against the CRDs we actually know.

    Only rows whose truth is known are scored, and the matcher's OWN inference
    is compared -- not the confirmed CRD it was handed -- otherwise this would
    grade its own answer key.
    """
    known = matched[matched["forbes_uri"].map(
        lambda u: re.sub(r"-\d+$", "", str(u)) in truth)].copy()
    if known.empty:
        print("\nNo overlap with Barron's -- cannot calibrate.")
        return
    known["truth_crd"] = known["forbes_uri"].map(
        lambda u: truth[re.sub(r"-\d+$", "", str(u))])
    known["correct"] = known["inferred_crd"] == known["truth_crd"]

    print(f"\nCalibration against {len(known)} advisors with a known CRD")
    print(f"{'threshold':>10} {'accepted':>9} {'correct':>8} {'precision':>10} {'recall':>8}")
    for threshold in (0.60, 0.66, 0.72, 0.78, 0.84, 0.90):
        picked = known[(known["inferred_score"] >= threshold) & (~known["ambiguous"])
                       & (known["namesakes"] <= NAMESAKE_CAP)]
        correct = int(picked["correct"].sum())
        precision = correct / len(picked) * 100 if len(picked) else 0.0
        recall = correct / len(known) * 100
        print(f"{threshold:>10.2f} {len(picked):>9} {correct:>8} "
              f"{precision:>9.1f}% {recall:>7.1f}%")

    wrong = known[(known["inferred_score"] >= ACCEPT) & (~known["ambiguous"])
                  & (known["namesakes"] <= NAMESAKE_CAP) & (~known["correct"])]
    if len(wrong):
        print(f"\nFALSE POSITIVES at the shipping threshold ({ACCEPT}):")
        for row in wrong.itertuples():
            print(f"  {row.advisor_name:<26} picked {str(row.inferred_crd):<10} "
                  f"truth {row.truth_crd:<10} score {row.match_score}")
    else:
        print(f"\nNo false positives at the shipping threshold ({ACCEPT}).")

    missed = known[~((known["inferred_score"] >= ACCEPT) & (~known["ambiguous"])
                     & (known["namesakes"] <= NAMESAKE_CAP))]
    if False:
        print(f"\nNot accepted ({len(missed)}) -- these fall back to 'review':")
        for row in missed.itertuples():
            flag = "ambiguous" if row.ambiguous else f"score {row.inferred_score}"
            hit = "would be RIGHT" if row.inferred_crd == row.truth_crd else "would be WRONG"
            print(f"  {row.advisor_name:<26} {flag:<16} {hit}  "
                  f"(name {row.name_score} firm {row.firm_score} city {row.city_score})")


def main(path: str | None = None) -> None:
    source = (pathlib.Path(path) if path
              else INTERIM / "forbes_rankings.parquet")
    if not source.exists():
        raise SystemExit(f"{source} not found. Run src/parse_forbes.py first.")

    forbes = pd.read_parquet(source)
    advisors, branches, employment, firm_name, aliases = load_reference()
    truth = barrons_truth()
    index = build_index(advisors, branches, employment)

    matched = match(forbes, index, firm_name, aliases, truth)
    matched.drop(columns=["inferred_crd", "inferred_score"]).to_parquet(
        INTERIM / "forbes_matches.parquet", index=False)

    total = len(matched)
    print(f"forbes_matches.parquet  {total:,} rows")
    for tier in ("confirmed", "high", "review", "none"):
        n = int((matched["match_tier"] == tier).sum())
        print(f"  {tier:<10}{n:>7,}  ({n / max(1, total) * 100:5.1f}%)")

    calibrate(matched, truth)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
