"""Normalise and de-duplicate the IAPD other/former firm names.

The raw `otherNames` list is unranked and repetitive: MERRILL LYNCH, PIERCE,
FENNER & SMITH appears three ways, Edelman files both "FINANCIAL ENGINES
ADVISORS LLC" and "FINANCIAL ENGINES ADVISORS L.L.C.", and a handful of firms
have filed literal junk -- Strategic Advisers lists an alias of "SAME".

So the list is useful for two different jobs with different rules:
  search  -- keep everything, because a rep may type any spelling
  display -- a couple of genuinely distinct names, or the panel fills with
             punctuation variants of the name already at the top of the page
"""
from __future__ import annotations

import re

# Order matters: punctuation is stripped first, so "L.L.C." arrives here as
# "L L C" and needs its own spaced alternative.
SUFFIX = re.compile(
    r"\b(?:INC|INCORPORATED|LLC|L\s*L\s*C|LLP|LP|L\s*P|LTD|CO|COMPANY|CORP|"
    r"CORPORATION|PLLC|PC|PA|SA|NA|N\s*A|TRUST|GROUP)\b\.?", re.I)

# Filed placeholders that are not names. Compared after normalisation.
JUNK = {"SAME", "NA", "NONE", "N A", "NOT APPLICABLE", "SEE ABOVE", "NULL"}


def normalize(name: str) -> str:
    """Comparison key: case, punctuation, ampersands and entity suffixes removed."""
    text = str(name or "").upper().replace("&", " AND ")
    text = re.sub(r"[^\w\s]", " ", text)
    text = SUFFIX.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def dedupe(current: str, others) -> list:
    """Distinct alias names, in filed order, excluding the firm's current name.

    Filed order is kept deliberately: it is not alphabetical and not ranked, but
    it is at least stable across refreshes, so a name does not jump position in
    the UI between pipeline runs.
    """
    seen = {normalize(current)}
    seen.discard("")
    out = []
    for name in others or []:
        key = normalize(name)
        if not key or key in seen or key in JUNK or len(key) < 3:
            continue
        seen.add(key)
        out.append(str(name).strip())
    return out
