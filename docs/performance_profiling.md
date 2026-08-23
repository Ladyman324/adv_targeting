# Desktop map performance profiling

The desktop profiler attaches to an existing Chrome debugging session, opens a
dedicated Advisor Map tab, records measurements, and closes only that tab. It
does not navigate or close any tab that was already open.

## Prerequisites

- Chrome is running with its DevTools endpoint on `127.0.0.1:9222`.
- The same Chrome profile already has an authenticated tab open at
  `https://lively-stone-05fce880f.7.azurestaticapps.net/`.
- Python 3.8 or newer and the Playwright Python package are installed. Confirm
  with `py -c "import playwright; print('Playwright available')"`. Attaching
  over CDP does not require launching another browser.
- Run commands from the repository root.

The profiler accepts only the exact production origin above. CDP can control an
entire signed-in browser profile, so this allowlist and the dedicated-page rule
are security boundaries, not conveniences.

## Deploy instrumentation before measuring regions

The regional benchmark depends on transition signals added to `webapp/app.js`:
transition start, first regional batch visible, and all regional batches
settled. The production build includes those signals as of 2026-08-23. If a
target deployment is older, has been rolled back, or otherwise lacks the
instrumentation, a normal run exits with this deliberate error:

    deployed app lacks transition instrumentation; deploy this branch first; --smoke only validates CDP lifecycle

That error means the target deployment lacks or predates the required signals;
publish the instrumentation before measuring regions. Do not interpret the
error JSON file as a partial regional baseline. Follow the static-site procedure
in [deployment_automation.md](deployment_automation.md), then verify the
deployed asset version before profiling.

## Lifecycle smoke check

This confirms that CDP attachment, authentication, national-page loading,
result writing, and owned-tab cleanup work. It does not test regional transition
signals and is not a performance baseline:

    py scripts/perf_desktop.py --smoke --background

`--background` is intentionally valid only with `--smoke`. Chrome throttles
animation frames in background tabs, so allowing it during timing runs would
produce misleading first-paint and batch-settled measurements.

## Authoritative timing runs

Leave off `--background`. The harness brings its dedicated tab to the foreground
because regional timing is tied to browser paint frames.

Five target-scoped cold-ish repetitions:

    py scripts/perf_desktop.py --mode cold-ish --runs 5

Five warm repetitions, after an automatic unrecorded warm-up sequence:

    py scripts/perf_desktop.py --mode warm --runs 5

Cold-ish followed by warm in one result file:

    py scripts/perf_desktop.py --mode both --runs 5

The default regional sequence is Georgia, California, and the West territory.
It can be stated explicitly or narrowed while diagnosing one scope:

    py scripts/perf_desktop.py --mode both --runs 5 --scopes GA CA T:West
    py scripts/perf_desktop.py --mode cold-ish --runs 5 --scopes CA

Cold-ish mode disables the HTTP cache and bypasses the service worker only for
the harness-owned page. It does not clear the browser's global cache, cookies,
or authentication. Warm mode enables normal caching and runs the complete scope
sequence once before recording repetitions. Every sequence uses a unique
same-origin query URL, which forces a fresh HTML document and a fresh `PERF`
object; JavaScript and data asset URLs remain unchanged and therefore cacheable.

## Results and comparison

Every invocation writes a timestamped JSON file under `.playwright/`, which is
ignored by Git. The important fields are:

- `modes.<mode>[].national.app.usableMs`: navigation start to usable national
  map. `perf.usableAt` uses the browser's navigation-relative
  `performance.now()` clock, so this includes HTML, asset, and script-start
  delivery.
- `modes.<mode>[].national.app.scriptStartToUsableMs`: app script execution to
  usable national map. This narrower diagnostic excludes delivery before
  `app.js` begins and must not be reported as total startup time.
- `regions[].transition.firstBatchVisible.elapsedMs`: scope selection to the
  first painted regional marker batch.
- `regions[].transition.batchesSettled.elapsedMs`: scope selection to the final
  painted marker batch.
- `regions[].app.spans`: download, `JSON.parse`, rehydration, aggregation, and
  marker timing recorded by the app.
- `resourceTransferBytes` and `resources`: transferred bytes and per-resource
  timings for the measured phase.
- `longTasks`, `heapBytes`, and `paints`: main-thread stalls, memory, and paint
  context for interpreting elapsed time.
- `error`, `cleanupErrors`, or `cleanupWarnings`: a run that needs investigation
  before its numbers are used.

Compare medians across the same number of repetitions, scope order, Chrome
version, workstation, and network conditions. Keep cold-ish separate from warm;
they answer different questions. Use the span breakdown to choose the remedy:
download time points to payload or delivery, parse/rehydration to data shape,
and a large first-batch or settled gap after data is ready to Leaflet/rendering.
