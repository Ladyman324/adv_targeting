"""Choose contact greetings by sending address, independently of identity gates."""
from __future__ import annotations

import json
import pathlib
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping

import pandas as pd

from contact_normalization import parse_full_name
from contact_provenance import sha256_file
from identity_normalize import (HONORIFICS, NON_NAME_GREETINGS, clean_text,
                                name_token, normalize_email, strict_nickname_pair)
from identity_resolver import format_greeting, sec_greeting
from identity_schema import (MANIFEST_FILENAME, SOURCE_RECORDS_FILENAME,
                             content_hash)
from roster_greetings import resolve_roster_greeting, valid_greeting

ACT_PRIMARY_SOURCE = "act_primary_email"
ROSTER_SOURCE = "firm_roster"
SEC_SOURCE = "sec_fallback"
_ANNOTATIONS = frozenset({"male", "female"})
_NON_NAMES = NON_NAME_GREETINGS | _ANNOTATIONS | frozenset({
    "retired", "retried", "leftindustry", "deceased", "inactive",
    "donotcontact", "donotmail", "nomail", "unsubscribed", "none", "na",
})


@dataclass(frozen=True)
class ContactGreeting:
    greeting: str
    source: str
    reason: str
    evidence_hash: str
    act_contact_id: str = ""


def build_act_primary_email_index(records: Iterable[Mapping]) -> dict[str, list[Mapping]]:
    """Count all primary claims, including inactive rows; never index alternates."""
    by_email = defaultdict(list)
    for record in records:
        email = normalize_email(record.get("norm_email"))
        if email:
            by_email[email].append(record)
    return dict(by_email)


