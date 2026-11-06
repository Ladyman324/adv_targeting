"""Edward Jones branch-list selection, not person/CRD or recipient approval.

The same source-specific selector is usable by acquisition and normalization.
Branch candidates are never ordered by ownership, and a sole candidate can be
support staff. Keep candidates intact when selection requires review.
"""
from dataclasses import dataclass
from functools import lru_cache
import re
import unicodedata
from urllib.parse import urlsplit

from identity_normalize import DESIGNATIONS, HONORIFICS, STRICT_NICKNAMES, SUFFIXES
from forbes_match import NICKNAMES


@dataclass(frozen=True)
class Selection:
    email: str
    reason: str
    matched: tuple = ()


def letters(value):
    ascii_text = unicodedata.normalize('NFKD', str(value or '')).encode('ascii', 'ignore').decode()
    return re.sub('[^a-z]', '', ascii_text.lower())


@lru_cache(maxsize=2048)
def given_forms(given):
    # Reuse the application's established literal nickname groups rather
    # than inventing surname-only ownership when a calling name differs.
    forms = {given} | set(NICKNAMES.get(given, ()))
    for formal, short in STRICT_NICKNAMES.items():
        group = {formal} | set(short)
        if given in group:
            forms.update(group)
    return frozenset(forms)


def name_parts(name):
    text = str(name or '').replace('\u2019', "'")
    aliases = {letters(word) for block in re.findall(r'\(([^)]*)\)', text)
               for word in block.split() if letters(word)}
    text = re.sub(r'\([^)]*\)', ' ', text)
    raw = re.split(r'[\s.,]+', text.strip())
    tokens = [letters(word) for word in raw if letters(word)]
    while tokens and tokens[0] in HONORIFICS:
        tokens.pop(0)
    # JR can itself be a published calling name (JR Kaster/Frakes/Whitaker).
    # Credentials and generational suffixes are suffixes only at the end.
    while len(tokens) > 1 and tokens[-1] in DESIGNATIONS | SUFFIXES:
        tokens.pop()
    if len(tokens) < 2:
        return '', (), frozenset()
    # Preserve a published middle calling name after a leading initial.
    given = next((token for token in tokens[:-1] if len(token) > 1), tokens[0])
    # The last surname and exact full remaining-name form are both explicit
    # in the source (e.g. Michele Pearson Tucker -> pearson.tucker).
    tails = {tokens[-1], ''.join(tokens[tokens.index(given) + 1:])}
    return given, tuple(sorted(tails)), given_forms(given) | aliases


def select_email(name, candidates, profile_url=''):
    given, surnames, forms = name_parts(name)
    if not given or not surnames:
        return Selection('', 'missing_source_name')
    clean = sorted({str(value or '').strip().lower() for value in candidates
                    if re.fullmatch(r"[^@\s<>]+@edwardjones\.com", str(value or '').strip(), re.I)})
    if not clean:
        return Selection('', 'no_published_candidates')
    literal, nickname, initial = [], [], []
    for address in clean:
        local = address.split('@')[0]
        # A publisher's numeric mailbox disambiguator is not part of a name.
        # Do not strip alphabetic/generational suffixes that may mean a son.
        local = re.sub(r'\d+$', '', local)
        compact = letters(local)
        chunks = [letters(part) for part in re.split(r'[._]', local) if letters(part)]
        # Publisher emails commonly include a middle initial not printed in
        # the short display name (rhonda.l.jones, v.daniel.howard).
        def matches_with_initial(form):
            if len(chunks) < 2 or chunks[-1] not in surnames:
                return False
            head = chunks[:-1]
            return any(part == form
                       and all(len(other) == 1 for j, other in enumerate(head) if j != i)
                       for i, part in enumerate(head))
        if any(compact == given + surname for surname in surnames) or matches_with_initial(given):
            literal.append(address)
        elif (any(compact == form + surname for form in forms for surname in surnames)
              or any(matches_with_initial(form) for form in forms)):
            nickname.append(address)
        elif re.fullmatch(r'[a-z][._-].+', local) and any(
                compact == given[0] + surname for surname in surnames):
            initial.append(address)
    for matches, reason in [(literal, 'literal_full_name'), (nickname, 'known_given_name'),
                            (initial, 'given_initial_and_surname')]:
        if matches:
            if len(matches) == 1:
                return Selection(matches[0], reason, tuple(matches))
            return Selection('', 'ambiguous_' + reason, tuple(matches))
    # The publisher often uses a formal name in this same record's personal
    # profile route and a calling name in faName. This is row-scoped source
    # evidence, never a global nickname, CRD bridge, or arbitrary surname hit.
    try:
        route = urlsplit(str(profile_url or ''))
    except ValueError:
        return Selection('', 'invalid_source_profile')
    safe_origin = (not route.scheme and not route.netloc or
                   route.scheme == 'https' and route.netloc == 'www.edwardjones.com')
    match = re.fullmatch(r'/us-en/financial-advisor/([A-Za-z0-9][A-Za-z0-9_-]*)/?', route.path)
    if safe_origin and not route.query and not route.fragment and match:
        slug = match.group(1).lower()
        key = letters(slug)
        first = letters(slug.split('-')[0])
        route_tokens = [letters(part) for part in slug.split('-') if letters(part)]
        agrees = (first in forms or any(key.endswith(surname) for surname in surnames)
                  or any(key == surname + form for surname in surnames for form in forms)
                  or len(route_tokens) >= 2 and route_tokens[0].endswith(given)
                  and any(route_tokens[-1].startswith(surname) for surname in surnames))
        supported = [address for address in clean if letters(address.split('@')[0]) == key]
        if agrees and supported:
            if len(supported) == 1:
                return Selection(supported[0], 'published_personal_profile_name', tuple(supported))
            return Selection('', 'ambiguous_published_profile_name', tuple(supported))
    return Selection('', 'no_name_supported_candidate')
