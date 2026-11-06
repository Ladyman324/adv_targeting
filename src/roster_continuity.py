"""Comparison-only normalization; never grants CRD/contact/sending authority."""
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

CREDENTIALS = {'aams','aif','awm','caia','cep','cepa','cfa','cfp','chfc','cima','ckp',
    'clu','cpfa','cpa','cpwa','crpc','crps','ctfa','jd','llm','macc','mba','ms','ricp'}


def person_name(value):
    parts = value.split(',')
    while len(parts)>1:
        tail=re.sub('[^a-z]','',parts[-1].lower())
        tail=tail[:-2] if tail.endswith('tm') else tail
        if tail not in CREDENTIALS:break
        parts.pop()
    return ','.join(parts).strip()


def canonical_value(field,value):
    if field=='name':return person_name(value)
    if field=='profile':
        parts=urlsplit(value)
        if parts.hostname and parts.scheme in {'http','https'}:
            query=[(k,v) for k,v in parse_qsl(parts.query,keep_blank_values=True)
                if not k.startswith('utm_') and k not in {'gclid','fbclid','icid'}]
            return urlunsplit(('https',parts.netloc,parts.path.rstrip('/'),urlencode(query),''))
    if field=='address':
        # Normalize spelling only, retaining unit, floor, building and direction.
        from normalize_addr import SUFFIX,DIRECTION
        result=value.upper().replace('.','').replace(',',' ')
        # Equivalent civic/unit/floor spellings only. Never erase a unit,
        # direction, or floor number: those can identify different offices.
        result=re.sub(r'^ONE\s+(?=\S)','1 ',result)
        result=re.sub(r'\b(\d+)(?:ST|ND|RD|TH)\s+FLOOR\b',r'FLOOR \1',result)
        result=re.sub(r'(?:\bSUITE\b|\bSTE\b|#)\s*([A-Z0-9-]+)',r'UNIT \1',result)
        for full,short in DIRECTION.items():result=re.sub(r'\b'+full+r'\b',short,result)
        for full,short in SUFFIX.items():result=re.sub(r'\b'+full+r'\b',short,result)
        return ' '.join(result.lower().split())
    return value


def compatible_names(old,new):
    """A narrow email-backed continuity check, not a registration match."""
    from forbes_match import split_name,suffix_of
    from identity_normalize import given_agreement
    if len(old)!=1 or len(new)!=1:return False
    old_name,new_name=next(iter(old)),next(iter(new))
    if suffix_of(old_name)!=suffix_of(new_name):return False
    a,last_a=split_name(old_name);b,last_b=split_name(new_name)
    if not a or not b or not last_a or not last_b or last_a!=last_b:return False
    if not (given_agreement(a[:1],b[:1])[0] or given_agreement(b[:1],a[:1])[0]):return False
    for x,y in zip(a[1:],b[1:]):
        if x!=y and not (len(x)==1 and y.startswith(x) or len(y)==1 and x.startswith(y)):
            return False
    return True


def email_owner_names_agree(old,current,old_keys,new_keys):
    """Comparison-only: renamed profile keys must not masquerade as swaps.

    A new incompatible named owner (including a group versus an individual)
    still requires review. Blank names cannot corroborate ownership.
    """
    before={n for key in old_keys for n in old[key].get('name',set())}
    after={n for key in new_keys for n in current[key].get('name',set())}
    return bool(before and after) and all(
        a==b or compatible_names({a},{b}) for a in before for b in after)


def bridge_keys(old,current,old_emails,new_emails,slug):
    """Allow specific key migrations using unique email AND compatible name."""
    proposed={}
    for address,new_keys in new_emails.items():
        previous=old_emails.get(address,set())
        if len(previous)!=1 or len(new_keys)!=1:continue
        a,b=next(iter(previous)),next(iter(new_keys))
        if a==b or a in current or b in old:continue
        namespace_change=(a.startswith('provider:') and b.startswith('provider:')
            and a.split(':',2)[1]!=b.split(':',2)[1])
        merrill_promotion=(slug=='merrill' and a.startswith('provider:Advisor ID:p_')
            and b.startswith('provider:Advisor ID:a_'))
        profile_change=a.startswith('profile:') and b.startswith('profile:')
        if not (namespace_change or merrill_promotion or profile_change):continue
        if compatible_names(old[a].get('name',set()),current[b].get('name',set())):
            proposed.setdefault(b,set()).add(a)
    unique={b:next(iter(aa)) for b,aa in proposed.items() if len(aa)==1}
    from collections import Counter
    counts=Counter(unique.values())
    aliases={b:a for b,a in unique.items() if counts[a]==1}
    return ({aliases.get(k,k):v for k,v in current.items()},
            {address:{aliases.get(k,k) for k in keys} for address,keys in new_emails.items()},len(aliases))
