"""Normalize street lines to USPS-style abbreviations for the Census geocoder.

Two distinct failure classes were found in the Georgia run:

  1. FORMAT  -- the same building written several ways. "3280 Peachtree Road
     Northeast", "3280 Peachtree Road North East" and "3280 PEACHTREE ROAD
     NORTHEAST, SUITE 2000" all failed while "3280 Peachtree Rd Ne Ste 300"
     matched. Fixable here.
  2. TIGER GAPS -- the house number genuinely is not in the Census range file
     (3438 Peachtree Rd NE fails; 3436 next door matches). Not fixable by
     rewriting the string; needs a neighbour-number fallback or another
     geocoder.

Deliberately NOT done: dropping the directional or the ZIP to force a hit.
Both were tested and returned confidently wrong coordinates -- dropping "NE"
matched Peachtree St NW, dropping the ZIP landed 7 miles away in 30341.
"""
from __future__ import annotations

import re

SUFFIX = {
    "STREET": "ST", "ROAD": "RD", "AVENUE": "AVE", "DRIVE": "DR",
    "BOULEVARD": "BLVD", "PARKWAY": "PKWY", "LANE": "LN", "COURT": "CT",
    "CIRCLE": "CIR", "PLACE": "PL", "TERRACE": "TER", "HIGHWAY": "HWY",
    "TRAIL": "TRL", "SQUARE": "SQ", "TURNPIKE": "TPKE", "EXPRESSWAY": "EXPY",
    "CENTER": "CTR", "PLAZA": "PLZ", "POINT": "PT", "RIDGE": "RDG",
    "CROSSING": "XING", "EXTENSION": "EXT", "FREEWAY": "FWY", "JUNCTION": "JCT",
}
DIRECTION = {
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
    "NORTH EAST": "NE", "NORTH WEST": "NW", "SOUTH EAST": "SE", "SOUTH WEST": "SW",
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
}
# secondary-unit designators that belong on line 2, not line 1
UNIT = r"(?:SUITE|STE|FLOOR|FL|UNIT|APT|APARTMENT|ROOM|RM|BLDG|BUILDING|#)"


def normalize(street: str) -> str:
    """Return a USPS-ish street line, or '' if nothing usable remains."""
    if not isinstance(street, str) or not street.strip():
        return ""
    s = street.upper().strip()

    s = s.replace(".", " ").replace(",", " , ")
    s = re.sub(r"\s+", " ", s)

    # strip a leading building name glued on with a dash: "PHIPPS TOWER-3438 ..."
    s = re.sub(r"^[A-Z][A-Z \-']*?[-–]\s*(?=\d)", "", s)

    # cut any secondary unit and everything after it
    s = re.sub(rf"\s*,?\s*\b{UNIT}\b.*$", "", s)
    s = re.sub(r"\s*,.*$", "", s)          # anything after a remaining comma

    for long, short in DIRECTION.items():   # longest first (NORTH EAST before NORTH)
        s = re.sub(rf"\b{long}\b", short, s)
    for long, short in SUFFIX.items():
        s = re.sub(rf"\b{long}\b", short, s)

    s = re.sub(r"\s+", " ", s).strip()
    # must still start with a house number to be worth sending
    return s if re.match(r"^\d", s) else ""


def pick_street(street1: str, street2: str) -> str:
    """Choose the line that actually carries a street address.

    Filers sometimes put the building name on line 1 and the address on
    line 2 -- "PHIPPS TOWER" / "3438 PEACHTREE ROAD NE, SUITE 900", or
    "Enterprise Mill" / "1450 Greene Street, Suite 501". normalize() rejects
    a line with no house number, so fall through to line 2 in that case.
    Used for geocoding only; the filed lines are displayed unchanged.
    """
    a = normalize(street1)
    if a:
        return a
    return normalize(street2)


if __name__ == "__main__":
    for t in ["3438 PEACHTREE RD NE",
              "PHIPPS TOWER-3438 PEACHTREE RD NE",
              "3280 Peachtree Road Northeast",
              "3280 PEACHTREE ROAD NORTHEAST, SUITE 2000",
              "3280 Peachtree Road North East",
              "3438 PEACHTREE RD. NE.",
              "900 ASHWOOD PARKWAY",
              "200 ASHFORD CENTER NORTH",
              "1 PRIMERICA PARKWAY",
              "PO BOX 1234"]:
        print(f"{t:45s} -> {normalize(t)!r}")
