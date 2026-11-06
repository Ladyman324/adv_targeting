"""Raymond James-only checkpoint transactions and interruption recovery."""
import hashlib
import json
import os
from pathlib import Path
import re
import uuid


SCHEMA = 3
COMPLETED_NAME = 'raymond_james_completed_locations.json'


# BEGIN REVIEWED FORK VALIDATOR
# Kept inside this already-bound helper: no sixth, unbound resume dependency.
import ast
import csv
import gzip
import io
import zipfile

FORK_DIR = 'raymond_james_ancestry'
FORK_INTENT = '.raymond-reviewed-fork.json'
FORK_CODE = {'raymond_checkpoint.py', 'raymond_james_async.py', 'raymond_browser.py',
             'raymond_ui.py', 'raymond_pacing.py'}
FORK_JOURNAL = 'raymond_james_ui_diagnostics/native_responses/observations.jsonl'
FORK_PARSERS = {'clean_text', 'make_advisor_record', 'extract_advisor_records', 'advisor_dedup_key',
                'merge_advisor_record', 'add_records', 'finalize_record', 'write_dataset_csv',
                'load_existing_dataset', 'process_payload_page', 'validate_result_page', 'validate_city_totals'}


def _fork_require(ok, message='Invalid reviewed fork ancestry'):
    if not ok:
        raise ValueError(message)


def _fork_sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _fork_path(root, name):
    _fork_require(isinstance(name, str) and ':' not in name and not name.startswith(('/', '\\')),
                  'Unsafe fork evidence path')
    parts = name.replace('\\', '/').split('/')
    _fork_require(all(part not in {'', '.', '..'} for part in parts), 'Unsafe fork evidence path')
    path = root.joinpath(*parts)
    _fork_require(not path.is_symlink(), 'Fork evidence symlinks are not supported')
    path.resolve().relative_to(root.resolve())
    return path


def _fork_archive(raw, expected):
    _fork_require(isinstance(expected, dict) and set(expected) == FORK_CODE, 'Invalid fork code receipt')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        _fork_require(len(archive.namelist()) == len(set(archive.namelist())), 'Duplicate fork code member')
        result = {name: archive.read(name) for name in sorted(FORK_CODE)}
    _fork_require(all(_fork_sha(raw) == expected[name] for name, raw in result.items()),
                  'Fork code archive hash mismatch')
    return result


def _fork_ast(raw):
    return ast.parse(raw.decode('utf-8-sig'))


def _fork_checkpoint_base(tree):
    # Only the explicitly marked admission extension may differ from the old
    # checkpoint. All pre-existing transactions/recovery logic stays identical.
    body = []
    for node in tree.body:
        name = getattr(node, 'name', '')
        if name.startswith('_fork_') or name == 'validate_reviewed_fork':
            continue
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id.startswith('FORK_')
                                               for target in node.targets):
            continue
        if isinstance(node, ast.Import) and all(alias.name in {'ast', 'csv', 'gzip', 'io', 'zipfile'} for alias in node.names):
            continue
        if name == 'advisor_csv_path':
            admitted = {ast.dump(ast.parse(call).body[0], include_attributes=False) for call in
                        ('_fork_guard_unpublished(root)', 'validate_reviewed_fork(root, scope)')}
            class AdmissionCalls(ast.NodeTransformer):
                def visit_Expr(self, statement):
                    if ast.dump(statement, include_attributes=False) in admitted:
                        return None
                    return self.generic_visit(statement)
            node = AdmissionCalls().visit(node)
        body.append(node)
    tree.body = body
    return ast.dump(tree, include_attributes=False)


def _fork_code_contract(old, new):
    for name in ('raymond_browser.py', 'raymond_ui.py', 'raymond_pacing.py'):
        _fork_require(old[name] == new[name], 'Reviewed fork cannot change browser/UI/pacing helpers')
    _fork_require(_fork_checkpoint_base(_fork_ast(old['raymond_checkpoint.py'])) ==
                  _fork_checkpoint_base(_fork_ast(new['raymond_checkpoint.py'])),
                  'Reviewed fork checkpoint change exceeds admission validation')
    allowed = {'RaymondJamesHTTPError', 'read_json_response', '_run_collection_locked'}
    trees = [_fork_ast(version['raymond_james_async.py']) for version in (old, new)]
    for tree in trees:
        names = {getattr(node, 'name', '') for node in tree.body}
        _fork_require(FORK_PARSERS.issubset(names), 'Reviewed fork missing parser contract')
        tree.body = [node for node in tree.body if getattr(node, 'name', '') not in allowed]
    _fork_require(ast.dump(trees[0], include_attributes=False) == ast.dump(trees[1], include_attributes=False),
                  'Reviewed fork changes configuration/data-parser or unrelated collector code')


