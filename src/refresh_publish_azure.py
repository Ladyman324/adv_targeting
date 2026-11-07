"""Azure adapter for exact scheduled publication; credentials remain in memory."""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time

import refresh_core as core
from refresh_publish import PublicationHeld

ORIGIN="https://advisors.eicatlanta.com"
APP="eic-advisors-api"
SITE="eic-advisor-map"
GROUP="rg-advisor-map"
ACCOUNT="eicadvisorlog"
SETTING="DATA_FRESHNESS_PUBLICATION_RECEIPT_JSON"
STAMP="/data/refresh_release.json"

def archive_bundle(root, run_id):
    directory,run,stage=core.load_run(Path(root),run_id)
    release=core.read_json(directory/"candidate/release.json")
    claimed=release.get("contentHash")
    if (claimed!=core.digest({k:v for k,v in release.items() if k!="contentHash"})
        or claimed!=run.get("candidate",{}).get("contentHash") or release.get("dirty")
        or run.get("validation",{}).get("status")!="success" or not run["validation"].get("runTests")):
        raise PublicationHeld("Retained release integrity failed")
    for relative,fact in release["files"].items():
        file=core.contained(directory/"candidate",relative)
        if file.is_symlink() or not file.is_file() or core.file_hash(file)!=fact["sha256"]:
            raise PublicationHeld("A retained package or lookup changed")
    return dict(root=str(root),run_id=run_id,directory=str(directory),stage=str(stage),
        candidate_hash=claimed,release=release,descriptor=core.read_json(stage/"api/shared/data-freshness.json"),
        machine_probe=(stage/"api/refresh-release/function.json").is_file())

def extract(bundle,kind):
    file=Path(bundle["directory"])/"candidate"/(kind+".tgz")
    if core.file_hash(file)!=bundle["release"]["files"][kind+".tgz"]["sha256"]:
        raise PublicationHeld("Archive changed")
    target=Path(tempfile.mkdtemp(prefix="eic-auto-"+kind+"-"))
    with tarfile.open(str(file),"r:gz") as archive:
        for item in archive.getmembers():
            relative=PurePosixPath(item.name)
            if (not(item.isdir() or item.isfile()) or relative.is_absolute()
                or ".." in relative.parts or "\\" in item.name or ":" in item.name):
                raise PublicationHeld("Unsafe archive member")
            core.contained(target,item.name)
        archive.extractall(str(target))
    return target

