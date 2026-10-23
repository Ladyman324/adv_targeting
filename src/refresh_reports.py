"""Private, spreadsheet-safe release comparison reports (not identity authority)."""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
from pathlib import Path


# Exact operational timestamp keys only. Employment dates, edition years and
# other business timestamps must continue to affect content identity.
OPERATIONAL_TIMESTAMPS = frozenset({'generatedUtc', 'generated_utc', 'generated',
                                  'built_utc', 'builtUtc', 'as_of'})


def semantic_content_hash(value) -> str:
    def clean(item):
        if isinstance(item, dict):
            return {key: clean(val) for key, val in item.items()
                    if key not in OPERATIONAL_TIMESTAMPS}
        if isinstance(item, list):
            return [clean(val) for val in item]
        return item
    raw = json.dumps(clean(value), sort_keys=True, separators=(',', ':'),
                     ensure_ascii=False, allow_nan=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _json(root: Path, relative: str) -> dict:
    path = root / relative
    if not path.is_file():
        if path.with_suffix(path.suffix + '.gz').is_file():
            path = path.with_suffix(path.suffix + '.gz')
        else:
            return {}
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8-sig') as handle:
        return json.load(handle)


def _parquet(root: Path, relative: str, key: str, fields: tuple) -> dict:
    path = root / relative
    if not path.is_file():
        return {}
    import pandas as pd
    frame = pd.read_parquet(path).fillna('')
    if key not in frame:
        raise ValueError(f'Missing key {key} in {relative}')
    return {str(row[key]): {field: row.get(field, '') for field in fields}
            for row in frame.to_dict('records')}


def _parquet_rows(root, relative, columns=None):
    path = root / relative
    if not path.is_file():
        return []
    import pandas as pd
    if columns is not None:
        import pyarrow.parquet as pq
        available = set(pq.read_schema(path).names)
        columns = [column for column in columns if column in available]
    frame = pd.read_parquet(path, columns=columns)
    rows = frame.astype(object).where(pd.notna(frame), None).to_dict('records')
    return json.loads(json.dumps(rows, default=lambda value: value.isoformat(), allow_nan=False))


def _grouped_parquet(root, relative, keys, fields):
    result = {}
    for row in _parquet_rows(root, relative, list(dict.fromkeys(keys + fields))):
        if any(key not in row for key in keys):
            raise ValueError(f'Missing grouping key in {relative}')
        key = json.dumps([str(row[name] or '') for name in keys], separators=(',', ':'))
        result.setdefault(key, []).append({field: row[field] for field in fields if field in row})
    for rows in result.values():
        rows.sort(key=lambda row: json.dumps(row, sort_keys=True, separators=(',', ':')))
    return result


def _ranking_matches(root, provider):
    relative = ('data/interim/forbes_matches.parquet' if provider == 'forbes'
                else 'data/interim/barrons_rankings.parquet')
    rows = _parquet_rows(root, relative)
    result = {}
    counts = {'rows': len(rows), 'unmatched': 0, 'ambiguous': 0, 'tiers': {},
              'available': (root / relative).is_file()}
    for row in rows:
        crd = str(row.get('advisor_crd') or '')
        tier = str(row.get('match_tier') or ('confirmed' if crd else 'none'))
        ambiguous = row.get('ambiguous') is True
        counts['tiers'][tier] = counts['tiers'].get(tier, 0) + 1
        counts['unmatched'] += int(not crd or tier == 'none')
        counts['ambiguous'] += int(ambiguous)
        identity = [row.get('forbes_uri') or row.get('barrons_url') or row.get('advisor_name'),
                    row.get('ranking') or row.get('category'), row.get('year'),
                    row.get('rank_state'), row.get('rank_market'), row.get('rank_segment')]
        key = json.dumps(identity, separators=(',', ':'), ensure_ascii=False)
        result.setdefault(key, []).append({field: row.get(field) for field in (
            'advisor_name', 'advisor_crd', 'match_tier', 'match_score', 'ambiguous',
            'candidates', 'rank', 'year', 'ranking', 'category', 'rank_state')})
    for values in result.values():
        values.sort(key=lambda row: json.dumps(row, sort_keys=True))
    return result, counts


def _coverage(contacts, index, recipients, desktop, field):
    emails = {str(row.get('e') or '').strip().lower() for row in contacts.values()}
    phones = {str(row.get(key) or '').strip() for row in contacts.values() for key in ('w', 'c')}
    emails.discard('')
    phones.discard('')
    return {'contacts': len(contacts), 'map': len(index),
        'eligibleRecipients': len(recipients.get('recipients', {})),
        'emailAddresses': len(emails), 'phoneNumbers': len(phones),
        'emailContacts': sum(bool(str(row.get('e') or '').strip()) for row in contacts.values()),
        'phoneContacts': sum(bool(str(row.get('w') or '').strip() or
                                  str(row.get('c') or '').strip()) for row in contacts.values()),
        'contactable': sum(bool(str(row.get('e') or '').strip() or
                               str(row.get('w') or '').strip() or
                               str(row.get('c') or '').strip()) for row in contacts.values()),
        'desktopSearch': desktop.get('advisors'),
        'fieldSearch': field.get('searchable'), 'fieldUnplaced': field.get('unplaced')}


def _index(root):
    payload = _json(root, 'webapp/data/advisor_index.json')
    firms, cities = payload.get('firms', []), payload.get('cities', [])
    def resolve(values, index):
        return values[index] if isinstance(index, int) and 0 <= index < len(values) else index
    return {str(row[0]): {'name': row[1], 'firm': resolve(firms, row[2]),
                         'state': row[3], 'city': resolve(cities, row[4]),
                         'states': row[5] if len(row) > 5 else ''}
            for row in payload.get('advisors', [])}


def _safe_cell(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False)
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')):
        return "'" + value
    return value


def _write_csv(path, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=['id', 'change', 'before', 'after'])
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _safe_cell(value) for key, value in row.items()})


