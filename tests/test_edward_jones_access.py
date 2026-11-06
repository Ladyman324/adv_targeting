import contextlib
from datetime import datetime,timedelta,timezone
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch

import httpx

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import edward_jones_async as ej
from scrape_runtime import SourceUnhealthy


class EdwardJonesAccessTests(unittest.IsolatedAsyncioTestCase):
    def test_completed_checkpoint_retains_exact_observation_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            checkpoint=Path(folder)/'checkpoint.jsonl'
            raw=b'{"observed_utc":"source-time","source_body_sha256":"body-proof"}\n'
            checkpoint.write_bytes(raw)
            archive=ej.archive_completed_checkpoint(checkpoint)
            self.assertFalse(checkpoint.exists());self.assertEqual(raw,archive.read_bytes())
            self.assertIn(ej.hashlib.sha256(raw).hexdigest(),archive.name)

    def test_identical_completed_checkpoint_reuses_verified_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            checkpoint=Path(folder)/'checkpoint.jsonl';checkpoint.write_bytes(b'first observation\n')
            archive=ej.archive_completed_checkpoint(checkpoint)
            checkpoint.write_bytes(b'first observation\n')
            self.assertEqual(archive,ej.archive_completed_checkpoint(checkpoint))
            self.assertEqual(b'first observation\n',archive.read_bytes());self.assertFalse(checkpoint.exists())

    def test_inconsistent_archive_does_not_discard_resume_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            checkpoint=Path(folder)/'checkpoint.jsonl';raw=b'observation\n';checkpoint.write_bytes(raw)
            archive=checkpoint.with_name(checkpoint.name+'.completed-'+ej.hashlib.sha256(raw).hexdigest()+'.jsonl')
            archive.write_bytes(b'inconsistent archive\n')
            with self.assertRaises(SourceUnhealthy):ej.archive_completed_checkpoint(checkpoint)
            self.assertEqual(raw,checkpoint.read_bytes());self.assertEqual(b'inconsistent archive\n',archive.read_bytes())

    def test_http200_challenge_title_is_not_contact_data(self):
        for title in ['Request Rejected','Access Denied','Just a moment','Verify you are human','Too many requests']:
            self.assertTrue(ej.publisher_challenge('<html><title>'+title+'</title></html>'))
        self.assertFalse(ej.publisher_challenge('<title>Financial Advisor</title><p>Just a moment in my career</p>'))
        self.assertFalse(ej.publisher_challenge('<html>Financial advisor profile</html>'))

    def test_default_bootstrap_uses_verified_published_advisor_search_route(self):
        args=ej.parse_args(['enrich','--input','source.json'])
        self.assertEqual('https://www.edwardjones.com/us-en/search/financial-advisor/results',ej.BOOTSTRAP_URL)
        self.assertEqual(ej.BOOTSTRAP_URL,args.bootstrap_url)

    def browser(self,status=200,title='Find a financial advisor'):
        page=SimpleNamespace(
            goto=AsyncMock(return_value=None if status is None else SimpleNamespace(status=status,headers={'retry-after':'120','set-cookie':'private-cookie'})),
            url='https://www.edwardjones.com/us-en/find-a-financial-advisor?private-query#private-fragment',
            title=AsyncMock(return_value=title),wait_for_timeout=AsyncMock(),close=AsyncMock())
        context=SimpleNamespace(new_page=AsyncMock(return_value=page),cookies=AsyncMock(return_value=[]),close=AsyncMock())
        browser=SimpleNamespace(close=AsyncMock())
        manager=AsyncMock()
        manager.__aenter__.return_value=Mock()
        args=SimpleNamespace(timeout=1,bootstrap_url='https://www.edwardjones.com/us-en/find-a-financial-advisor?private-query')
        return args,page,context,browser,manager

    async def test_bootstrap_status_diagnostics_without_cookie_export_or_sensitive_data(self):
        for status in [403,429,503,None]:
            with self.subTest(status=status):
                args,page,context,browser,manager=self.browser(status)
                with patch('playwright.async_api.async_playwright',return_value=manager),\
                     patch.object(ej,'connect_debug_chrome',new=AsyncMock(return_value=(browser,context,False))),\
                     patch.object(ej,'publisher_cookie',new=AsyncMock()) as cookies,\
                     self.assertLogs(level='WARNING') as logs:
                    with self.assertRaises(ej.PublisherBootstrapError) as caught:
                        await ej.browser_cookie(args)
                self.assertEqual(status,caught.exception.diagnostics['httpStatus'])
                diagnostic=json.dumps(caught.exception.diagnostics)+' '.join(logs.output)
                for secret in ['private-cookie','private-query','private-fragment']:self.assertNotIn(secret,diagnostic)
                cookies.assert_not_awaited();page.close.assert_awaited_once()
                context.close.assert_not_awaited();browser.close.assert_not_awaited()

    async def test_successful_bootstrap_keeps_existing_scoped_cookie_handoff(self):
        args,page,context,browser,manager=self.browser()
        with patch('playwright.async_api.async_playwright',return_value=manager),\
             patch.object(ej,'connect_debug_chrome',new=AsyncMock(return_value=(browser,context,False))),\
             patch.object(ej,'publisher_cookie',new=AsyncMock(return_value='publisher=synthetic')) as cookies:
            self.assertEqual('publisher=synthetic',await ej.browser_cookie(args))
        cookies.assert_awaited_once_with(context);page.close.assert_awaited_once()
        context.close.assert_not_awaited();browser.close.assert_not_awaited()

    async def test_http200_challenge_is_not_a_healthy_cookie_handoff(self):
        args,page,context,browser,manager=self.browser(title='Request Rejected')
        with patch('playwright.async_api.async_playwright',return_value=manager),\
             patch.object(ej,'connect_debug_chrome',new=AsyncMock(return_value=(browser,context,False))),\
             patch.object(ej,'publisher_cookie',new=AsyncMock()) as cookies, self.assertLogs(level='WARNING'):
            with self.assertRaises(ej.PublisherBootstrapError) as caught:await ej.browser_cookie(args)
        self.assertEqual('access_challenge',caught.exception.diagnostics['reason']);cookies.assert_not_awaited()

    async def test_navigation_exception_does_not_log_raw_exception_secrets(self):
        args,page,context,browser,manager=self.browser()
        page.goto.side_effect=TimeoutError('private-exception-secret')
        with patch('playwright.async_api.async_playwright',return_value=manager),\
             patch.object(ej,'connect_debug_chrome',new=AsyncMock(return_value=(browser,context,False))),\
             self.assertLogs(level='WARNING') as logs:
            with self.assertRaises(ej.PublisherBootstrapError) as caught:await ej.browser_cookie(args)
        self.assertEqual('TimeoutError',caught.exception.diagnostics['exceptionType'])
        self.assertNotIn('private-exception-secret',' '.join(logs.output))

    async def test_invalid_bootstrap_host_rejected_before_browser_access(self):
        with patch.object(ej,'connect_debug_chrome',new=AsyncMock()) as connect:
            for url in ['https://attacker.invalid/','http://www.edwardjones.com/','https://user@www.edwardjones.com/']:
                with self.assertRaises(ValueError):await ej.browser_cookie(SimpleNamespace(bootstrap_url=url))
            connect.assert_not_awaited()

    async def test_access_denials_are_not_transient_request_retries(self):
        for status in [401,403,429]:
            response=httpx.Response(status,request=httpx.Request('GET','https://www.edwardjones.com/'))
            client=SimpleNamespace(get=AsyncMock(return_value=response))
            limiter=SimpleNamespace(wait=AsyncMock(),penalize=AsyncMock(),cap_rate=AsyncMock())
            self.assertIs(response,await ej.request_with_retries(client,'https://www.edwardjones.com/',limiter,4))
            client.get.assert_awaited_once();limiter.penalize.assert_not_awaited();limiter.cap_rate.assert_not_awaited()

    async def test_server_error_still_has_bounded_backoff_retry(self):
        url='https://www.edwardjones.com/'
        client=SimpleNamespace(get=AsyncMock(side_effect=[httpx.Response(503,request=httpx.Request('GET',url)),httpx.Response(200,request=httpx.Request('GET',url))]))
        limiter=SimpleNamespace(wait=AsyncMock(),penalize=AsyncMock())
        with patch.object(ej,'retry_delay',return_value=0),self.assertLogs(level='WARNING'):
            response=await ej.request_with_retries(client,url,limiter,1)
        self.assertEqual(200,response.status_code);self.assertEqual(2,client.get.await_count);limiter.penalize.assert_awaited_once()

    def args(self,root):
        return SimpleNamespace(url_field=None,resume=True,checkpoint=root/'checkpoint.jsonl',
            cookie='publisher=synthetic',cookie_file=None,concurrency=1,rate=100000,timeout=2,
            retries=4,progress_every=100,output=root/'roster.csv')

    async def test_first_profile_denial_checkpoints_and_preserves_existing_roster(self):
        for status in [401,403,429]:
            with self.subTest(status=status),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);args=self.args(root);args.output.write_text('last good')
                records=[{'faName':'Alice Smith','faUrl':'/us-en/financial-advisor/person-'+str(i)} for i in range(8)]
                client=AsyncMock();client.get.return_value=httpx.Response(status,headers={'Retry-After':'600'},request=httpx.Request('GET','https://www.edwardjones.com/'))
                manager=AsyncMock();manager.__aenter__.return_value=client
                with patch.object(ej.httpx,'AsyncClient',return_value=manager),\
                     patch.object(ej,'browser_cookie',new=AsyncMock()) as bootstrap,contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SourceUnhealthy):await ej.enrich(args,records)
                client.get.assert_awaited_once();bootstrap.assert_not_awaited()
                self.assertEqual('last good',args.output.read_text())
                checkpoint=[json.loads(line) for line in args.checkpoint.read_text().splitlines()]
                self.assertEqual(2,len(checkpoint));self.assertEqual(status,checkpoint[-1]['http_status'])
                self.assertEqual('600',checkpoint[-1]['retry_after']);self.assertIn('observed_utc',checkpoint[-1])

    async def test_successful_enrichment_retains_response_hash_and_observation_time(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);args=self.args(root)
            url='https://www.edwardjones.com/us-en/financial-advisor/alice-smith'
            body=b'''<html><title>Alice Smith</title><input name="field_email_override" value='["alice.smith@edwardjones.com"]'></html>'''
            client=AsyncMock();client.get.return_value=httpx.Response(200,content=body,request=httpx.Request('GET',url))
            manager=AsyncMock();manager.__aenter__.return_value=client
            with patch.object(ej.httpx,'AsyncClient',return_value=manager),contextlib.redirect_stdout(io.StringIO()):
                await ej.enrich(args,[{'faName':'Alice Smith','faUrl':url}])
            self.assertTrue(args.output.is_file());self.assertFalse(args.checkpoint.exists())
            archives=list(root.glob('checkpoint.jsonl.completed-*.jsonl'));self.assertEqual(1,len(archives))
            observations=[json.loads(line) for line in archives[0].read_text(encoding='utf8').splitlines()]
            self.assertIn('_meta',observations[0]);row=observations[-1]
            self.assertEqual(ej.hashlib.sha256(body).hexdigest(),row['source_body_sha256'])
            self.assertTrue(row['observed_utc']);self.assertEqual(['alice.smith@edwardjones.com'],row['emails'])

    async def test_denied_checkpoint_cannot_automatically_bootstrap_or_request(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);args=self.args(root)
            url='https://www.edwardjones.com/us-en/financial-advisor/person'
            fingerprint=ej.stable_fingerprint({'urls':[url],'extractor_version':3})
            args.checkpoint.write_text(json.dumps({'_meta':{'fingerprint':fingerprint}})+'\n'+json.dumps({'profile_url':url,'status':'http_error','http_status':429})+'\n')
            before=args.checkpoint.read_bytes()
            with patch.object(ej.httpx,'AsyncClient') as client,patch.object(ej,'browser_cookie',new=AsyncMock()) as bootstrap:
                for resume in [True,False]:
                    args.resume=resume
                    with self.assertRaisesRegex(SourceUnhealthy,'operator review'):await ej.enrich(args,[{'faName':'Alice Smith','faUrl':url}])
            client.assert_not_called();bootstrap.assert_not_awaited();self.assertEqual(before,args.checkpoint.read_bytes())

    async def test_full_run_checks_prior_denial_before_discovery(self):
        with tempfile.TemporaryDirectory() as folder:
            checkpoint=Path(folder)/'checkpoint.jsonl'
            checkpoint.write_text(json.dumps({'profile_url':'https://www.edwardjones.com/example','status':'http_error','http_status':429})+'\n')
            with patch.object(ej,'discover_in_browser',new=AsyncMock()) as discovery:
                with self.assertRaisesRegex(SourceUnhealthy,'operator review'):
                    await ej.run_all(SimpleNamespace(checkpoint=checkpoint))
            discovery.assert_not_awaited()

    async def test_explicit_review_does_not_disable_new_denial_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);args=self.args(root);args.reviewed_access_resume=True
            args.output.write_text('last good')
            url='https://www.edwardjones.com/us-en/financial-advisor/person'
            fingerprint=ej.stable_fingerprint({'urls':[url],'extractor_version':3})
            args.checkpoint.write_text(json.dumps({'_meta':{'fingerprint':fingerprint}})+'\n'+json.dumps({'profile_url':url,'status':'http_error','http_status':429})+'\n')
            client=AsyncMock();client.get.return_value=httpx.Response(403,request=httpx.Request('GET',url))
            manager=AsyncMock();manager.__aenter__.return_value=client
            with patch.object(ej.httpx,'AsyncClient',return_value=manager),self.assertLogs(level='WARNING') as logs,contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SourceUnhealthy):await ej.enrich(args,[{'faName':'Alice Smith','faUrl':url}])
            client.get.assert_awaited_once();self.assertEqual('last good',args.output.read_text())
            self.assertNotIn('publisher=synthetic',' '.join(logs.output))

    def test_retry_after_header_is_canonicalized_not_logged_verbatim(self):
        self.assertEqual('120',ej.safe_retry_after(SimpleNamespace(headers={'Retry-After':'120'})))
        self.assertEqual('2015-10-21T07:28:00+00:00',ej.safe_retry_after(SimpleNamespace(headers={'retry-after':'Wed, 21 Oct 2015 07:28:00 GMT'})))
        self.assertEqual('',ej.safe_retry_after(SimpleNamespace(headers={'retry-after':'private-header-secret'})))

    def test_operator_review_cannot_shorten_publisher_retry_after(self):
        now=datetime.now(timezone.utc)
        for value in ['3600',(now+timedelta(hours=1)).isoformat()]:
            result={'http_status':429,'retry_after':value,'observed_utc':now.isoformat()}
            with self.assertRaisesRegex(SourceUnhealthy,'wait is still active'):
                ej.check_access_resume(SimpleNamespace(reviewed_access_resume=True),{'https://www.edwardjones.com/example':result})


if __name__=='__main__':unittest.main()
