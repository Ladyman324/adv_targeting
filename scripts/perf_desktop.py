#!/usr/bin/env python3
'''Profile the deployed desktop map through an existing Chrome CDP session.

Chrome must already be running with remote debugging on 127.0.0.1:9222 and
must have an authenticated Advisor Map tab. The profiler creates and closes its
own tab in that authenticated context; it never navigates an existing tab.

Results are always written below .playwright/, which is ignored by Git.
'''

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse


CDP_ENDPOINT = 'http://127.0.0.1:9222'
ALLOWED_ORIGINS = frozenset({
    'https://lively-stone-05fce880f.7.azurestaticapps.net',
})
DEFAULT_SCOPES = ('GA', 'CA', 'T:West')
PROFILE_HASH = '#desktop-perf'
TRANSITION_INSTRUMENTATION_ERROR = (
    'deployed app lacks transition instrumentation; deploy this branch first; '
    '--smoke only validates CDP lifecycle'
)

OBSERVER_SCRIPT = r'''
(() => {
  const state = { longTasks: [], paints: [], observerErrors: [] };
  window.__desktopPerf = state;
  const observe = (type, sink) => {
    try {
      new PerformanceObserver(list => list.getEntries().forEach(entry =>
        state[sink].push({
          name: entry.name, startTime: entry.startTime, duration: entry.duration,
        })
      )).observe({ type, buffered: true });
    } catch (error) {
      state.observerErrors.push(type + ': ' + String(error));
    }
  };
  observe('longtask', 'longTasks');
  observe('paint', 'paints');
})();
'''


