'''Reconcile ACT account codes absent from the deployed EIC-assets map.

Writes a private, account-level CSV and summary JSON under data/output/.
No identity approvals or application artifacts are changed.
'''
from __future__ import annotations

import collections
import csv
import json
import pathlib

from act_book import build, slots
from act_economic_links import spreadsheet_safe
from build_act_assets import load_deployed_advisor_crds, load_economic_links
from contact_provenance import sha256_file
from identity_normalize import is_generic_email, normalize_email

ROOT = pathlib.Path(__file__).parents[1]
OUT = ROOT / 'data' / 'output'


def personal(value: object) -> str:
    email = normalize_email(value)
    return email if email and not is_generic_email(email) else ''


def main() -> None:
    source, rows, links, _manifest = load_economic_links()
    accounts, _conflicts, unresolved = build(rows)
    if unresolved:
        raise SystemExit('ACT account values have unresolved conflicts')
    deployed, _fact = load_deployed_advisor_crds()
    app = json.loads((ROOT / 'webapp' / 'data' / 'contacts.json').read_text(
        encoding='utf-8')).get('advisors') or {}
    by_map_email = collections.defaultdict(set)
    for crd, item in app.items():
        email = personal((item or {}).get('e'))
        if email:
            by_map_email[email].add(str(crd))
    by_act_email = collections.Counter(personal(row.get('emailAddress'))
                                        for row in rows)
    by_act_email.pop('', None)
    ledger = {str(item['act_id']): item for item in links.to_dict('records')}
    holders = collections.defaultdict(list)
    for row in rows:
        for code, *_ in slots(row.get('customFields') or {}):
            if code:
                holders[code].append(row)

    report = []
    for code, account in sorted(accounts.items()):
        people = holders.get(code, [])
        on_map = any(
            str(ledger.get(str(p.get('id')), {}).get('economic_status')) == 'approved'
            and str(ledger[str(p.get('id'))].get('advisor_crd')) in deployed
            for p in people)
        if on_map:
            continue
        approved_off_map = False
        exact = []
        duplicate = False
        any_email = False
        any_map_email = False
        for person in people:
            item = ledger.get(str(person.get('id')), {})
            crd = str(item.get('advisor_crd') or '')
            if item.get('economic_status') == 'approved' and crd not in deployed:
                approved_off_map = True
            email = personal(person.get('emailAddress'))
            if not email:
                continue
            any_email = True
            map_crds = by_map_email.get(email, set())
            any_map_email = any_map_email or bool(map_crds)
            if len(map_crds) > 1 or by_act_email[email] > 1:
                duplicate = True
            elif len(map_crds) == 1:
                matched_crd = next(iter(map_crds))
                exact.append((person, item, matched_crd))
        if approved_off_map:
            category = 'approved_crd_off_map'
        elif any(hit[2] in deployed for hit in exact):
            category = 'unique_email_to_mapped_crd_not_approved'
        elif exact:
            category = 'unique_email_to_off_map_crd'
        elif duplicate:
            category = 'duplicate_email'
        elif any_map_email:
            category = 'email_match_not_unique'
        elif any_email:
            category = 'no_map_email_match'
        else:
            category = 'no_act_email'
        matched = [hit for hit in exact if hit[2] in deployed]
        report.append({
            'account_code': code, 'category': category,
            'value': round(account['value'], 2),
            'acv': round(account['acv_sma'], 2),
            'lcv': round(account['large'], 2),
            'mutual_fund': round(account['fund'], 2),
            'midcap': round(account['midcap'], 2),
            'holder_count': len(people),
            'act_ids': '; '.join(sorted(str(p.get('id')) for p in people)),
            'act_names': '; '.join(sorted(set(str(p.get('fullName') or '') for p in people))),
            'act_emails': '; '.join(sorted(set(personal(p.get('emailAddress'))
                                               for p in people if personal(p.get('emailAddress'))))),
            'exact_email_map_crds': '; '.join(sorted(set(hit[2] for hit in exact))),
            'mapped_exact_crds': '; '.join(sorted(set(hit[2] for hit in matched))),
            'economic_statuses': '; '.join(sorted(set(
                str(ledger.get(str(p.get('id')), {}).get('economic_status') or '')
                for p in people))),
            'economic_reasons': '; '.join(sorted(set(
                str(ledger.get(str(p.get('id')), {}).get('reason') or '')
                for p in people))),
        })
    OUT.mkdir(parents=True, exist_ok=True)
    csv_path = OUT / 'act_unmapped_asset_audit.csv'
    with csv_path.open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(report[0]) if report else [])
        writer.writeheader()
        writer.writerows({key: spreadsheet_safe(value) for key, value in item.items()}
                         for item in report)
    summary = {'source': source.name, 'accounts': len(report),
               'value': round(sum(item['value'] for item in report), 2),
               'categories': {}}
    published = json.loads((ROOT / 'webapp' / 'data' / 'act_assets.json').read_text(
        encoding='utf-8'))
    summary['published_asset_index_matches_current'] = (
        (published.get('advisor_index') or {}).get('sha256')
        == sha256_file(ROOT / 'webapp' / 'data' / 'advisor_index.json'))
    summary['published_unapproved_accounts'] = (
        (published.get('source_totals') or {}).get('unapproved_or_unresolved') or {}
    ).get('accounts')
    summary['published_approved_off_map_accounts'] = (
        (published.get('source_totals') or {}).get('approved_off_map') or {}
    ).get('accounts')
    for category in sorted(set(item['category'] for item in report)):
        found = [item for item in report if item['category'] == category]
        summary['categories'][category] = {
            'accounts': len(found),
            'value': round(sum(item['value'] for item in found), 2),
        }
    summary_path = OUT / 'act_unmapped_asset_audit.json'
    summary_path.write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2))
    print(csv_path)


if __name__ == '__main__':
    main()
