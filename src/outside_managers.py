"""Who hires outside managers -- one definition, used by every exporter.

WHY THIS IS NOT JUST ITEM 5.G(7)
--------------------------------
The map's default filter equated "uses outside managers" with Form ADV Item
5.G(7), "selection of other advisers". LPL Financial reports Q5G7="N" and is
nonetheless the largest independent wrap sponsor in the country: Item 5.I(2)(c)
carries $598.6 billion across five programmes. Sponsoring a wrap programme IS
putting client money with managers who are not you; LPL simply answers a
different question on the same form.

So a filter built on 5.G(7) alone hid LPL, Empower, Betterment, Wealthfront and
586 other firms -- and it is ON BY DEFAULT, so a rep browsing normally never saw
them. Reps at those firms told us so, which is a slow and expensive way to learn
it.

Either signal counts:

    5.G(7)            the firm says it selects other advisers
    wrap sponsor      the firm sponsors a wrap programme (Item 5.I(2)(a)/(c))

The scoring model already treated these as equivalent evidence -- FIT gives ten
points to each -- so this brings the filter into line with what the model always
believed.

NOT included: wrap PORTFOLIO MANAGER only (5.I(2)(b)). A firm managing money
inside somebody else's wrap programme is a competitor, not a buyer.
"""
from __future__ import annotations

import pandas as pd


def _truthy(value) -> bool:
    if value is True:
        return True
    return str(value).strip().upper() in ("TRUE", "1", "Y", "YES")


def hires_outside_managers(row) -> bool:
    """True when either signal is present on a firm row."""
    return _truthy(row.get("g_select_advisers")) or _truthy(row.get("is_wrap_sponsor"))


def reason(row) -> str:
    """Which signal fired, for a card that has to say more than yes or no."""
    selects = _truthy(row.get("g_select_advisers"))
    sponsor = _truthy(row.get("is_wrap_sponsor"))
    if selects and sponsor:
        return "both"
    if selects:
        return "selects"
    if sponsor:
        return "wrap"
    return ""


def series(frame: pd.DataFrame) -> pd.Series:
    """Vectorised form for whole frames."""
    selects = frame.get("g_select_advisers")
    sponsor = frame.get("is_wrap_sponsor")
    out = pd.Series(False, index=frame.index)
    if selects is not None:
        out = out | selects.fillna(False).astype(bool)
    if sponsor is not None:
        out = out | sponsor.fillna(False).astype(bool)
    return out
