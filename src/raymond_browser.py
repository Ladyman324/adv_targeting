"""Normal-tab RJ sessions. No incognito contexts, HTTP replay or identity changes.

Caller verifies that endpoint and explicit dedicated profile identify the same
Chrome. Only a newly opened tab is owned. Explicitly borrowed tabs stay open;
their cookies and existing page state are retained.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import PureWindowsPath
import re
import json
import subprocess
import time
from urllib.parse import urlsplit

LOCATOR_URL = 'https://www.raymondjames.com/find-an-advisor'
CHROME_PATH = r'C:\Program Files\Google\Chrome\Application\chrome.exe'
# NavigationTiming inherits ResourceTiming.responseStatus. This read-only
# fallback makes no request and leaves unsupported/opaque status 0 unknown.
# https://www.w3.org/TR/navigation-timing-2/
# https://www.w3.org/TR/resource-timing/
NAVIGATION_TIMING_SCRIPT = """() => {
    const entries = performance.getEntriesByType('navigation');
    const entry = entries.length ? entries[entries.length - 1] : null;
    return entry ? {name: entry.name, responseStatus: entry.responseStatus,
                    currentUrl: location.href} : null;
}"""
CONSENT_BANNER_SELECTOR = '.cookie-consent-banner-wrapper'
CONSENT_DECLINE_SELECTOR = 'button.cookie-consent-banner-deny'
READINESS_SCRIPT = """() => {
    const input = document.querySelector('#zip-v2');
    const controls = input && input.getAttribute('aria-controls');
    return {documentReady: ['interactive', 'complete'].includes(document.readyState),
            mainContentPresent: Boolean(document.body),
            autocompleteInitialized: Boolean(controls && document.getElementById(controls))};
}"""


def _readiness_result(page, choice, timeout_ms):
    if choice not in {'operator', 'decline'}:
        raise ValueError('Consent choice must be operator or explicitly requested decline')
    if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or not 1 <= timeout_ms <= 30000:
        raise ValueError('Consent readiness timeout must be 1..30000 milliseconds')
    p = urlsplit(page.url)
    provider_page = (p.scheme == 'https' and p.hostname == 'www.raymondjames.com'
                     and p.port in {None, 443} and not p.username and not p.password)
    return {'provider': 'raymond_james', 'bannerVisible': None, 'bannerVisibleAfter': None,
            'choiceRequested': choice, 'choiceApplied': None, 'ready': False,
            'markers': {'providerPage': provider_page, 'documentReady': False,
                        'mainContentPresent': False, 'locatorInputVisible': False,
                        'autocompleteInitialized': False}}


def _complete_readiness(result, raw_markers):
    for key in ('documentReady', 'mainContentPresent', 'autocompleteInitialized'):
        result['markers'][key] = isinstance(raw_markers, dict) and raw_markers.get(key) is True
    # A successfully used manual locator can retain a visible consent banner.
    # Report it separately; visibility alone is not an initialization failure.
    result['ready'] = (result['markers']['providerPage'] and result['markers']['documentReady']
                       and result['markers']['mainContentPresent'])
    return result


async def observe_raymond_readiness(page, *, consent_choice='operator', timeout_ms=10000):
    """Observe normal initialization; only explicit decline may click consent.

    No automatic acceptance, generic dismissal, storage reads, or requests. A
    visible banner is reported, not treated as proof the page is unusable. Locator readiness is a
    separate marker so this also supports ordinary branch pages.
    """
    result = _readiness_result(page, consent_choice, timeout_ms)
    if not result['markers']['providerPage']: return result
    banners = page.locator(CONSENT_BANNER_SELECTOR)
    count = await banners.count()
    if count > 10: raise RuntimeError('Unexpected Raymond James consent markup')
    visible = [banners.nth(i) for i in range(count) if await banners.nth(i).is_visible()]
    result['bannerVisible'] = result['bannerVisibleAfter'] = bool(visible)
    if len(visible) > 1: raise RuntimeError('Multiple visible Raymond James consent banners')
    if visible and consent_choice == 'decline':
        banner = visible[0]
        button = banner.locator(CONSENT_DECLINE_SELECTOR)
        if await button.count() != 1 or not await button.is_visible() or not await button.is_enabled():
            raise RuntimeError('Raymond James Decline button is not uniquely available')
        await button.click(timeout=timeout_ms)
        await banner.wait_for(state='hidden', timeout=timeout_ms)
        result['choiceApplied'] = 'decline'
        result['bannerVisibleAfter'] = False
    locator = page.locator('#zip-v2')
    result['markers']['locatorInputVisible'] = bool(await locator.count() and await locator.first.is_visible())
    raw = await asyncio.wait_for(page.evaluate(READINESS_SCRIPT), timeout=timeout_ms / 1000)
    return _complete_readiness(result, raw)


def observe_raymond_readiness_sync(page, *, consent_choice='operator', timeout_ms=10000):
    """Synchronous equivalent; default operator choice never clicks anything."""
    result = _readiness_result(page, consent_choice, timeout_ms)
    if not result['markers']['providerPage']: return result
    banners = page.locator(CONSENT_BANNER_SELECTOR)
    count = banners.count()
    if count > 10: raise RuntimeError('Unexpected Raymond James consent markup')
    visible = [banners.nth(i) for i in range(count) if banners.nth(i).is_visible()]
    result['bannerVisible'] = result['bannerVisibleAfter'] = bool(visible)
    if len(visible) > 1: raise RuntimeError('Multiple visible Raymond James consent banners')
    if visible and consent_choice == 'decline':
        banner = visible[0]
        button = banner.locator(CONSENT_DECLINE_SELECTOR)
        if button.count() != 1 or not button.is_visible() or not button.is_enabled():
            raise RuntimeError('Raymond James Decline button is not uniquely available')
        button.click(timeout=timeout_ms)
        banner.wait_for(state='hidden', timeout=timeout_ms)
        result['choiceApplied'] = 'decline'
        result['bannerVisibleAfter'] = False
    locator = page.locator('#zip-v2')
    result['markers']['locatorInputVisible'] = bool(locator.count() and locator.first.is_visible())
    return _complete_readiness(result, page.evaluate(READINESS_SCRIPT))


def validate_endpoint(value):
    p = urlsplit(value)
    if (p.scheme != 'http' or p.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or p.username or p.password or p.path not in {'', '/'}
            or p.query or p.fragment or not p.port):
        raise ValueError('CDP endpoint must be an explicit loopback HTTP port')
    return value.rstrip('/')


def validate_profile(value):
    p = PureWindowsPath(str(value))
    if (not p.is_absolute() or str(p) == p.anchor or '..' in p.parts
            or p.drive.startswith('\\\\') or p.name.casefold() in {'default', 'user data'}):
        raise ValueError('An absolute dedicated local Chrome profile directory is required')
    return str(p)


def validate_raymond_url(value):
    p = urlsplit(value)
    allowed = (p.path in {'', '/'} or p.path.rstrip('/') == '/find-an-advisor'
               or re.fullmatch(r'/[a-z0-9]+(?:-[a-z0-9]+)*/about-us/our-team/?', p.path))
    if (p.scheme != 'https' or p.hostname != 'www.raymondjames.com'
            or p.port not in {None, 443} or p.username or p.password
            or p.query or p.fragment or not allowed):
        raise ValueError('Only the RJ homepage, locator or published branch team URLs are allowed')
    return value


def open_native_tab(*, chrome_path, profile_path, url):
    """Use Chrome's ordinary startup URL. Never terminate the browser."""
    profile = validate_profile(profile_path)
    validate_raymond_url(url)
    executable = PureWindowsPath(str(chrome_path))
    if not executable.is_absolute() or executable.name.casefold() != 'chrome.exe':
        raise ValueError('An absolute installed chrome.exe path is required')
    return subprocess.Popen([str(executable), '--user-data-dir=' + profile, '--new-tab', url],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _command_profile(command):
    match = re.search(r'(?:^|\s)(?:"--user-data-dir=([^"]+)"|--user-data-dir="([^"]+)"|--user-data-dir=([^\s"]+)|--user-data-dir\s+"([^"]+)"|--user-data-dir\s+([^\s"]+))', command)
    if not match:
        raise RuntimeError('CDP Chrome has no explicit dedicated profile')
    return validate_profile(next(value for value in match.groups() if value is not None))


def verify_endpoint_profile(endpoint, profile_path, runner=None):
    """Verify the local listening process is Chrome with this explicit profile.

    Only the validated integer port enters the PowerShell script. Command lines
    are examined in memory and never printed or persisted. Fail closed on an
    unavailable/ambiguous listener instead of opening a different Chrome.
    """
    endpoint = validate_endpoint(endpoint)
    profile_path = validate_profile(profile_path)
    port = urlsplit(endpoint).port
    script = ("$owners = @(Get-NetTCPConnection -State Listen -LocalPort " + str(port)
              + " -ErrorAction Stop | Select-Object -ExpandProperty OwningProcess -Unique); "
              "@($owners | ForEach-Object { Get-CimInstance Win32_Process -Filter ('ProcessId=' + $_) "
              "-ErrorAction Stop | Select-Object ProcessId,Name,CommandLine }) | ConvertTo-Json -Compress")
    completed = (runner or subprocess.run)(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
        capture_output=True, text=True, timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if completed.returncode:
        raise RuntimeError('Could not verify CDP listener ownership')
    try:
        records = json.loads(completed.stdout)
        if isinstance(records, dict): records = [records]
        if not isinstance(records, list) or len(records) != 1:
            raise ValueError('ambiguous listener')
        record = records[0]
        command = record.get('CommandLine') or ''
        if (record.get('Name', '').casefold() != 'chrome.exe'
                or not re.search(r'(?:^|\s)"?--remote-debugging-port=' + str(port) + r'(?:"|\s|$)', command)
                or _command_profile(command).casefold() != profile_path.casefold()):
            raise ValueError('profile or process mismatch')
    except (ValueError, TypeError, AttributeError, KeyError):
        raise RuntimeError('CDP endpoint and dedicated Chrome profile do not match') from None
    return int(record['ProcessId'])


async def clear_raymond_cookies(context):
    """Opt-in filtered cleanup only; never fall back to unfiltered cleanup.

    Requires Playwright cookie filters. Cookie values are never logged/returned.
    """
    count = 0
    # Enumerate in memory: a URL filter would omit /dotcom and subdomain
    # cookies, making an explicitly requested full RJ reset incomplete.
    for cookie in await context.cookies():
        domain = cookie.get('domain', '')
        normalized = domain.lstrip('.').casefold()
        if normalized == 'raymondjames.com' or normalized.endswith('.raymondjames.com'):
            await context.clear_cookies(name=cookie['name'], domain=domain, path=cookie['path'])
            count += 1
    return count


def clear_raymond_cookies_sync(context):
    count = 0
    for cookie in context.cookies():
        domain = cookie.get('domain', '')
        normalized = domain.lstrip('.').casefold()
        if normalized == 'raymondjames.com' or normalized.endswith('.raymondjames.com'):
            context.clear_cookies(name=cookie['name'], domain=domain, path=cookie['path'])
            count += 1
    return count


@dataclass
class OwnedRaymondSession:
    context: object
    page: object
    endpoint: str
    profile_path: str
    cleared_cookie_count: int = 0
    _navigation_status: object = None
    _retry_after: object = None
    _observations: object = None
    _navigation_url: object = None
    owns_page: bool = True

    @property
    def navigation_status(self):
        # Initial navigation may commit its URL before the response callback is
        # dispatched. Keep metadata live while the caller waits for readiness.
        if self._observations is not None:
            observed = self._observations.get(id(self.page))
            if observed is not None: return observed[0]
        if self._navigation_url is not None and self.page.url != self._navigation_url:
            return None
        return self._navigation_status

    @property
    def retry_after(self):
        if self._observations is not None:
            return self._observations.get(id(self.page), (self._navigation_status, self._retry_after))[1]
        return self._retry_after

    @property
    def navigation_status_source(self):
        if self._observations is not None and id(self.page) in self._observations:
            return 'response_event'
        if self._navigation_url is not None and self.navigation_status is not None:
            return 'navigation_timing'
        return None

    async def refresh_navigation_metadata(self):
        """After DOM readiness, recover a missed initial CDP event without HTTP."""
        if self.navigation_status_source == 'response_event': return self.navigation_status
        expected_url = self.page.url
        try:
            timing = await asyncio.wait_for(self.page.evaluate(NAVIGATION_TIMING_SCRIPT), timeout=5)
        except Exception:
            return self.navigation_status
        _apply_navigation_timing(self, timing, expected_url)
        return self.navigation_status


class OwnedRaymondSessionSync(OwnedRaymondSession):
    def refresh_navigation_metadata(self):
        if self.navigation_status_source == 'response_event': return self.navigation_status
        expected_url = self.page.url
        try:
            timing = self.page.evaluate(NAVIGATION_TIMING_SCRIPT)
        except Exception:
            return self.navigation_status
        _apply_navigation_timing(self, timing, expected_url)
        return self.navigation_status


def _apply_navigation_timing(session, timing, expected_url):
    if (not isinstance(timing, dict) or timing.get('name') != expected_url
            or timing.get('currentUrl') != expected_url or session.page.url != expected_url):
        return
    status = timing.get('responseStatus')
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        session._navigation_status = status
        session._navigation_url = expected_url
        session._retry_after = None  # Timing data does not expose headers.


def _install_navigation_observer(context, before, *, selected_page=None):
    observations = {}
    def observe(response):
        try:
            request = response.request
            page = request.frame.page
            excluded = page is not selected_page if selected_page is not None else page in before
            if (excluded or not request.is_navigation_request()
                    or request.frame != page.main_frame): return
            p = urlsplit(response.url)
            if p.hostname not in {'www.raymondjames.com', 'errors.edgesuite.net'}: return
            observations[id(page)] = (response.status, response.headers.get('retry-after'))
        except Exception:
            # Metadata unavailable is explicit None, never a fabricated 200.
            pass
    if hasattr(context, 'on'):
        context.on('response', observe)
    return observations, observe


def validate_existing_target_id(value):
    """Require an explicit Chrome page identity, never a tab index or URL hint."""
    if not isinstance(value, str) or not re.fullmatch(r'[A-Fa-f0-9]{32}', value):
        raise ValueError('An exact 32-character hexadecimal Chrome target ID is required')
    return value


def validate_borrowed_locator_url(value):
    """Validate the locator without exposing its query or fragment contents."""
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == 'https' and parsed.hostname == 'www.raymondjames.com'
                 and parsed.port in {None, 443} and not parsed.username and not parsed.password
                 and parsed.path in {'/find-an-advisor', '/find-an-advisor/'})
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ValueError('The selected existing tab must be the HTTPS Raymond James locator')
    return value


@asynccontextmanager
async def borrowed_raymond_session(browser, *, target_id, profile_path,
                                   endpoint='http://127.0.0.1:9222', profile_verifier=None):
    """Borrow only an explicitly selected existing locator in the default context.

    Target metadata reads and an owned response observer are the only browser
    operations here. Never open, navigate, activate, clear, or close a tab. A
    missing, closed, ambiguous, or invalid target stops rather than selecting a
    different tab. Caller must separately check readiness/challenge/denial.
    """
    endpoint = validate_endpoint(endpoint)
    profile_path = validate_profile(profile_path)
    target_id = validate_existing_target_id(target_id)
    (profile_verifier or verify_endpoint_profile)(endpoint, profile_path)
    if not browser.contexts:
        raise RuntimeError('CDP browser has no existing default context')
    context = browser.contexts[0]
    matches = []
    for candidate in list(context.pages):
        if candidate.is_closed():
            continue
        metadata_session = await context.new_cdp_session(candidate)
        try:
            metadata = await metadata_session.send('Target.getTargetInfo')
            info = metadata.get('targetInfo') if isinstance(metadata, dict) else None
            if not isinstance(info, dict) or not isinstance(info.get('targetId'), str):
                raise RuntimeError('Could not verify existing Chrome target metadata')
            if info['targetId'].casefold() == target_id.casefold():
                if info.get('type') != 'page':
                    raise RuntimeError('Selected Chrome target is not a page')
                validate_borrowed_locator_url(info.get('url'))
                matches.append(candidate)
        finally:
            await metadata_session.detach()
    if len(matches) != 1:
        raise RuntimeError('Selected existing Raymond James target is missing or ambiguous')
    page = matches[0]
    if page.is_closed():
        raise RuntimeError('Selected existing Raymond James tab was closed')
    validate_borrowed_locator_url(page.url)
    observations, observer = _install_navigation_observer(context, set(), selected_page=page)
    try:
        # Check once more after listener installation, without changing state.
        if page.is_closed():
            raise RuntimeError('Selected existing Raymond James tab was closed')
        validate_borrowed_locator_url(page.url)
        yield OwnedRaymondSession(context, page, endpoint, profile_path,
                                  _observations=observations, owns_page=False)
    finally:
        if hasattr(context, 'remove_listener'):
            context.remove_listener('response', observer)


@asynccontextmanager
async def native_raymond_session(browser, *, profile_path,
                                 endpoint='http://127.0.0.1:9222', chrome_path=CHROME_PATH,
                                 start_url=LOCATOR_URL, clear_cookies=False, opener=None,
                                 timeout_seconds=45, poll_seconds=0.2, profile_verifier=None):
    """Borrow default context and own one NEW normal tab. No automatic retry.

    Listener PID/profile pairing is verified first. Ambiguous new tabs fail
    without selecting/closing them. Never close browser or borrowed context.
    """
    endpoint = validate_endpoint(endpoint)
    profile_path = validate_profile(profile_path)
    validate_raymond_url(start_url)
    if not 0 < timeout_seconds <= 120 or not 0 < poll_seconds <= 5:
        raise ValueError('Invalid bounded native-tab wait')
    if not browser.contexts:
        raise RuntimeError('CDP browser has no existing default context')
    (profile_verifier or verify_endpoint_profile)(endpoint, profile_path)
    context = browser.contexts[0]
    before = set(context.pages)
    count = await clear_raymond_cookies(context) if clear_cookies else 0
    observations, observer = _install_navigation_observer(context, before)
    expected = urlsplit(start_url)
    deadline = time.monotonic() + timeout_seconds
    page = None
    try:
        (opener or open_native_tab)(chrome_path=chrome_path, profile_path=profile_path, url=start_url)
        while time.monotonic() < deadline:
            matches = []
            for candidate in context.pages:
                if candidate in before or candidate.is_closed():
                    continue
                p = urlsplit(candidate.url)
                if ((p.scheme == expected.scheme and p.hostname == expected.hostname
                        and p.path.rstrip('/') == expected.path.rstrip('/')
                        and not p.username and not p.password)
                        or (p.scheme == 'https' and p.hostname == 'errors.edgesuite.net')):
                    matches.append(candidate)
            if len(matches) > 1:
                raise RuntimeError('Multiple new RJ tabs; ownership is ambiguous')
            if matches:
                page = matches[0]
                await page.bring_to_front()
                status, retry_after = observations.get(id(page), (None, None))
                yield OwnedRaymondSession(context, page, endpoint, profile_path, count, status, retry_after, observations)
                return
            await asyncio.sleep(poll_seconds)
        raise TimeoutError('No new RJ tab in selected CDP Chrome; verify endpoint/profile pairing')
    finally:
        if hasattr(context, 'remove_listener'):
            context.remove_listener('response', observer)
        if page is not None and not page.is_closed():
            await page.close()


@contextmanager
def native_raymond_session_sync(browser, *, profile_path,
                                endpoint='http://127.0.0.1:9222', chrome_path=CHROME_PATH,
                                start_url=LOCATOR_URL, clear_cookies=False, opener=None,
                                timeout_seconds=45, poll_seconds=0.2, profile_verifier=None):
    """Synchronous equivalent for branch capture; identical ownership policy."""
    endpoint = validate_endpoint(endpoint)
    profile_path = validate_profile(profile_path)
    validate_raymond_url(start_url)
    if not 0 < timeout_seconds <= 120 or not 0 < poll_seconds <= 5:
        raise ValueError('Invalid bounded native-tab wait')
    if not browser.contexts:
        raise RuntimeError('CDP browser has no existing default context')
    (profile_verifier or verify_endpoint_profile)(endpoint, profile_path)
    context = browser.contexts[0]
    before = set(context.pages)
    count = clear_raymond_cookies_sync(context) if clear_cookies else 0
    observations, observer = _install_navigation_observer(context, before)
    expected = urlsplit(start_url)
    deadline = time.monotonic() + timeout_seconds
    page = None
    try:
        (opener or open_native_tab)(chrome_path=chrome_path, profile_path=profile_path, url=start_url)
        while time.monotonic() < deadline:
            matches = []
            for candidate in context.pages:
                if candidate in before or candidate.is_closed():
                    continue
                p = urlsplit(candidate.url)
                if ((p.scheme == expected.scheme and p.hostname == expected.hostname
                        and p.path.rstrip('/') == expected.path.rstrip('/')
                        and not p.username and not p.password)
                        or (p.scheme == 'https' and p.hostname == 'errors.edgesuite.net')):
                    matches.append(candidate)
            if len(matches) > 1:
                raise RuntimeError('Multiple new RJ tabs; ownership is ambiguous')
            if matches:
                page = matches[0]
                page.bring_to_front()
                status, retry_after = observations.get(id(page), (None, None))
                yield OwnedRaymondSessionSync(context, page, endpoint, profile_path, count, status, retry_after, observations)
                return
            # Playwright sync transport must be pumped while awaiting a native tab.
            if context.pages:
                context.pages[0].wait_for_timeout(poll_seconds * 1000)
            else:
                time.sleep(poll_seconds)
        raise TimeoutError('No new RJ tab in selected CDP Chrome; verify endpoint/profile pairing')
    finally:
        if hasattr(context, 'remove_listener'):
            context.remove_listener('response', observer)
        if page is not None and not page.is_closed():
            page.close()
