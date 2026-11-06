import asyncio
import contextlib
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from scrape_runtime import AcquisitionProgress,SourceUnhealthy
import edward_jones_async as ej
import wells_fargo_async as wells
import raymond_james_async as rj
import httpx


class ProgressTests(unittest.TestCase):
    def test_systemic_errors_stop_but_isolated_departures_do_not(self):
        tracker=AcquisitionProgress()
        for _ in range(4):tracker.observe('http_error',400)
        with self.assertRaisesRegex(SourceUnhealthy,'preflight'):tracker.observe('http_error',400)
        tracker=AcquisitionProgress()
        for status in ['ok']*19+['not_found']+['ok']*30:tracker.observe(status)
        self.assertEqual(49,tracker.summary()['usable'])


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_cookie_scope_and_defensive_domain_filter(self):
        context=SimpleNamespace(cookies=AsyncMock(return_value=[
            {'domain':'.edwardjones.com','name':'publisher','value':'synthetic'},
            {'domain':'.other.example','name':'private','value':'must-not-transfer'},
            {'domain':'edwardjones.com.attacker.example','name':'fake','value':'must-not-transfer'}]))
        self.assertEqual('publisher=synthetic',await ej.publisher_cookie(context))
        self.assertTrue(all(url.startswith('https://www.edwardjones.com/') for url in context.cookies.call_args.args[0]))

    async def test_enrichment_failure_preserves_roster_and_retains_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);output=root/'roster.csv';output.write_text('last good')
            args=SimpleNamespace(url_field=None,resume=True,checkpoint=root/'checkpoint.jsonl',
                cookie=None,cookie_file=None,concurrency=1,rate=100000,timeout=2,
                retries=0,progress_every=100,output=output)
            records=[{'faName':'Alice Smith','faUrl':'/us-en/financial-advisor/person-'+str(i)} for i in range(8)]
            client=AsyncMock();client.get.return_value=httpx.Response(400,request=httpx.Request('GET','https://www.edwardjones.com/'))
            manager=AsyncMock();manager.__aenter__.return_value=client
            with patch.object(ej.httpx,'AsyncClient',return_value=manager), \
                    patch.object(ej,'browser_cookie',new=AsyncMock(return_value='publisher=synthetic')) as bootstrap, \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(SourceUnhealthy,'preflight'):await ej.enrich(args,records)
            bootstrap.assert_awaited_once()
            self.assertEqual(5,client.get.await_count)
            self.assertEqual('last good',output.read_text())
            self.assertEqual(6,len(args.checkpoint.read_text().splitlines()))

    async def test_profile_host_is_validated_before_any_cookie_can_be_sent(self):
        args=SimpleNamespace(url_field=None)
        with self.assertRaisesRegex(ValueError,'publisher host'):
            await ej.enrich(args,[{'faUrl':'https://attacker.example/profile'}])

    async def test_edward_jones_cooldown_does_not_hold_coordination_lock(self):
        limiter=ej.RateLimiter(1)
        limiter.next_start=ej.time.monotonic()+3600
        entered=asyncio.Event();release=asyncio.Event()
        async def sleeping(delay):
            self.assertFalse(limiter.lock.locked())
            entered.set();await release.wait()
        with patch.object(ej.asyncio,'sleep',side_effect=sleeping):
            task=asyncio.create_task(limiter.wait())
            try:
                await asyncio.wait_for(entered.wait(),timeout=1)
                await asyncio.wait_for(limiter.penalize(120),timeout=1)
                await asyncio.wait_for(limiter.cap_rate(.5),timeout=1)
                self.assertEqual(2,limiter.interval)
            finally:
                task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_wells_sitemap_timeout_retries_and_succeeds(self):
        response=SimpleNamespace(status=200,raise_for_status=Mock(),text=AsyncMock(return_value='<urlset/>'))
        failed=AsyncMock();failed.__aenter__.side_effect=asyncio.TimeoutError()
        success=AsyncMock();success.__aenter__.return_value=response
        session=SimpleNamespace(get=Mock(side_effect=[failed,success]))
        with patch.object(wells.asyncio,'sleep',new=AsyncMock()):
            self.assertEqual('<urlset/>',await wells.fetch_sitemap(session,'https://example.invalid/sitemap',{}))
        self.assertEqual(2,session.get.call_count)

    async def test_raymond_james_uses_owned_page_not_native_tab(self):
        page=SimpleNamespace(goto=AsyncMock(),close=AsyncMock(),
            locator=Mock(return_value=SimpleNamespace(first=SimpleNamespace(wait_for=AsyncMock()))))
        context=SimpleNamespace(new_page=AsyncMock(return_value=page))
        with patch.object(rj,'attach_page_diagnostics'),patch.object(rj,'page_shows_access_denied',new=AsyncMock(return_value=False)), \
                patch.object(rj,'open_locator_tab_natively') as native:
            self.assertIs(page,await rj.open_owned_locator(context));native.assert_not_called()
            self.assertEqual(rj.LOCATOR_URL,page.goto.call_args.args[0])
        with patch.object(rj,'attach_page_diagnostics'),patch.object(rj,'page_shows_access_denied',new=AsyncMock(return_value=True)):
            with self.assertRaises(rj.RaymondJamesBlockedError):await rj.open_owned_locator(context)
        page.close.assert_awaited_once()


if __name__=='__main__':unittest.main()
