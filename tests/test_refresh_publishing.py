import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
import refresh_core as core
import refresh_publish as pub
import refresh_publish_policy as policy
from refresh_publish_azure import AzurePublisher

class FakeBackend:
    def __init__(self,fail=None,busy=False):
        self.fail=fail;self.busy=busy;self.events=[];self.live="old";self.adopt_count=0
    def step(self,name):
        self.events.append(name)
        if self.fail==name:raise RuntimeError("fixture failure")
    @contextmanager
    def lock(self):
        self.step("lock")
        yield
    def current(self):self.step("current");return {"id":self.live}
    def is_candidate(self,current,spec):return current["id"]=="new"
    def assert_baseline(self,current):self.step("baseline")
    def check_changes(self,spec):self.step("compare")
    def quiet(self):self.step("quiet");return not self.busy
    def backup(self,current):self.step("backup");return "backup.json"
    def transfer(self,spec):self.step("transfer")
    def deploy_api(self,spec):self.step("api")
    def verify_api(self,spec):self.step("api_verify")
    def deploy_static(self,spec):self.step("static");self.live="new"
    def verify(self,spec):self.step("verify")
    def record_receipt(self,spec):self.step("receipt")
    def adopt(self,spec):self.adopt_count+=1;self.step("adopt");return {"verified":True}
    def record_current(self,spec,adopted):self.step("record")
    def rollback(self,backup,spec):self.step("rollback");self.live="old"

class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.run=self.root/"run";self.run.mkdir()
        self.spec=dict(root=str(self.root),run_id="fixture",directory=str(self.run),
            stage=str(self.root/"stage"),candidate_hash="a"*64,code_commit="b"*40,warnings=[])
    def run_pub(self,backend,**changes):
        spec=dict(self.spec,**changes)
        return pub.publish(self.root,"fixture",backend=backend,config={"enabled":True},gate=lambda *args:spec)
    def state(self):return core.read_json(self.run/"publication/automatic.json")
    def test_explicit_registration_required(self):
        with self.assertRaises(pub.PublicationHeld):pub.publish(self.root,"fixture",backend=FakeBackend(),config={})
    def test_known_minor_warnings_publish(self):
        backend=FakeBackend()
        result=self.run_pub(backend,warnings=["ACT account-value discrepancies: 6 broker codes; retained"])
        self.assertEqual(result["status"],"completed")
        self.assertLess(backend.events.index("transfer"),backend.events.index("api"))
        self.assertLess(backend.events.index("api_verify"),backend.events.index("static"))
        self.assertLess(backend.events.index("verify"),backend.events.index("receipt"))
        self.assertLess(backend.events.index("receipt"),backend.events.index("adopt"))
    def test_busy_window_defers_without_mutations(self):
        backend=FakeBackend(busy=True);result=self.run_pub(backend)
        self.assertEqual(result["status"],"deferred_busy")
        self.assertNotIn("backup",backend.events);self.assertNotIn("transfer",backend.events)
    def test_each_remote_failure_restores_prior_pair(self):
        for failure in ["transfer","api","api_verify","static","verify"]:
            with self.subTest(failure=failure):
                if (self.run/"publication/automatic.json").exists():(self.run/"publication/automatic.json").unlink()
                backend=FakeBackend(fail=failure)
                with self.assertRaises(RuntimeError):self.run_pub(backend)
                self.assertIn("rollback",backend.events)
                self.assertNotIn("adopt",backend.events)
                self.assertEqual(backend.live,"old")
                self.assertEqual(self.state()["status"],"rolled_back")
    def test_rollback_failure_is_reported(self):
        backend=FakeBackend(fail="api")
        def broken(*args):raise OSError("fixture rollback failed")
        backend.rollback=broken
        with self.assertRaises(RuntimeError):self.run_pub(backend)
        self.assertEqual(self.state()["status"],"rollback_failed")
    def test_completed_run_does_not_redeploy(self):
        backend=FakeBackend();self.run_pub(backend);before=list(backend.events)
        self.run_pub(backend);self.assertEqual(backend.events,before)
    def test_changed_candidate_identity_cannot_reuse_journal(self):
        backend=FakeBackend();self.run_pub(backend)
        with self.assertRaises(pub.PublicationHeld):self.run_pub(backend,candidate_hash="c"*64)
    def test_failed_local_adoption_keeps_verified_app_and_resumes(self):
        backend=FakeBackend(fail="adopt")
        with self.assertRaises(RuntimeError):self.run_pub(backend)
        self.assertEqual(backend.live,"new")
        self.assertNotIn("rollback",backend.events)
        self.assertEqual(self.state()["status"],"published_adoption_failed")
        backend.fail=None;self.run_pub(backend)
        self.assertEqual(backend.events.count("api"),1)
        self.assertEqual(self.state()["status"],"completed")
    def test_interrupted_mixed_pair_is_restored_before_retry(self):
        folder=self.run/"publication";folder.mkdir()
        core.atomic_json(folder/"automatic.json",dict(candidateHash="a"*64,status="api_deployed",backup="backup.json",events=[],warnings=[]))
        backend=FakeBackend()
        with self.assertRaises(pub.PublicationHeld):self.run_pub(backend)
        self.assertIn("rollback",backend.events)
        self.assertNotIn("transfer",backend.events)
        self.assertEqual(self.state()["status"],"rolled_back")

    def test_failed_rollback_cannot_retry_deployment(self):
        backend=FakeBackend(fail="api")
        backend.rollback=lambda *a:(_ for _ in ()).throw(OSError("rollback failed"))
        with self.assertRaises(RuntimeError):self.run_pub(backend)
        before=list(backend.events)
        with self.assertRaisesRegex(pub.PublicationHeld,"operator recovery"):self.run_pub(backend)
        self.assertEqual(backend.events,before)

    def test_interrupted_complete_pair_failing_verification_rolls_back(self):
        folder=self.run/"publication";folder.mkdir()
        core.atomic_json(folder/"automatic.json",dict(candidateHash="a"*64,status="static_deployed",backup="backup.json",events=[],warnings=[]))
        backend=FakeBackend(fail="verify");backend.live="new"
        with self.assertRaises(RuntimeError):self.run_pub(backend)
        self.assertEqual(backend.live,"old")
        self.assertEqual(self.state()["status"],"rolled_back")
        self.assertNotIn("receipt",backend.events)

    def test_receipt_failure_resumes_without_redeployment(self):
        backend=FakeBackend(fail="receipt")
        with self.assertRaises(RuntimeError):self.run_pub(backend)
        self.assertEqual(backend.live,"new")
        backend.fail=None;self.run_pub(backend)
        self.assertEqual(backend.events.count("api"),1)
        self.assertEqual(self.state()["status"],"completed")