def load_act_primary_email_index(identity_dir: pathlib.Path,
                                 contacts_path: pathlib.Path) -> dict[str, list[Mapping]]:
    """Load primary claims from the exact ACT snapshot bound to the ledger."""
    identity_dir, contacts_path = map(pathlib.Path, (identity_dir, contacts_path))
    manifest = json.loads((identity_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    core = {k: v for k, v in manifest.items() if k not in {"generatedUtc", "contentHash"}}
    if manifest.get("contentHash") != content_hash(core):
        raise RuntimeError("Identity manifest contentHash is invalid.")
    act = manifest.get("actSource") or {}
    if (act.get("file") != contacts_path.name
            or act.get("sha256") != sha256_file(contacts_path)):
        raise RuntimeError("Greeting source describes different ACT contact bytes.")
    path = identity_dir / SOURCE_RECORDS_FILENAME
    meta = (manifest.get("outputs") or {}).get(SOURCE_RECORDS_FILENAME) or {}
    if (not path.exists() or meta.get("sha256") != sha256_file(path)
            or int(meta.get("rows") or -1) != int(act.get("rows") or -2)):
        raise RuntimeError("ACT greeting source records are stale or modified.")
    frame = pd.read_parquet(path).fillna("")
    required = {"norm_email", "raw_salutation", "source_record_id", "record_active"}
    if not required <= set(frame.columns) or len(frame) != int(meta["rows"]):
        raise RuntimeError("ACT greeting source records are incomplete.")
    return build_act_primary_email_index(frame.to_dict("records"))


def clean_act_salutation(value: object) -> str:
    """Clean a trusted CRM salutation without validating it against SEC names."""
    text = clean_text(value)
    if (not text or re.match(r"^(?:" + "|".join(sorted(HONORIFICS)) + r")(?:[.\s]|$)", text, re.I)
            or name_token(text) in _NON_NAMES):
        return ""
    for parenthetical in re.findall(r"\(([^()]*)\)", text):
        candidate = format_greeting(parenthetical)
        if (name_token(candidate) not in _NON_NAMES
                and valid_greeting(candidate, source=ACT_PRIMARY_SOURCE)):
            return candidate
    text = format_greeting(re.sub(r"\([^()]*\)", "", text))
    return text if valid_greeting(text, source=ACT_PRIMARY_SOURCE) else ""


def roster_names_for_contact(group: pd.DataFrame, email: object,
                             preferred_name: object = "") -> list[str]:
    """Only accepted roster rows for the exact sending address may supply a name."""
    target = normalize_email(email)
    candidates = []
    for row in group.to_dict("records"):
        if (row.get("tier") not in {"confirmed", "high"}
                or str(row.get("source", "")).upper() == "CRM"
                or normalize_email(row.get("email")) != target):
            continue
        name = clean_text(row.get("name"))
        if name:
            candidates.append((name == clean_text(preferred_name),
                               float(row.get("match_score") or 0), name))
    return list(dict.fromkeys(name for _, _, name in sorted(candidates, reverse=True)))


def choose_contact_greeting(*, email: object, act_primary_by_email: Mapping,
                            roster_names: Iterable[str], sec: Mapping,
                            email_unique: bool = True,
                            authoritative_domain: bool = False) -> ContactGreeting:
    """One presentation decision: unique active ACT primary, roster, then SEC."""
    email = normalize_email(email)
    records = act_primary_by_email.get(email, ())
    record = records[0] if len(records) == 1 else None
    active = record is not None and str(record.get("record_active", "")).lower() in {"true", "1"}

    def result(greeting, source, reason, act_id=""):
        payload = {"email": email, "greeting": greeting, "source": source,
                   "reason": reason, "actContactId": act_id}
        return ContactGreeting(greeting, source, reason, content_hash(payload), act_id)

    if active:
        greeting = clean_act_salutation(record.get("raw_salutation"))
        if greeting:
            return result(greeting, ACT_PRIMARY_SOURCE, "unique_active_primary_email",
                          clean_text(record.get("source_record_id")))
    for roster_name in roster_names:
        decision = resolve_roster_greeting(
            roster_name=roster_name, email=email,
            sec_first=sec.get("first", ""), sec_middle=sec.get("middle", ""),
            sec_used=sec.get("used", ""),
            sec_last=sec.get("last") or parse_full_name(roster_name).last,
            sec_aliases=tuple(sec.get("aliases", ())), email_unique=email_unique,
            authoritative_domain=authoritative_domain)
        greeting = format_greeting(decision.greeting)
        if (decision.reason.startswith("roster_")
                and valid_greeting(greeting, source=ROSTER_SOURCE)):
            return result(greeting, ROSTER_SOURCE, decision.reason)
    sec_names = {"first_name": sec.get("first", ""),
                 "middle_name": sec.get("middle", ""),
                 "used_first_name": sec.get("used", "")}
    greeting = sec_greeting(sec_names)
    if not valid_greeting(greeting, source=SEC_SOURCE):
        # A compound used name can fail the existing merge-name syntax. Keep
        # the route's usable filed first/middle name instead (Dee Dee -> Deirdre).
        greeting = sec_greeting({**sec_names, "used_first_name": ""})
    if valid_greeting(greeting, source=SEC_SOURCE):
        return result(greeting, SEC_SOURCE, "sec_given_name_fallback")
    return result("", "no_usable_greeting", "no_usable_given_name")


def _preferred_token(value) -> str:
    text = clean_text(value)
    if (not text or len(text) > 40 or any(ch.isspace() for ch in text)
            or not text[0].isalpha()
            or any(not (ch.isalpha() or ch in "-'’") for ch in text)):
        return ""
    return text


def preferred_display_name(formal_name: object, preferred_first: object) -> str:
    """Show a preference transparently, for example Christopher (Chris) Tolman."""
    formal = clean_text(formal_name)
    preferred = _preferred_token(preferred_first)
    if not formal or not preferred:
        return formal
    parts = formal.split()
    if name_token(parts[0]) == name_token(preferred):
        return formal
    # If the view already uses a strict nickname and ACT supplies the legal
    # form, adding it in parentheses is backwards: Chris (Christopher).
    if strict_nickname_pair(preferred, parts[0]):
        return formal
    if len(parts) > 1 and parts[1].startswith("(") and parts[1].endswith(")"):
        return formal
    return f"{parts[0]} ({preferred})" + (
        f" {' '.join(parts[1:])}" if len(parts) > 1 else "")
