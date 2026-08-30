"""Shared, fail-closed checks for geocoder jurisdiction evidence.

State files, sales territories, search, and field tiles all trust the state
that selected a branch row. A coordinate in another state is therefore not
just a bad dot: it gives the advisor to the wrong salesperson. Census returns
the matched jurisdiction in a stable display string, so reject a result before
it can cross that boundary.
"""
from __future__ import annotations

import re


# Census: ``100 PARK AVE, ORANGE, TX, 77630``
# Google: ``100 Park Ave, Beachwood, OH 44122, USA``
# Anchored near the end so two-letter words in a street are not read as states.
_USPS_REGION = re.compile(
    r"(?:,\s*|\b)([A-Z]{2})[\s,]+(\d{5})(?:-\d{4})?"
    r"(?:,\s*(?:USA|UNITED STATES))?\s*$",
    re.IGNORECASE,
)

# Nominatim uses full jurisdiction names instead of USPS abbreviations.  Match
# complete comma-separated components so words inside street or company names
# cannot be mistaken for a state.
_STATE_NAME_TO_USPS = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT",
    "DELAWARE": "DE", "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI",
    "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA",
    "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME",
    "MARYLAND": "MD", "MASSACHUSETTS": "MA", "MICHIGAN": "MI",
    "MINNESOTA": "MN", "MISSISSIPPI": "MS", "MISSOURI": "MO",
    "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM",
    "NEW YORK": "NY", "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND",
    "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR",
    "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT",
    "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
    "DISTRICT OF COLUMBIA": "DC", "PUERTO RICO": "PR",
    "UNITED STATES VIRGIN ISLANDS": "VI", "U.S. VIRGIN ISLANDS": "VI",
    "VIRGIN ISLANDS": "VI",
}
USPS_REGIONS = frozenset(_STATE_NAME_TO_USPS.values())


def returned_state(matched: object) -> str:
    """Return the explicit USPS jurisdiction in a geocoder display string."""
    value = str("" if matched is None else matched).strip()
    hit = _USPS_REGION.search(value)
    if hit:
        return hit.group(1).upper()
    for component in reversed(value.split(",")):
        state = _STATE_NAME_TO_USPS.get(component.strip().upper())
        if state:
            return state
    return ""


def state_agrees(matched: object, expected_state: object) -> bool:
    """True only when the result explicitly names the requested jurisdiction."""
    expected = str("" if expected_state is None else expected_state).strip().upper()
    actual = returned_state(matched)
    return bool(expected and actual and actual == expected)


def validate_geocoded_frame(frame, expected_state: str, label: str = "") -> None:
    """Raise when a placed row contradicts the state shard that owns it.

    Unparseable third-party/manual display strings abstain here; Census results
    cannot reach an artifact unparseable because ``geocode.tier1/tier2`` reject
    them first.  Explicit disagreement is always fatal.
    """
    expected = str(expected_state or "").strip().upper()
    placed = frame[frame["lat"].notna() & frame["lon"].notna()].copy()
    shard_bad = placed["branch_state"].fillna("").astype(str).str.upper().str.strip().ne(expected)
    actual = placed["matched"].map(returned_state) if "matched" in placed else ""
    if not isinstance(actual, str):
        result_bad = actual.ne("") & actual.ne(expected)
    else:
        result_bad = shard_bad & False
    bad = placed[shard_bad | result_bad]
    if bad.empty:
        return
    samples = []
    for row in bad.head(4).itertuples():
        got = returned_state(getattr(row, "matched", "")) or "unparseable"
        samples.append(
            f"CRD {getattr(row, 'advisor_crd', '?')} "
            f"{getattr(row, 'branch_city', '')}/{getattr(row, 'branch_state', '')} "
            f"returned {got}"
        )
    where = label or expected
    raise ValueError(
        f"{where}: {len(bad)} placed row(s) conflict with state shard {expected}: "
        + "; ".join(samples)
    )