def _fork_guard_unpublished(root):
    if (root / FORK_INTENT).exists():
        _fork_require((root / FORK_DIR / 'published.json').is_file(), 'Reviewed fork is unpublished; do not resume')


def validate_reviewed_fork(root, scope, *, repo_root=None, allow_unpublished=False):
    """Validate a single fork before checkpoint admission; never mutate evidence.

    Called under the collector/report/fork tool's OS lock. Hashes are integrity
    evidence, not signatures; the explicit reviewed collector pair is required.
    """
    root = Path(root).resolve()
    ref = scope.get('reviewedFork')
    present = (root / FORK_INTENT).exists() or (root / FORK_DIR).exists()
    if ref is None:
        _fork_require(not present, 'Fork evidence exists without a bound ancestry reference')
        return None
    _fork_require(isinstance(ref, dict) and ref.get('schema') == 1)
    base = root / FORK_DIR
    receipt_raw = _fork_path(base, 'receipt.json').read_bytes()
    _fork_require(_fork_sha(receipt_raw) == ref.get('receiptSha256'), 'Fork receipt hash mismatch')
    receipt = json.loads(receipt_raw)
    _fork_require(receipt.get('schema') == 'raymond-reviewed-fork-v1')
    destination = Path(receipt.get('destination', '')).resolve()
    staging = allow_unpublished and root.parent == destination.parent and root.name.startswith('.raymond-fork-unpublished-')
    _fork_require(destination == root or staging, 'Fork is bound to a different destination')
    _fork_require(isinstance(receipt.get('reason'), str) and receipt['reason'].strip())
    if not allow_unpublished:
        publication = json.loads(_fork_path(base, 'published.json').read_bytes())
        _fork_require(publication == {'schema': 1, 'receiptSha256': ref['receiptSha256']}, 'Invalid fork publication')
    inventory = receipt.get('inventory')
    _fork_require(isinstance(inventory, dict) and bool(inventory), 'Invalid fork inventory')
    required = {
        FORK_INTENT, FORK_DIR + '/source_scope.json', FORK_DIR + '/old_code.zip',
        FORK_DIR + '/new_code.zip', FORK_DIR + '/source_audit.json',
        FORK_DIR + '/baseline_pins.json', FORK_DIR + '/source_journal.jsonl', FORK_DIR + '/source_failures.json'}
    _fork_require(required.issubset(inventory), 'Fork inventory omits ancestry evidence')
    for name, fact in inventory.items():
        raw = _fork_path(root, name).read_bytes()
        _fork_require(isinstance(fact, dict) and _fork_sha(raw) == fact.get('sha256')
                      and len(raw) == fact.get('bytes'), 'Fork immutable inventory changed')
    old_scope_raw = (base / 'source_scope.json').read_bytes()
    _fork_require(_fork_sha(old_scope_raw) == receipt.get('sourceScopeSha256') and
                  _fork_sha((base / 'source_audit.json').read_bytes()) == receipt.get('sourceAuditSha256') and
                  _fork_sha((base / 'baseline_pins.json').read_bytes()) == receipt.get('baselinePinsSha256') and
                  _fork_sha((base / 'source_failures.json').read_bytes()) == receipt.get('initialFailuresSha256'),
                  'Fork ancestry receipt fields differ from inventory')
    old_scope = json.loads(old_scope_raw)
    _fork_require(old_scope.get('schema') == scope.get('schema') == SCHEMA and old_scope.get('pendingWrite') is None
                  and old_scope.get('reviewedFork') is None and old_scope.get('radius') == scope.get('radius')
                  and old_scope.get('csvName') == scope.get('csvName'), 'Unsupported fork ancestry scope')
    old_code, new_code = receipt.get('oldCodeSha256'), receipt.get('newCodeSha256')
    _fork_require(old_code == old_scope.get('codeSha256') and new_code == scope.get('codeSha256'))
    old_archive, new_archive = (base / 'old_code.zip').read_bytes(), (base / 'new_code.zip').read_bytes()
    _fork_require(_fork_sha(old_archive) == receipt.get('oldArchiveSha256') and
                  _fork_sha(new_archive) == receipt.get('newArchiveSha256'), 'Fork archive receipt mismatch')
    old_files, new_files = _fork_archive(old_archive, old_code), _fork_archive(new_archive, new_code)
    _fork_require(receipt.get('reviewedOldCollectorSha256') == old_code['raymond_james_async.py'] and
                  receipt.get('reviewedNewCollectorSha256') == new_code['raymond_james_async.py'] and
                  old_code['raymond_james_async.py'] != new_code['raymond_james_async.py'], 'Unreviewed collector code pair')
    _fork_code_contract(old_files, new_files)
    for field in ('csvSha256', 'completedSha256'):
        digest = old_scope.get(field)
        _fork_require(receipt.get(field) == digest, 'Fork initial checkpoint hash mismatch')
        snapshot_name = '.raymond_committed/' + str(digest) + '.bin'
        _fork_require(snapshot_name in inventory and _fork_sha(_fork_path(root, snapshot_name).read_bytes()) == digest,
                      'Fork initial snapshot missing or changed')
    original = (base / 'source_journal.jsonl').read_bytes()
    prefix = receipt.get('journalPrefix')
    _fork_require(isinstance(prefix, dict) and prefix.get('bytes') == len(original)
                  and prefix.get('sha256') == _fork_sha(original)
                  and prefix.get('observations') == len(original.splitlines())
                  and (not original or original.endswith(b'\n')), 'Invalid fork journal prefix receipt')
    journal = _fork_path(root, FORK_JOURNAL).read_bytes()
    _fork_require(journal.startswith(original), 'Fork inherited journal prefix changed')
    for line in journal.splitlines():
        item = json.loads(line)
        digest = item.get('bodySha256')
        _fork_require(isinstance(digest, str) and re.fullmatch('[a-f0-9]{64}', digest)
                      and item.get('file') == digest + '.body.gz', 'Unsafe fork native evidence')
        compressed = _fork_path(root, FORK_JOURNAL.rsplit('/', 1)[0] + '/' + item['file']).read_bytes()
        body = gzip.decompress(compressed)
        _fork_require(_fork_sha(body) == digest and len(body) == item.get('bodyBytes')
                      and item.get('origin') == 'https://www.raymondjames.com'
                      and item.get('format') == 'native_http_body', 'Fork native body failed validation')
    audit = json.loads((base / 'source_audit.json').read_bytes())
    _fork_require(audit.get('schema') == 'rj-advisor-coverage-review-v1'
                  and audit.get('candidate', {}).get('sha256') == old_scope['csvSha256']
                  and audit['candidate'].get('scopeSha256') == _fork_sha(old_scope_raw)
                  and audit.get('code', {}).get('archiveSha256') == receipt['oldArchiveSha256']
                  and audit.get('nativeEvidence', {}).get('journalSha256') == prefix['sha256']
                  and audit.get('checkpoint', {}).get('failuresSha256') == receipt['initialFailuresSha256']
                  and audit.get('checks', {}).get('provenanceBindingsVerified') is True
                  and audit['checks'].get('allCheckpointCompletedCitiesReconciled') is True,
                  'Fork source audit does not bind the inherited checkpoint')
    repo = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parent.parent
    pins = json.loads((base / 'baseline_pins.json').read_bytes()).get('canonicalBaselinesReadOnly')
    _fork_require(isinstance(pins, list) and bool(pins), 'Invalid fork baseline pins')
    facts = []
    for pin in pins:
        path = _fork_path(repo, pin['path'])
        raw = path.read_bytes()
        with path.open(encoding='utf-8-sig', newline='') as handle:
            rows = list(csv.DictReader(handle, strict=True))
        _fork_require(_fork_sha(raw) == pin.get('sha256') and len(raw) == pin.get('bytes')
                      and len(rows) == pin.get('rows'), 'Fork baseline changed')
        facts.append({'file': path.name, 'sha256': _fork_sha(raw), 'rows': len(rows)})
    _fork_require(facts == audit.get('canonicalBaselines'), 'Fork baseline audit facts differ')
    return {'receiptSha256': ref['receiptSha256'], 'journalPrefix': prefix,
            'oldCodeSha256': old_code, 'newCodeSha256': new_code,
            'oldArchiveSha256': receipt['oldArchiveSha256'], 'newArchiveSha256': receipt['newArchiveSha256']}
