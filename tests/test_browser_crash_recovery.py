"""Tests for cycle error recovery (browser crash, scan failures, etc.)."""

from __future__ import annotations

import unittest
from unittest import mock

from selenium.common.exceptions import TimeoutException, WebDriverException

import script_clean as sc


class RecoverableCrashClassificationTests(unittest.TestCase):
    def test_tab_crashed_recoverable(self):
        exc = WebDriverException("Message: tab crashed\n(Session info: chrome=151.0.7922.71)")
        self.assertTrue(sc.is_recoverable_browser_crash(exc))

    def test_tab_crashed_case_insensitive(self):
        self.assertTrue(sc.is_recoverable_browser_crash(WebDriverException("TAB CRASHED")))

    def test_chrome_not_reachable_recoverable(self):
        self.assertTrue(
            sc.is_recoverable_browser_crash(WebDriverException("chrome not reachable"))
        )

    def test_devtools_disconnect_recoverable(self):
        self.assertTrue(
            sc.is_recoverable_browser_crash(
                WebDriverException("disconnected: not connected to DevTools")
            )
        )

    def test_page_crash_session_deleted_recoverable(self):
        self.assertTrue(
            sc.is_recoverable_browser_crash(
                WebDriverException("session deleted because of page crash")
            )
        )

    def test_unrelated_webdriver_exception_not_classified_as_crash(self):
        self.assertFalse(
            sc.is_recoverable_browser_crash(
                WebDriverException("invalid session id: browser has closed the connection")
            )
        )
        self.assertFalse(
            sc.is_recoverable_browser_crash(WebDriverException("element click intercepted"))
        )


