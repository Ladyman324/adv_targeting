"""One-time migration from the previously verified browser-only release."""
from __future__ import annotations
import asyncio
import hashlib
import json
from pathlib import Path
import os
import subprocess
import sys
import time
import refresh_core as core
from refresh_publish import PublicationHeld

ORIGIN="https://advisors.eicatlanta.com"

def inspect_legacy(backend,last):
    """Only the explicitly registered bootstrap release may use existing SSO."""
    if not backend.config.get("allowLegacyBootstrap"):
        raise PublicationHeld("Legacy bootstrap is not authorized")
    try:
        api=backend.api_probe()
        static=backend.request(ORIGIN+"/data/refresh_release.json?v="+str(time.time_ns()))
        return dict(api=api,static=static)
    except PublicationHeld:
        pass
    async def inspect():
        from playwright.async_api import async_playwright
        asyncio.get_running_loop().set_exception_handler(lambda loop,context:None)
        async with async_playwright() as pw:
            browser=await pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
            context=browser.contexts[0]
            async def get(path):
                reply=await context.request.get(ORIGIN+path,max_redirects=0,timeout=60000)
                if reply.status!=200:raise PublicationHeld("Refresh the one-time bootstrap sign-in")
                body=await reply.body();await reply.dispose();return body
            health=json.loads(await get("/api/health"))
            fresh=json.loads(await get("/api/data-freshness"))
            # A prior attempt may have completed both components before journaling.
            try:
                public=json.loads(await get("/data/refresh_release.json?v="+str(time.time_ns())))
                if public.get("candidateId")==fresh["candidate"]["id"]:
                    return dict(api=dict(health,candidate=fresh["candidate"]),static=public)
            except PublicationHeld:pass
            app=await get("/app.js?bootstrap="+str(time.time_ns()))
            if hashlib.sha256(app).hexdigest()!=last["bootstrapAppSha256"]:
                raise PublicationHeld("Legacy website differs from its registered archive")
            return dict(api=dict(health,candidate=fresh["candidate"]),static=last["stamp"])
    try:return asyncio.run(inspect())
    except PublicationHeld:raise
    except Exception:raise PublicationHeld("One-time bootstrap browser check unavailable") from None

def quiet_metadata(backend):
    """Only scheduling/lease fields are read; subject/body/recipient data is excluded."""
    backend.container()
    env=os.environ.copy()
    env["AZURE_STORAGE_CONNECTION_STRING"]=backend._connection
    rows=[]
    sources=[
        ("EmailBatches",None,["PartitionKey","RowKey","status","scheduledForUtc","scheduleLeaseUntilUtc"]),
        ("EmailMessages",
         "state ne 'sent' and state ne 'failed' and state ne 'canceled' and state ne 'draft_ready' and state ne 'auth_required' and state ne 'editing' and state ne 'ready' and state ne 'invalid'",
         ["PartitionKey","RowKey","state","leaseUntilUtc","retryAfterUtc","plannedSendUtc","capacityDay","updatedUtc","handledManuallyUtc","retryBatchId"])]
    batch_status={}
    for table,query,select in sources:
        args=[__import__("shutil").which("az"),"storage","entity","query",
            "--account-name","eicadvisorlog","--table-name",table,"--auth-mode","key",
            "--select"]+select+["--num-results","1000","--output","json","--only-show-errors"]
        if query:args += ["--filter",query]
        data=json.loads(backend.command(args,environment=env,timeout=180))
        if any((data.get("next_marker") or {}).values()):raise PublicationHeld("Quiet-window metadata limit exceeded")
        for row in data.get("items",[]):
            if table=="EmailMessages":
                rows.append(dict(row,parentBatchStatus=batch_status.get(row.get("PartitionKey"))))
            else:
                batch_status[row["PartitionKey"]+"_"+row["RowKey"]]=row.get("status")
                if row.get("status")=="scheduled":rows.append(dict(state="scheduled_preflight",
                    plannedSendUtc=row.get("scheduledForUtc"),leaseUntilUtc=row.get("scheduleLeaseUntilUtc")))
    del env
    # Reuse the deployed worker's exact Eastern sending-window helper and probe algorithm.
    script="const fs=require('node:fs'); const p=require(process.argv[1]); process.stdout.write(JSON.stringify(p.quiet(JSON.parse(fs.readFileSync(0,'utf8')))));"
    env={k:v for k,v in os.environ.items() if not any(s in k.upper() for s in ["SECRET","TOKEN","PASSWORD","CONNECTION_STRING","ACCOUNT_KEY"])}
    env["NODE_PATH"]=str(backend.root/"api/node_modules")
    result=json.loads(backend.command(["node","-e",script,str(backend.root/"api/refresh-release/index.js")],
        environment=env,input_text=json.dumps(rows),timeout=180))
    core.atomic_json(backend.folder/"bootstrap-quiet-window.json",result)
    return result.get("quiet") is True

