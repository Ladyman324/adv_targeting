"""Derive a safe spoken name after a roster contact has been identity-resolved.

This module never decides *who* a contact is and never authorizes an email
route. It only chooses presentation fields for a confirmed/high-confidence
contact whose firm roster route has already been accepted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from display_name import tidy
from identity_normalize import (
    DESIGNATIONS,
    HONORIFICS,
    NON_NAME_GREETINGS,
    SUFFIXES,
    clean_text,
    name_token,
    strict_nickname_pair,
)
from identity_schema import content_hash


RULESET = "roster_greeting_v1"
_WORD = re.compile(r"[A-Za-z]+(?:[-'][A-Za-z]+)*")
_PAREN = re.compile(r"\(([^()]*)\)")
_GREETING_NICKNAMES = {
    "cameron": {"cam"},
    "elizabeth": {"bets"},
    "phillip": {"phil"},
}


@dataclass(frozen=True)
class GreetingDecision:
    greeting: str
    last_name: str
    presentation_last_name: str
    presentation_name: str
    reason: str
    evidence_hash: str


def _words(value: object) -> list[str]:
    return _WORD.findall(clean_text(value))


def _valid_given(value: object) -> bool:
    text = clean_text(value)
    token = name_token(text)
    return bool(
        text and len(token) > 1 and len(text) <= 40
        and len(_words(text)) == 1
        and token not in HONORIFICS
        and token not in SUFFIXES
        and token not in DESIGNATIONS
        and token not in NON_NAME_GREETINGS
    )


def valid_greeting(value: object) -> bool:
    """Public release-gate predicate for a single spoken given name."""
    return _valid_given(value)


def _nickname_pair(legal: str, candidate: str) -> bool:
    return (strict_nickname_pair(legal, candidate)
            or name_token(candidate) in _GREETING_NICKNAMES.get(
                name_token(legal), set()))


def _surname_words(value: object) -> list[str]:
    words = _words(value)
    while words and name_token(words[-1]) in (SUFFIXES | DESIGNATIONS):
        words.pop()
    return words


def _find_sequence(haystack: list[str], needle: list[str]) -> int:
    h = [name_token(x) for x in haystack]
    n = [name_token(x) for x in needle]
    for index in range(0, len(h) - len(n) + 1):
        if h[index:index + len(n)] == n:
            return index
    return -1


def _choose_surname(roster_name: str, sec_last: str,
                    sec_aliases: tuple[str, ...]) -> list[str]:
    """Prefer the SEC-filed surname that the current firm actually publishes."""
    candidates = [_surname_words(sec_last)]
    candidates.extend(_surname_words(value) for value in sec_aliases)
    unique = []
    seen = set()
    for words in candidates:
        key = tuple(name_token(word) for word in words)
        if words and key not in seen:
            seen.add(key)
            unique.append(words)
    roster_words = _words(roster_name)
    matches = [words for words in unique
               if _find_sequence(roster_words, words) >= 0]
    return max(matches, key=len) if matches else (unique[0] if unique else [])


def _roster_given_words(roster_name: str, surname: list[str]) -> list[str]:
    raw = clean_text(roster_name)
    before, comma, after = raw.partition(",")
    before_words = _words(before)
    surname_tokens = [name_token(x) for x in surname]
    if comma and [name_token(x) for x in before_words] == surname_tokens:
        candidates = _words(after)
    else:
        all_words = _words(raw)
        position = _find_sequence(all_words, surname)
        if position < 0:
            return []
        candidates = all_words[:position]
    while candidates and name_token(candidates[0]) in HONORIFICS:
        candidates.pop(0)
    return [word for word in candidates
            if name_token(word) not in (HONORIFICS | SUFFIXES | DESIGNATIONS)]


def _compatible(candidate: str, sec_names: list[str]) -> bool:
    token = name_token(candidate)
    return any(token == name_token(name) or _nickname_pair(name, candidate)
               for name in sec_names if name_token(name))


def _formal_given(sec_first: str, sec_used: str) -> str:
    """Readable SEC given name without duplicating initial-plus-used forms."""
    used_words = _words(sec_used)
    if len(used_words) == 1 and _valid_given(used_words[0]):
        return tidy(used_words[0])
    first_words = _words(sec_first)
    substantive = [word for word in first_words if _valid_given(word)]
    initials = [word for word in first_words if len(name_token(word)) == 1]
    if substantive and not initials:
        return tidy(" ".join(substantive))
    if initials:
        return ". ".join(tidy(word) for word in initials) + "."
    return tidy(substantive[0]) if substantive else ""


def _email_given(email: str, surname: list[str]) -> str:
    if email.count("@") != 1:
        return ""
    local = email.split("@", 1)[0]
    parts = [part for part in re.split(r"[^A-Za-z]+", local) if part]
    if len(parts) < 2:
        return ""
    surname_tokens = [name_token(x) for x in surname]
    terminal = name_token(parts[-1])
    if terminal not in {surname_tokens[-1], "".join(surname_tokens)}:
        return ""
    return parts[0] if _valid_given(parts[0]) else ""


def resolve_roster_greeting(
        *, roster_name: str, email: str, sec_first: str, sec_middle: str,
        sec_used: str, sec_last: str, email_unique: bool = True,
        authoritative_domain: bool = False,
        sec_aliases: tuple[str, ...] = (),
        approved_greeting: str = "") -> GreetingDecision:
    """Choose greeting/surname from authoritative evidence, deterministically."""
    sec_aliases = tuple(clean_text(value) for value in sec_aliases
                        if clean_text(value))
    surname = _choose_surname(roster_name, sec_last, sec_aliases)
    last_name = tidy(" ".join(surname))
    primary_surname = _surname_words(sec_last)
    presentation_last_name = (
        tidy(sec_last) if [name_token(x) for x in surname]
        == [name_token(x) for x in primary_surname] else last_name)
    given_words = _roster_given_words(roster_name, surname) if surname else []
    sec_names = [sec_first, sec_middle, sec_used]
    sec_name_forms = [name for value in sec_names for name in _words(value)
                      if name_token(name)]
    greeting = ""
    reason = "no_safe_greeting"

    for match in _PAREN.findall(clean_text(roster_name)):
        candidate = clean_text(match)
        if (_valid_given(candidate)
                and (name_token(candidate) == name_token(sec_used)
                     or any(_nickname_pair(name, candidate)
                            for name in (sec_first, sec_middle)))):
            greeting, reason = tidy(candidate), "roster_parenthetical_preference"
            break

    if not greeting:
        used_token = name_token(sec_used)
        used_match = next((word for word in given_words
                           if used_token and name_token(word) == used_token
                           and _valid_given(word)), "")
        if used_match:
            greeting, reason = tidy(used_match), "roster_sec_used_explicit"

    if not greeting:
        published = next((word for word in given_words
                          if _valid_given(word)), "")
        if published:
            greeting, reason = tidy(published), "roster_published_given"

    initial_led = bool(given_words and len(name_token(given_words[0])) == 1)
    email_given = (_email_given(email, surname)
                   if email_unique and authoritative_domain else "")
    if email_given and initial_led and _compatible(email_given, sec_name_forms):
        if not greeting:
            greeting, reason = tidy(email_given), "roster_email_initial_refinement"
        elif name_token(email_given) != name_token(greeting):
            if (_nickname_pair(greeting, email_given)
                    or any(_nickname_pair(name, email_given)
                           for name in sec_name_forms if name_token(name))):
                greeting, reason = tidy(email_given), "roster_email_initial_refinement"

    if not greeting:
        used_parts = [part for part in _words(sec_used)
                      if _valid_given(part)]
        fallback = (sec_used if _valid_given(sec_used)
                    else (used_parts[-1] if used_parts else sec_first))
        if _valid_given(fallback):
            greeting = tidy(fallback)
            reason = ("sec_used_fallback" if fallback == sec_used
                      else "sec_legal_fallback")

    formal_given = _formal_given(sec_first, sec_used)
    display_greeting = (tidy(approved_greeting)
                        if _valid_given(approved_greeting) else greeting)
    visible_given = formal_given or display_greeting
    if (display_greeting and name_token(display_greeting) not in
            {name_token(word) for word in _words(visible_given)}):
        visible_given = (f"{visible_given} ({display_greeting})"
                         if visible_given else display_greeting)
    presentation_name = " ".join(
        value for value in (visible_given, presentation_last_name) if value)

    evidence = {
        "ruleset": RULESET, "rosterName": clean_text(roster_name),
        "email": clean_text(email).lower(), "secFirst": clean_text(sec_first),
        "secMiddle": clean_text(sec_middle), "secUsed": clean_text(sec_used),
        "secLast": clean_text(sec_last), "secAliases": list(sec_aliases),
        "approvedGreeting": clean_text(approved_greeting),
        "emailUnique": bool(email_unique),
        "authoritativeDomain": bool(authoritative_domain),
        "greeting": greeting, "lastName": last_name,
        "presentationLastName": presentation_last_name,
        "presentationName": presentation_name, "reason": reason,
    }
    return GreetingDecision(greeting, last_name, presentation_last_name,
                            presentation_name, reason, content_hash(evidence))
