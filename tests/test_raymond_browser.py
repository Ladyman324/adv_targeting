import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch, call
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import raymond_browser as rb
REAL_VERIFIER = rb.verify_endpoint_profile


def consent_page(async_mode=True, banner_count=1, banner_visible=True, button_count=1):
    factory = AsyncMock if async_mode else Mock
    button = Mock(count=factory(return_value=button_count), is_visible=factory(return_value=True),
                  is_enabled=factory(return_value=True), click=factory())
    banner = Mock(is_visible=factory(return_value=banner_visible), wait_for=factory(),
                  locator=Mock(return_value=button))
    banners = Mock(count=factory(return_value=banner_count), nth=Mock(return_value=banner))
    input_box = Mock(count=factory(return_value=1))
    input_box.first = Mock(is_visible=factory(return_value=True))
    def locate(selector):
        if selector == rb.CONSENT_BANNER_SELECTOR: return banners
        if selector == '#zip-v2': return input_box
        raise AssertionError('Unexpected selector: ' + selector)
    page = Mock(url=rb.LOCATOR_URL, locator=Mock(side_effect=locate), evaluate=factory(return_value={
        'documentReady': True, 'mainContentPresent': True, 'autocompleteInitialized': True,
        'untrustedExtra': 'must-not-be-returned'}))
    return page, banner, button


class RaymondBrowserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        verifier = patch.object(rb, 'verify_endpoint_profile', return_value=123)
        verifier.start()
        self.addCleanup(verifier.stop)

    def test_loopback_endpoint_validation(self):
        for value in ['http://example.com:9222', 'https://127.0.0.1:9222', 'http://localhost',
                      'http://user@localhost:9222', 'http://localhost:9222/secret', 'http://localhost:9222?token=x']:
            with self.subTest(value=value), self.assertRaises(ValueError): rb.validate_endpoint(value)
        self.assertEqual('http://127.0.0.1:9222', rb.validate_endpoint('http://127.0.0.1:9222/'))

    def test_dedicated_profile_validation(self):
        for value in ['C:\\', r'..\profile', r'C:\x\..\other', r'\\server\share\profile',
                      r'C:\Users\someone\User Data', r'C:\Users\someone\Default']:
            with self.subTest(value=value), self.assertRaises(ValueError): rb.validate_profile(value)
        self.assertEqual(r'C:\SeleniumChrome', rb.validate_profile(r'C:\SeleniumChrome'))

    def test_allowlisted_pages(self):
        rb.validate_raymond_url('https://www.raymondjames.com/')
        rb.validate_raymond_url('https://www.raymondjames.com')
        rb.validate_raymond_url(rb.LOCATOR_URL)
        rb.validate_raymond_url('https://www.raymondjames.com/bethesda-branch/about-us/our-team')
        for value in ['https://evil.example/find-an-advisor', 'http://www.raymondjames.com/find-an-advisor',
                      'https://www.raymondjames.com/dotcom/api/getplaceautocomplete/',
                      rb.LOCATOR_URL+'?q=x', rb.LOCATOR_URL+'#x', 'https://user@www.raymondjames.com/find-an-advisor',
                      'https://www.raymondjames.com/?q=x', 'https://www.raymondjames.com/unapproved-page']:
            with self.subTest(value=value), self.assertRaises(ValueError): rb.validate_raymond_url(value)

    def test_native_open_exact_profile_no_shell(self):
        with patch.object(rb.subprocess, 'Popen') as popen:
            rb.open_native_tab(chrome_path=rb.CHROME_PATH, profile_path=r'C:\SeleniumChrome', url=rb.LOCATOR_URL)
        args, kwargs = popen.call_args
        self.assertEqual([rb.CHROME_PATH, r'--user-data-dir=C:\SeleniumChrome', '--new-tab', rb.LOCATOR_URL], args[0])
        self.assertNotIn('shell', kwargs)

    async def test_only_new_tab_owned_and_cookies_retained(self):
        old = Mock(url=rb.LOCATOR_URL, is_closed=Mock(return_value=False))
        new = Mock(url=rb.LOCATOR_URL, is_closed=Mock(return_value=False), bring_to_front=AsyncMock(), close=AsyncMock())
        context = SimpleNamespace(pages=[old], cookies=AsyncMock(), clear_cookies=AsyncMock(), close=AsyncMock())
        browser = SimpleNamespace(contexts=[context], new_context=AsyncMock(), close=AsyncMock())
        def opener(**kwargs): context.pages.append(new)
        async with rb.native_raymond_session(browser, profile_path=r'C:\SeleniumChrome', opener=opener) as session:
            self.assertIs(new, session.page)
            self.assertIs(context, session.context)
        new.close.assert_awaited_once()
        old.close.assert_not_called()
        context.cookies.assert_not_awaited()
        context.clear_cookies.assert_not_awaited()
        context.close.assert_not_awaited()
        browser.new_context.assert_not_awaited()
        browser.close.assert_not_awaited()

    async def test_cleanup_on_collection_error(self):
        new = Mock(url=rb.LOCATOR_URL, is_closed=Mock(return_value=False), bring_to_front=AsyncMock(), close=AsyncMock())
        context = SimpleNamespace(pages=[])
        def opener(**kwargs): context.pages.append(new)
        with self.assertRaises(RuntimeError):
            async with rb.native_raymond_session(SimpleNamespace(contexts=[context]), profile_path=r'C:\SeleniumChrome', opener=opener):
                raise RuntimeError('synthetic failure')
        new.close.assert_awaited_once()

    async def test_ambiguous_tabs_not_selected_or_closed(self):
        pages = [Mock(url=rb.LOCATOR_URL, is_closed=Mock(return_value=False), close=AsyncMock()) for _ in range(2)]
        context = SimpleNamespace(pages=[])
        def opener(**kwargs): context.pages.extend(pages)
        with self.assertRaisesRegex(RuntimeError, 'ambiguous'):
            async with rb.native_raymond_session(SimpleNamespace(contexts=[context]), profile_path=r'C:\SeleniumChrome', opener=opener):
                self.fail('must not yield')
        for page in pages: page.close.assert_not_awaited()

    async def test_timeout_never_borrows_old_tab(self):
        old = Mock(url=rb.LOCATOR_URL, is_closed=Mock(return_value=False), close=AsyncMock())
        browser = SimpleNamespace(contexts=[SimpleNamespace(pages=[old])])
        with self.assertRaises(TimeoutError):
            async with rb.native_raymond_session(browser, profile_path=r'C:\SeleniumChrome', opener=Mock(), timeout_seconds=.001, poll_seconds=.001):
                self.fail('must not yield')
        old.close.assert_not_awaited()

    async def test_cookie_clear_is_opt_in_exact_and_scoped(self):
        context = SimpleNamespace(cookies=AsyncMock(return_value=[
            {'domain':'.raymondjames.com','path':'/','name':'rj','value':'secret'},
            {'domain':'www.raymondjames.com','path':'/dotcom','name':'api','value':'secret'},
            {'domain':'sub.raymondjames.com','path':'/x','name':'sub','value':'secret'},
            {'domain':'evilraymondjames.com','path':'/','name':'evil','value':'secret'},
            {'domain':'.example.com','path':'/','name':'other','value':'secret'},
        ]), clear_cookies=AsyncMock())
        self.assertEqual(3, await rb.clear_raymond_cookies(context))
        context.cookies.assert_awaited_once_with()
        self.assertEqual([call(name='rj',domain='.raymondjames.com',path='/'),
                          call(name='api',domain='www.raymondjames.com',path='/dotcom'),
                          call(name='sub',domain='sub.raymondjames.com',path='/x')], context.clear_cookies.await_args_list)

    async def test_no_unfiltered_fallback_on_old_playwright(self):
        context = SimpleNamespace(cookies=AsyncMock(return_value=[{'domain':'.raymondjames.com','path':'/','name':'rj'}]),
                                  clear_cookies=AsyncMock(side_effect=TypeError('unsupported filters')))
        with self.assertRaises(TypeError): await rb.clear_raymond_cookies(context)
        context.clear_cookies.assert_awaited_once_with(name='rj',domain='.raymondjames.com',path='/')

    async def test_profile_mismatch_prevents_cookie_clear_and_open(self):
        context = SimpleNamespace(pages=[], cookies=AsyncMock(), clear_cookies=AsyncMock())
        opener = Mock()
        with self.assertRaises(RuntimeError):
            async with rb.native_raymond_session(SimpleNamespace(contexts=[context]),
                    profile_path=r'C:\SeleniumChrome', clear_cookies=True, opener=opener,
                    profile_verifier=Mock(side_effect=RuntimeError('mismatch'))):
                self.fail('must not yield')
        opener.assert_not_called()
        context.cookies.assert_not_called()

    async def test_denial_redirect_returned_with_passive_status(self):
        new = Mock(url='https://errors.edgesuite.net/opaque', is_closed=Mock(return_value=False),
                   bring_to_front=AsyncMock(), close=AsyncMock())
        listeners = {}
        context = SimpleNamespace(pages=[], on=lambda event, callback:listeners.update({event:callback}),
                                  remove_listener=Mock())
        def opener(**kwargs):
            context.pages.append(new)
            new.main_frame = SimpleNamespace(page=new)
            request = SimpleNamespace(frame=new.main_frame, is_navigation_request=lambda:True)
            listeners['response'](SimpleNamespace(request=request, url=new.url, status=403, headers={'retry-after':'120'}))
        async with rb.native_raymond_session(SimpleNamespace(contexts=[context]), profile_path=r'C:\SeleniumChrome', opener=opener) as session:
            self.assertIs(new, session.page)
            self.assertEqual(403, session.navigation_status)
            self.assertEqual('120', session.retry_after)
        context.remove_listener.assert_called_once()
        new.close.assert_awaited_once()

    def test_sync_native_session_ownership_and_status(self):
        old = Mock(url='https://example.com', is_closed=Mock(return_value=False))
        new = Mock(url=rb.LOCATOR_URL, is_closed=Mock(return_value=False))
        listeners = {}
        context = SimpleNamespace(pages=[old], on=lambda event, callback:listeners.update({event:callback}),
                                  remove_listener=Mock(), clear_cookies=Mock())
        def opener(**kwargs):
            context.pages.append(new)
            new.main_frame = SimpleNamespace(page=new)
            request = SimpleNamespace(frame=new.main_frame, is_navigation_request=lambda:True)
            listeners['response'](SimpleNamespace(request=request, url=new.url, status=200, headers={}))
        with rb.native_raymond_session_sync(SimpleNamespace(contexts=[context]), profile_path=r'C:\SeleniumChrome', opener=opener) as session:
            self.assertEqual(200, session.navigation_status)
            self.assertIs(new, session.page)
        new.close.assert_called_once()
        old.close.assert_not_called()
        context.clear_cookies.assert_not_called()

    def test_profile_mapping_checks_listener_and_exact_command_profile(self):
        import json
        record = {'ProcessId':19548, 'Name':'chrome.exe',
                  'CommandLine':'"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=9222 --user-data-dir=C:\\SeleniumChrome'}
        runner = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps(record)))
        self.assertEqual(19548, REAL_VERIFIER('http://127.0.0.1:9222', r'C:\SeleniumChrome', runner=runner))
        self.assertIn('9222', runner.call_args[0][0][-1])
        with self.assertRaises(RuntimeError):
            REAL_VERIFIER('http://127.0.0.1:9222', r'C:\DifferentProfile', runner=runner)
        record['Name'] = 'other.exe'
        runner.return_value.stdout = json.dumps(record)
        with self.assertRaises(RuntimeError): REAL_VERIFIER('http://127.0.0.1:9222', r'C:\SeleniumChrome', runner=runner)

    def test_quoted_profile_with_spaces_and_ambiguous_listener_fail_closed(self):
        import json
        self.assertEqual(r'C:\My RJ Profile', rb._command_profile('chrome.exe "--user-data-dir=C:\\My RJ Profile" --remote-debugging-port=9222'))
        runner = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps([{}, {}])))
        with self.assertRaises(RuntimeError): REAL_VERIFIER('http://127.0.0.1:9222', r'C:\SeleniumChrome', runner=runner)

    async def test_async_navigation_metadata_updates_after_session_yields(self):
        new = Mock(url=rb.LOCATOR_URL, is_closed=Mock(return_value=False),
                   bring_to_front=AsyncMock(), close=AsyncMock())
        listeners = {}
        context = SimpleNamespace(pages=[], on=lambda event, callback:listeners.update({event:callback}),
                                  remove_listener=Mock())
        def opener(**kwargs): context.pages.append(new)
        async with rb.native_raymond_session(SimpleNamespace(contexts=[context]), profile_path=r'C:\SeleniumChrome', opener=opener) as session:
            self.assertIsNone(session.navigation_status)
            new.main_frame = SimpleNamespace(page=new)
            request = SimpleNamespace(frame=new.main_frame, is_navigation_request=lambda:True)
            listeners['response'](SimpleNamespace(request=request, url=new.url, status=403, headers={'retry-after':'90'}))
            self.assertEqual(403, session.navigation_status)
            self.assertEqual('90', session.retry_after)
        self.assertEqual(403, session.navigation_status)

    def test_sync_navigation_metadata_updates_after_session_yields(self):
        new = Mock(url=rb.LOCATOR_URL, is_closed=Mock(return_value=False))
        listeners = {}
        context = SimpleNamespace(pages=[], on=lambda event, callback:listeners.update({event:callback}),
                                  remove_listener=Mock())
        def opener(**kwargs): context.pages.append(new)
        with rb.native_raymond_session_sync(SimpleNamespace(contexts=[context]), profile_path=r'C:\SeleniumChrome', opener=opener) as session:
            self.assertIsNone(session.navigation_status)
            new.main_frame = SimpleNamespace(page=new)
            request = SimpleNamespace(frame=new.main_frame, is_navigation_request=lambda:True)
            listeners['response'](SimpleNamespace(request=request, url=new.url, status=200, headers={}))
            self.assertEqual(200, session.navigation_status)
            self.assertIsNone(session.retry_after)

    def test_sync_cookie_cleanup_covers_path_and_subdomain_but_not_unrelated(self):
        context = SimpleNamespace(cookies=Mock(return_value=[
            {'domain':'.raymondjames.com','path':'/dotcom','name':'api','value':'secret'},
            {'domain':'sub.raymondjames.com','path':'/nested','name':'sub','value':'secret'},
            {'domain':'.notraymondjames.com','path':'/','name':'other','value':'secret'},
        ]), clear_cookies=Mock())
        self.assertEqual(2, rb.clear_raymond_cookies_sync(context))
        context.cookies.assert_called_once_with()
        self.assertEqual([call(name='api',domain='.raymondjames.com',path='/dotcom'),
                          call(name='sub',domain='sub.raymondjames.com',path='/nested')], context.clear_cookies.call_args_list)

    def test_iframe_navigation_never_overrides_main_document_status(self):
        page = Mock()
        page.main_frame = SimpleNamespace(page=page, kind='main')
        iframe = SimpleNamespace(page=page, kind='iframe')
        context = SimpleNamespace(on=Mock())
        observations, observer = rb._install_navigation_observer(context, set())
        def response(frame, status):
            return SimpleNamespace(request=SimpleNamespace(frame=frame, is_navigation_request=lambda:True),
                                   url=rb.LOCATOR_URL, status=status, headers={'retry-after':'90'})
        observer(response(iframe, 429))
        self.assertEqual({}, observations)
        observer(response(page.main_frame, 200))
        self.assertEqual((200, '90'), observations[id(page)])
        observer(response(iframe, 403))
        self.assertEqual((200, '90'), observations[id(page)])

    async def test_missing_native_response_recovered_from_navigation_timing(self):
        page = Mock(url=rb.LOCATOR_URL, evaluate=AsyncMock(return_value={
            'name':rb.LOCATOR_URL, 'currentUrl':rb.LOCATOR_URL, 'responseStatus':403}))
        session = rb.OwnedRaymondSession(None, page, 'http://127.0.0.1:9222', r'C:\SeleniumChrome', _observations={})
        self.assertEqual(403, await session.refresh_navigation_metadata())
        self.assertEqual('navigation_timing', session.navigation_status_source)
        self.assertIsNone(session.retry_after)
        page.evaluate.assert_awaited_once_with(rb.NAVIGATION_TIMING_SCRIPT)
        page.url = 'https://www.raymondjames.com/another-page'
        self.assertIsNone(session.navigation_status)
        self.assertIsNone(session.navigation_status_source)

    def test_sync_navigation_timing_missing_event_fallback(self):
        page = Mock(url=rb.LOCATOR_URL, evaluate=Mock(return_value={
            'name':rb.LOCATOR_URL, 'currentUrl':rb.LOCATOR_URL, 'responseStatus':200}))
        session = rb.OwnedRaymondSessionSync(None, page, 'http://127.0.0.1:9222', r'C:\SeleniumChrome', _observations={})
        self.assertEqual(200, session.refresh_navigation_metadata())
        self.assertEqual('navigation_timing', session.navigation_status_source)
        page.evaluate.assert_called_once_with(rb.NAVIGATION_TIMING_SCRIPT)

    async def test_timing_zero_absent_invalid_or_wrong_document_remains_unknown(self):
        timings = [None, {}, {'responseStatus':403},
                   {'name':'https://elsewhere.example', 'currentUrl':rb.LOCATOR_URL, 'responseStatus':403},
                   {'name':rb.LOCATOR_URL, 'currentUrl':'https://elsewhere.example', 'responseStatus':403}]
        timings.extend({'name':rb.LOCATOR_URL,'currentUrl':rb.LOCATOR_URL,'responseStatus':s}
                       for s in [None, 0, 99, 600, '200', True, 200.5])
        for timing in timings:
            with self.subTest(timing=timing):
                page = Mock(url=rb.LOCATOR_URL, evaluate=AsyncMock(return_value=timing))
                session = rb.OwnedRaymondSession(None, page, 'http://127.0.0.1:9222', r'C:\SeleniumChrome', _observations={})
                self.assertIsNone(await session.refresh_navigation_metadata())
                self.assertIsNone(session.navigation_status_source)

    async def test_response_event_is_preferred_to_navigation_timing(self):
        page = Mock(url=rb.LOCATOR_URL, evaluate=AsyncMock())
        observations = {id(page):(429,'120')}
        session = rb.OwnedRaymondSession(None, page, 'http://127.0.0.1:9222', r'C:\SeleniumChrome', _observations=observations)
        self.assertEqual(429, await session.refresh_navigation_metadata())
        self.assertEqual('response_event', session.navigation_status_source)
        self.assertEqual('120', session.retry_after)
        page.evaluate.assert_not_awaited()

    async def test_timing_evaluation_failure_preserves_unknown(self):
        page = Mock(url=rb.LOCATOR_URL, evaluate=AsyncMock(side_effect=RuntimeError('target closed')))
        session = rb.OwnedRaymondSession(None, page, 'http://127.0.0.1:9222', r'C:\SeleniumChrome', _observations={})
        self.assertIsNone(await session.refresh_navigation_metadata())

    async def test_default_consent_observes_banner_without_any_click(self):
        page, banner, button = consent_page()
        result = await rb.observe_raymond_readiness(page)
        self.assertEqual('operator', result['choiceRequested'])
        self.assertTrue(result['bannerVisible'])
        self.assertTrue(result['bannerVisibleAfter'])
        self.assertIsNone(result['choiceApplied'])
        self.assertTrue(result['ready'])
        self.assertTrue(result['markers']['autocompleteInitialized'])
        button.click.assert_not_called()
        banner.locator.assert_not_called()
        self.assertNotIn('untrustedExtra', result['markers'])

    async def test_explicit_decline_uses_only_exact_provider_button_and_waits_hidden(self):
        page, banner, button = consent_page()
        result = await rb.observe_raymond_readiness(page, consent_choice='decline', timeout_ms=5000)
        self.assertTrue(result['bannerVisible'])
        self.assertFalse(result['bannerVisibleAfter'])
        self.assertEqual('decline', result['choiceApplied'])
        self.assertTrue(result['ready'])
        banner.locator.assert_called_once_with('button.cookie-consent-banner-deny')
        button.click.assert_awaited_once_with(timeout=5000)
        banner.wait_for.assert_awaited_once_with(state='hidden', timeout=5000)

    async def test_missing_or_hidden_banner_requires_no_choice(self):
        for count, visible in [(0, False), (1, False)]:
            page, banner, button = consent_page(banner_count=count, banner_visible=visible)
            result = await rb.observe_raymond_readiness(page, consent_choice='decline')
            self.assertTrue(result['ready'])
            self.assertFalse(result['bannerVisible'])
            self.assertIsNone(result['choiceApplied'])
            button.click.assert_not_called()

    async def test_unsupported_consent_and_foreign_page_never_clicked(self):
        page, banner, button = consent_page()
        for choice in ['accept', 'dismiss', '', None]:
            with self.assertRaises(ValueError): await rb.observe_raymond_readiness(page, consent_choice=choice)
        page.url = 'https://errors.edgesuite.net/denial'
        result = await rb.observe_raymond_readiness(page, consent_choice='decline')
        self.assertFalse(result['ready'])
        self.assertFalse(result['markers']['providerPage'])
        page.locator.assert_not_called()
        button.click.assert_not_called()

    async def test_ambiguous_banner_or_decline_button_never_clicked(self):
        for wrappers, buttons in [(2, 1), (1, 2), (1, 0)]:
            page, banner, button = consent_page(banner_count=wrappers, button_count=buttons)
            with self.assertRaises(RuntimeError): await rb.observe_raymond_readiness(page, consent_choice='decline')
            button.click.assert_not_called()

    async def test_unconfirmed_decline_is_not_reported_as_ready(self):
        page, banner, button = consent_page()
        banner.wait_for.side_effect = TimeoutError('banner remains visible')
        with self.assertRaises(TimeoutError): await rb.observe_raymond_readiness(page, consent_choice='decline')
        button.click.assert_awaited_once()

    def test_sync_consent_observation_and_explicit_decline(self):
        for choice in ['operator', 'decline']:
            page, banner, button = consent_page(async_mode=False)
            result = rb.observe_raymond_readiness_sync(page, consent_choice=choice)
            self.assertTrue(result['ready'])
            if choice == 'operator': button.click.assert_not_called()
            else:
                button.click.assert_called_once_with(timeout=10000)
                banner.wait_for.assert_called_once_with(state='hidden', timeout=10000)


if __name__ == '__main__': unittest.main()
