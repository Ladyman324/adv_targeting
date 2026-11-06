"""One-command supervised refresh, with explicit per-source last-good reuse.

Acquisition/build/validation remain separate authorities. This module never
reviews held candidates, edits adapters, publishes, or sends email.
"""
from __future__ import annotations

import csv
from pathlib import Path

import refresh_core as core


def source_plan(mode,firms=(),sources=()):
    from refresh_acquire import _source,_compatible
    from firm_rosters import FIRMS
    if mode not in core.MODES:raise core.RefreshError('Unknown refresh mode')
    if mode=='selected-firm' and not firms:raise core.RefreshError('Selected-firm workflow requires firms')
    chosen=list(sources) if sources else (
        ['act'] if mode=='act-only' else ['roster:'+f for f in firms] if mode=='selected-firm' else
        ['sec_individuals','sec_firms','sec_adv'] if mode=='sec-only' else
        ['barrons','forbes'] if mode=='rankings-only' else
        ['act','sec_individuals','sec_firms','sec_adv','barrons','forbes']+['roster:'+f for f in sorted(FIRMS)])
    for sid in chosen:
        _source(sid);_compatible({'mode':mode,'firms':list(firms)},sid)
    if len(chosen)!=len(set(chosen)):raise core.RefreshError('Duplicate source selection')
    return chosen


def _next_action(sid,record):
    status=record.get('status')
    if status=='refreshed':return 'No source action; candidate build and validation are separate.'
    if status=='reused_last_good':return 'Older snapshot deliberately retained; refresh this source when available.'
    if status=='needs_review':return 'Review the exact held comparison; approve/import explicitly or retain last-good.'
    if status in {'pending','running','interrupted'}:return 'Resume the same run; do not start a competing worker.'
    if sid=='sec_adv':return 'Download the official SEC ADV CSV/ZIP and import it with its source date.'
    if sid in {'barrons','forbes'}:return 'Capture rankings in the authorized browser and import with edition/category.'
    if sid=='roster:rj_branches':return 'Complete the authorized branch browser capture and import its CSV.'
    if sid=='act':return 'Check ACT user/database and ACT_PASSWORD; inspect logs, then use --retry-failed.'
    return 'Inspect acquisition logs and publisher/session health; resolve the cause, then use --retry-failed.'


def _write_summary(folder,state):
    summary={'runId':state['runId'],'status':state['status'],'publish':False,
        'counts':{status:sum(r.get('status')==status for r in state['sources'].values())
            for status in ('refreshed','reused_last_good','needs_review','failed','pending','running','interrupted')},
        'sources':{sid:{**record,'nextAction':_next_action(sid,record)} for sid,record in state['sources'].items()},
        'pipeline':state.get('pipeline',{}),
        'warnings':state.get('pipeline',{}).get('warnings',[]),
        'updatedUtc':core.utc()}
    core.atomic_json(folder/'summary.json',summary)
    from refresh_reports import _safe_cell
    with (folder/'source_review.csv').open('w',encoding='utf-8-sig',newline='') as handle:
        fields=['source','status','errorCode','issueCodes','comparisonId','sourceDate','retrievedUtc','selectedSha256','reason','nextAction']
        writer=csv.DictWriter(handle,fieldnames=fields);writer.writeheader()
        for sid,record in summary['sources'].items():
            writer.writerow({key:_safe_cell(sid if key=='source' else record.get(key,'')) for key in fields})
    return summary


