from __future__ import annotations

import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = (ROOT / "webapp" / "app.js").read_text(encoding="utf-8")
FIELD = (ROOT / "webapp" / "field.js").read_text(encoding="utf-8")
FIELD_HTML = (ROOT / "webapp" / "field.html").read_text(encoding="utf-8")


def section(source: str, start: str, end: str) -> str:
    at = source.index(start)
    return source[at:source.index(end, at)]


class WorkQueueContractTests(unittest.TestCase):
    def test_desktop_uses_one_action_attribute_for_rendering_and_dispatch(self):
        queue = section(APP, "const QUEUE_ACTIONS", "/* ---- advisor email activity")
        self.assertIn('data-wq-action="snooze"', queue)
        self.assertIn('data-wq-action="dismiss_bounce"', queue)
        self.assertIn('closest("[data-wq-action]")', queue)
        for old in ("data-wq-op", "data-wq-state", "data-wq-follow", "data-wq-open"):
            self.assertNotIn(old, queue)

    def test_field_uses_one_action_attribute_for_rendering_and_dispatch(self):
        queue = section(FIELD, "const WORK_ACTIONS", "/* ---------- email activity")
        handler = section(FIELD, "/* ---- needs attention ---- */", "/* ---- preferences ---- */")
        self.assertIn('data-work-action="snooze"', queue)
        self.assertIn('data-work-action="dismiss_bounce"', queue)
        self.assertIn('closest("[data-work-action]")', handler)
        self.assertNotIn('closest("[data-work]")', handler)

    def test_reason_fallbacks_exclude_ineffective_actions(self):
        for source, anchor in ((APP, "const QUEUE_ACTIONS"), (FIELD, "const WORK_ACTIONS")):
            table = section(source, anchor, "};")
            self.assertIn('reply_new: ["mark_reviewed", "snooze"]', table)
            self.assertIn('due: ["follow_up", "snooze"]', table)
            self.assertIn('bounced: ["dismiss_bounce", "snooze"]', table)
            self.assertIn('quiet_warm: ["follow_up", "snooze"]', table)
            self.assertNotIn('bounced: ["follow_up"', table)
            self.assertNotIn('due: ["done"', table)

    def test_sync_status_stays_inside_deferred_queue_loads(self):
        desktop = section(APP, "async function openWorkQueue", "/* ---- advisor email activity")
        field = section(FIELD, "async function loadWork", "/* The count on the header button.")
        self.assertEqual(1, APP.count("op=sweep_status"))
        self.assertEqual(1, FIELD.count("op=sweep_status"))
        self.assertIn("op=sweep_status", desktop)
        self.assertIn("op=sweep_status", field)
        self.assertIn("Promise.allSettled", desktop)
        self.assertIn("Promise.allSettled", field)

    def test_empty_states_do_not_claim_all_clear_when_sync_is_incomplete(self):
        self.assertIn("not an all-clear while sync is incomplete", APP)
        self.assertIn("not an all-clear while sync is incomplete", FIELD)

    def test_queue_dialogs_have_keyboard_and_label_contracts(self):
        self.assertIn('e.key === "Escape"', APP)
        self.assertIn('e.key === "Escape"', FIELD)
        self.assertIn('aria-labelledby="workQueueTitle"', APP)
        self.assertIn(
            '<div id="work" role="dialog" aria-modal="true" aria-labelledby="workTitle"',
            FIELD_HTML,
        )
        self.assertIn('aria-label="Snooze ${who} for 30 days"', APP)
        self.assertIn('aria-label="Snooze ${who} for 30 days"', FIELD)

    def test_server_ingestion_status_precedes_legacy_inference(self):
        desktop = section(APP, "function queueSyncState", "function queueSyncHtml")
        field = section(FIELD, "function workSyncState", "function closeWork")
        for source in (desktop, field):
            self.assertIn("row.ingestionStatus", source)
            for status in (
                "healthy",
                "reconnect_required",
                "failed",
                "stale",
                "never_run",
                "catching_up",
            ):
                self.assertIn(f'ingestion === "{status}"', source)
            self.assertLess(source.index("row.ingestionStatus"), source.index("row.needsReconnect"))

    def test_field_only_newest_overlapping_load_commits_shared_state(self):
        load = section(FIELD, "async function loadWork", "/* The count on the header button.")
        self.assertIn("const generation = ++workLoadGeneration", load)
        guard = load.index("generation !== workLoadGeneration")
        self.assertLess(guard, load.index("workData ="))
        self.assertLess(guard, load.index("workStatus ="))

    def test_follow_up_replaces_queue_modal_and_refreshes_field_after_success(self):
        desktop = section(APP, "async function openWorkQueue", "/* ---- advisor email activity")
        follow = desktop.index('action === "follow_up"')
        follow = desktop[follow:desktop.index("const payload =", follow)]
        self.assertLess(follow.index("close(false)"), follow.index("openFollowUp("))

        field_handler = section(
            FIELD, "/* ---- needs attention ---- */", "/* ---- preferences ---- */"
        )
        follow = field_handler.index('action === "follow_up"')
        follow = field_handler[follow:field_handler.index('action === "open"', follow)]
        self.assertLess(follow.index("closeWork()"), follow.index("showFollowUp("))
        self.assertIn("() => loadWork()", follow)

        composer = section(FIELD, "async function showFollowUp", "/* One message, read on the phone.")
        self.assertIn("if (onSent) Promise.resolve(onSent())", composer)


if __name__ == "__main__":
    unittest.main()
