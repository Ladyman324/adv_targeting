"""The one function that decides what an advisor is called on screen.

WHY THIS EXISTS
---------------
There were two answers and nothing comparing them. The desktop map built a name
from the SEC feed -- first_name, overridden by used_first_name -- while the field
app took whatever contact record won in contacts.json. 47,371 advisors, a third
of the overlap, showed a DIFFERENT NAME depending on which application a rep
opened.

Cosmo Boyd, CRD 845934, is the example that surfaced it: the desktop calls him
Cosmo, the field app calls him Montague, and a rep searching the phone for the
name they had just read on the desk found nobody.

Neither source was clean, which is why "just pick one" was not the fix:

  the SEC side took used_first_name literally, including values that are not
  names at all -- "Jr Harrison", "(Iii) Moncrieff". 215 records carry a suffix
  or a bracketed fragment in that field.

  the contacts side carried CRM formatting -- 17,788 all-caps names, 3,770 with
  doubled spaces, 174 with a designation appended: "Teresa Finney, CEPA".

So the rule lives here, once, and every builder calls it.

THE RULE
--------
1. used_first_name wins, because it is the name the advisor gave the regulator
   as the one they go by -- Bill for William, Cosmo for Montague.
2. UNLESS it is not a name: a generational suffix, a bracketed fragment, an
   honorific, or a single letter. Then first_name wins.
3. Formatting is normalised either way: casing, spacing, and any trailing
   professional designation, which belongs in its own field and not in a name.

WHAT THIS DOES NOT DO
---------------------
It does not choose between two people. Deciding that CRD 1213372 is Patrick
rather than Edward is a MATCHING question and lives in the crosswalk; deciding
whether Raj beats Naganath on the evidence of a firm's own website lives in
src/reconcile_display_names.py, which runs afterwards and patches the artifacts
this produces. This function only formats what the SEC feed already says.
"""
from __future__ import annotations

import re

# used_first_name is meant to hold the name somebody goes by. These are what
# turns up there instead, and taking them literally produced "Jr Harrison" and
# "(Iii) Moncrieff" on the map.
NOT_A_NAME = {
    "jr", "sr", "ii", "iii", "iv", "v", "jnr", "snr",
    "mr", "mrs", "ms", "miss", "dr", "prof", "rev",
    "none", "n/a", "na", "null", "-", ".",
}

# Letters after a comma that are a qualification rather than part of a name.
# Anchored to a comma so "Van Buren" and "St John" are untouched.
DESIGNATION = re.compile(
    r",\s*(?:[A-Za-z]{2,6}\W*){1,4}$"
)

# Roman numerals and generational suffixes keep their conventional casing, which
# .title() would otherwise mangle into "Iii" and "Ii".
SUFFIX_CASE = {"ii": "II", "iii": "III", "iv": "IV", "v": "V",
               "jr": "Jr.", "sr": "Sr.", "jr.": "Jr.", "sr.": "Sr."}


def looks_like_a_name(value: str) -> bool:
    """Is this a first name, or something that landed in the field by mistake?"""
    text = str(value or "").strip()
    if not text:
        return False
    # "(JR)", "(III)" -- a bracketed fragment is an annotation, not a name.
    if "(" in text or ")" in text:
        return False
    bare = text.strip(". ").lower()
    if bare in NOT_A_NAME:
        return False
    # A single letter is an initial. It is not better than the filed first name,
    # and "B Gioffre" is a worse label than "Bruno Gioffre".
    return len(bare) > 1


def tidy(value: str) -> str:
    """CRM casing and spacing -> something readable, without losing the name.

    Deliberately conservative. It fixes what is unambiguously wrong -- ALL CAPS,
    doubled spaces, a trailing qualification -- and leaves everything else,
    because a name is the thing a rep reads out loud on a call and a clever
    normaliser that "corrects" McDonald or O'Brien is worse than none.
    """
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    text = DESIGNATION.sub("", text).strip(" ,")
    # Only re-case when the source has no case information of its own. A name
    # already in mixed case was typed by somebody and is left alone.
    if text == text.upper() or text == text.lower():
        parts = []
        for word in text.split(" "):
            low = word.lower()
            if low in SUFFIX_CASE:
                parts.append(SUFFIX_CASE[low])
            elif "'" in word or word.lower().startswith("mc"):
                # O'Brien and McDonald: capitalise after the marker too.
                parts.append(re.sub(r"(^|['])([a-z])",
                                    lambda m: m.group(1) + m.group(2).upper(),
                                    low).replace("Mc", "Mc", 1)
                             if "'" in word else low[:2].title() + low[2:].title())
            else:
                parts.append(word.capitalize())
        text = " ".join(parts)
    return text


def display_name(first: str, last: str, used: str = "", middle: str = "") -> str:
    """The name to show. See the module docstring for the rule."""
    first = tidy(first)
    last = tidy(last)
    used = tidy(used)
    given = used if looks_like_a_name(used) else first
    if not given and not last:
        return ""
    return " ".join(x for x in (given, last) if x)


def filed_name(first: str, last: str, middle: str = "") -> str:
    """The name as FILED, for the "filed as" line -- never the display name.

    Shown beside the display name where the two differ, so a rep checking
    against IAPD can see both rather than wondering which one we changed.
    """
    return " ".join(x for x in (tidy(first), tidy(middle), tidy(last)) if x)
