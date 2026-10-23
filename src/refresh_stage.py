"""Durable snapshot capture and repairable staging, never production mutation."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import secrets
import shutil
import subprocess
import tempfile


def _workspace(core, manifest):
    stage = Path(manifest['workspaceRoot']).resolve()
    if stage == Path(manifest['baselineRoot']).resolve():
        raise core.RefreshError('Staging workspace cannot be the baseline')
    marker = stage / '.refresh-workspace.json'
    expected = {'runId': manifest['id'], 'ownerToken': manifest['ownerToken']}
    if marker.exists():
        if core.read_json(marker) != expected:
            raise core.RefreshError('Workspace ownership mismatch')
    else:
        if stage.exists() and any(stage.iterdir()):
            raise core.RefreshError('Unmarked nonempty staging workspace; manual review required')
        stage.mkdir(parents=True, exist_ok=True)
        core.atomic_json(marker, expected)
    return stage


def _capture(core, root, manifest):
    from refresh_inventory import inventory, validate_act_snapshot
    sources = inventory(root)
    validate_act_snapshot(root)
    code = core.facts(core.code_files(root), root)
    inputs = core.facts(core.source_paths(root, manifest['mode']), root)
    baseline = core.facts(core.baseline_paths(root), root)
    rankings = core.facts([root / 'data/raw' / (name + '_rankings.json')
                          for name in ('barrons', 'forbes')], root)
    plan = {}
    for role in (code, inputs, baseline, rankings):
        for relative, fact in role.items():
            if relative in plan and plan[relative] != fact:
                raise core.RefreshError('Input changed while snapshot was being captured; resume capture')
            plan[relative] = fact
    try:
        commit = core.subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=str(root), text=True).strip()
        dirty = bool(core.subprocess.check_output(['git', 'status', '--porcelain'], cwd=str(root), text=True).strip())
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = '0' * 40, True
    manifest.update(codeCommit=commit, codeDirty=dirty, codeFiles=code,
        codeHash=core.digest(code), codeNormalization={p: core.normalized_code_hash(root / p, p) for p in code},
        inputs=inputs, capturedInputs=inputs, baselineArtifacts=baseline,
        legacyRankingFacts=rankings, sources=sources, copyPlan=plan,
        copyPlanHash=core.digest(plan), snapshotComplete=True, phase='copy')


def _verify_originals(core, root, manifest):
    if core.digest(manifest['copyPlan']) != manifest.get('copyPlanHash'):
        raise core.RefreshError('Staging copy plan integrity failed; create a new run')
    roles = (
        ('controller code', core.code_files(root), manifest['codeFiles']),
        ('source inputs', core.source_paths(root, manifest['mode']), manifest['capturedInputs']),
        ('baseline artifacts', core.baseline_paths(root), manifest['baselineArtifacts']),
        ('legacy ranking inputs', [root / 'data/raw' / (p + '_rankings.json')
                                  for p in ('barrons', 'forbes')], manifest['legacyRankingFacts']))
    merged = {}
    for label, paths, expected in roles:
        observed = {path.relative_to(root).as_posix() for path in paths if path.is_file()}
        if observed != set(expected):
            raise core.RefreshError('Recorded ' + label + ' changed; create a new run')
        for relative, fact in expected.items():
            if relative in merged and merged[relative] != fact:
                raise core.RefreshError('Staging copy plan roles are inconsistent; create a new run')
            merged[relative] = fact
    if merged != manifest['copyPlan']:
        raise core.RefreshError('Staging copy plan does not match its registered input roles')
    # Discover every role's complete set, then hash each physical input once.
    # Role attribution of an error is preserved without rereading the bytes.
    current = core.facts([root / p for p in merged], root)
    for label, _, expected in roles:
        if {relative: current.get(relative) for relative in expected} != expected:
            raise core.RefreshError('Recorded ' + label + ' changed; create a new run')
    if current != merged:
        raise core.RefreshError('Pinned originals changed; create a new run')


def _copy_file(core, source, destination, fact):
    """Partial files are never trusted; replacement happens only after hashing."""
    if destination.is_symlink() or source.is_symlink():
        raise core.RefreshError('Staging symlink refused')
    temporary = destination.with_name(destination.name + '.refresh-copy.tmp')
    if temporary.is_symlink():
        raise core.RefreshError('Staging temporary symlink refused')
    if (destination.is_file() and destination.stat().st_size == fact['bytes']
            and core.file_hash(destination) == fact['sha256']):
        if temporary.is_file():
            temporary.unlink()  # only the validated target's exact interrupted-copy temporary
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(source), str(temporary))
    if temporary.stat().st_size != fact['bytes'] or core.file_hash(temporary) != fact['sha256']:
        raise core.RefreshError('Source changed during staging copy; create a new run')
    os.replace(str(temporary), str(destination))


def _generated_targets(manifest):
    """Exact deterministic derivations, never a wildcard source allowance."""
    targets = set()
    for relative, fact in manifest['legacyRankingFacts'].items():
        provider = Path(relative).name.split('_', 1)[0]
        targets.add('data/refresh/ranking_editions/' + provider + '/' + fact['sha256'] + '.json')
    if 'api/shared/act_mail_codes.json' in manifest['copyPlan']:
        targets.add('data/refresh/supporting_inputs/act_mail_codes.json')
    return targets


def _verify_stage_namespace(core, stage, manifest, allow_temporary=True):
    targets = set(manifest['copyPlan']) | _generated_targets(manifest)
    allowed = targets | {'.refresh-workspace.json'}
    if allow_temporary:
        allowed |= {target + '.refresh-copy.tmp' for target in targets}
    for path in stage.rglob('*'):
        if path.is_symlink():
            raise core.RefreshError('Unregistered staging symlink; create a new run')
        if path.is_file() and path.relative_to(stage).as_posix() not in allowed:
            raise core.RefreshError('Unregistered staged file outside the pinned copy plan; create a new run')


def _finish(core, root, stage, manifest):
    from refresh_inventory import validate_act_snapshot
    _verify_originals(core, root, manifest)
    _verify_stage_namespace(core, stage, manifest)
    plan = manifest['copyPlan']
    def one(relative):
        _copy_file(core, root / relative, core.contained(stage, relative), plan[relative])
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(one, sorted(plan)))
    if core.facts([stage / p for p in plan], stage) != plan:
        raise core.RefreshError('Staged snapshot does not match its registered copy plan')
    _verify_originals(core, root, manifest)
    _verify_stage_namespace(core, stage, manifest)
    for relative, fact in manifest['legacyRankingFacts'].items():
        provider = Path(relative).name.split('_', 1)[0]
        target = core.contained(stage, 'data/refresh/ranking_editions/' + provider + '/' + fact['sha256'] + '.json')
        _copy_file(core, stage / relative, target, fact)
    floor = stage / 'api/shared/act_mail_codes.json'
    if floor.is_file():
        fact = {'bytes': floor.stat().st_size, 'sha256': core.file_hash(floor)}
        _copy_file(core, floor, core.contained(stage, 'data/refresh/supporting_inputs/act_mail_codes.json'), fact)
    validate_act_snapshot(stage)
    _verify_stage_namespace(core, stage, manifest, allow_temporary=False)
    manifest['inputs'] = core.facts(core.source_paths(stage, manifest['mode']), stage)
    if manifest['mode'] == 'rankings-only':
        regenerated = set(core.paths_matching(stage, [pattern
            for item in core.build_steps('rankings-only', rankings=('barrons', 'forbes'))
            for pattern in item['outputs']]))
        regenerated.update((stage / 'webapp/data/practices').rglob('*'))
        regenerated.add(stage / 'webapp/data/.gzip_manifest.json')
        manifest['reusedArtifacts'] = core.facts([stage / p for p in manifest['baselineArtifacts']
                                                 if stage / p not in regenerated], stage)
    manifest.update(status='ready', phase='ready',
                    outcome='success_with_warnings' if manifest.get('warnings') else 'success')
    manifest.pop('errorCode', None)
    manifest.pop('recovery', None)


def _run_staging(core, directory, manifest, resuming=False):
    root = Path(manifest['baselineRoot']).resolve()
    if (manifest.get('steps') or manifest.get('imports') or manifest.get('plan')
            or manifest.get('phase') not in {'capture', 'copy'}
            or manifest.get('status') not in {'staging', 'failed', 'incomplete'}):
        raise core.RefreshError('Staging cannot be resumed after it is ready or building; use build resume')
    if resuming and not manifest.get('snapshotComplete'):
        manifest.setdefault('warnings', []).append('Interrupted capture had no registered snapshot; reselecting current inputs before any outputs are used.')
    manifest.update(status='staging', outcome='incomplete')
    core.save_run(directory, manifest)
    try:
        stage = _workspace(core, manifest)
        if not manifest.get('snapshotComplete'):
            _capture(core, root, manifest)
            core.save_run(directory, manifest)  # immutable copy-plan commit boundary
        _finish(core, root, stage, manifest)
    except BaseException as exc:
        interrupted = isinstance(exc, (KeyboardInterrupt, SystemExit))
        manifest.update(status='incomplete' if interrupted else 'failed',
            outcome='incomplete' if interrupted else 'failed',
            errorCode='STAGING_INTERRUPTED' if interrupted else 'STAGING_FAILED',
            recovery='Resume this run to verify originals and repair staging; changed pinned originals require a new run.')
        core.save_run(directory, manifest)
        raise
    core.save_run(directory, manifest)
    return manifest


def start_stage(root, mode, firms=(), stage_parent=None, run_id=None):
    import refresh_core as core
    root = Path(root).resolve()
    if mode not in core.MODES:
        raise core.RefreshError('Unsupported refresh mode')
    if mode == 'selected-firm' and not firms:
        raise core.RefreshError('selected-firm requires --firm')
    run_id = run_id or (core.dt.datetime.now(core.dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(4))
    directory = core.run_path(root, run_id)
    directory.mkdir(parents=True, exist_ok=False)
    token = secrets.token_hex(24)
    parent = Path(stage_parent) if stage_parent is not None else Path(tempfile.gettempdir())
    stage = (parent / ('advisor-refresh-' + run_id + '-' + token[:12])).resolve()
    manifest = {'schemaVersion': 1, 'id': run_id, 'ownerToken': token,
        'createdUtc': core.utc(), 'workspaceRoot': str(stage), 'baselineRoot': str(root),
        'mode': mode, 'firms': list(firms), 'status': 'staging', 'phase': 'capture',
        'outcome': 'incomplete', 'snapshotComplete': False, 'steps': {}, 'imports': [], 'attempts': [], 'warnings': []}
    with core.lock(directory):
        core.save_run(directory, manifest)  # before inventory, hashing or copying
        return _run_staging(core, directory, manifest)


def resume_stage(root, run_id):
    import refresh_core as core
    root = Path(root).resolve()
    directory = core.run_path(root, run_id)
    with core.lock(directory):
        manifest = core.read_json(directory / 'run.json')
        if (manifest.get('schemaVersion') != 1 or manifest.get('id') != run_id
                or Path(manifest.get('baselineRoot', '')).resolve() != root):
            raise core.RefreshError('Invalid staging manifest identity')
        return _run_staging(core, directory, manifest, resuming=True)
