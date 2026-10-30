"""Offline proposals only: never imported by the emailer or applied to contacts."""
import argparse
import csv
import gzip
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from identity_schema import runtime_content_hash

ROOT = Path(__file__).resolve().parents[1]

# Explicit display proposals, not corporate identity or succession assertions.
ALIASES = {
    'UBS Financial Services Inc.': 'UBS',
    'Morgan Stanley Wealth Management, LLC': 'Morgan Stanley',
    'Raymond James (branch pages)': 'Raymond James',
    'Raymond James Financial Services': 'Raymond James',
    'Raymond James Financial Services, Inc.': 'Raymond James',
    'Raymond James & Associates': 'Raymond James',
    'Wells Fargo Advisors, LLC': 'Wells Fargo Advisors',
    'Stifel Nicolaus & Company Inc': 'Stifel',
    'Stifel Nicolaus': 'Stifel',
    'LPL Financial LLC': 'LPL Financial',
    'Ameriprise Financial Services, Inc': 'Ameriprise',
    'Ameriprise Financial Services, Inc.': 'Ameriprise',
    'Robert W. Baird & Co.': 'Robert W. Baird',
    'Charles Schwab & Co., Inc.': 'Charles Schwab',
    'Benjamin F. Edwards & Company': 'Benjamin F. Edwards',
    'Benjamin F. Edwards & Co.': 'Benjamin F. Edwards',
    'Oppenheimer & Co. Inc.': 'Oppenheimer',
    'Oppenheimer & Co.': 'Oppenheimer',
    'Oppenheimer & Co': 'Oppenheimer',
    'Seventy2Capital Wealth Mgmt': 'Seventy2 Capital Wealth Management',
}

SPELLING = {
    'Wlliam Blair': 'William Blair',
    'Collective Finanical Group': 'Collective Financial Group',
    'Encore Wealth Managemnt': 'Encore Wealth Management',
    'Fiat Wealth Managment': 'Fiat Wealth Management',
    'Cardan Captail Partners': 'Cardan Capital Partners',
    'Santuary Securities': 'Sanctuary Securities',
    'Rinaldi Waelth Management': 'Rinaldi Wealth Management',
    'Clear Creek Financial Managment': 'Clear Creek Financial Management',
    'Reaserach Financial Strategies': 'Research Financial Strategies',
    'Moravec Financial Adviors, Inc.': 'Moravec Financial Advisors',
    'Schiavi & Dattani Financial Plannning': 'Schiavi & Dattani Financial Planning',
    'North Star Resource Gorup': 'North Star Resource Group',
    'Velocity Conecepts': 'Velocity Concepts',
    'Citi Personal Wealth Mangement': 'Citi Personal Wealth Management',
    'SApient Capital': 'Sapient Capital',
}

# Keep these labels pending a human decision; no inferred parent/successor.
BRAND_REVIEW = {
    'Alex. Brown (Raymond James)', 'Alex. Brown - A Division of Raymond James',
    'Graystone Consulting from Morgan Stanley', 'Greystone Consulting From Morgan Stanley',
    'Wells Fargo Advisors Financial Network', 'Wells Fargo Wealth Management',
    'Wells Fargo Clearing Services, LLC', 'Wells Fargo Clearing Services, LLC.',
    'Wells Fargo Bank', 'Merrill Private Wealth Management', 'B of A Merrill Lynch',
    'JP Morgan Private Bank', 'J.P. Morgan', 'Fidelity', 'Fidelity Investments',
    'Thrivent Financial', 'Thrivent Financial for Lutherans',
    'Thrivent Financial For Lutherans', 'Thrivent Investment Management Inc.',
}
RELATIONSHIP_REVIEW = {
    'BB&T Scott & Stringfellow', 'BB&T Investments', 'SunTrust Bank', 'SunTrust',
    'SunTrust Investment Services, Inc.', 'Hilliard Lyons',
    'First Republic Private Wealth Management', 'U.S. Trust', 'H.D. Vest Investment Services',
    'Securities America, Inc.', 'Securities America Advisors',
    'Royal Alliance Associates, Inc.', 'Royal Alliance Associates',
    'NFP Advisor Services, LLC', 'Sterne, Agee & Leach Inc.',
    'Southwest Securities, Inc.', 'SWS Financial Services', 'Boston Private',
    'Luminous Capital', 'Miller Russell Associates', 'Woodbury Financial Services',
    'Tiedemann Wealth Management, LLC', 'Convergent Wealth Advisors',
}
AMBIGUOUS = {
    'Rock', 'First Southern', 'Insight Global Wealth Strategie',
    'The Commercer Company', 'LaSelle St. Securities', 'Faircourt Partners/ LLC',
    'Generations Financial Planning @ Wealth Mgmt.', 'Duncan.Newman Associates',
    'Premier Income Partner', 'Stratos Wealth Partner',
}
LEGAL_SUFFIX = re.compile(
    r'(?:,?\s+(?:L\.?L\.?C\.?|L\.?L\.?P\.?|L\.?P\.?|Inc\.?|Incorporated|'
    r'Ltd\.?|Corp\.?|Corporation|P\.?A\.?|P\.?C\.?))+$', re.I)