class MinorPolicyTests(unittest.TestCase):
    def test_account_and_held_identity_rows_are_nonblocking(self):
        values=["ACT account-value discrepancies: 6 broker codes; values preserved",
            "Roster identity review: 199 source rows held rather than reassigned"]
        self.assertEqual(policy.minor_warnings(values),values)
    def test_unknown_warning_needs_review(self):
        with self.assertRaises(core.RefreshError):policy.minor_warnings(["Identity acceptance policy unexpectedly changed"])
    def test_critical_source_never_automatically_reused(self):
        for source in ["act","sec_individuals","sec_firms","sec_adv"]:
            self.assertFalse(policy.can_reuse_source(source,["prior.json"]))
        self.assertTrue(policy.can_reuse_source("roster:cetera",["prior.csv"]))
        self.assertFalse(policy.can_reuse_source("roster:cetera",[]))
    def test_too_many_last_good_fallbacks_hold_publication(self):
        sources={"roster:"+str(i):{"status":"reused_last_good" if i<2 else "refreshed"} for i in range(4)}
        with self.assertRaises(core.RefreshError):policy.check_source_results(sources)
    def test_small_map_gap_needs_no_dropped_usable_point(self):
        comparison={"mapContinuity":{"previouslyMappedStillInSEC":1000,"missingFromCandidate":5,"missingWithUsablePlacement":0}}
        self.assertIsNotNone(policy.minor_map_reason(comparison,["previously_mapped_current_SEC"]))
        comparison["mapContinuity"]["missingWithUsablePlacement"]=1
        self.assertIsNone(policy.minor_map_reason(comparison,["previously_mapped_current_SEC"]))
    def test_large_or_unclassified_coverage_drop_is_major(self):
        comparison={"mapContinuity":{"previouslyMappedStillInSEC":1000,"missingFromCandidate":11,"missingWithUsablePlacement":0}}
        self.assertIsNone(policy.minor_map_reason(comparison,["previously_mapped_current_SEC"]))
        self.assertIsNone(policy.minor_map_reason(comparison,["eligibleRecipients"]))
    def test_missing_placement_evidence_fails_closed(self):
        comparison={"mapContinuity":{"previouslyMappedStillInSEC":1000,"missingFromCandidate":1}}
        self.assertIsNone(policy.minor_map_reason(comparison,["previously_mapped_current_SEC"]))

    def test_unresolved_and_critical_source_statuses_hold(self):
        for status in ["failed","pending","interrupted","needs_review","running"]:
            with self.assertRaises(core.RefreshError):policy.check_source_results({"act":{"status":status}})
        with self.assertRaises(core.RefreshError):policy.check_source_results({"act":{"status":"reused_last_good"}})

