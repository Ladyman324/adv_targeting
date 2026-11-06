"""Saturday refresh dispatcher. Prepares tested candidates; never publishes."""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
ACT_KEYS = ('ACT_USER', 'ACT_PASSWORD', 'ACT_DB')


def last_slot(now):
    """Most recent Saturday 03:00, including a delayed/missed task start."""
    day = now.date() - dt.timedelta(days=(now.weekday() - 5) % 7)
    slot = dt.datetime.combine(day, dt.time(3))
    return slot - dt.timedelta(days=7) if slot > now else slot


def quarter_slot(day):
    first = dt.date(day.year, 1 + 3 * ((day.month - 1) // 3), 1)
    return first + dt.timedelta(days=(5 - first.weekday()) % 7)


def make_plan(now, active_from, attempted_quarters=()):
    slot = last_slot(now)
    quarter = quarter_slot(slot.date())
    quarterly = (active_from <= quarter <= slot.date()
                 and (quarter.isoformat() not in attempted_quarters or slot.date() == quarter))
    day = quarter if quarterly else slot.date()
    mode = 'full' if quarterly else 'act-only'
    return dict(runId='scheduled-' + ('full-' if quarterly else 'act-') + day.strftime('%Y%m%d'),
                mode=mode, scheduledDate=day.isoformat(), quarterly=quarterly,
                sources='all' if quarterly else ['act'], publish=False, runTests=True,
                through='prepare-release')


def act_credentials(environ=None, runner=subprocess.run):
    """Existing environment or the existing application's settings, memory only."""
    environ = os.environ if environ is None else environ
    if all(environ.get(key) for key in ACT_KEYS):
        return {key: environ[key] for key in ACT_KEYS}
    az = shutil.which('az')
    if not az:
        raise RuntimeError('ACT credentials unavailable: Azure CLI is not installed')
    query = "[?name=='ACT_USER' || name=='ACT_PASSWORD' || name=='ACT_DB' || name=='ACT_BASE'].{name:name,value:value}"
    result = runner([az, 'functionapp', 'config', 'appsettings', 'list', '--name',
                     'eic-advisors-api', '--resource-group', 'rg-advisor-map',
                     '--query', query, '--output', 'json', '--only-show-errors'],
                    capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError('ACT credential lookup failed; check the scheduled user Azure sign-in')
    try:
        settings = {item['name']: item['value'] for item in json.loads(result.stdout)}
    except (ValueError, KeyError, TypeError):
        raise RuntimeError('ACT credential response is invalid') from None
    if not all(settings.get(key) for key in ACT_KEYS):
        raise RuntimeError('ACT settings are incomplete')
    if settings.get('ACT_BASE', 'https://apius.act.com/act.web.api').rstrip('/') != 'https://apius.act.com/act.web.api':
        raise RuntimeError('ACT endpoint requires review')
    return {key: settings[key] for key in ACT_KEYS}


def execute(root, stage_parent, registration, now):
    import refresh_core as core
    from refresh_workflow import run_workflow
    root, stage_parent = Path(root).resolve(), Path(stage_parent).resolve()
    registration = core.read_json(registration)
    if registration.get('timezone') != 'Eastern Standard Time':
        raise RuntimeError('Schedule must use the approved Eastern time zone')
    folder = root / 'data/refresh/scheduled'
    folder.mkdir(parents=True, exist_ok=True)
    # Shared with the supervised full-refresh launcher; never force or delete it.
    with core.lock(folder):
        history_path = folder / 'history.json'
        history = core.read_json(history_path) if history_path.exists() else {'quarterlyAttempts': [], 'runs': {}}
        plan = make_plan(now, dt.date.fromisoformat(registration['activeFrom']), history['quarterlyAttempts'])
        prior = history['runs'].get(plan['runId'], {})
        if prior.get('status') in ('completed', 'completed_with_reuse'):
            return dict(plan, status='already_completed')
        credentials = act_credentials()
        history['runs'][plan['runId']] = dict(status='starting', startedUtc=core.utc(), plan=plan)
        if plan['quarterly'] and plan['scheduledDate'] not in history['quarterlyAttempts']:
            history['quarterlyAttempts'].append(plan['scheduledDate'])
        core.atomic_json(history_path, history)
        previous = {key: os.environ.get(key) for key in ACT_KEYS}
        try:
            os.environ.update(credentials)
            result = run_workflow(root, plan['runId'], mode=plan['mode'],
                sources=() if plan['quarterly'] else ('act',), stage_parent=stage_parent,
                allow_network=True, through='prepare-release', act_user=credentials['ACT_USER'],
                act_db=credentials['ACT_DB'])
            history['runs'][plan['runId']].update(status=result['status'], finishedUtc=core.utc())
            core.atomic_json(history_path, history)
            return dict(plan, status=result['status'], summary=str(root / 'data/refresh/runs' / plan['runId'] / 'workflow/summary.json'))
        except BaseException:
            history['runs'][plan['runId']].update(status='needs_attention', finishedUtc=core.utc())
            core.atomic_json(history_path, history)
            raise
        finally:
            credentials.clear()
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--stage-parent', type=Path)
    parser.add_argument('--registration', type=Path)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--allow-network', action='store_true')
    parser.add_argument('--on', help='Read-only plan at local ISO datetime; execution always uses the real clock')
    args = parser.parse_args(argv)
    now = dt.datetime.now()
    if not args.execute:
        when = dt.datetime.fromisoformat(args.on) if args.on else now
        active = dt.date.fromisoformat(json.loads(args.registration.read_text(encoding='utf-8-sig'))['activeFrom']) if args.registration else now.date()
        print(json.dumps(dict(make_plan(when, active), readOnly=True), sort_keys=True))
        return 0
    if not args.allow_network or not args.stage_parent or not args.registration or args.on:
        parser.error('Execution requires --allow-network, --stage-parent and --registration, with no simulated clock')
    result = execute(args.root, args.stage_parent, args.registration, now)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] in ('completed', 'completed_with_reuse', 'already_completed') else 2


if __name__ == '__main__':
    raise SystemExit(main())
