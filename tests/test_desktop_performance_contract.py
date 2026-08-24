from __future__ import annotations

import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "webapp" / "app.js"


def source_without_comments() -> str:
    source = APP.read_text(encoding="utf-8")
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", source)


def function_body(source: str, name: str, next_anchor: str) -> str:
    start = source.index(f"function {name}")
    end = source.index(next_anchor, start)
    return source[start:end]


class DesktopPerformanceContractTests(unittest.TestCase):
    def test_first_paint_waits_only_for_compact_national_view(self):
        source = source_without_comments()
        self.assertIn('PERF.time("data:national", loadNational).then(() => {', source)
        boot = source[source.index('PERF.time("data:national", loadNational).then'):]
        boot = boot[:boot.index("function scopeLabel")]
        self.assertNotIn("Promise.all([", boot)
        self.assertLess(boot.index("renderAll(true)"), boot.index("scheduleSupportLoads"))

    def test_background_work_never_prefetches_advisor_index(self):
        source = source_without_comments()
        background = function_body(source, "runBackgroundLoads", "function scheduleBackgroundLoads")
        self.assertIn("loadNationalDetail", background)
        self.assertIn("loadContacts", background)
        self.assertNotIn("loadAdvisorIndex", background)

    def test_first_regional_batch_clears_busy_state_before_async_paint(self):
        source = source_without_comments()
        batches = function_body(source, "addMarkerBatches", "map.addLayer(cluster)")
        attached = batches.index("target.addLayers")
        cleared = batches.index("clearScopePending", attached)
        painted = batches.index("afterNextPaint", cleared)
        self.assertLess(attached, cleared)
        self.assertLess(cleared, painted)
        # start=0/end=0 follows this same unconditional path, so an empty scope
        # cannot leave aria-busy or the visible loading pill stranded.
        self.assertIn("if (start === 0 && transition)", batches)

    def test_scope_fetches_are_abortable_and_commit_after_download(self):
        source = source_without_comments()
        fetcher = function_body(source, "fetchScopeJson", "async function loadState")
        self.assertIn("{ signal }", fetcher)
        switch = function_body(source, "switchScope", "scopeSel.addEventListener")
        self.assertIn("scopeController.abort()", switch)
        self.assertLess(switch.index("await (next.startsWith"),
                        switch.index("resetForScopeChange()"))
        self.assertIn('status:"superseded"', switch)
        self.assertIn('status:"failed"', switch)

    def test_deferred_geo_never_turns_location_enter_into_advisor_navigation(self):
        source = source_without_comments()
        locations = function_body(source, "locSuggest", "function renderLocSuggest")
        self.assertIn("Object.entries(STATE_NAMES)", locations)
        self.assertIn("if (GEO && /^\\d+$/.test(q))", locations)
        keydown = source[source.index('searchBox.addEventListener("keydown"'):]
        keydown = keydown[:keydown.index("globalOut.addEventListener")]
        self.assertIn("const locationLike = looksLikeLocation(query)", keydown)
        self.assertNotIn('locOut.querySelector(".lres") || advOut.querySelector(".ares")',
                         keydown)


if __name__ == "__main__":
    unittest.main()