class AdapterPrivacyTests(unittest.TestCase):
    def test_redaction_precedes_log_storage(self):
        backend=AzurePublisher.__new__(AzurePublisher);backend.secrets=["private-fixture-token"]
        raw="token=private-fixture-token\nCookie: value\nurl?sig=secret&x=1\nAuthorization: private"
        result=backend.redact(raw)
        self.assertNotIn("private-fixture-token",result)
        self.assertNotIn("sig=secret",result)
        self.assertNotIn("Authorization: private",result)
        self.assertNotIn("Cookie: value",result)
    def test_unexpected_verification_host_is_refused(self):
        backend=AzurePublisher.__new__(AzurePublisher);backend._host="trusted.azurewebsites.net"
        with self.assertRaises(pub.PublicationHeld):backend.request("https://example.invalid/data")
    def test_api_metadata_failure_is_generic(self):
        backend=AzurePublisher.__new__(AzurePublisher);backend.secrets=[]
        with patch("refresh_publish_azure.subprocess.run",return_value=SimpleNamespace(returncode=1,stdout="private-fixture-token")):
            with self.assertRaises(pub.PublicationHeld) as caught:backend.command(["fake"])
        self.assertNotIn("private-fixture-token",str(caught.exception))

class LegacyMetadataTests(unittest.TestCase):
    def test_bootstrap_uses_supported_page_size_and_only_worker_metadata(self):
        from refresh_publish_legacy import quiet_metadata
        class Backend:
            def __init__(self,root):self.root=root;self.folder=root;self._connection='private-fixture';self.calls=[]
            def container(self):return None
            def command(self,args,**kw):
                self.calls.append(args)
                if args[0]=='node':
                    rows=json.loads(kw['input_text'])
                    self.assertions(rows,kw)
                    return '{"quiet":true}'
                self.assert_page(args)
                if args[args.index('--table-name')+1]=='EmailBatches':
                    return json.dumps(dict(items=[dict(PartitionKey='user',RowKey='old',status='canceled')]))
                return json.dumps(dict(items=[dict(PartitionKey='user_old',state='draft_pending')]))
            def assert_page(self,args):
                assert args[args.index('--num-results')+1]=='1000'
                assert not any(x in args for x in ['bodyText','subject','recipientEmail'])
            def assertions(self,rows,kw):
                assert rows[0]['parentBatchStatus']=='canceled'
                assert kw['timeout']>=180
                assert 'AZURE_STORAGE_CONNECTION_STRING' not in kw['environment']
        with tempfile.TemporaryDirectory() as name:
            self.assertTrue(quiet_metadata(Backend(Path(name))))

