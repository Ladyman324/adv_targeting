"""Conservative normalization for identity evidence; no fuzzy matching."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

HONORIFICS = frozenset({"mr", "mrs", "ms", "miss", "dr", "prof", "rev", "sir"})
SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v", "jnr", "snr"})
DESIGNATIONS = frozenset({"aams", "aif", "cfa", "cfp", "chfc", "cima", "clu", "cpwa", "crpc", "ricp", "mba", "phd", "cpa", "esq", "cepa", "cdfa", "crps", "apma"})
GENERIC_EMAIL_LOCAL_PARTS = frozenset({"admin", "advisor", "advisors", "assistant", "clientservice", "contact", "customerservice", "hello", "info", "office", "service", "support", "team", "wealthmanagement"})
NON_NAME_GREETINGS = frozenset({"advisor", "advisors", "all", "assistant", "client", "clients", "contact", "customer", "customers", "everyone", "friend", "friends", "office", "sir", "madam", "team", "there", "valuedclient"})

# Strict legal-name -> short-form pairs. "bo" is intentionally absent: it
# requires a reviewed decision for the particular ACT GUID and CRD.
STRICT_NICKNAMES = {
    "alexander": {"alex"}, "andrew": {"andy", "drew"}, "anthony": {"tony"},
    "benjamin": {"ben"}, "catherine": {"cathy", "kate", "katie"},
    "charles": {"charlie", "chuck"}, "christine": {"chris"},
    "christopher": {"chris"}, "daniel": {"dan", "danny"}, "david": {"dave"},
    "deborah": {"deb", "debbie"}, "donald": {"don"},
    "edward": {"ed", "eddie", "ted"},
    "elizabeth": {"beth", "betsy", "betty", "liz", "libby"},
    "frederick": {"fred", "freddie"}, "geoffrey": {"geoff", "jeff"},
    "gregory": {"greg"}, "james": {"jim", "jimmy", "jamie"},
    "jennifer": {"jen", "jenny"}, "jeffrey": {"jeff"},
    "john": {"jack", "johnny", "jon"}, "jonathan": {"jon"},
    "joseph": {"joe", "joey"}, "katherine": {"kate", "katie", "kathy"},
    "kathryn": {"kate", "katie", "kat"}, "kenneth": {"ken", "kenny"},
    "lawrence": {"larry"}, "margaret": {"maggie", "meg", "peggy"},
    "matthew": {"matt"}, "michael": {"mike"}, "nicholas": {"nick"},
    "patrick": {"pat"}, "patricia": {"pat", "trish"}, "philip": {"phil"},
    "raymond": {"ray"}, "rebecca": {"becca", "becky"},
    "richard": {"rich", "rick"}, "robert": {"bob", "bobby", "rob"},
    "ronald": {"ron"}, "samuel": {"sam"}, "stephen": {"steve"},
    "steven": {"steve"}, "theodore": {"ted", "theo"},
    "thomas": {"tom", "tommy"}, "timothy": {"tim"},
    "william": {"bill", "billy", "liam", "will"},
}


def clean_text(value: object) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).replace("\ufffd", " ")
    return " ".join(text.split()).strip()


def ascii_fold(value: object) -> str:
    text = unicodedata.normalize("NFKD", clean_text(value))
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def name_token(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", ascii_fold(value).lower())


def name_tokens(value: object) -> list[str]:
    return [name_token(p) for p in re.findall(r"[\w'-]+", ascii_fold(value))
            if name_token(p)]


def normalize_crd(value: object) -> str:
    text = clean_text(value)
    if re.fullmatch(r"\d+(?:\.0+)?", text):
        return text.split(".", 1)[0].lstrip("0") or "0"
    return ""


def normalize_email(value: object) -> str:
    text = clean_text(value).lower()
    if text.count("@") != 1 or any(ch.isspace() for ch in text):
        return ""
    local, domain = text.split("@")
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        return ""
    return text


def is_generic_email(value: object) -> bool:
    """True when an address names a shared role rather than one person."""
    email = normalize_email(value)
    if not email:
        return False
    return name_token(email.split("@", 1)[0]) in GENERIC_EMAIL_LOCAL_PARTS


def normalize_phone(value: object) -> str:
    digits = re.sub(r"\D", "", clean_text(value))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def normalize_suffix(value: object) -> str:
    token = name_token(value)
    return token if token in SUFFIXES else ""


def normalize_postal(value: object) -> str:
    value = re.sub(r"[^A-Za-z0-9]", "", clean_text(value)).upper()
    return value[:5] if value.isdigit() else value


@dataclass(frozen=True)
class ParsedName:
    first: str = ""
    middle: str = ""
    last: str = ""
    suffix: str = ""

    @property
    def full(self) -> str:
        return " ".join(x for x in (self.first, self.middle, self.last,
                                     self.suffix) if x)


def parse_full_name(value: object) -> ParsedName:
    """Conservatively parse the ordinary Act fullName representation."""
    raw = clean_text(value)
    if not raw or "," in raw:
        return ParsedName()
    words = [w for w in re.findall(r"[A-Za-z0-9'-]+", ascii_fold(raw)) if w]
    while words and name_token(words[0]) in HONORIFICS:
        words.pop(0)
    while words and name_token(words[-1]) in DESIGNATIONS:
        words.pop()
    suffix = ""
    if words and name_token(words[-1]) in SUFFIXES:
        suffix = words.pop()
    if len(words) < 2:
        return ParsedName()
    return ParsedName(words[0], " ".join(words[1:-1]), words[-1], suffix)


def structured_name(first: object, middle: object, last: object,
                    suffix: object) -> ParsedName:
    first_s, middle_s, last_s = map(clean_text, (first, middle, last))
    if name_token(first_s) in HONORIFICS or name_token(last_s) in HONORIFICS:
        return ParsedName()
    return ParsedName(first_s, middle_s, last_s, clean_text(suffix))


def strict_nickname_pair(legal: object, candidate: object) -> bool:
    legal_t, candidate_t = name_token(legal), name_token(candidate)
    return bool(legal_t and candidate_t and
                candidate_t in STRICT_NICKNAMES.get(legal_t, set()))


def given_agreement(act_names: Iterable[object],
                    sec_names: Iterable[object]) -> tuple[bool, str]:
    """Return deterministic given-name agreement and its strongest reason."""
    act = [name_token(v) for v in act_names if name_token(v)]
    sec = [name_token(v) for v in sec_names if name_token(v)]
    if not act or not sec:
        return False, "missing_given_name"
    if set(act) & set(sec):
        return True, "given_exact"
    for legal in sec:
        for candidate in act:
            if strict_nickname_pair(legal, candidate):
                return True, "given_strict_nickname"
    return False, "given_name_conflict"


def preferred_name_status(preferred: object, legal_first: object,
                          used_first: object = "") -> tuple[str, str]:
    """Classify a greeting independently from identity resolution."""
    preferred_t = name_token(preferred)
    if not preferred_t:
        return "absent", "no_act_salutation"
    if preferred_t in NON_NAME_GREETINGS:
        return "review", "non_personal_salutation"
    if preferred_t in {name_token(legal_first), name_token(used_first)}:
        return "approved_auto", "preferred_exact"
    if strict_nickname_pair(legal_first, preferred_t):
        return "approved_auto", "preferred_strict_nickname"
    return "review", "preferred_requires_review"