class BrowserCrashRecoveryFlowTests(unittest.TestCase):
    def setUp(self):
        self.old_retries = sc.Config.TAB_CRASH_MAX_RETRIES
        self.old_delay = sc.Config.TAB_CRASH_RETRY_DELAY_SECONDS
        sc.Config.TAB_CRASH_MAX_RETRIES = 2
        sc.Config.TAB_CRASH_RETRY_DELAY_SECONDS = 60

    def tearDown(self):
        sc.Config.TAB_CRASH_MAX_RETRIES = self.old_retries
        sc.Config.TAB_CRASH_RETRY_DELAY_SECONDS = self.old_delay

    def test_successful_retry_recreates_driver_and_returns_state(self):
        old_driver = mock.Mock(name="old_driver")
        new_driver = mock.Mock(name="new_driver")
        crash = WebDriverException("Message: tab crashed")
        cycle = mock.Mock(side_effect=[crash, False])
        alerts = []

        def capture_alert(*args, **kwargs):
            alerts.append((args, kwargs))

        with mock.patch.object(sc, "run_monitoring_cycle", cycle), mock.patch.object(
            sc, "_sleep_interruptible"
        ) as sleep_mock, mock.patch.object(
            sc, "safe_quit_driver"
        ) as quit_mock, mock.patch.object(
            sc, "initialize_driver", return_value=new_driver
        ) as init_mock, mock.patch.object(
            sc,
            "setup_session",
            return_value={"success": True, "alert_sent": False, "message": "cookies"},
        ) as setup_mock, mock.patch.object(
            sc, "send_error_notification", side_effect=capture_alert
        ):
            driver_out, cold = sc.run_monitoring_cycle_with_browser_recovery(
                old_driver,
                cold_start_pending=True,
                dry_run=True,
                check_number=7,
            )

        self.assertIs(driver_out, new_driver)
        self.assertFalse(cold)
        self.assertEqual(cycle.call_count, 2)
        quit_mock.assert_called()
        sleep_mock.assert_called_once_with(60)
        init_mock.assert_called_once()
        setup_mock.assert_called_once_with(new_driver)
        self.assertEqual(alerts, [])  # no intermediate MONITORING_CYCLE:FAILED

    def test_broken_driver_quit_before_new_driver(self):
        order = []
        old_driver = mock.Mock(name="old")
        new_driver = mock.Mock(name="new")

        def quit_side_effect(d):
            order.append(("quit", d))

        def init_side_effect():
            order.append(("init",))
            return new_driver

        with mock.patch.object(
            sc,
            "run_monitoring_cycle",
            side_effect=[WebDriverException("tab crashed"), False],
        ), mock.patch.object(sc, "_sleep_interruptible"), mock.patch.object(
            sc, "safe_quit_driver", side_effect=quit_side_effect
        ), mock.patch.object(
            sc, "initialize_driver", side_effect=init_side_effect
        ), mock.patch.object(
            sc,
            "setup_session",
            return_value={"success": True, "alert_sent": False, "message": "cookies"},
        ), mock.patch.object(sc, "send_error_notification"):
            sc.run_monitoring_cycle_with_browser_recovery(
                old_driver, cold_start_pending=False, check_number=1
            )

        self.assertEqual(order[0][0], "quit")
        self.assertIs(order[0][1], old_driver)
        self.assertEqual(order[1][0], "init")

    def test_retry_exhaustion_raises_without_intermediate_cycle_alert(self):
        sc.Config.TAB_CRASH_MAX_RETRIES = 2
        crash = WebDriverException("tab crashed")
        alerts = []
        cycle = mock.Mock(side_effect=crash)

        with mock.patch.object(sc, "run_monitoring_cycle", cycle), mock.patch.object(
            sc, "_sleep_interruptible"
        ) as sleep_mock, mock.patch.object(sc, "safe_quit_driver"), mock.patch.object(
            sc, "initialize_driver", return_value=mock.Mock()
        ), mock.patch.object(
            sc,
            "setup_session",
            return_value={"success": True, "alert_sent": False, "message": "cookies"},
        ), mock.patch.object(
            sc,
            "send_error_notification",
            side_effect=lambda *a, **k: alerts.append(a[0] if a else None),
        ):
            with self.assertRaises(WebDriverException):
                sc.run_monitoring_cycle_with_browser_recovery(
                    mock.Mock(), cold_start_pending=False, check_number=3
                )

        self.assertEqual(cycle.call_count, 3)
        self.assertEqual(sleep_mock.call_count, 2)
        sleep_mock.assert_called_with(60)
        self.assertNotIn("MONITORING_CYCLE:FAILED", alerts)

    def test_any_cycle_error_triggers_recovery(self):
        """Non-crash scan/cycle errors also retry after the configured delay."""
        old_driver = mock.Mock(name="old_driver")
        new_driver = mock.Mock(name="new_driver")
        cycle = mock.Mock(side_effect=[RuntimeError("scan failed"), False])

        with mock.patch.object(sc, "run_monitoring_cycle", cycle), mock.patch.object(
            sc, "_sleep_interruptible"
        ) as sleep_mock, mock.patch.object(sc, "safe_quit_driver"), mock.patch.object(
            sc, "initialize_driver", return_value=new_driver
        ), mock.patch.object(
            sc,
            "setup_session",
            return_value={"success": True, "alert_sent": False, "message": "cookies"},
        ), mock.patch.object(sc, "send_error_notification"):
            driver_out, cold = sc.run_monitoring_cycle_with_browser_recovery(
                old_driver, cold_start_pending=False, check_number=1
            )

        self.assertIs(driver_out, new_driver)
        self.assertFalse(cold)
        self.assertEqual(cycle.call_count, 2)
        sleep_mock.assert_called_once_with(60)

    def test_scan_for_projects_reraises_without_immediate_alert(self):
        driver = mock.Mock()
        driver.find_elements.side_effect = WebDriverException("tab crashed")
        alerts = []

        with mock.patch.object(sc, "WebDriverWait") as wait_cls, mock.patch.object(
            sc,
            "send_error_notification",
            side_effect=lambda *a, **k: alerts.append(a[0] if a else None),
        ):
            wait_cls.return_value.until.return_value = True
            with self.assertRaises(WebDriverException):
                sc.scan_for_projects(driver)

        self.assertEqual(alerts, [])
        self.assertEqual(sc.last_scan_issue["classification"], "SCAN:PROJECT_LIST_FAILED")

    def test_scan_for_projects_timeout_reraises_without_immediate_alert(self):
        driver = mock.Mock()
        alerts = []

        with mock.patch.object(sc, "WebDriverWait") as wait_cls, mock.patch.object(
            sc,
            "send_error_notification",
            side_effect=lambda *a, **k: alerts.append(a[0] if a else None),
        ):
            wait_cls.return_value.until.side_effect = TimeoutException("timed out")
            with self.assertRaises(TimeoutException):
                sc.scan_for_projects(driver)

        self.assertEqual(alerts, [])
        self.assertEqual(sc.last_scan_issue["classification"], "SCAN:TIMEOUT")


if __name__ == "__main__":
    unittest.main()
