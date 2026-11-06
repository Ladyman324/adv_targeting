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


def _source_root(core, root, manifest):
    """An earlier run supplies pinned inputs only, never build success."""
    snapshot = manifest.get('sourceSnapshot')
    if not snapshot:
        return root
    directory, prior, stage = core.load_run(Path(snapshot.get('baselineRoot', root)), snapshot['runId'])
    if (core.file_hash(directory / 'run.json') != snapshot['manifestSha256']
            or str(stage) != snapshot['workspaceRoot']
            or core.digest(prior['inputs']) != snapshot['inputsHash']):
        raise core.RefreshError('Inherited source snapshot changed; create a new run')
    if core.active_children(directory):
        raise core.RefreshError('Inherited source run still has active children')
    return stage


def _capture(core, root, manifest):
    from refresh_inventory import inventory, validate_act_snapshot
    source_root = _source_root(core, root, manifest)
    sources = inventory(source_root)
    validate_act_snapshot(source_root)
    roles = [core.code_files(root), core.source_paths(source_root, manifest['mode']),
             core.baseline_paths(root), [source_root / 'data/raw' / (name + '_rankings.json')
                                         for name in ('barrons', 'forbes')]]
    # Role overlap (notably normalized SEC tables) must not cause duplicate
    # multi-hundred-MB reads. Final original/stage verification remains intact.
    role_roots = [root, source_root, root, source_root]
    if source_root == root:
        captured = core.facts([path for paths in roles for path in paths], root)
        role_facts = [captured] * 4
    else:
        control = core.facts(roles[0] + roles[2], root)
        source = core.facts(roles[1] + roles[3], source_root)
        role_facts = [control, source, control, source]
    code, inputs, baseline, rankings = [
        {p.relative_to(base).as_posix(): captured[p.relative_to(base).as_posix()]
         for p in paths if p.relative_to(base).as_posix() in captured}
        for paths, base, captured in zip(roles, role_roots, role_facts)]
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
    source_root = _source_root(core, root, manifest)
    roles = (
        ('controller code', core.code_files(root), manifest['codeFiles']),
        ('source inputs', core.source_paths(source_root, manifest['mode']), manifest['capturedInputs']),
        ('baseline artifacts', core.baseline_paths(root), manifest['baselineArtifacts']),
        ('legacy ranking inputs', [source_root / 'data/raw' / (p + '_rankings.json')
                                  for p in ('barrons', 'forbes')
                                  if (source_root / 'data/raw' / (p + '_rankings.json')).is_file()], manifest['legacyRankingFacts']))
    role_roots = [root, source_root, root, source_root]
    merged = {}
    for (label, paths, expected), base in zip(roles, role_roots):
        observed = {path.relative_to(base).as_posix() for path in paths}
        if observed != set(expected):
            raise core.RefreshError('Recorded ' + label + ' changed; create a new run')
        for relative, fact in expected.items():
            if relative in merged and merged[relative] != fact:
                raise core.RefreshError('Staging copy plan roles are inconsistent; create a new run')
            merged[relative] = fact
    if merged != manifest['copyPlan']:
        raise core.RefreshError('Staging copy plan does not match its registered input roles')
    if source_root == root:
        current = core.facts([path for _, paths, _ in roles for path in paths], root)
        current_roles = [current] * 4
    else:
        control = core.facts(roles[0][1] + roles[2][1], root)
        source = core.facts(roles[1][1] + roles[3][1], source_root)
        current_roles = [control, source, control, source]
        current = {**control, **source}
    for (label, _, expected), observed in zip(roles, current_roles):
        if {relative: observed.get(relative) for relative in expected} != expected:
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
    source_root = _source_root(core, root, manifest)
    source_files = set(manifest['capturedInputs']) | set(manifest['legacyRankingFacts'])
    def one(relative):
        origin = source_root if relative in source_files else root
        _copy_file(core, origin / relative, core.contained(stage, relative), plan[relative])
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


def start_stage(root, mode, firms=(), stage_parent=None, run_id=None, source_run=None, source_run_root=None):
    import refresh_core as core
    root = Path(root).resolve()
    if mode not in core.MODES:
        raise core.RefreshError('Unsupported refresh mode')
    if mode == 'selected-firm' and not firms:
        raise core.RefreshError('selected-firm requires --firm')
    run_id = run_id or (core.dt.datetime.now(core.dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(4))
    snapshot = None
    source_warnings = []
    if source_run_root is not None and not source_run:
        raise core.RefreshError("Source run root requires a source run")
    if source_run:
        source_owner = Path(source_run_root).resolve() if source_run_root is not None else root
        previous, prior, source_stage = core.load_run(source_owner, source_run)
        if (prior.get('phase') != 'ready' or prior.get('status') in {'building', 'validating', 'importing', 'acquiring'}
                or prior['mode'] != mode or set(prior.get('firms', [])) != set(firms)
                or Path(prior['baselineRoot']).resolve() != source_owner or core.active_children(previous)):
            raise core.RefreshError('Source run must be idle, staged and have matching scope')
        if core.facts(core.source_paths(source_stage, mode), source_stage) != prior['inputs']:
            raise core.RefreshError('Inherited source inputs changed')
        snapshot = {'runId': source_run, 'baselineRoot': str(source_owner), 'manifestSha256': core.file_hash(previous / 'run.json'),
                    'workspaceRoot': str(source_stage), 'inputsHash': core.digest(prior['inputs'])}
        source_warnings = [f'Inherited source snapshots from {source_run}; no build or validation results reused.']
        latest = {a['sourceId']: a for a in prior.get('attempts', [])}
        for sid, attempt in sorted(latest.items()):
            if attempt.get('status') != 'success':
                source_warnings.append(f'{sid}: last-good source inherited; prior candidate remains held. See {source_run}.')
    directory = core.run_path(root, run_id)
    directory.mkdir(parents=True, exist_ok=False)
    token = secrets.token_hex(24)
    parent = Path(stage_parent) if stage_parent is not None else Path(tempfile.gettempdir())
    stage = (parent / ('advisor-refresh-' + run_id + '-' + token[:12])).resolve()
    manifest = {'schemaVersion': 1, 'id': run_id, 'ownerToken': token,
        'createdUtc': core.utc(), 'workspaceRoot': str(stage), 'baselineRoot': str(root),
        'mode': mode, 'firms': list(firms), 'status': 'staging', 'phase': 'capture',
        'outcome': 'incomplete', 'snapshotComplete': False, 'steps': {}, 'imports': [], 'attempts': [], 'warnings': []}
    if snapshot:
        manifest['sourceSnapshot'] = snapshot
        manifest['warnings'].extend(source_warnings)
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