def proposal(name, internal_only=False):
    if internal_only:
        return name, 'internal_only', 'Internal recipients; not an external-brand cleanup target.', False
    if name in BRAND_REVIEW:
        return name, 'review_distinct_brand', 'Confirm preferred wording; preserve practice/platform distinctions. No parent-brand inference.', True
    if name in RELATIONSHIP_REVIEW:
        return name, 'review_relationship_wording', 'Confirm desired current wording. No merger, successor or employment inference has been made.', True
    if name in AMBIGUOUS or not name.strip():
        return name, 'review_ambiguous_label', 'Possibly abbreviated, misspelled or unusually punctuated; no replacement guessed.', True
    if name in SPELLING:
        return SPELLING[name], 'review_spelling', 'Spelling/capitalization proposal only; confirm the intended firm before approving.', True
    if name in ALIASES:
        kind = 'remove_source_annotation' if name == 'Raymond James (branch pages)' else 'propose_exact_display_alias'
        return ALIASES[name], kind, 'Explicit email-display proposal only; underlying firm, identity and routing stay unchanged.', True
    shortened = LEGAL_SUFFIX.sub('', name).rstrip(' ,')
    if shortened and shortened != name:
        return shortened, 'review_legal_suffix', 'Mechanical removal of a terminal legal suffix; not independently verified branding.', True
    return name, 'keep_as_is', 'No explicit display change proposed; not a verification of current branding or employment.', False


def audit(payload):
    grouped = defaultdict(list)
    for recipient in payload['recipients'].values():
        if recipient.get('eligible') is not True:
            raise ValueError('Unexpected ineligible record inside approved recipients')
        grouped[str(recipient.get('firm') or '')].append(recipient)
    rows = []
    for name, recipients in grouped.items():
        internal = sum(r.get('internal') is True for r in recipients)
        proposed, category, reason, review = proposal(name, internal == len(recipients))
        domains = Counter(str(r.get('email', '')).rsplit('@', 1)[-1].casefold()
                          for r in recipients if '@' in str(r.get('email', '')))
        sources = Counter(str(r.get('source') or 'unspecified') for r in recipients)
        changed = proposed != name
        priority = (1 if category == 'remove_source_annotation' else
                    2 if category in {'review_spelling', 'review_ambiguous_label', 'review_relationship_wording'} else
                    3 if review else 4)
        rows.append(dict(review_priority=priority, current_company_name=name,
            proposed_email_display_name=proposed, recipient_count=len(recipients),
            external_recipient_count=len(recipients)-internal, internal_recipient_count=internal,
            recommendation=category, proposed_change=changed, needs_review=review,
            decision='Pending review' if review else 'Keep unchanged', reason=reason,
            source_mix='; '.join('%s: %s' % x for x in sources.most_common()),
            top_email_domains='; '.join('%s: %s' % x for x in domains.most_common(3)),
            proposed_name_already_in_registry=proposed in grouped,
            example_sentence='How are things going at %s?' % proposed,
            application_change_applied=False))
    rows.sort(key=lambda r: (r['review_priority'], -r['recipient_count'], r['current_company_name']))
    return rows


def safe_cell(value):
    text = str(value)
    # Quote CSV normally, and neutralize spreadsheet formulas independently.
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) else text


def roster_recipients(payload):
    return {key: value for key, value in payload['recipients'].items()
            if str(value.get('source', '')).strip()
            and str(value.get('source', '')).strip().upper() not in {'CRM', 'EIC'}
            and value.get('internal') is not True}