def exact_origin(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return ''
    if parsed.username or parsed.password:
        return ''
    return '{}://{}'.format(parsed.scheme.lower(), parsed.netloc.lower())


def ensure_allowed(url: str) -> None:
    origin = exact_origin(url)
    if origin not in ALLOWED_ORIGINS:
        raise RuntimeError('refusing non-allowlisted page origin: {!r}'.format(origin or url))


def poll_value(page: Any, expression: str, arg: Any,
               timeout_ms: int) -> Any:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        value = page.evaluate(expression, arg)
        if value:
            return value
        time.sleep(0.05)
    raise TimeoutError('browser condition did not become true within {}ms'.format(timeout_ms))


def wait_signal(page: Any, name: str, scope: str, after: int,
                timeout_ms: int, request: int = 0) -> Dict[str, Any]:
    return poll_value(
        page,
        '''arg => {
          const events = (window.PERF && window.PERF.signals) || [];
          return events.slice(arg.after).find(event =>
            event.name === arg.name && event.scope === arg.scope &&
            (!arg.request || event.request === arg.request)) || false;
        }''',
        {'name': name, 'scope': scope, 'after': after, 'request': request},
        timeout_ms,
    )


def snapshot(page: Any, since: float = 0, span_start: int = 0,
             signal_start: int = 0) -> Dict[str, Any]:
    return page.evaluate(
        '''arg => {
          const nav = performance.getEntriesByType('navigation')[0];
          const resources = performance.getEntriesByType('resource')
            .filter(entry => entry.startTime >= arg.since)
            .map(entry => {
              let label = entry.name;
              try {
                const url = new URL(entry.name, location.href);
                label = url.origin === location.origin
                  ? url.pathname : url.origin + url.pathname;
              } catch (_) {}
              return {
                name: label, initiatorType: entry.initiatorType,
                startTime: entry.startTime, duration: entry.duration,
                transferSize: entry.transferSize,
                encodedBodySize: entry.encodedBodySize,
                decodedBodySize: entry.decodedBodySize,
              };
            });
          const state = window.__desktopPerf || {};
          const perf = window.PERF || { spans: [], signals: [] };
          return {
            app: {
              usableMs: perf.usableAt == null ? null : perf.usableAt,
              scriptStartToUsableMs: perf.usableAt == null || perf.t0 == null
                ? null : perf.usableAt - perf.t0,
              spans: (perf.spans || []).slice(arg.spanStart),
              signals: (perf.signals || []).slice(arg.signalStart),
            },
            navigation: nav ? {
              type: nav.type, duration: nav.duration,
              domContentLoaded: nav.domContentLoadedEventEnd,
              loadEvent: nav.loadEventEnd, transferSize: nav.transferSize,
              encodedBodySize: nav.encodedBodySize,
              decodedBodySize: nav.decodedBodySize,
            } : null,
            resources,
            resourceTransferBytes: resources.reduce(
              (total, entry) => total + (entry.transferSize || 0), 0),
            longTasks: (state.longTasks || []).filter(
              entry => entry.startTime >= arg.since),
            paints: state.paints || [],
            observerErrors: state.observerErrors || [],
            heapBytes: performance.memory ? performance.memory.usedJSHeapSize : null,
          };
        }''',
        {'since': since, 'spanStart': span_start, 'signalStart': signal_start},
    )


def wait_map_usable(page: Any, timeout_ms: int) -> None:
    poll_value(
        page, '() => window.PERF && Number.isFinite(window.PERF.usableAt)',
        None, timeout_ms,
    )
    poll_value(
        page,
        '''() => Array.from(document.querySelectorAll('#scope option'))
          .some(option => option.value === 'T:West')''',
        None, timeout_ms,
    )


def validate_transition_capabilities(capabilities: Dict[str, Any]) -> None:
    required = ('signalsArray', 'signalFunction', 'timeSyncFunction')
    if not all(capabilities.get(name) for name in required):
        raise RuntimeError(TRANSITION_INSTRUMENTATION_ERROR)


def require_transition_instrumentation(page: Any) -> None:
    capabilities = page.evaluate(
        '''() => ({
          signalsArray: Array.isArray(window.PERF && window.PERF.signals),
          signalFunction: typeof (window.PERF && window.PERF.signal) === 'function',
          timeSyncFunction: typeof (window.PERF && window.PERF.timeSync) === 'function',
        })'''
    )
    validate_transition_capabilities(capabilities)


def return_to_national(page: Any, timeout_ms: int) -> None:
    signal_start = page.evaluate('window.PERF.signals.length')
    page.select_option('#scope', 'US')
    started = wait_signal(page, 'scope:transition-start', 'US', signal_start, timeout_ms)
    wait_signal(page, 'scope:transition-settled', 'US', signal_start,
                timeout_ms, int(started['request']))


def measure_scope(page: Any, scope: str, timeout_ms: int) -> Dict[str, Any]:
    ensure_allowed(page.url)
    span_start = page.evaluate('window.PERF.spans.length')
    signal_start = page.evaluate('window.PERF.signals.length')
    page.select_option('#scope', scope)
    started = wait_signal(page, 'scope:transition-start', scope, signal_start, timeout_ms)
    request = int(started['request'])
    first = wait_signal(page, 'regional:first-batch-visible', scope,
                        signal_start, timeout_ms, request)
    settled = wait_signal(page, 'regional:batches-settled', scope,
                          signal_start, timeout_ms, request)
    ensure_allowed(page.url)
    result = snapshot(page, float(started['at']), span_start, signal_start)
    result['scope'] = scope
    result['transition'] = {
        'start': started,
        'firstBatchVisible': first,
        'batchesSettled': settled,
    }
    return result


def measure_run(page: Any, origin: str, scopes: List[str],
                timeout_ms: int) -> Dict[str, Any]:
    target = origin + '/' + PROFILE_HASH
    ensure_allowed(target)
    page.goto(target, wait_until='domcontentloaded', timeout=timeout_ms)
    ensure_allowed(page.url)
    wait_map_usable(page, timeout_ms)
    require_transition_instrumentation(page)
    run = {'national': snapshot(page), 'regions': []}
    for scope in scopes:
        result = measure_scope(page, scope, timeout_ms)
        run['regions'].append(result)
        return_to_national(page, timeout_ms)
    return run


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('cold-ish', 'warm', 'both'),
                        default='both',
                        help='target-scoped cache mode; both runs cold-ish then warm')
    parser.add_argument('--runs', type=int, default=1,
                        help='recorded repetitions per mode (default: 1)')
    parser.add_argument('--timeout-ms', type=int, default=180000,
                        help='timeout for navigation and each scope (default: 180000)')
    parser.add_argument('--scopes', nargs='+', default=list(DEFAULT_SCOPES),
                        choices=DEFAULT_SCOPES,
                        help='regional scopes to measure (default: GA CA T:West)')
    parser.add_argument('--background', action='store_true',
                        help='smoke only: do not foreground the dedicated tab')
    parser.add_argument('--smoke', action='store_true',
                        help='attach, load the national map once, and exit')
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error('--runs must be at least 1')
    if args.timeout_ms < 1000:
        parser.error('--timeout-ms must be at least 1000')
    if args.background and not args.smoke:
        parser.error('--background is only valid with --smoke; paint timings '
                     'require a foreground tab')
    return args


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    origin = next(iter(ALLOWED_ORIGINS))
    repo = Path(__file__).resolve().parents[1]
    output_dir = repo / '.playwright'
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output_path = output_dir / 'desktop-perf-{}.json'.format(stamp)
    payload: Dict[str, Any] = {
        'createdUtc': datetime.now(timezone.utc).isoformat(),
        'cdpEndpoint': CDP_ENDPOINT,
        'origin': origin,
        'modes': {},
    }

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('Playwright is not installed; run: pip install playwright', file=sys.stderr)
        return 2

    page = None
    cdp = None
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
            payload['browserVersion'] = browser.version
            eligible = [
                context for context in browser.contexts
                if any(exact_origin(existing.url) == origin for existing in context.pages)
            ]
            if not eligible:
                raise RuntimeError(
                    'no authenticated Advisor Map tab exists in the CDP browser')
            context = eligible[0]
            page = context.new_page()
            initial_page_count = len(context.pages) - 1
            try:
                page.set_default_timeout(args.timeout_ms)
                page.add_init_script(OBSERVER_SCRIPT)
                if not args.background:
                    page.bring_to_front()
                cdp = context.new_cdp_session(page)
                cdp.send('Network.enable')
                if args.smoke:
                    cdp.send('Network.setCacheDisabled', {'cacheDisabled': False})
                    cdp.send('Network.setBypassServiceWorker', {'bypass': False})
                    target = origin + '/' + PROFILE_HASH
                    page.goto(target, wait_until='domcontentloaded', timeout=args.timeout_ms)
                    ensure_allowed(page.url)
                    wait_map_usable(page, args.timeout_ms)
                    payload['smoke'] = snapshot(page)
                else:
                    modes = ('cold-ish', 'warm') if args.mode == 'both' else (args.mode,)
                    for mode in modes:
                        cold = mode == 'cold-ish'
                        cdp.send('Network.setCacheDisabled', {'cacheDisabled': cold})
                        cdp.send('Network.setBypassServiceWorker', {'bypass': cold})
                        if mode == 'warm':
                            print('Warming the dedicated tab...', flush=True)
                            measure_run(page, origin, list(args.scopes), args.timeout_ms)
                        recorded = []
                        for number in range(1, args.runs + 1):
                            print('{} run {}/{}...'.format(
                                mode, number, args.runs), flush=True)
                            recorded.append(measure_run(
                                page, origin, list(args.scopes), args.timeout_ms))
                        payload['modes'][mode] = recorded
            finally:
                cleanup_errors = []
                if cdp is not None:
                    try:
                        cdp.send('Network.setCacheDisabled', {'cacheDisabled': False})
                        cdp.send('Network.setBypassServiceWorker', {'bypass': False})
                    except Exception as cleanup_error:
                        cleanup_errors.append('CDP reset: {}'.format(cleanup_error))
                try:
                    page.close()
                except Exception as cleanup_error:
                    cleanup_errors.append('page close: {}'.format(cleanup_error))
                try:
                    if not page.is_closed():
                        cleanup_errors.append('dedicated page reports that it is still open')
                except Exception as cleanup_error:
                    cleanup_errors.append('page closure check: {}'.format(cleanup_error))
                if len(context.pages) != initial_page_count:
                    payload.setdefault('cleanupWarnings', []).append(
                        'context page count changed during the run; the owned page '
                        'was checked independently')
                if cleanup_errors:
                    payload['cleanupErrors'] = cleanup_errors
                    if sys.exc_info()[0] is None:
                        raise RuntimeError('; '.join(cleanup_errors))
    except Exception as error:
        payload['error'] = '{}: {}'.format(type(error).__name__, error)
        raise
    finally:
        output_path.write_text(json.dumps(payload, indent=2), encoding='utf-8')
        print('Results: {}'.format(output_path), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