def _diff(before, after, fields=None):
    rows = []
    for key in sorted(set(before) | set(after)):
        if key not in before:
            rows.append({'id': key, 'change': 'added', 'before': None, 'after': after[key]})
        elif key not in after:
            rows.append({'id': key, 'change': 'removed_from_artifact', 'before': before[key], 'after': None})
        elif fields is None:
            if semantic_content_hash(before[key]) != semantic_content_hash(after[key]):
                rows.append({'id': key, 'change': 'changed', 'before': before[key], 'after': after[key]})
        else:
            for label, names in fields.items():
                old = {name: before[key].get(name) for name in names}
                new = {name: after[key].get(name) for name in names}
                if old != new:
                    rows.append({'id': key, 'change': label, 'before': old, 'after': new})
    return rows


def _assets(payload):
    """Reconcile deduplicated account vectors, never sum per-advisor assets."""
    columns = payload.get('columns', ['acv', 'lcv', 'mf', 'midcap'])
    accounts = payload.get('accounts', [])
    totals = payload.get('totals', {})
    vector_totals = {name: round(sum(float(row[i] or 0) for row in accounts), 2)
                     for i, name in enumerate(columns)}
    issues = []
    if payload:
        for name, amount in vector_totals.items():
            if abs(amount - float(totals.get(name, 0) or 0)) > max(.02, len(accounts) * .005):
                issues.append(f'Account table does not reconcile with totals.{name}')
        source = payload.get('source_totals', {})
        parts = payload.get('deployment_reconciliation', {})
        if source and parts:
            for name in columns:
                amount = sum(float(part.get(name, 0) or 0) for part in parts.values())
                if abs(amount - float(source.get(name, 0) or 0)) > .05:
                    issues.append(f'Source book does not reconcile by category: {name}')
    return {'accountCount': len(accounts), 'accountVectorTotals': vector_totals,
            'mappedTotals': totals, 'sourceTotals': payload.get('source_totals', {}),
            'deploymentReconciliation': payload.get('deployment_reconciliation', {}),
            'issues': issues}


