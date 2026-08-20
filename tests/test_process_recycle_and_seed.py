"""Tests for process recycle helpers and first-run / cold-start seed paths."""

from __future__ import annotations

import os
import unittest
from unittest import mock

import browser_process
import script_clean as sc


class BrowserProcessExhaustionTests(unittest.TestCase):
    def test_cannot_fork_classified(self):
        self.assertTrue(
            browser_process.is_browser_process_exhaustion(
                OSError("Cannot fork: Resource temporarily unavailable")
            )
        )

    def test_posix_spawn_classified(self):
        self.assertTrue(
            browser_process.is_browser_process_exhaustion(
                RuntimeError("posix_spawn failed: Resource temporarily unavailable")
            )
        )

    def test_unrelated_not_classified(self):
        self.assertFalse(
            browser_process.is_browser_process_exhaustion(RuntimeError("tab crashed"))
        )


class ProcessRecycleTimingTests(unittest.TestCase):
    def test_should_recycle_respects_hours(self):
        with mock.patch.dict(os.environ, {"PROCESS_RECYCLE_HOURS": "3"}):
            with mock.patch.object(browser_process, "process_uptime_seconds", return_value=100):
                self.assertFalse(browser_process.should_recycle_process())
            with mock.patch.object(
                browser_process, "process_uptime_seconds", return_value=3 * 3600 + 1
            ):
                self.assertTrue(browser_process.should_recycle_process())

    def test_recycle_disabled_when_zero(self):
        with mock.patch.dict(os.environ, {"PROCESS_RECYCLE_HOURS": "0"}):
            with mock.patch.object(
                browser_process, "process_uptime_seconds", return_value=999999
            ):
                self.assertFalse(browser_process.should_recycle_process())


class SeedPathTests(unittest.TestCase):
    def setUp(self):
        self.old_suppress = sc.Config.SUPPRESS_PROJECT_EMAILS_ON_FIRST_SCAN

    def tearDown(self):
        sc.Config.SUPPRESS_PROJECT_EMAILS_ON_FIRST_SCAN = self.old_suppress

    def test_empty_platform_always_cold_start_seeds(self):
        """Empty DB cold start suppresses emails even when SUPPRESS_PROJECT_EMAILS=false."""
        sc.Config.SUPPRESS_PROJECT_EMAILS_ON_FIRST_SCAN = False
        seed = mock.Mock(
            return_value={
                "inserted": 2,
                "details_attempted": 2,
                "details_completed": 2,
                "details_failed": 0,
                "details_partial": 0,
                "field_coverage": {},
                "email_not_sent_reason": "COLD_START_SEED",
            }
        )
        with mock.patch.object(sc.db, "mark_stale_running_runs"), mock.patch.object(
            sc.db, "create_scraper_run", return_value={"id": "run1"}
        ), mock.patch.object(sc.db, "complete_scraper_run"), mock.patch.object(
            sc, "_navigate_to_search"
        ), mock.patch.object(
            sc, "scan_for_projects", return_value=[{"id": "p1", "title": "A"}]
        ), mock.patch.object(sc, "seed_cold_start", seed), mock.patch.object(
            sc, "retry_pending_emails", return_value={"sent": 0, "failed": 0}
        ):
            cold, first = sc.run_monitoring_cycle(
                mock.Mock(),
                cold_start_pending=True,
                first_run_seed_pending=True,
                dry_run=False,
            )

        self.assertFalse(cold)
        self.assertFalse(first)
        seed.assert_called_once()
        self.assertEqual(seed.call_args.kwargs["email_not_sent_reason"], "COLD_START_SEED")

    def test_first_run_seed_when_suppress_true(self):
        sc.Config.SUPPRESS_PROJECT_EMAILS_ON_FIRST_SCAN = True
        seed = mock.Mock(
            return_value={
                "inserted": 1,
                "details_attempted": 1,
                "details_completed": 1,
                "details_failed": 0,
                "details_partial": 0,
                "field_coverage": {},
                "email_not_sent_reason": "FIRST_RUN_SEED",
            }
        )
        with mock.patch.object(sc.db, "mark_stale_running_runs"), mock.patch.object(
            sc.db, "create_scraper_run", return_value={"id": "run1"}
        ), mock.patch.object(sc.db, "complete_scraper_run"), mock.patch.object(
            sc, "_navigate_to_search"
        ), mock.patch.object(
            sc, "scan_for_projects", return_value=[{"id": "p1", "title": "A"}]
        ), mock.patch.object(sc, "seed_cold_start", seed), mock.patch.object(
            sc, "retry_pending_emails", return_value={"sent": 0, "failed": 0}
        ):
            cold, first = sc.run_monitoring_cycle(
                mock.Mock(),
                cold_start_pending=False,
                first_run_seed_pending=True,
                dry_run=False,
            )

        self.assertFalse(cold)
        self.assertFalse(first)
        self.assertEqual(seed.call_args.kwargs["email_not_sent_reason"], "FIRST_RUN_SEED")

    def test_first_run_emails_when_suppress_false(self):
        sc.Config.SUPPRESS_PROJECT_EMAILS_ON_FIRST_SCAN = False
        seed = mock.Mock()
        with mock.patch.object(sc.db, "mark_stale_running_runs"), mock.patch.object(
            sc.db, "create_scraper_run", return_value={"id": "run1"}
        ), mock.patch.object(sc.db, "complete_scraper_run"), mock.patch.object(
            sc, "_navigate_to_search"
        ), mock.patch.object(
            sc, "scan_for_projects", return_value=[]
        ), mock.patch.object(sc, "seed_cold_start", seed):
            cold, first = sc.run_monitoring_cycle(
                mock.Mock(),
                cold_start_pending=False,
                first_run_seed_pending=True,
                dry_run=False,
            )

        # No projects → seed not called; first_run flag retained until a successful
        # non-empty scan clears it via normal path. Empty list returns early.
        self.assertFalse(cold)
        self.assertTrue(first)
        seed.assert_not_called()


class ErrorCooldownDefaultTests(unittest.TestCase):
    def test_zero_cooldown_always_allows(self):
        old = sc.Config.ERROR_EMAIL_COOLDOWN_MINUTES
        sc.Config.ERROR_EMAIL_COOLDOWN_MINUTES = 0
        try:
            ok, remaining = sc.should_send_error_alert("same|sig", force=False)
            self.assertTrue(ok)
            self.assertEqual(remaining, 0)
            # Even with a prior send recorded, cooldown 0 still allows.
            with sc._error_alert_lock:
                sc._error_alert_last_sent["same|sig"] = 1.0
            ok2, _ = sc.should_send_error_alert("same|sig", force=False)
            self.assertTrue(ok2)
        finally:
            sc.Config.ERROR_EMAIL_COOLDOWN_MINUTES = old
            with sc._error_alert_lock:
                sc._error_alert_last_sent.pop("same|sig", None)


if __name__ == "__main__":
    unittest.main()
