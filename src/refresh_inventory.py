"""Read-only, content-addressed source inventory; never invent retrieval dates."""
from __future__ import annotations

import csv
import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def file_fact(root: Path, path: Path) -> dict:
    return {'file': path.relative_to(root).as_posix(), 'sha256': sha256_file(path),
            'bytes': path.stat().st_size}


def snapshot_facts(root: Path, paths) -> dict:
    facts = [file_fact(root, path) for path in sorted(set(paths))]
    digest = hashlib.sha256(json.dumps(facts, sort_keys=True,
                            separators=(',', ':')).encode()).hexdigest()
    return {'snapshotId': digest, 'sha256': digest, 'fileFacts': facts,
            'files': [fact['file'] for fact in facts]}


def _json(path: Path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _stamp(value: str) -> str | None:
    found = re.search(r'(\d{4}-\d{2}-\d{2}T\d{6}Z)', value)
    if found:
        try:
            return datetime.strptime(found[1], '%Y-%m-%dT%H%M%SZ').replace(
                tzinfo=timezone.utc).isoformat().replace('+00:00', 'Z')
        except ValueError:
            return None
    return None


def _source_date(name: str) -> str | None:
    for pattern, fmt in ((r'(\d{4}-\d{2}-\d{2})', '%Y-%m-%d'),
                         (r'(\d{2}_\d{2}_\d{4})', '%m_%d_%Y')):
        found = re.search(pattern, name)
        if found:
            try:
                return datetime.strptime(found[1], fmt).date().isoformat()
            except ValueError:
                pass
    found = re.fullmatch(r'ia(\d{6})\.zip', name, flags=re.I)
    if found:
        try:
            return datetime.strptime(found[1], '%m%d%y').date().isoformat()
        except ValueError:
            pass
    return None


def _records(path: Path) -> int | None:
    if path.suffix.lower() == '.csv':
        with path.open(encoding='utf-8-sig', newline='') as handle:
            rows = csv.reader(handle)
            next(rows, None)
            return sum(1 for row in rows if row)
    if path.suffix.lower() == '.json':
        obj = _json(path)
        if isinstance(obj, list):
            return len(obj)
        for field in ('advisors', 'owner_by_contact_id', 'records', 'rows'):
            if isinstance(obj.get(field), (list, dict)):
                return len(obj[field])
    if path.suffix.lower() == '.parquet':
        import pyarrow.parquet as pq
        return pq.read_metadata(path).num_rows
    return None


def validate_act_snapshot(root: Path) -> dict:
    """Raise on broken/newer orphan pair; never silently fall back."""
    from build_identity_ledger import select_act_pull
    contacts, owners, manifest, payload = select_act_pull(root / 'data' / 'raw')
    result = snapshot_facts(root, [contacts, owners, manifest])
    result.update({'contacts': contacts.relative_to(root).as_posix(),
                   'owners': owners.relative_to(root).as_posix(),
                   'manifest': manifest.relative_to(root).as_posix(),
                   'records': next(f['rows'] for f in payload['inputs']
                                   if f['role'] == 'act_contacts'),
                   'retrievedUtc': payload.get('generated_utc') or _stamp(contacts.name),
                   'sourceDate': None})
    return result


def _entry(root, source_id, label, kind, paths, freshness, **extra):
    paths = list(paths)
    result = {'id': source_id, 'label': label, 'kind': kind, 'sourceDate': None,
              'edition': None, 'retrievedUtc': None, 'lastSuccessUtc': None,
              'lastAttemptUtc': None, 'attemptStatus': 'unknown',
              'status': 'never_loaded' if not paths else 'unknown',
              'records': None, 'sha256': None, 'snapshotId': None,
              'freshnessDays': freshness, 'files': [], 'fileFacts': []}
    if paths:
        result.update(snapshot_facts(root, paths))
    result.update(extra)
    if result['retrievedUtc']:
        result['lastSuccessUtc'] = result['retrievedUtc']
        result['lastAttemptUtc'] = result['retrievedUtc']
        result['attemptStatus'] = 'succeeded'
        result['status'] = 'available'
    return result


RECEIPT_FIELDS = frozenset({'retrievedUtc', 'lastSuccessUtc', 'lastAttemptUtc',
    'attemptStatus', 'status', 'errorCode', 'sourceDate', 'edition'})

SNAPSHOT_RECEIPT_FIELDS = frozenset({'retrievedUtc', 'lastSuccessUtc',
                                   'sourceDate', 'edition'})


def _apply_receipts(root: Path, entries: list[dict]) -> list[dict]:
    """Acquisition metadata never replaces observed file evidence or row counts."""
    path = root / 'data/refresh/source_receipts.json'
    receipts = _json(path) if path.is_file() else {}
    if not isinstance(receipts, dict):
        raise ValueError('Source receipts must be an object keyed by source id')
    for entry in entries:
        receipt = receipts.get(entry['id'], {})
        if not isinstance(receipt, dict):
            raise ValueError('Source receipt must be an object')
        observed_failed = entry.get('status') == 'failed'
        bound = bool(entry.get('snapshotId') and
                     receipt.get('snapshotId') == entry['snapshotId'])
        for key in RECEIPT_FIELDS:
            value = receipt.get(key)
            if value is None:
                continue  # missing metadata cannot erase a known successful date
            if not isinstance(value, str):
                raise ValueError(f'Source receipt field {key} must be a string')
            if key.endswith('Utc'):
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    raise ValueError(f'Source receipt field {key} needs an explicit timezone')
            elif key == 'sourceDate':
                datetime.strptime(value, '%Y-%m-%d')
            if observed_failed and key in {'status', 'attemptStatus', 'errorCode'}:
                continue
            if key in SNAPSHOT_RECEIPT_FIELDS:
                # Native manifest/publication metadata is stronger evidence.
                if not bound or entry.get(key) is not None:
                    continue
            elif key == 'status' and value != 'failed' and not bound:
                continue
            entry[key] = value
    policy_path = root / 'refresh_policy.json'
    if policy_path.is_file():
        policy = _json(policy_path)
        if policy.get('schemaVersion') != 1 or not isinstance(policy.get('freshnessDays', {}), dict):
            raise ValueError('Invalid refresh freshness policy')
        for entry in entries:
            days = policy['freshnessDays'].get(entry['id'], policy['freshnessDays'].get('roster:*') if entry['id'].startswith('roster:') else None)
            if days is not None:
                if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 3660:
                    raise ValueError('Freshness days must be between 1 and 3660')
                entry['freshnessDays'] = days
    return entries


def candidate_inventory(root: Path, mode: str, entries=None) -> list[dict]:
    """Describe consumed inputs, not an unrelated raw file merely on disk."""
    root = Path(root)
    entries = copy.deepcopy(entries if entries is not None else inventory(root))
    if mode not in {"sec-only", "full"}:
        normalized = {
            "sec_individuals": ("SEC individuals cached normalized", [
                # Identity matching uses the unfiltered SEC interim universe;
                # map/geography consume filtered exports. Both are inputs.
                "data/interim/advisors.parquet", "data/interim/advisor_employments.parquet",
                "data/interim/advisor_branches.parquet", "data/interim/advisor_other_names.parquet",
                "data/interim/advisor_employment_history.parquet",
                "data/output/advisors.parquet", "data/output/advisor_employments.parquet",
                "data/output/advisor_branches.parquet", "data/output/advisor_other_names.parquet",
                "data/output/advisor_employment_history.parquet"]),
            "sec_firms": ("SEC firm compilation cached normalized", [
                "data/interim/firm_state_registrations.parquet", "data/interim/firm_websites.parquet",
                "data/interim/firm_website_primary.parquet", "data/interim/firm_other_names.parquet",
                "data/output/firm_state_registrations.parquet", "data/output/firm_websites.parquet",
                "data/output/firm_other_names.parquet"]),
            "sec_adv": ("SEC ADV firms cached normalized", [
                "data/output/firms.parquet", "data/interim/firms_scored.parquet"]),
        }
        for entry in entries:
            if entry["id"] not in normalized:
                continue
            label, relatives = normalized[entry["id"]]
            paths = [root / relative for relative in relatives if (root / relative).is_file()]
            entry["availableRawSnapshot"] = {"sourceDate": entry.get("sourceDate"), "sha256": entry.get("sha256")}
            entry.update(label=label, sourceDate=None, snapshotDate=None, retrievedUtc=None,
                         lastSuccessUtc=None, lastAttemptUtc=None, attemptStatus="unknown",
                         status="reused" if paths else "unavailable", records=None,
                         buildUsage="reused_normalized_with_unknown_original_retrieval")
            entry.update(snapshot_facts(root, paths) if paths else {"sha256": None, "snapshotId": None, "files": [], "fileFacts": []})
    floor = root / "data/refresh/supporting_inputs/act_mail_codes.json"
    if floor.is_file():
        for entry in entries:
            if entry["id"] == "act_preference_floor":
                entry.update(snapshot_facts(root, [floor]))
    return entries


def inventory(root: Path) -> list[dict]:
    """Inventory sources without changing them; failures stay visible per source."""
    root = Path(root)
    raw = root / 'data' / 'raw'
    entries = []
    manifests = sorted(raw.glob('act_pull_manifest_*.json'))
    contacts = sorted(raw.glob('act_contacts_*.json'))
    if manifests or contacts:
        try:
            act = validate_act_snapshot(root)
            paths = [root / path for path in act.pop('files')]
            entries.append(_entry(root, 'act', 'ACT!', 'act', paths, 7, **act))
        except (SystemExit, OSError, ValueError, KeyError) as exc:
            newest = manifests[-1:] + contacts[-1:]
            entries.append(_entry(root, 'act', 'ACT!', 'act', newest, 7,
                status='failed', attemptStatus='failed', error=str(exc),
                lastAttemptUtc=_stamp(newest[-1].name) if newest else None))
    else:
        entries.append(_entry(root, 'act', 'ACT!', 'act', [], 7))
    for sid, label, pattern, days in (
        ('sec_individuals', 'SEC individuals', 'IA_INDVL_Feed_*', 31),
        ('sec_firms', 'SEC firm compilation', 'IA_FIRM_SEC_Feed_*', 31),
        ('sec_adv', 'SEC ADV firm data', 'ia??????.zip', 92)):
        candidates = sorted(raw.glob(pattern),
                            key=lambda p: (_source_date(p.name) or '', p.name))
        if sid == 'sec_adv':
            explicit = sorted(raw.glob('refresh_adv_*'))
            if explicit:
                candidates = explicit
        selected = candidates[-1:]
        entries.append(_entry(root, sid, label, 'sec', selected, days,
            sourceDate=_source_date(selected[0].name) if selected else None))
    from build_contacts import FIRMS
    roster_dir = raw / 'firm_rosters'
    for slug, meta in sorted(FIRMS.items()):
        selected = sorted(roster_dir.glob(f'{slug}_*.csv'))[-1:]
        extra = {'slug': slug}
        if selected:
            try:
                extra.update(records=_records(selected[0]),
                             retrievedUtc=_stamp(selected[0].name),
                             snapshotDate=_source_date(selected[0].name), status='available')
            except (OSError, ValueError, csv.Error) as exc:
                extra.update(status='failed', attemptStatus='failed', error=str(exc))
        entries.append(_entry(root, f'roster:{slug}', meta.get('label', slug),
                              'roster', selected, 31, **extra))
    entries.append(_entry(root, 'roster_provenance', 'Complete roster evidence set',
        'provenance', [p for p in roster_dir.glob('*') if p.is_file()], 31))
    for name in ('barrons', 'forbes'):
        candidates = sorted(raw.glob(f'{name}_rankings*.json'))
        history = sorted((root / 'data/refresh/ranking_editions' / name).rglob('*.json'))
        canonical = raw / f'{name}_rankings.json'
        selected = ([canonical] if canonical.is_file() else candidates[-1:])
        evidence = selected + history
        extra = {'history': [p.relative_to(root).as_posix() for p in (history or candidates)],
                 'immutableHistory': bool(history)}
        if selected:
            try:
                payload = _json(selected[0])
                rows = payload if isinstance(payload, list) else payload.get('rows', [])
                editions = sorted({str(row.get('year') or row.get('edition'))
                    for row in rows if row.get('year') or row.get('edition')})
                extra.update(records=len(rows), edition=', '.join(editions) or None,
                             retrievedUtc=(payload.get('retrievedUtc')
                                 if isinstance(payload, dict) else None), status='available')
            except (ValueError, OSError, TypeError, AttributeError) as exc:
                extra.update(status='failed', attemptStatus='failed', error=str(exc))
        elif history:
            # Canonical union may not have been materialized yet; bind editions
            # but do not invent a count from possibly overlapping categories.
            extra['status'] = 'available'
        entries.append(_entry(root, name, "Barron's" if name == 'barrons' else 'Forbes',
                              'rankings', evidence, 366, **extra))
    for sid, label, relative in (
        ('firm_names_cache', 'Normalized SEC firm names (derived)', 'data/output/firms.parquet'),
        ('internal_directory', 'EIC internal directory', 'data/raw/EIC_Contacts.xlsx'),
        ('act_preference_floor', 'Historical ACT opt-out floor', 'api/shared/act_mail_codes.json'),
        ('identity_decisions', 'Reviewed ACT identity decisions', 'data/identity/act_identity_decisions.json'),
        ('contact_geocode_cache', 'Contact working-location geocode cache', 'data/interim/contact_work_geocodes.csv'),
        ('contact_google_cache', 'Contact Google geocode cache', 'data/interim/contact_work_google_cache.jsonl'),
        ('firm_aliases_cache', 'Normalized SEC firm aliases (derived)', 'data/output/firm_other_names.parquet'),
        ('firm_owners_cache', 'Firm ownership evidence (derived)', 'data/interim/firm_owners.parquet')):
        path = root / relative
        entries.append(_entry(root, sid, label, 'supporting', [path] if path.is_file() else [], 0))
    return _apply_receipts(root, entries)