def compare(before_root: Path, after_root: Path, out_dir: Path,
            source_statuses=None) -> dict:
    """Write restricted-workspace JSON+CSV; never publish contact-level diffs."""
    before_root, after_root, out_dir = map(Path, (before_root, after_root, out_dir))
    # Reports contain private contact data and must never be web assets.
    if any(part.lower() in {'webapp', 'public', 'wwwroot'} for part in out_dir.parts):
        raise ValueError('Private refresh reports cannot be written under a public artifact tree')
    out_dir.mkdir(parents=True, exist_ok=True)
    contacts = [_json(root, 'webapp/data/contacts.json').get('advisors', {})
                for root in (before_root, after_root)]
    assets = [_json(root, 'webapp/data/act_assets.json') for root in (before_root, after_root)]
    indexes = [_index(root) for root in (before_root, after_root)]
    territory_configs = [_json(root, 'webapp/data/territories.json')
                         for root in (before_root, after_root)]
    for index, config in zip(indexes, territory_configs):
        for row in index.values():
            row['territory'] = (config.get('states', {}).get(row['state']) or {}).get('c')
    recipients = [_json(root, 'data/identity/approved_recipients.json')
                  for root in (before_root, after_root)]
    categories = {'contacts': _diff(*contacts, fields={
        'email': ('e',), 'phone': ('w', 'c', 'wx'), 'name': ('n', 'g', 'pn'),
        'source_identity': ('src', 't', 'aid', 'ih'), 'firm_team': ('tn', 'tm', 'pk')})}
    categories['map'] = _diff(*indexes, fields={
        'firm': ('firm',), 'location': ('state', 'city', 'states'), 'name': ('name',),
        'territory': ('territory',)})
    categories['assets'] = _diff(*[p.get('advisors', {}) for p in assets])
    working_fields = ('branch_street1', 'branch_street2', 'branch_city', 'branch_state',
        'branch_postal', 'lat', 'lon', 'addr_key', 'location_source', 'coordinate_origin',
        'geocode_source', 'geocode_precision')
    for category, relative, keys, fields in (
        ('working_offices', 'data/interim/contact_work_branches.parquet',
         ('advisor_crd', 'firm_crd'), working_fields),
        ('placements', 'data/interim/advisor_placement.parquet',
         ('advisor_crd', 'firm_crd'), ('addr_key', 'uncertain', 'location_type', 'home_label')),
        ('sec_employments', 'data/interim/advisor_employments.parquet',
         ('advisor_crd',), ('firm_crd', 'firm_name', 'firm_name_on_record', 'emp_city',
             'emp_state', 'emp_postal', 'emp_street1', 'from_date', 'to_date',
             'reg_earliest_date', 'reg_categories', 'reg_status')),
        ('sec_employment_history', 'data/interim/advisor_employment_history.parquet',
         ('advisor_crd',), ('firm_crd', 'firm_name_on_record', 'city', 'state', 'from_date', 'to_date'))):
        categories[category] = _diff(*[_grouped_parquet(root, relative, keys, fields)
                                       for root in (before_root, after_root)])
    ranking_coverage = {'before': {}, 'after': {}}
    for name in ('barrons', 'forbes'):
        categories[name] = _diff(*[_json(root, f'webapp/data/{name}.json').get('advisors', {})
                                  for root in (before_root, after_root)])
        ranking_matches = [_ranking_matches(root, name) for root in (before_root, after_root)]
        categories[name + '_matches'] = _diff(*[pair[0] for pair in ranking_matches])
        for side, pair in zip(('before', 'after'), ranking_matches):
            ranking_coverage[side][name] = pair[1]
    categories['identity_links'] = _diff(*[_parquet(root,
        'data/identity/act_identity_links.parquet', 'source_record_id',
        ('advisor_crd', 'identity_status', 'decision_reason', 'resolved_evidence_hash'))
        for root in (before_root, after_root)])
    categories['economic_links'] = _diff(*[_parquet(root,
        'data/identity/act_economic_links.parquet', 'act_id',
        ('advisor_crd', 'economic_status', 'link_type', 'reason'))
        for root in (before_root, after_root)])
    recipient_maps = [{**{str(crd): {'eligible': False, 'reason': reason}
                              for crd, reason in payload.get('ineligible', {}).items()},
                       **{str(crd): {'eligible': True, 'email': row.get('email'),
                                    'tier': row.get('tier'), 'source': row.get('source')}
                              for crd, row in payload.get('recipients', {}).items()}}
                      for payload in recipients]
    categories['recipient_eligibility'] = _diff(*recipient_maps)
    categories['territory_config'] = _diff(
        *[config.get('states', {}) for config in territory_configs])
    search = {side: {'desktop': _json(root, 'webapp/data/advisor_search.json'),
                     'field': _json(root, 'webapp/data/name_index.json')}
              for side, root in (('before', before_root), ('after', after_root))}
    result = {'schemaVersion': 1, 'private': True,
        'warning': 'Removed means absent from this candidate artifact, not a verified departure.',
        'sources': source_statuses or [], 'categories': {},
        'artifactAvailability': {side: {name: (root / name).is_file() or
            (root / (name + '.gz')).is_file() for name in (
                'webapp/data/contacts.json', 'webapp/data/advisor_index.json',
                'webapp/data/act_assets.json', 'data/identity/approved_recipients.json',
                'webapp/data/advisor_search.json', 'webapp/data/name_index.json',
                'data/interim/contact_work_branches.parquet', 'data/interim/advisor_placement.parquet',
                'data/interim/advisor_employments.parquet', 'data/interim/advisor_employment_history.parquet',
                'data/interim/forbes_matches.parquet', 'data/interim/barrons_rankings.parquet')}
            for side, root in (('before', before_root), ('after', after_root))},
        'assets': {'before': _assets(assets[0]), 'after': _assets(assets[1])},
        'rankingMatchCoverage': ranking_coverage,
        'coverage': {side: _coverage(contacts[i], indexes[i], recipients[i],
                         search[side]['desktop'], search[side]['field'])
                     for i, side in enumerate(('before', 'after'))}}
    for name, rows in categories.items():
        result['categories'][name] = {'changes': len(rows), 'csv': name + '.csv',
            'added': sum(row['change'] == 'added' for row in rows),
            'removed': sum(row['change'] == 'removed_from_artifact' for row in rows)}
        _write_csv(out_dir / (name + '.csv'), rows)
    result['semanticContentHash'] = semantic_content_hash(result)
    (out_dir / 'comparison.json').write_text(json.dumps(result, ensure_ascii=False,
                                               sort_keys=True, indent=2), encoding='utf-8')
    return result
