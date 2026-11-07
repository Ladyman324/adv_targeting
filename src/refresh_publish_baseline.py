"""Retain exactly the verified published data before the next scheduled build."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import csv
import gzip
import json
import shutil
import subprocess
import zipfile
import refresh_core as core
from refresh_data import make_plan
from refresh_inventory import candidate_inventory
from refresh_publish import PublicationHeld

SHELLS={"webapp/app.js","webapp/field.js","webapp/sw.js","webapp/index.html","webapp/field.html"}
CANONICAL={"data/raw/barrons_rankings.json","data/raw/forbes_rankings.json"}

def _bindings(m):
    result={**m["baselineArtifacts"],**m.get("reusedArtifacts",{}),**m["inputs"],**m.get("legacyRankingFacts",{})}
    for step in m["steps"].values():result.update(step.get("finalOutputs",step.get("outputs",{})))
    result.update(m["validation"]["artifacts"])
    for source in m["sourceInventory"]:
        for fact in source.get("fileFacts",[]):
            value=dict(sha256=fact["sha256"],bytes=fact["bytes"])
            if fact["file"] in CANONICAL:result[fact["file"]]=value
            else:result.setdefault(fact["file"],value)
    return result

def adopt(root,run_id):
    root=Path(root).resolve()
    directory,m,stage=core.load_run(root,run_id)
    live=core.read_json(directory/"publication/live-verification.json")
    if not live.get("verified"):raise PublicationHeld("Verified publication is required before adoption")
    baseline=set(core.baseline_paths(stage))
    paths=baseline|{stage/"api/shared/data-freshness.json"}
    paths.update(stage/x for x in m["inputs"])
    paths.update(stage/x for x in SHELLS)
    for rel in m["validation"]["artifacts"]:
        if rel.startswith("webapp/data/"):paths.add(stage/rel)
    for rel in CANONICAL:
        if (stage/rel).exists():paths.add(stage/rel)
    bindings=_bindings(m)
    expected={x.relative_to(stage).as_posix():bindings[x.relative_to(stage).as_posix()] for x in paths}
    allowed=("data/interim/","data/output/","data/identity/","webapp/data/","data/raw/",
        "data/reference/","data/refresh/ranking_editions/","data/refresh/source_archive/","data/refresh/supporting_inputs/")
    other=SHELLS|{"api/shared/act_contacts.json","api/shared/act_mail_codes.json",
        "api/shared/approved-recipient-release.json","api/shared/data-freshness.json","data/refresh/source_receipts.json"}
    if not all(x.startswith(allowed) or x in other for x in expected):
        raise PublicationHeld("Baseline path is outside its allowed scope")
    if core.facts([stage/x for x in expected],stage)!=expected:
        raise PublicationHeld("Published workspace facts changed")
    folder=directory/"publication";backup=folder/"prior-operational-baseline.zip"
    before=core.facts([root/x for x in expected],root)
    changed=[x for x in expected if before.get(x)!=expected[x]]
    # Keep first-attempt rollback data across resumable adoption.
    if not backup.exists():
        with zipfile.ZipFile(str(backup),"w",zipfile.ZIP_DEFLATED) as z:
            for rel in changed:
                if rel in before:z.write(str(root/rel),rel)
        core.atomic_json(folder/"prior-operational-baseline.json",before)
    def copy(rel):
        source=stage/rel;target=core.contained(root,rel)
        target.parent.mkdir(parents=True,exist_ok=True)
        temp=target.with_name(target.name+".published-copy.tmp")
        shutil.copy2(str(source),str(temp))
        if core.file_hash(temp)!=expected[rel]["sha256"]:raise PublicationHeld("Baseline copy hash differs")
        temp.replace(target)
    with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(copy,changed))
    if core.facts([root/x for x in expected],root)!=expected:raise PublicationHeld("Baseline readback differs")
    base_keys={x.relative_to(stage).as_posix() for x in baseline}
    if {x.relative_to(root).as_posix() for x in core.baseline_paths(root)}!=base_keys:
        raise PublicationHeld("Operational baseline contains unexpected artifacts")
    plan=make_plan(root,"full")
    raw={x["id"]:x for x in m["sourceInventory"]}
    for row in plan["sources"]:
        if row.get("sha256")!=raw[row["id"]].get("sha256"):raise PublicationHeld("Raw snapshot selection differs")
    consumed=candidate_inventory(root,m["mode"],plan["sources"])
    published={x["id"]:x for x in core.read_json(stage/"api/shared/data-freshness.json")["sources"]}
    for row in consumed:
        if row.get("sha256")!=published[row["id"]].get("sha256"):
            raise PublicationHeld("Consumed source selection differs")
    current={x:core.normalized_code_hash(root/x,x) for x in m["codeFiles"]}
    if current!=m["codeNormalization"]:raise PublicationHeld("Implementation changed during adoption")
    def git(args):
        reply=subprocess.run(["git","-C",str(root)]+args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        if reply.returncode:raise PublicationHeld("Operational source metadata could not be committed")
        return reply.stdout.strip()
    edits=git(["diff","--name-only"]).splitlines()
    if not set(edits)<=SHELLS|{"api/shared/approved-recipient-release.json"} or git(["diff","--cached","--name-only"]):
        raise PublicationHeld("Unrelated source edits prevent automatic metadata adoption")
    if edits:
        git(["add","--"]+edits)
        git(["commit","-m","Retain published data baseline "+run_id])
    if git(["status","--porcelain"]):raise PublicationHeld("Operational source is not clean after adoption")
    core.atomic_json(folder/"published-baseline-artifacts.json",{x:expected[x] for x in base_keys})
    core.atomic_json(folder/"published-source-inputs.json",m["inputs"])
    core.atomic_json(folder/"next-refresh-plan.json",plan)
    result=dict(verified=True,files=len(expected),changed_files=len(changed),source_hashes=len(consumed),
        factsHash=core.digest(expected),operationalCommit=git(["rev-parse","HEAD"]),utc=core.utc())
    core.atomic_json(folder/"baseline-adoption.json",result)
    return result

def recipient_comparison(previous,spec):
    """Actual published before/after, independent of the checkout comparison."""
    def payload(bundle):
        root=Path(bundle["root"])
        _,m,stage=core.load_run(root,bundle["run_id"])
        relative="data/identity/approved_recipients.json.gz"
        fact=m["validation"]["artifacts"][relative]
        if core.file_hash(stage/relative)!=fact["sha256"]:raise PublicationHeld("Recipient comparison source changed")
        with gzip.open(stage/relative,"rt",encoding="utf-8-sig") as stream:return json.load(stream)["recipients"]
    before,after=payload(previous),payload(spec)
    added=set(after)-set(before);removed=set(before)-set(after)
    if before and len(after)<len(before)*0.8:raise PublicationHeld("Major recipient-count loss")
    directory=Path(spec["directory"])/"publication"
    path=directory/"recipient-changes.csv"
    def safe(v):return "'"+v if isinstance(v,str) and v.lstrip().startswith(("=","+","-","@")) else v
    with path.open("w",encoding="utf-8-sig",newline="") as stream:
        writer=csv.DictWriter(stream,fieldnames=["change","crd","before_email","after_email","before_greeting","after_greeting"])
        writer.writeheader()
        for crd in sorted(set(before)|set(after)):
            a,b=before.get(crd,{}),after.get(crd,{})
            kinds=["added"] if crd in added else ["removed"] if crd in removed else [
                name for name,key in [("email_changed","email"),("greeting_changed","greetingName"),("act_route_changed","actContactId")]
                if a.get(key)!=b.get(key)]
            for kind in kinds:writer.writerow({k:safe(v) for k,v in dict(change=kind,crd=crd,
                before_email=a.get("email",""),after_email=b.get("email",""),
                before_greeting=a.get("greetingName",""),after_greeting=b.get("greetingName","")).items()})
    result=dict(before=len(before),after=len(after),added=len(added),removed=len(removed),csv=str(path))
    core.atomic_json(directory/"recipient-comparison.json",result)
    return result