# END REVIEWED FORK VALIDATOR


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _save(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _fields(scope):
    name = scope.get('csvName', '')
    if not isinstance(name, str) or not name or Path(name).name != name or not name.endswith('.csv'):
        raise ValueError('Unsafe Raymond checkpoint CSV filename')
    return {name: 'csvSha256', COMPLETED_NAME: 'completedSha256'}


def _snapshot_path(root, digest):
    if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise ValueError('Invalid Raymond committed snapshot hash')
    return root / '.raymond_committed' / (digest + '.bin')


def _put_immutable(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError('Raymond immutable evidence changed: ' + path.name)
        return
    # An interrupted creation is not a committed snapshot. Finish the durable
    # temporary before exposing the content-addressed destination.
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('wb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _verify_snapshots(root, scope):
    for name, field in _fields(scope).items():
        digest = scope.get(field)
        if digest is not None:
            snapshot = _snapshot_path(root, digest)
            if not snapshot.is_file() or _digest(snapshot) != digest:
                raise ValueError('Raymond committed snapshot is missing or corrupt: ' + name)


def _verify_working(root, scope, ignore=()):
    for name, field in _fields(scope).items():
        if name in ignore:
            continue
        path = root / name
        digest = scope.get(field)
        if path.exists() != bool(digest) or (path.exists() and _digest(path) != digest):
            raise ValueError('Raymond checkpoint data changed without a recoverable write intent: ' + name)


def _validate_completed(root, scope):
    completed = root / COMPLETED_NAME
    if completed.exists():
        value = json.loads(completed.read_text(encoding='utf-8'))
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError('Invalid Raymond completed-city checkpoint')
        if value and not (root / scope['csvName']).is_file():
            raise ValueError('Completed Raymond cities have no CSV; refusing an incomplete resume')


def _recover_pending(root, manifest, scope):
    pending = scope.get('pendingWrite')
    if pending is None:
        return scope
    fields = _fields(scope)
    if (not isinstance(pending, dict) or pending.get('name') not in fields
            or not isinstance(pending.get('transactionId'), str)
            or not re.fullmatch(r'[a-f0-9]{32}', pending['transactionId'])
            or pending.get('priorSha256') != scope.get(fields[pending['name']])):
        raise ValueError('Invalid Raymond pending-write intent; preserve for review')
    name = pending['name']
    # Check all committed evidence and unrelated files before changing anything.
    _verify_snapshots(root, scope)
    _verify_working(root, scope, ignore={name})
    recovery = root / 'raymond_james_recovery' / pending['transactionId']
    receipt_path = recovery / 'receipt.json'
    previous = None
    if receipt_path.exists():
        previous = json.loads(receipt_path.read_text(encoding='utf-8'))
        if (previous.get('transactionId') != pending['transactionId']
                or previous.get('fileName') != name
                or previous.get('restoredSha256') != scope.get(fields[name])
                or not isinstance(previous.get('archived'), list)):
            raise ValueError('Raymond recovery receipt changed; preserve for review')
        for item in previous['archived']:
            if (not isinstance(item, dict) or not isinstance(item.get('file'), str)
                    or not isinstance(item.get('sha256'), str)
                    or not re.fullmatch(r'[a-f0-9]{64}', item['sha256'])):
                raise ValueError('Invalid Raymond recovery evidence receipt')
            saved = (root / item['file']).resolve()
            try:
                saved.relative_to(recovery.resolve())
            except ValueError:
                raise ValueError('Raymond recovery evidence escapes its transaction') from None
            if (not saved.is_file() or _digest(saved) != item['sha256']
                    or saved.stat().st_size != item.get('bytes')):
                raise ValueError('Raymond interrupted bytes are missing or changed; preserve for review')
    archived = []
    path = root / name
    expected = scope.get(fields[name])
    interrupted = path.exists() and (expected is None or _digest(path) != expected)
    candidates = ([path] if interrupted else []) + [path.with_suffix(path.suffix + '.tmp')]
    for candidate in candidates:
        if not candidate.exists():
            continue
        raw = candidate.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        saved = recovery / (candidate.name + '.' + digest + '.uncommitted')
        _put_immutable(saved, raw)
        archived.append({'file': str(saved.relative_to(root)), 'sha256': digest, 'bytes': len(raw)})
    receipt = {'transactionId': pending['transactionId'], 'fileName': name,
               'restoredSha256': expected, 'archived': archived}
    recovery.mkdir(parents=True, exist_ok=True)
    # Write the receipt before restoring, so another interrupted recovery can
    # always be repeated using the still-present intent and committed snapshots.
    if previous is not None:
        known = {item['file']: item for item in previous.get('archived', [])}
        known.update({item['file']: item for item in archived})
        receipt['archived'] = list(known.values())
    _save(receipt_path, receipt)
    if expected is None:
        if path.exists():
            # The bytes have already been durably preserved above. Moving the
            # working copy keeps absent-before-first-write recovery non-destructive.
            destination = recovery / (path.name + '.' + _digest(path) + '.uncommitted')
            path.replace(destination)
    elif not path.exists() or _digest(path) != expected:
        raw = _snapshot_path(root, expected).read_bytes()
        temporary = path.with_name(path.name + '.recovery.tmp')
        with temporary.open('wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    _verify_working(root, scope)
    _validate_completed(root, scope)
    scope = dict(scope)
    scope.pop('pendingWrite')
    scope['lastRecovery'] = str(receipt_path.relative_to(root))
    _save(manifest, scope)
    return scope


def prepare_file_write(path):
    """Record intent before replacing mutable data; the parent collector owns its lock."""
    path = Path(path)
    manifest = path.parent / 'raymond_james_scope.json'
    if not manifest.exists():
        return
    scope = json.loads(manifest.read_text(encoding='utf-8'))
    fields = _fields(scope)
    if path.name not in fields:
        return
    if scope.get('schema') != SCHEMA:
        raise ValueError('Raymond checkpoint schema changed; use a new reviewed scope')
    if scope.get('pendingWrite') is not None:
        raise ValueError('Raymond interrupted write requires checkpoint resume before another write')
    _verify_snapshots(path.parent, scope)
    _verify_working(path.parent, scope)
    scope['pendingWrite'] = {'name': path.name, 'transactionId': uuid.uuid4().hex,
                             'priorSha256': scope.get(fields[path.name])}
    _save(manifest, scope)


def seal_file(path):
    """Commit durable immutable bytes before advancing the manifest pointer."""
    path = Path(path)
    manifest = path.parent / 'raymond_james_scope.json'
    if not manifest.exists():
        return
    scope = json.loads(manifest.read_text(encoding='utf-8'))
    fields = _fields(scope)
    if path.name not in fields:
        return
    if scope.get('schema') != SCHEMA:
        raise ValueError('Raymond checkpoint schema changed; use a new reviewed scope')
    pending = scope.get('pendingWrite')
    if pending is not None and pending.get('name') != path.name:
        raise ValueError('A different Raymond file write is still pending')
    _verify_snapshots(path.parent, scope)
    _verify_working(path.parent, scope, ignore={path.name})
    if path.name == COMPLETED_NAME:
        _validate_completed(path.parent, scope)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    _put_immutable(_snapshot_path(path.parent, digest), raw)
    scope[fields[path.name]] = digest
    scope.pop('pendingWrite', None)
    _save(manifest, scope)


def advisor_csv_path(output_dir, *, proposed_name, radius, collector_path, fresh=False):
    root = Path(output_dir)
    manifest = root / 'raymond_james_scope.json'
    _fork_guard_unpublished(root)
    dependencies = [Path(__file__), Path(collector_path)]
    for helper_name in ('raymond_browser.py', 'raymond_ui.py', 'raymond_pacing.py'):
        helper = Path(__file__).with_name(helper_name)
        if helper.exists():
            dependencies.append(helper)
    expected = {'schema': SCHEMA, 'radius': radius,
                'codeSha256': {str(p.name): _digest(p) for p in dependencies}}
    if manifest.exists():
        if fresh:
            raise ValueError('Fresh collection requires a new output directory; existing Raymond scope is preserved')
        scope = json.loads(manifest.read_text(encoding='utf-8'))
        if any(scope.get(key) != value for key, value in expected.items()):
            raise ValueError('Raymond checkpoint scope/code changed; review and use a new output directory')
        _fields(scope)
        validate_reviewed_fork(root, scope)
        scope = _recover_pending(root, manifest, scope)
        _verify_snapshots(root, scope)
        _verify_working(root, scope)
        _validate_completed(root, scope)
        return root / scope['csvName']
    if any(root.glob('raymond_james_*.csv')) or (root / COMPLETED_NAME).exists():
        raise ValueError('Legacy Raymond checkpoint needs explicit review; do not silently trust completed cities')
    if Path(proposed_name).name != proposed_name or not proposed_name.endswith('.csv'):
        raise ValueError('Unsafe Raymond CSV filename')
    root.mkdir(parents=True, exist_ok=True)
    _save(manifest, dict(expected, csvName=proposed_name))
    return root / proposed_name