class AzurePublisher:
    def __init__(self,root,config):
        self.root=Path(root).resolve()
        self.config=config
        self.folder=self.root/"data/refresh/publishing"
        self.folder.mkdir(parents=True,exist_ok=True)
        self.current_file=self.folder/"current.json"
        self.secrets=[]
        self._container=None
        self._connection=None
        self._probe_key=None
        self._host=None
        self._lease=None
        self._lease_lost=False
        self._last=None
        self._candidate=None
        self._stable_before=None
        self._static_finished=None

    def redact(self,value):
        for secret in self.secrets:
            if secret:value=value.replace(secret,"[REDACTED]")
        value=re.sub(r"(?i)([?&](?:sig|token|code)=)[^&\s]+",r"\1[REDACTED]",value)
        return re.sub(r"(?im)^.*(?:authorization|(?:set-)?cookie|x-functions-key|accountkey|deployment.token|password|connection_string)\s*[:=].*$",
            "[credential-bearing diagnostic suppressed]",value)

    def command(self,args,*,cwd=None,environment=None,log=None,timeout=900,input_text=None):
        try:
            result=subprocess.run(args,cwd=str(cwd or tempfile.gettempdir()),env=environment,
                input=input_text,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                text=True,errors="replace",timeout=timeout)
        except Exception:
            raise PublicationHeld("Publication command could not complete") from None
        if log:Path(log).write_text(self.redact(result.stdout),encoding="utf-8")
        if result.returncode:raise PublicationHeld("Publication command failed; inspect redacted diagnostics")
        return result.stdout

    def az(self,args,*,secret=False):
        executable=shutil.which("az")
        if not executable:raise PublicationHeld("Azure CLI is unavailable")
        raw=self.command([executable]+args+["--only-show-errors","--output","json"],timeout=180)
        try:value=json.loads(raw)
        except ValueError:raise PublicationHeld("Invalid Azure response") from None
        if secret and isinstance(value,str):self.secrets.append(value)
        return value

    def setting(self,name):
        return self.az(["functionapp","config","appsettings","list","--name",APP,"--resource-group",GROUP,
            "--query","[?name=='"+name+"'].value | [0]"],secret=True)

    def set_receipt_setting(self,value):
        path=self.folder/"publication-setting.json"
        core.atomic_json(path,{SETTING:value})
        self.command([shutil.which("az"),"functionapp","config","appsettings","set","--name",APP,
            "--resource-group",GROUP,"--settings","@"+str(path),"--output","none","--only-show-errors"],timeout=180)

    def container(self):
        if self._container is None:
            from azure.storage.blob import BlobServiceClient
            connection=self.setting("DEPLOYMENT_STORAGE_CONNECTION_STRING")
            if not connection or "defaultendpointsprotocol=https" not in connection.lower():
                raise PublicationHeld("HTTPS storage credential unavailable")
            service=BlobServiceClient.from_connection_string(connection)
            if service.account_name!=ACCOUNT or service.url.rstrip("/")!="https://"+ACCOUNT+".blob.core.windows.net":
                raise PublicationHeld("Publication storage account differs")
            self._connection=connection
            self._container=service.get_container_client("lookups")
        return self._container

    @staticmethod
    def stored_bytes(client):
        return client.download_blob(decompress=False).readall()

    def assert_lease(self):
        if self._lease is None or self._lease_lost:raise PublicationHeld("Publication lease lost")

    @contextmanager
    def lock(self):
        from azure.core.exceptions import ResourceExistsError
        blob=self.container().get_blob_client("refresh-publications/publisher.lock")
        try:blob.upload_blob(b"",overwrite=False)
        except ResourceExistsError:pass
        try:self._lease=blob.acquire_lease(lease_duration=60)
        except Exception:raise PublicationHeld("Another publisher holds the application lease") from None
        stopped=threading.Event()
        def renew():
            while not stopped.wait(20):
                try:self._lease.renew()
                except Exception:self._lease_lost=True;return
        worker=threading.Thread(target=renew,daemon=True);worker.start()
        try:yield
        finally:
            stopped.set();worker.join(timeout=5)
            try:self._lease.release()
            except Exception:pass
            self._lease=None

    def request(self,url,headers=None):
        import requests
        from urllib.parse import urlparse
        host=urlparse(url)
        if host.scheme!="https" or host.hostname not in {"advisors.eicatlanta.com",self._host}:
            raise PublicationHeld("Unexpected verification host")
        try:
            response=requests.get(url,headers=headers or {},timeout=90,allow_redirects=False)
            if response.status_code!=200:raise PublicationHeld("Machine verification HTTP "+str(response.status_code))
            return response.json()
        except PublicationHeld:raise
        except Exception:raise PublicationHeld("Machine verification failed") from None

    def api_probe(self,window=False):
        if self._host is None:
            self._host=self.az(["functionapp","show","--name",APP,"--resource-group",GROUP,"--query","defaultHostName || properties.defaultHostName"])
            if not isinstance(self._host,str) or not self._host.endswith(".azurewebsites.net"):
                raise PublicationHeld("Unexpected Function App host")
        if self._probe_key is None:
            self._probe_key=self.az(["functionapp","function","keys","list","--name",APP,"--resource-group",GROUP,
                "--function-name","refresh-release","--query","default || properties.default"],secret=True)
            if not self._probe_key:raise PublicationHeld("Read-only function probe key unavailable")
        return self.request("https://"+self._host+"/api/refresh-release"+("?window=1" if window else ""),
            {"x-functions-key":self._probe_key})

    def current(self):
        if not self.current_file.is_file():raise PublicationHeld("Register a verified rollback baseline first")
        self._last=core.read_json(self.current_file)
        if self._last.get("blocked"):raise PublicationHeld("Previous publication needs recovery")
        if not self._last.get("machine_probe"):
            from refresh_publish_legacy import inspect_legacy
            return inspect_legacy(self,self._last)
        return dict(api=self.api_probe(),static=self.request(ORIGIN+STAMP+"?v="+str(time.time_ns())))

    def bundle(self,spec):
        if self._candidate is None or self._candidate["run_id"]!=spec["run_id"]:
            self._candidate=archive_bundle(spec["root"],spec["run_id"])
        return self._candidate

    @staticmethod
    def stamp(bundle):
        v=bundle["descriptor"]["candidate"]
        return dict(schemaVersion=1,candidateId=v["id"],codeCommit=v["codeCommit"],
            staticDataFingerprint=v["staticDataFingerprint"],recipientRegistryHash=v["recipientRegistryHash"],
            descriptorSha256=core.file_hash(Path(bundle["stage"])/"api/shared/data-freshness.json"))

    def is_candidate(self,current,spec):
        expected=self.bundle(spec)["descriptor"]["candidate"]
        return (current.get("api",{}).get("candidate",{}).get("id")==expected["id"]
            and current.get("static",{}).get("candidateId")==expected["id"])

    def assert_baseline(self,current):
        v=self._last["candidate"];api=current["api"]
        if (api.get("release",{}).get("id")!=v["id"] or api.get("release",{}).get("commit")!=v["codeCommit"]
            or current["static"]!=self._last["stamp"] or not api.get("storageOk")
            or api.get("recipientEligibility")!=self._last["recipient_eligibility"]):
            raise PublicationHeld("Live components differ from rollback baseline")

    def check_changes(self,spec):
        from refresh_publish_baseline import recipient_comparison
        previous=archive_bundle(self._last["root"],self._last["run_id"])
        _,before,_=core.load_run(Path(previous["root"]),previous["run_id"])
        _,after,_=core.load_run(Path(spec["root"]),spec["run_id"])
        critical=("src/identity_normalize.py","src/identity_resolver.py","src/identity_schema.py",
                  "src/export_approved_recipients.py","api/shared/recipient-registry.js")
        if any(before["codeNormalization"].get(x)!=after["codeNormalization"].get(x) for x in critical):
            raise PublicationHeld("Identity or recipient safety implementation changed; review required")
        return recipient_comparison(previous,spec)

    def quiet(self):
        if self._last.get("machine_probe"):
            return self.api_probe(window=True).get("deploymentWindow",{}).get("quiet") is True
        from refresh_publish_legacy import quiet_metadata
        return quiet_metadata(self)

    def backup(self,current):
        self.assert_lease()
        previous=archive_bundle(self._last["root"],self._last["run_id"])
        client=self.container().get_blob_client("advisor_emails.json.gz")
        value=self.stored_bytes(client)
        expected=previous["release"]["files"]["lookups/advisor_emails.json.gz"]["sha256"]
        if hashlib.sha256(value).hexdigest()!=expected:raise PublicationHeld("Stable lookup differs from rollback archive")
        path=self.folder/"backups"/(str(time.time_ns())+".json")
        backup=dict(previous=self._last,stable_sha256=expected,stable_etag=client.get_blob_properties().etag,
            receipt_setting=self.setting(SETTING),utc=core.utc())
        core.atomic_json(path,backup);self._stable_before=backup
        return str(path)

    def transfer(self,spec):
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceExistsError
        from azure.storage.blob import ContentSettings
        bundle=self.bundle(spec);candidate=Path(bundle["directory"])/"candidate";files=bundle["release"]["files"]
        names=[n for n in files if n.startswith("lookups/")]
        names.sort(key=lambda n:(n=="lookups/advisor_emails.json.gz",n.endswith("manifest.json"),n))
        receipt=[]
        for name in names:
            self.assert_lease();source=core.contained(candidate,name);blob=name[len("lookups/"):]
            client=self.container().get_blob_client(blob)
            settings=ContentSettings(content_type="application/json",content_encoding="gzip" if blob.endswith(".gz") else None)
            if blob=="advisor_emails.json.gz":
                sha=hashlib.sha256(self.stored_bytes(client)).hexdigest()
                if sha!=files[name]["sha256"]:
                    if sha!=self._stable_before["stable_sha256"] or client.get_blob_properties().etag!=self._stable_before["stable_etag"]:
                        raise PublicationHeld("Concurrent stable lookup change")
                    with source.open("rb") as stream:client.upload_blob(stream,overwrite=True,
                        etag=self._stable_before["stable_etag"],match_condition=MatchConditions.IfNotModified,content_settings=settings)
            else:
                if not blob.startswith("approved_recipients/releases/"):raise PublicationHeld("Unexpected private path")
                try:
                    with source.open("rb") as stream:client.upload_blob(stream,overwrite=False,content_settings=settings)
                except ResourceExistsError:pass
            if hashlib.sha256(self.stored_bytes(client)).hexdigest()!=files[name]["sha256"]:
                raise PublicationHeld("Uploaded lookup hash differs")
            if client.get_blob_properties().content_settings.content_encoding!=settings.content_encoding:
                raise PublicationHeld("Lookup compression metadata differs")
            receipt.append(dict(blob=blob,sha256=files[name]["sha256"]))
        core.atomic_json(Path(spec["directory"])/"publication/lookup-transfer.json",receipt)

    def deploy(self,bundle,kind,log):
        self.assert_lease();target=extract(bundle,kind)
        env={k:v for k,v in os.environ.items() if not any(x in k.upper()
            for x in ("PASSWORD","SECRET","TOKEN","CONNECTION_STRING","ACCOUNT_KEY","ACCESS_KEY"))}
        npm=Path(os.environ["APPDATA"])/"npm"
        if kind=="api":
            self.command([str(npm/"func.cmd"),"azure","functionapp","publish",APP,"--build","remote"],
                cwd=target,environment=env,log=log,timeout=1200)
            self._probe_key=None
        else:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,"Environment") as key:
                token=winreg.QueryValueEx(key,"SWA_CLI_DEPLOYMENT_TOKEN")[0]
            if not token:raise PublicationHeld("Website deployment credential unavailable")
            self.secrets.append(token);env["SWA_CLI_DEPLOYMENT_TOKEN"]=token
            cwd=Path(tempfile.mkdtemp(prefix="eic-auto-publisher-"))
            self.command([str(npm/"swa.cmd"),"deploy",str(target),"--env","production","--app-name",SITE],
                cwd=cwd,environment=env,log=log,timeout=1200)
            self._static_finished=core.utc()

    def deploy_api(self,spec):
        self.deploy(self.bundle(spec),"api",Path(spec["directory"])/"publication/api-deployment.log")

    def deploy_static(self,spec):
        self.deploy(self.bundle(spec),"static",Path(spec["directory"])/"publication/static-deployment.log")

    def verify_api(self,spec):
        bundle=self.bundle(spec);expected=bundle["descriptor"]["candidate"];stage=Path(bundle["stage"])
        for attempt in range(12):
            try:
                v=self.api_probe()
                if (v.get("available") and v.get("storageOk") and all(v.get("candidate",{}).get(k)==expected[k] for k in ("id","codeCommit","staticDataFingerprint","recipientRegistryHash"))
                    and v.get("release",{}).get("commit")==expected["codeCommit"]
                    and v.get("release",{}).get("dirty") is False
                    and v.get("descriptorSha256")==self.stamp(bundle)["descriptorSha256"]
                    and v.get("recipientEligibility")==self._last["recipient_eligibility"]
                    and v.get("actContactsSha256")==core.file_hash(stage/"api/shared/act_contacts.json")
                    and v.get("actMailCodesSha256")==core.file_hash(stage/"api/shared/act_mail_codes.json")):
                    return v
            except Exception:pass
            time.sleep(10)
        raise PublicationHeld("New API machine health did not pass")

    def verify(self,spec):
        bundle=self.bundle(spec);api=self.verify_api(spec);expected=self.stamp(bundle)
        for attempt in range(12):
            try:
                observed=self.request(ORIGIN+STAMP+"?v="+str(time.time_ns()))
                if observed==expected:break
            except Exception:pass
            time.sleep(10)
        else:raise PublicationHeld("Website and API identities differ")
        for name,fact in bundle["release"]["files"].items():
            if name.startswith("lookups/"):
                if hashlib.sha256(self.stored_bytes(self.container().get_blob_client(name[8:]))).hexdigest()!=fact["sha256"]:
                    raise PublicationHeld("Live private lookup changed")
        result=dict(verified=True,api=api,static=observed,utc=core.utc())
        core.atomic_json(Path(spec["directory"])/"publication/live-verification.json",result)
        return result

    def record_receipt(self,spec):
        from refresh_publication import prepare_receipt
        stamp=self.stamp(self.bundle(spec))
        result=prepare_receipt(self.root,spec["run_id"],published_utc=self._static_finished or core.utc(),
            api_commit=stamp["codeCommit"],static_fingerprint=stamp["staticDataFingerprint"],
            recipient_registry_hash=stamp["recipientRegistryHash"],confirm_runtime_verified=True)
        receipt=core.read_json(Path(result["receiptFile"]))
        self.set_receipt_setting(json.dumps(receipt,separators=(",",":")))
        for attempt in range(12):
            try:
                p=self.api_probe().get("publication") or {}
                if p.get("verified") and p.get("candidateId")==spec["run_id"]:return receipt
            except Exception:pass
            time.sleep(10)
        raise PublicationHeld("Publication receipt not accepted")

    def adopt(self,spec):
        from refresh_publish_baseline import adopt
        return adopt(self.root,spec["run_id"])

    def record_current(self,spec,adopted):
        bundle=self.bundle(spec)
        record=dict(schemaVersion=1,root=str(self.root),run_id=spec["run_id"],candidate_hash=bundle["candidate_hash"],
            candidate=bundle["descriptor"]["candidate"],stamp=self.stamp(bundle),machine_probe=True,
            recipient_eligibility=self._last["recipient_eligibility"],baseline=adopted,publishedUtc=core.utc(),warnings=spec["warnings"])
        core.atomic_json(self.current_file,record);core.atomic_json(self.folder/"latest.json",record)

    def block(self,spec):
        record=core.read_json(self.current_file)
        record.update(blocked=True,recoveryRunId=spec["run_id"],updatedUtc=core.utc())
        core.atomic_json(self.current_file,record)

    def rollback(self,backup_path,spec):
        from azure.core import MatchConditions
        from azure.storage.blob import ContentSettings
        self.assert_lease();backup=core.read_json(Path(backup_path));old=backup["previous"]
        previous=archive_bundle(old["root"],old["run_id"])
        client=self.container().get_blob_client("advisor_emails.json.gz")
        sha=hashlib.sha256(self.stored_bytes(client)).hexdigest()
        new_sha=self.bundle(spec)["release"]["files"]["lookups/advisor_emails.json.gz"]["sha256"]
        if sha not in {backup["stable_sha256"],new_sha}:raise PublicationHeld("Unrecognized concurrent lookup; rollback held")
        if sha!=backup["stable_sha256"]:
            source=Path(previous["directory"])/"candidate/lookups/advisor_emails.json.gz"
            with source.open("rb") as stream:client.upload_blob(stream,overwrite=True,
                etag=client.get_blob_properties().etag,match_condition=MatchConditions.IfNotModified,
                content_settings=ContentSettings(content_type="application/json",content_encoding="gzip"))
        self.deploy(previous,"api",Path(spec["directory"])/"publication/rollback-api.log")
        self.deploy(previous,"static",Path(spec["directory"])/"publication/rollback-static.log")
        self.set_receipt_setting(backup.get("receipt_setting") or "")
        self._last=old;current=self.current();self.assert_baseline(current)
        core.atomic_json(Path(spec["directory"])/"publication/rollback-verification.json",
            dict(verified=True,restoredRunId=old["run_id"],utc=core.utc()))

