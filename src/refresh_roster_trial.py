"""One isolated, evidence-bound public roster acquisition; never publish.

Unlike a full candidate build, this copies no ACT contacts, identity decisions,
mailbox settings, or private release lookups. Failed attempts retain their
workspace/checkpoints for diagnosis; use a new trial ID after changing code.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys
import tempfile
import time

import refresh_core as core
from firm_rosters import FIRMS, NAME_RE
from refresh_acquire import acquisition_commands
from refresh_history import compare_roster


def run_trial(root, trial_id, firm, *, allow_network=False, discovery=None, checkpoint=None, rate=None,cooldown_seconds=0):
    root = Path(root).resolve()
    if not allow_network:
        raise core.RefreshError('Public acquisition requires --allow-network')
    if firm not in FIRMS:
        raise core.RefreshError('Unknown firm')
    if not 0 <= cooldown_seconds <= 3600:raise core.RefreshError('Cooldown must be between 0 and 3600 seconds')
    # Reuse the existing identifier/path validation, not arbitrary path joins.
    core.run_path(root, trial_id)
    folder = root / 'data/refresh/roster_trials' / trial_id
    folder.mkdir(parents=True, exist_ok=True)
    with core.lock(folder):
        state_path = folder / 'trial.json'
        if state_path.exists():
            raise core.RefreshError('Trial already exists; inspect its process/checkpoints before starting another')
        workspace = Path(tempfile.mkdtemp(prefix='advisor-roster-' + firm + '-'))
        state = {'id': trial_id, 'firm': firm, 'status': 'staging',
                 'workspaceRoot': str(workspace), 'startedUtc': core.utc(),
                 'productionChanged': False, 'sourceOnlyTrial': True}
        core.atomic_json(state_path, state)
        try:
            code = list((root / 'src').glob('*.py'))
            code += [root / name for name in ('config.py', 'run.py') if (root / name).is_file()]
            for source in code:
                target = workspace / source.relative_to(root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            state['code'] = core.facts(code, root)
            if core.facts([workspace / path for path in state['code']], workspace) != state['code']:
                raise core.RefreshError('Code copy verification failed')
            raw = workspace / 'data/raw/firm_rosters'; raw.mkdir(parents=True)
            baselines = [p for p in (root / 'data/raw/firm_rosters').glob(firm + '_*')
                         if NAME_RE.match(p.name) and NAME_RE.match(p.name).group('slug') == firm]
            baseline = max(baselines, key=lambda p: p.name) if baselines else None
            if baseline:
                saved = folder / 'baselines' / baseline.name
                saved.parent.mkdir(exist_ok=True); shutil.copy2(baseline, saved)
                shutil.copy2(saved, raw / saved.name)
                if core.file_hash(baseline) != core.file_hash(saved):
                    raise core.RefreshError('Baseline changed during capture')
                state['baseline'] = {'file': str(saved.relative_to(folder)), 'sha256': core.file_hash(saved)}
            for relative in ('data/output/advisor_branches.parquet', 'data/output/firms.parquet',
                             'webapp/data/geo_index.json'):
                source = root / relative
                if source.is_file():
                    target = workspace / relative; target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
            core.atomic_json(workspace / '.refresh-acquisition.json',
                             {'sourceId': 'roster:' + firm, 'trialId': trial_id})
            before = core.facts(raw.glob(firm + '_*.csv'), workspace)
            commands = acquisition_commands('roster:' + firm)
            if discovery:
                if firm != 'edward_jones':
                    raise core.RefreshError('Discovery seed is only supported for Edward Jones')
                seed = Path(discovery).resolve()
                records = core.read_json(seed)
                if not isinstance(records, list) or len(records) < 10000:
                    raise core.RefreshError('Discovery seed is not a complete-sized advisor array')
                target = workspace / 'data/interim/edward_jones_seed.json'
                target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(seed, target)
                state['discoverySeed'] = {'file': str(seed), 'sha256': core.file_hash(seed), 'records': len(records),
                                          'note': 'Prior discovery reused explicitly; profile retrieval occurs this trial'}
                commands = [['src/edward_jones_async.py', 'enrich', '--input', str(target), '--no-launch-chrome']]
            if checkpoint:
                if firm not in {'edward_jones','lpl','ameriprise'}:raise core.RefreshError('Checkpoint seed is only supported for Edward Jones, LPL and Ameriprise')
                source = Path(checkpoint).resolve()
                target = raw / '.edward_jones_email_checkpoint.jsonl' if firm=='edward_jones' else workspace/'data/interim/lpl_seed.jsonl'
                if firm=='ameriprise':target=workspace/'data/interim/ameriprise_seed.jsonl'
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(source,target)
                state['checkpointSeed'] = {'file':str(source),'sha256':core.file_hash(source)}
                if core.file_hash(target)!=state['checkpointSeed']['sha256']:
                    raise core.RefreshError('Checkpoint changed during capture')
                if firm=='lpl':commands=[['src/lpl_async.py','--resume-capture',str(target)]]
                if firm=='ameriprise':commands=[['src/ameriprise_browser.py','--resume-capture',str(target)]]
            if rate is not None:
                if firm != 'edward_jones' or rate <= 0:raise core.RefreshError('Positive --rate is only supported for Edward Jones')
                for command in commands:command.extend(['--rate',str(rate)])
            if cooldown_seconds:
                state.update(status='waiting_for_source_cooldown',cooldownSeconds=cooldown_seconds,commands=commands)
                core.atomic_json(state_path,state)
                deadline=time.monotonic()+cooldown_seconds
                print('Respecting source cooldown: '+str(cooldown_seconds)+' seconds before any publisher requests',flush=True)
                while time.monotonic()<deadline:time.sleep(min(30,max(0,deadline-time.monotonic())))
            state.update(status='acquiring', commands=commands)
            core.atomic_json(state_path, state)
            for number, command in enumerate(commands):
                core.execute_command(workspace, root, command, folder / ('step-' + str(number) + '.log'), online=True)
            state['retrievedUtc'] = core.utc()
            after = core.facts(raw.glob(firm + '_*.csv'), workspace)
            changed = [workspace / relative for relative, facts in after.items() if before.get(relative) != facts]
            if len(changed) != 1:
                raise core.RefreshError('Expected exactly one newly retrieved roster, found ' + str(len(changed)))
            candidate = folder / 'candidates' / changed[0].name
            candidate.parent.mkdir(exist_ok=True); shutil.copy2(changed[0], candidate)
            if core.file_hash(candidate) != core.file_hash(changed[0]):
                raise core.RefreshError('Candidate copy verification failed')
            report = compare_roster(candidate, folder / state['baseline']['file'] if baseline else None, firm)
            core.atomic_json(folder / 'comparison.json', report)
            state.update(status=report['status'], candidate=str(candidate.relative_to(folder)),
                         sha256=core.file_hash(candidate), issueCodes=[i['code'] for i in report['issues']])
        except BaseException as exc:
            state.update(status='interrupted' if isinstance(exc, (KeyboardInterrupt, SystemExit)) else 'failed',
                         errorType=type(exc).__name__)
            raise
        finally:
            state['finishedUtc'] = core.utc(); core.atomic_json(state_path, state)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trial', required=True)
    parser.add_argument('--firm', required=True, choices=sorted(FIRMS))
    parser.add_argument('--allow-network', action='store_true')
    parser.add_argument('--discovery', type=Path)
    parser.add_argument('--checkpoint', type=Path)
    parser.add_argument('--rate',type=float)
    parser.add_argument('--cooldown-seconds',type=int,default=0)
    args = parser.parse_args()
    result = run_trial(Path(__file__).resolve().parents[1], args.trial, args.firm,
                       allow_network=args.allow_network, discovery=args.discovery, checkpoint=args.checkpoint,rate=args.rate,
                       cooldown_seconds=args.cooldown_seconds)
    print(result['firm'], result['status'], flush=True)
    return 0 if result['status'] == 'accepted' else 2


if __name__ == '__main__':
    sys.exit(main())