def run_workflow(root,run_id,*,sources=(),mode=None,firms=(),stage_parent=None,
                 allow_network=False,reuse_last_good=(),fallback_reason=None,
                 retry_failed=False,through='validate',act_user=None,act_db=None):
    from refresh_acquire import acquire,_source_files
    root=Path(root)
    if through not in {'acquire','validate','prepare-release'}:raise core.RefreshError('Invalid workflow endpoint')
    directory=core.run_path(root,run_id)
    if not allow_network:raise core.RefreshError('Workflow acquisition requires explicit --allow-network')
    if not (directory/'run.json').exists():
        chosen=source_plan(mode or 'full',firms,sources)
        if set(reuse_last_good)-set(chosen):raise core.RefreshError('Reuse approval outside selected sources')
        if reuse_last_good and (not fallback_reason or len(fallback_reason.strip())<12):
            raise core.RefreshError('Per-source last-good reuse needs a meaningful --fallback-reason')
        core.create_run(root,mode or 'full',firms,stage_parent,run_id)
    directory,manifest,stage=core.load_run(root,run_id)
    if mode and mode!=manifest['mode']:raise core.RefreshError('Run mode cannot change on resume')
    if firms and set(firms)!=set(manifest.get('firms',[])):raise core.RefreshError('Selected firms cannot change on resume')
    if manifest.get('phase') in {'capture','copy'}:
        from refresh_stage import resume_stage
        resume_stage(root,run_id)
        directory,manifest,stage=core.load_run(root,run_id)
    existing_state=directory/'workflow/state.json'
    requested=sources or (core.read_json(existing_state).get('plan',[]) if existing_state.exists() else ())
    chosen=source_plan(manifest['mode'],manifest.get('firms',[]),requested)
    if set(reuse_last_good)-set(chosen):raise core.RefreshError('Reuse approval outside selected sources')
    if reuse_last_good and (not fallback_reason or len(fallback_reason.strip())<12):
        raise core.RefreshError('Per-source last-good reuse needs a meaningful --fallback-reason')
    folder=directory/'workflow';folder.mkdir(exist_ok=True)
    with core.lock(folder):
        if core.active_children(directory):raise core.RefreshError('A refresh child is still running; do not start another workflow')
        core.check_inputs(stage,manifest)
        path=folder/'state.json'
        if path.exists():
            state=core.read_json(path)
            if state.get('plan')!=chosen or state.get('runId')!=run_id:
                raise core.RefreshError('Workflow source plan cannot change on resume')
        else:
            state={'schemaVersion':1,'runId':run_id,'plan':chosen,'status':'acquiring',
                'sources':{sid:{'status':'pending'} for sid in chosen},'invocations':[]}
        state['invocations'].append({'startedUtc':core.utc(),'reuseLastGood':list(reuse_last_good),
            'reason':fallback_reason,'retryFailed':retry_failed,'through':through})
        state['status']='acquiring';core.atomic_json(path,state)
        for sid in chosen:
            prior=state['sources'][sid]
            if prior['status'] in {'refreshed','reused_last_good'}:
                if core.facts(_source_files(stage,sid),stage)!=prior.get('selectedFacts'):
                    raise core.RefreshError('Selected workflow source changed: '+sid)
                continue
            should_acquire=prior['status'] in {'pending','running','interrupted'} or retry_failed
            if should_acquire:
                state['sources'][sid]={'status':'running','startedUtc':core.utc()}
                core.atomic_json(path,state)
                try:
                    result=acquire(root,run_id,sid,allow_network=True,act_user=act_user,act_db=act_db)
                    item=next((s for s in result.get('sources',[]) if s['id']==sid),{})
                    imported=next((i for i in reversed(result.get('imports',[])) if i.get('sourceId')==sid),{})
                    state['sources'][sid]={'status':'refreshed' if imported.get('retrievedUtc') else 'failed',
                        'errorCode':'' if imported.get('retrievedUtc') else 'RETRIEVAL_NOT_PROVEN',
                        'sourceDate':item.get('sourceDate') or item.get('snapshotDate'),
                        'retrievedUtc':imported.get('retrievedUtc'),'selectedSha256':item.get('sha256')}
                except (core.RefreshError,ValueError,OSError) as exc:
                    _,current,_=core.load_run(root,run_id)
                    attempt=next((a for a in reversed(current.get('attempts',[])) if a['sourceId']==sid),{})
                    code=attempt.get('errorCode','ACQUISITION_FAILED')
                    state['sources'][sid]={'status':'needs_review' if code=='SOURCE_HISTORY_HELD' else 'failed',
                        'errorCode':code,'errorType':type(exc).__name__,
                        'issueCodes':attempt.get('historyIssueCodes',[]),
                        'comparisonId':attempt.get('historyComparisonId')}
                except BaseException:
                    state['sources'][sid]['status']='interrupted';state['status']='interrupted'
                    core.atomic_json(path,state);_write_summary(folder,state);raise
            if state['sources'][sid]['status'] in {'failed','needs_review','interrupted'} and sid in reuse_last_good:
                _,current,stage=core.load_run(root,run_id)
                core.check_inputs(stage,current)
                selected=_source_files(stage,sid)
                if not selected:raise core.RefreshError('No last-good snapshot exists for '+sid)
                selected_info=next((s for s in current.get('sources',[]) if s['id']==sid),{})
                state['sources'][sid].update(status='reused_last_good',reason=fallback_reason,
                    sourceDate=selected_info.get('sourceDate') or selected_info.get('snapshotDate'),
                    retrievedUtc=selected_info.get('retrievedUtc'),selectedSha256=selected_info.get('sha256'),
                    selectedFacts=core.facts(selected,stage))
            state['sources'][sid]['selectedFacts']=core.facts(_source_files(stage,sid),stage)
            core.atomic_json(path,state);_write_summary(folder,state)
        blocked=any(r['status'] not in {'refreshed','reused_last_good'} for r in state['sources'].values())
        state['status']='needs_attention' if blocked else 'acquired'
        core.atomic_json(path,state)
        if blocked or through=='acquire':return _write_summary(folder,state)
        try:
            _,current,_=core.load_run(root,run_id)
            latest={a['sourceId']:a for a in current.get('attempts',[])}
            uncovered=[sid for sid,a in latest.items() if a.get('status') in {'failed','incomplete','running'}
                and state['sources'].get(sid,{}).get('status')!='reused_last_good']
            if uncovered:raise core.RefreshError('Failed sources lack explicit workflow reuse approval: '+', '.join(uncovered))
            recorded_reasons=[r['reason'] for r in state['sources'].values() if r['status']=='reused_last_good']
            value=core.build(root,run_id,retry=True,fallback_reason='; '.join(recorded_reasons) or None)
            state['pipeline']={'build':value['status'],'warnings':value.get('warnings',[])}
            from refresh_release import validate,prepare
            value=validate(root,run_id,run_tests=True)
            state['pipeline']['validation']=value['status']
            if through=='prepare-release':
                value=prepare(root,run_id);state['pipeline']['candidate']=value['status']
            state['status']='completed_with_reuse' if any(r['status']=='reused_last_good' for r in state['sources'].values()) else 'completed'
        except BaseException:
            state['status']='pipeline_failed';core.atomic_json(path,state);_write_summary(folder,state);raise
        core.atomic_json(path,state)
        return _write_summary(folder,state)
