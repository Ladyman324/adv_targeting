"""Structured SEC individual-name aliases and conservative nickname evidence.

Form U4 ``OthrNms`` rows are person-specific name forms. A row such as
``NICOLE TESORIERO`` for legal ``NICOLE FLORES`` proves that *this person* has
used both surnames; it does not prove that either surname is interchangeable
for anybody else. Keep those rows intact and only combine given names that
belong to the same surname key.

The SEC feed is also useful evidence about common given-name variants, but it
is noisy (duplicates, initials, typos and former names). The evidence table
produced here deliberately grants no automatic matching authority. Existing
curated nickname rules remain authoritative until a separately reviewed rule
promotes an observed pair.
"""
from __future__ import annotations

import pathlib
import re
from collections import defaultdict

import pandas as pd


OTHER_NAME_COLUMNS = [
    "advisor_crd", "alias_ordinal", "first_name", "middle_name",
    "last_name", "suffix",
]

NICKNAME_EVIDENCE_COLUMNS = [
    "legal_first_name", "alternate_first_name", "distinct_crd_support",
    "evidence_status", "automatic_match", "ruleset_version",
]

NICKNAME_EVIDENCE_RULESET = "sec-othrnm-v1"
COMMON_SUPPORT = 10
GENERATIONAL = {"jr", "sr", "ii", "iii", "iv", "v", "vi"}


def clean_text(value: object) -> str:
    """String value with pandas nulls normalized to empty."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value)


def name_token(value: object) -> str:
    """Letters-only lower-case token, matching the established name indexes."""
    return re.sub(r"[^a-z]", "", clean_text(value).lower())


def surname_key(value: object) -> str:
    """Index key for a filed surname, without parentheticals or suffixes."""
    text = re.sub(r"\([^)]*\)", " ", clean_text(value))
    words = [name_token(t) for t in re.split(r"[\s.,]+", text) if name_token(t)]
    if words and words[-1] in GENERATIONAL:
        words.pop()
    return "".join(words)


def value_tokens(value: object) -> set[str]:
    """All usable name tokens in one SEC attribute."""
    return {
        token for token in
        (name_token(t) for t in re.split(r"[\s()/,]+", clean_text(value)))
        if token
    }


def aliases_by_crd(frame: pd.DataFrame | None) -> dict[str, list[dict]]:
    """Convert the normalized alias table to a CRD-keyed record mapping."""
    if frame is None or frame.empty:
        return {}
    out: dict[str, list[dict]] = defaultdict(list)
    for item in frame.itertuples(index=False):
        crd = clean_text(getattr(item, "advisor_crd", "")).strip()
        if crd:
            out[crd].append({
                "advisor_crd": crd,
                "alias_ordinal": getattr(item, "alias_ordinal", 0),
                "first_name": clean_text(getattr(item, "first_name", "")),
                "middle_name": clean_text(getattr(item, "middle_name", "")),
                "last_name": clean_text(getattr(item, "last_name", "")),
                "suffix": clean_text(getattr(item, "suffix", "")),
            })
    return dict(out)


def load_advisor_aliases(path: pathlib.Path) -> dict[str, list[dict]]:
    """Load SEC aliases, returning an empty mapping for a legacy build."""
    if not path.exists():
        return {}
    return aliases_by_crd(pd.read_parquet(path, columns=OTHER_NAME_COLUMNS))


def surname_given_groups(row, aliases: list[dict] | None = None) -> dict[str, set[str]]:
    """Surname -> given forms without mixing fields across SEC name rows.

    The legal row retains the historical behavior: filed first, middle and the
    conservatively-derived used name are valid forms for the legal surname.
    Each ``OthrNm`` contributes only its own first/middle forms to its own
    surname. Blank alias surname means the legal surname; a different surname
    is never populated with given names borrowed from the legal row.
    """
    legal_last = getattr(row, "last_key", "") or surname_key(
        getattr(row, "last_name", ""))
    groups: dict[str, set[str]] = defaultdict(set)
    if legal_last:
        for field in ("first_name", "middle_name", "used_first_name"):
            groups[legal_last] |= value_tokens(getattr(row, field, ""))
        for bracket in re.findall(
                r"\(([^)]*)\)", clean_text(getattr(row, "last_name", ""))):
            groups[legal_last] |= value_tokens(bracket)

    for alias in aliases or []:
        key = surname_key(alias.get("last_name")) or legal_last
        if not key:
            continue
        forms = value_tokens(alias.get("first_name"))
        forms |= value_tokens(alias.get("middle_name"))
        for bracket in re.findall(
                r"\(([^)]*)\)", clean_text(alias.get("last_name"))):
            forms |= value_tokens(bracket)
        if forms:
            groups[key] |= forms
    return dict(groups)


def build_nickname_evidence(advisors: pd.DataFrame,
                            aliases: pd.DataFrame) -> pd.DataFrame:
    """Aggregate safe-to-measure SEC given-name pairs, without approving them.

    Only a same-surname alternate row with a different, non-initial, single
    first-name token contributes. Counts are distinct people, so duplicate
    ``OthrNm`` rows cannot manufacture support. Even high-support pairs remain
    evidence-only: ``automatic_match`` is always false in this artifact.
    """
    legal = {}
    for item in advisors.itertuples(index=False):
        crd = clean_text(getattr(item, "advisor_crd", "")).strip()
        if crd:
            legal[crd] = {
                "first_name": clean_text(getattr(item, "first_name", "")),
                "last_name": clean_text(getattr(item, "last_name", "")),
            }

    support: dict[tuple[str, str], set[str]] = defaultdict(set)
    for item in aliases.itertuples(index=False):
        crd = clean_text(getattr(item, "advisor_crd", "")).strip()
        filed = legal.get(crd)
        if not filed:
            continue
        alias_first = clean_text(getattr(item, "first_name", ""))
        alias_last = clean_text(getattr(item, "last_name", ""))
        legal_first = name_token(filed.get("first_name"))
        alternate_first = name_token(alias_first)
        legal_last = surname_key(filed.get("last_name"))
        alternate_last = surname_key(alias_last) or legal_last
        if (not legal_first or not alternate_first or
                len(legal_first) < 2 or len(alternate_first) < 2 or
                legal_first == alternate_first or legal_last != alternate_last):
            continue
        if (len(value_tokens(filed.get("first_name"))) != 1 or
                len(value_tokens(alias_first)) != 1):
            continue
        support[(legal_first, alternate_first)].add(crd)

    rows = []
    for (legal_first, alternate_first), crds in sorted(support.items()):
        count = len(crds)
        rows.append({
            "legal_first_name": legal_first,
            "alternate_first_name": alternate_first,
            "distinct_crd_support": count,
            "evidence_status": "common_candidate" if count >= COMMON_SUPPORT
                               else "observed",
            "automatic_match": False,
            "ruleset_version": NICKNAME_EVIDENCE_RULESET,
        })
    return pd.DataFrame(rows, columns=NICKNAME_EVIDENCE_COLUMNS)
