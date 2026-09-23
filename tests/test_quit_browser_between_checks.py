"""Tests for QUIT_BROWSER_BETWEEN_CHECKS lifecycle in main()."""

from __future__ import annotations

import unittest
from unittest import mock

import script_clean as sc


class QuitBrowserBetweenChecksTests(unittest.TestCase):
    def setUp(self):
        self.old_flag = sc.Config.QUIT_BROWSER_BETWEEN_CHECKS
        self.old_interval = sc.Config.CHECK_INTERVAL
        sc.Config.CHECK_INTERVAL = 0

    def tearDown(self):
        sc.Config.QUIT_BROWSER_BETWEEN_CHECKS = self.old_flag
        sc.Config.CHECK_INTERVAL = self.old_interval

    def _run_two_cycles(self, *, quit_between: bool):
        sc.Config.QUIT_BROWSER_BETWEEN_CHECKS = quit_between
        first = mock.Mock(name="driver1")
        second = mock.Mock(name="driver2")
        created = {"n": 0}
        cycle_n = {"n": 0}

        def init_side_effect():
            created["n"] += 1
            return first if created["n"] == 1 else second

        def cycle_side_effect(driver, **kwargs):
            cycle_n["n"] += 1
            if cycle_n["n"] >= 2:
                raise KeyboardInterrupt()
            return driver, False, False

        with mock.patch.object(sc.browser_process, "acquire_worker_lock", return_value=True), mock.patch.object(
            sc.browser_process, "release_worker_lock"
        ), mock.patch.object(sc, "clean_old_evidence_files"), mock.patch.object(
            sc, "print_startup_banner"
        ), mock.patch.object(sc, "initialize_driver", side_effect=init_side_effect) as init_mock, mock.patch.object(
            sc,
            "setup_session",
            return_value={"success": True, "alert_sent": False, "message": "cookies"},
        ) as setup_mock, mock.patch.object(
            sc, "run_monitoring_cycle_with_browser_recovery", side_effect=cycle_side_effect
        ) as cycle_mock, mock.patch.object(sc, "safe_quit_driver") as quit_mock, mock.patch.object(
            sc, "stop_browser_before_sleep", wraps=sc.stop_browser_before_sleep
        ) as stop_wrap, mock.patch.object(
            sc.browser_process, "should_recycle_process", return_value=False
        ), mock.patch.object(sc, "db_is_cold_start", return_value=False), mock.patch.object(
            sc, "init_db"
        ), mock.patch.object(sc.time, "sleep"), mock.patch.object(sc, "send_error_notification"):
            sc.main()

        return init_mock, setup_mock, cycle_mock, quit_mock, stop_wrap

    def test_flag_true_quits_then_restarts_with_cookies(self):
        init_mock, setup_mock, cycle_mock, quit_mock, stop_wrap = self._run_two_cycles(
            quit_between=True
        )
        self.assertGreaterEqual(init_mock.call_count, 2)
        self.assertGreaterEqual(setup_mock.call_count, 2)
        self.assertEqual(cycle_mock.call_count, 2)
        stop_wrap.assert_called()
        quit_mock.assert_called()

    def test_flag_false_keeps_driver_between_cycles(self):
        init_mock, setup_mock, cycle_mock, quit_mock, stop_wrap = self._run_two_cycles(
            quit_between=False
        )
        self.assertEqual(init_mock.call_count, 1)
        self.assertEqual(setup_mock.call_count, 1)
        self.assertEqual(cycle_mock.call_count, 2)
        stop_wrap.assert_not_called()
        # Only shutdown finally should quit
        self.assertEqual(quit_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
