"""Keep office entities and disputed email claims out of person authority."""
import json
import re
from collections import defaultdict
import unicodedata

from roster_continuity import compatible_names,person_name

EMAIL=re.compile(r'[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+')


def morgan_ui_claim_correlates(name,address):
    """Reject clearly unrelated UI identifiers, not resolve a person's identity.

    A correlated token is only source-quality evidence. It never supplies a
    nickname, CRD, or registration match. Exact source claims remain retained.
    """
    def fold(value):return ''.join(c for c in unicodedata.normalize('NFKD',str(value)).casefold() if c.isascii())
    from identity_normalize import DESIGNATIONS,SUFFIXES
    ignored=set(DESIGNATIONS)|set(SUFFIXES)|{'qpa','cpm','fpqp','van','von','del','der','de','da','al','la','le'}
    tokens=[word for word in re.findall('[a-z]+',fold(person_name(str(name)))) if word not in ignored]
    local_parts=re.findall('[a-z]+',fold(str(address).split('@',1)[0]))
    local=''.join(local_parts)
    significant=[word for word in tokens if len(word)>=3]
    # Short names (Yu, Hu, Ho) need a whole mailbox component, not an
    # arbitrary substring: "Al" must not corroborate "sales-team".
    short_component=any(len(word)==2 and word in local_parts for word in tokens)
    return bool(local) and (any(word in local for word in significant) or short_component or len(local)<=3 and local==''.join(word[0] for word in tokens))


def quarantine_morgan_ui_claims(rows):
    """A staff-carousel navigation key is not itself an email ownership claim."""
    output=[dict(row) for row in rows];review=[]
    for row in output:
        address=str(row.get('Email') or '').strip()
        if row.get('Email Source')!='ui_identifier' or not address:continue
        if morgan_ui_claim_correlates(row.get('Name',''),address):continue
        row['Email']='';row['Email Review Reason']='ui_identifier_has_no_person_name_corroboration'
        row['Published Email Claims']=json.dumps([address])
        review.append({'name':row.get('Name',''),'claim':address,'reason':row['Email Review Reason'],
            'sourcePage':row.get('Team Page URL') or row.get('Profile URL','')})
    return output,review


def cetera_person(row):
    def present(value):
        return isinstance(value,str) and value.strip().casefold() not in {'','nan','none','null','n/a'}
    return present(row.get('first_name')) and present(row.get('last_name'))


def quarantine_shared_emails(rows,email_field='Emails',name_field='MarketingName'):
    """Retain names/phones; disputed claims remain in explicit review fields."""
    output=[dict(row) for row in rows]
    owners=defaultdict(set)
    for row in output:
        name=person_name(str(row.get(name_field) or '').strip().casefold())
        for address in EMAIL.findall(str(row.get(email_field) or '').lower()):owners[address].add(name)
    disputed=set()
    for address,names in owners.items():
        names=list(names)
        if any(not compatible_names({a},{b}) for i,a in enumerate(names) for b in names[i+1:]):
            disputed.add(address)
    review=[]
    for row in output:
        addresses=list(dict.fromkeys(EMAIL.findall(str(row.get(email_field) or '').lower())))
        conflicts=[a for a in addresses if a in disputed]
        if not conflicts:continue
        valid=[a for a in addresses if a not in disputed]
        row[email_field]=json.dumps(valid) if len(valid)>1 else valid[0] if valid else ''
        row['disputed_email_claims']=json.dumps(conflicts)
        row['email_review_reason']='incompatible_names_share_published_email'
        review.append({name_field:row.get(name_field,''),'emails':conflicts,
            'reason':row['email_review_reason']})
    return output,review