class StaticProofTests(unittest.TestCase):
    def test_public_stamp_does_not_feed_its_own_fingerprint(self):
        import web_assets as assets
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            (root/"metadata.json").write_text(json.dumps({"generated_utc":"2026-11-07T08:00:00Z"}))
            (root/"values.json").write_text('{"value":1}')
            first=assets.data_fingerprint(root)
            (root/"refresh_release.json").write_text('{"candidateId":"one"}')
            self.assertEqual(first,assets.data_fingerprint(root))
            (root/"values.json").write_text('{"value":2}')
            self.assertNotEqual(first,assets.data_fingerprint(root))

class BaselineBindingTests(unittest.TestCase):
    def test_final_canonical_rankings_override_legacy_but_consumed_outputs_stay_bound(self):
        from refresh_publish_baseline import _bindings
        old={"sha256":"old","bytes":1};new={"sha256":"new","bytes":2}
        value=dict(baselineArtifacts={"data/raw/barrons_rankings.json":old},inputs={},
            legacyRankingFacts={"data/raw/barrons_rankings.json":old},
            steps={"build":{"finalOutputs":{"data/interim/advisors.parquet":new}}},
            validation={"artifacts":{"webapp/data/metadata.json":new}},
            sourceInventory=[{"fileFacts":[dict(file="data/raw/barrons_rankings.json",**new),
                dict(file="data/interim/advisors.parquet",**old)]}])
        result=_bindings(value)
        self.assertEqual(result["data/raw/barrons_rankings.json"],new)
        self.assertEqual(result["data/interim/advisors.parquet"],new)
        self.assertEqual(result["webapp/data/metadata.json"],new)


class StoredBlobBytesTests(unittest.TestCase):
    def test_real_sdk_preserves_gzip_bytes_for_release_hashes(self):
        import gzip
        import threading
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        try:
            from azure.storage.blob import BlobClient
        except ImportError:
            self.skipTest("Optional Azure publishing SDK is not installed")
        wire=gzip.compress(b'{"metadata":"transport-fixture"}',mtime=0)
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                self.send_response(206)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Encoding','gzip')
                self.send_header('Content-Length',str(len(wire)))
                self.send_header('Content-Range','bytes 0-'+str(len(wire)-1)+'/'+str(len(wire)))
                self.send_header('ETag','"transport-fixture"')
                self.send_header('Last-Modified','Sat, 07 Nov 2026 12:00:00 GMT')
                self.send_header('x-ms-blob-type','BlockBlob')
                self.end_headers();self.wfile.write(wire)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        client=BlobClient.from_blob_url('http://127.0.0.1:'+str(server.server_port)+'/fixtures/lookup.json.gz',retry_total=0)
        try:
            self.assertEqual(AzurePublisher.stored_bytes(client),wire)
        finally:
            client.close();server.shutdown();server.server_close();worker.join(timeout=2)

class AzureMetadataLayoutTests(unittest.TestCase):
    def test_probe_accepts_flat_and_wrapped_cli_metadata(self):
        # Model the CLI's dotted-field/OR selectors for these metadata fixtures.
        def read_selector(expression,payload):
            for selector in expression.split('||'):
                value=payload
                for field in selector.strip().split('.'):
                    value=value.get(field) if isinstance(value,dict) else None
                if value is not None:return value
            return None
        from unittest.mock import patch
        for wrapped in [False,True]:
            with self.subTest(wrapped=wrapped):
                backend=AzurePublisher.__new__(AzurePublisher)
                backend._host=None;backend._probe_key=None;backend.secrets=[]
                def command(args,**kwargs):
                    payload={'default':'fixture-function-key'} if '--function-name' in args else {'defaultHostName':'fixture.azurewebsites.net'}
                    if wrapped:payload={'properties':payload}
                    result=read_selector(args[args.index('--query')+1],payload)
                    return '' if result is None else json.dumps(result)
                backend.command=command
                def request(url,headers):
                    self.assertEqual(url,'https://fixture.azurewebsites.net/api/refresh-release')
                    self.assertEqual(headers,{'x-functions-key':'fixture-function-key'})
                    return {'available':True}
                backend.request=request
                with patch('refresh_publish_azure.shutil.which',return_value='az.cmd'):
                    self.assertEqual(backend.api_probe(),{'available':True})

if __name__=="__main__":unittest.main()