def audit_roster_labels(payload):
    selected = dict(payload, recipients=roster_recipients(payload))
    rows = audit(selected)
    for row in rows:
        if row['current_company_name'] == 'Alex. Brown (Raymond James)':
            row.update(proposed_email_display_name='Alex. Brown',
                recommendation='propose_distinct_brand_shortening', proposed_change=True,
                needs_review=True, decision='Pending review',
                reason='Retain Alex. Brown as its own email display brand; omit the parent-company annotation. Routing stays unchanged.',
                proposed_name_already_in_registry=False,
                example_sentence='How are things going at Alex. Brown?')
        row['review_priority'] = 1 if row['proposed_change'] else 2
    rows.sort(key=lambda r: (r['review_priority'], -r['recipient_count'], r['current_company_name']))
    return rows


def run(root, output, roster_only=False):
    root = Path(root).resolve()
    source = root/'data/identity/approved_recipients.json.gz'
    descriptor = root/'api/shared/approved-recipient-release.json'
    compressed = source.read_bytes(); descriptor_bytes = descriptor.read_bytes()
    payload = json.loads(gzip.decompress(compressed).decode('utf-8'))
    release = json.loads(descriptor_bytes.decode('utf-8-sig'))
    core = {k: v for k, v in payload.items() if k not in {'generated', 'contentHash'}}
    if runtime_content_hash(core) != payload['contentHash'] or payload['contentHash'] != release['registryContentHash']:
        raise ValueError('Registry integrity/release binding failed')
    if len(payload['recipients']) != release['recipientCount']:
        raise ValueError('Registry count/release binding failed')
    selected_count = len(roster_recipients(payload)) if roster_only else len(payload['recipients'])
    rows = audit_roster_labels(payload) if roster_only else audit(payload)
    if sum(row['recipient_count'] for row in rows) != selected_count:
        raise ValueError('Audit does not account for every recipient')
    output = Path(output).resolve()
    output.relative_to(root/'data/output')
    if output == root/'data/output' or output.exists():
        raise ValueError('A new, dedicated audit output directory is required')
    output.mkdir(parents=True)
    outputs = []
    full_name = 'roster_company_names.csv' if roster_only else 'company_name_audit.csv'
    changes_name = 'recommended_changes.csv' if roster_only else 'changes_and_review.csv'
    for filename, selected in [(full_name, rows),
                               (changes_name, [r for r in rows if r['needs_review']])]:
        path = output/filename
        with path.open('x', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows({k: safe_cell(v) for k, v in row.items()} for row in selected)
        raw = path.read_bytes()
        outputs.append(dict(file=filename, rows=len(selected), sha256=hashlib.sha256(raw).hexdigest()))
    if source.read_bytes() != compressed or descriptor.read_bytes() != descriptor_bytes:
        raise ValueError('Source changed during audit; output must not be accepted')
    summary = dict(schema='email-company-name-audit-v1', proposal_only=True,
        registry_generated=payload.get('generated'), registry_content_hash=payload['contentHash'],
        source_sha256=hashlib.sha256(compressed).hexdigest(),
        release_descriptor_sha256=hashlib.sha256(descriptor_bytes).hexdigest(),
        release_binding_verified=True, sources_unchanged=True,
        recipient_count=selected_count, distinct_company_labels=len(rows),
        source_registry_recipient_count=len(payload['recipients']),
        scope='Non-ACT, non-internal roster-source winners only' if roster_only else 'All release-approved recipients',
        proposed_changed_labels=sum(r['proposed_change'] for r in rows),
        recipients_with_proposed_display_change=sum(r['recipient_count'] for r in rows if r['proposed_change']),
        labels_needing_review=sum(r['needs_review'] for r in rows),
        categories=dict(Counter(r['recommendation'] for r in rows)), outputs=outputs,
        email_behavior_changed=False, existing_drafts_changed=False,
        identity_or_routing_changed=False,
        limitations=['No internet/current-brand verification; proposals use explicit alias rules and conservative formatting only.',
            'Unchanged names are not certified correct. Flagged spellings require human confirmation.',
            'Scope is the API release-bound approved-recipient registry, not every raw ACT or SEC firm.',
            'No individual names or full email addresses are exported. Aggregated domains are review clues, not identity proof.'])
    with (output/'summary.json').open('x', encoding='utf-8') as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--roster-only', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(ROOT, args.output_dir, roster_only=args.roster_only), indent=2))
