"""Tests for Supabase transient network retry / client reset."""

from __future__ import annotations

import unittest
from unittest import mock

import database as db


class TransientNetworkClassificationTests(unittest.TestCase):
    def test_connection_terminated(self):
        self.assertTrue(
            db.is_transient_supabase_network_error(
                Exception("<ConnectionTerminated error_code:0, last_stream_id:3>")
            )
        )

    def test_remote_protocol_error(self):
        self.assertTrue(
            db.is_transient_supabase_network_error(
                Exception("httpx.RemoteProtocolError: connection")
            )
        )

    def test_unrelated_api_error_not_transient(self):
        self.assertFalse(
            db.is_transient_supabase_network_error(Exception("duplicate key value violates unique"))
        )

    def test_closed_client_is_transient(self):
        self.assertTrue(
            db.is_transient_supabase_network_error(
                Exception("Cannot send a request, as the client has been closed.")
            )
        )

    def test_ensure_schema_rebuilds_after_connection_terminated(self):
        """ensure_schema must rebuild with a fresh client — not reuse a closed one."""
        calls = {"n": 0}
        clients = [mock.Mock(name="c1"), mock.Mock(name="c2")]

        def build_fn(client):
            builder = mock.Mock()
            calls["n"] += 1
            if calls["n"] == 1:
                builder.execute.side_effect = Exception(
                    "<ConnectionTerminated error_code:0, last_stream_id:3>"
                )
            else:
                resp = mock.Mock()
                resp.data = [{"id": "ok"}]
                builder.execute.return_value = resp
            return builder

        old_retries = db._SUPABASE_MAX_RETRIES
        old_delay = db._SUPABASE_RETRY_BASE_SECONDS
        db._SUPABASE_MAX_RETRIES = 2
        db._SUPABASE_RETRY_BASE_SECONDS = 0
        try:
            with mock.patch.object(db, "get_supabase_client", side_effect=clients), mock.patch.object(
                db, "reset_supabase_client"
            ) as reset_mock:
                result = db._execute(
                    "ensure_schema",
                    "projects",
                    build_fn=build_fn,
                )
            self.assertEqual(result.data[0]["id"], "ok")
            self.assertEqual(calls["n"], 2)
            reset_mock.assert_called()
        finally:
            db._SUPABASE_MAX_RETRIES = old_retries
            db._SUPABASE_RETRY_BASE_SECONDS = old_delay

    def test_closed_client_after_reset_recovers_with_build_fn(self):
        calls = {"n": 0}

        def build_fn(client):
            builder = mock.Mock()
            calls["n"] += 1
            if calls["n"] == 1:
                builder.execute.side_effect = Exception(
                    "<ConnectionTerminated error_code:0>"
                )
            elif calls["n"] == 2:
                # Simulate the previous bug: closed client reused without rebuild
                # With build_fn this path gets a NEW builder instead.
                builder.execute.side_effect = Exception(
                    "Cannot send a request, as the client has been closed."
                )
            else:
                resp = mock.Mock()
                resp.data = [{"id": "recovered"}]
                builder.execute.return_value = resp
            return builder

        old_retries = db._SUPABASE_MAX_RETRIES
        old_delay = db._SUPABASE_RETRY_BASE_SECONDS
        db._SUPABASE_MAX_RETRIES = 3
        db._SUPABASE_RETRY_BASE_SECONDS = 0
        try:
            with mock.patch.object(db, "get_supabase_client", return_value=mock.Mock()), mock.patch.object(
                db, "reset_supabase_client"
            ):
                result = db._execute(
                    "ensure_schema",
                    "projects",
                    build_fn=build_fn,
                )
            self.assertEqual(result.data[0]["id"], "recovered")
            self.assertEqual(calls["n"], 3)
        finally:
            db._SUPABASE_MAX_RETRIES = old_retries
            db._SUPABASE_RETRY_BASE_SECONDS = old_delay


class ExecuteRetryTests(unittest.TestCase):
    def setUp(self):
        self._old_retries = db._SUPABASE_MAX_RETRIES
        self._old_delay = db._SUPABASE_RETRY_BASE_SECONDS
        db._SUPABASE_MAX_RETRIES = 2
        db._SUPABASE_RETRY_BASE_SECONDS = 0
        db.reset_supabase_client()

    def tearDown(self):
        db._SUPABASE_MAX_RETRIES = self._old_retries
        db._SUPABASE_RETRY_BASE_SECONDS = self._old_delay
        db.reset_supabase_client()

    def test_retries_then_succeeds_with_build_fn(self):
        calls = {"n": 0}
        clients = [mock.Mock(name="c1"), mock.Mock(name="c2"), mock.Mock(name="c3")]

        def build_fn(client):
            builder = mock.Mock()
            calls["n"] += 1
            if calls["n"] < 3:
                builder.execute.side_effect = Exception(
                    "<ConnectionTerminated error_code:0, last_stream_id:3>"
                )
            else:
                resp = mock.Mock()
                resp.data = [{"id": "run-ok"}]
                builder.execute.return_value = resp
            return builder

        with mock.patch.object(db, "get_supabase_client", side_effect=clients), mock.patch.object(
            db, "reset_supabase_client"
        ) as reset_mock:
            result = db._execute(
                "create_scraper_run",
                "scraper_runs",
                platform="catalant",
                build_fn=build_fn,
            )

        self.assertEqual(result.data[0]["id"], "run-ok")
        self.assertEqual(calls["n"], 3)
        self.assertGreaterEqual(reset_mock.call_count, 2)

    def test_exhaustion_raises_network_error(self):
        def build_fn(client):
            builder = mock.Mock()
            builder.execute.side_effect = Exception(
                "<ConnectionTerminated error_code:0, last_stream_id:3>"
            )
            return builder

        with mock.patch.object(db, "get_supabase_client", return_value=mock.Mock()), mock.patch.object(
            db, "reset_supabase_client"
        ), mock.patch.object(db.time, "sleep"):
            with self.assertRaises(db.SupabaseNetworkError) as ctx:
                db._execute(
                    "create_scraper_run",
                    "scraper_runs",
                    platform="catalant",
                    build_fn=build_fn,
                )
        self.assertIn("create_scraper_run", str(ctx.exception))
        self.assertIn("ConnectionTerminated", str(ctx.exception))

    def test_non_transient_raises_api_error_without_retry_loop(self):
        builder = mock.Mock()
        builder.execute.side_effect = Exception("column does_not_exist")

        with mock.patch.object(db, "reset_supabase_client") as reset_mock:
            with self.assertRaises(db.SupabaseAPIError):
                db._execute("op", "projects", builder)

        reset_mock.assert_not_called()
        self.assertEqual(builder.execute.call_count, 1)


class CycleRecoverySupabaseTests(unittest.TestCase):
    def setUp(self):
        import script_clean as sc

        self.sc = sc
        self.old_retries = sc.Config.TAB_CRASH_MAX_RETRIES
        self.old_delay = sc.Config.TAB_CRASH_RETRY_DELAY_SECONDS
        sc.Config.TAB_CRASH_MAX_RETRIES = 1
        sc.Config.TAB_CRASH_RETRY_DELAY_SECONDS = 0

    def tearDown(self):
        self.sc.Config.TAB_CRASH_MAX_RETRIES = self.old_retries
        self.sc.Config.TAB_CRASH_RETRY_DELAY_SECONDS = self.old_delay

    def test_supabase_network_skips_driver_recreate(self):
        sc = self.sc
        driver = mock.Mock(name="driver")
        err = db.SupabaseNetworkError(
            "operation=create_scraper_run table=scraper_runs platform=catalant "
            "project_id=-: <ConnectionTerminated error_code:0>"
        )
        cycle = mock.Mock(side_effect=[err, (False, False)])

        with mock.patch.object(sc, "run_monitoring_cycle", cycle), mock.patch.object(
            sc, "_sleep_interruptible"
        ), mock.patch.object(sc, "initialize_driver") as init_mock, mock.patch.object(
            sc, "setup_session"
        ) as setup_mock, mock.patch.object(db, "reset_supabase_client") as reset_mock, mock.patch.object(
            sc, "send_error_notification"
        ):
            out_driver, cold, first = sc.run_monitoring_cycle_with_browser_recovery(
                driver, cold_start_pending=False, check_number=15
            )

        self.assertIs(out_driver, driver)
        self.assertEqual(cycle.call_count, 2)
        init_mock.assert_not_called()
        setup_mock.assert_not_called()
        reset_mock.assert_called()


if __name__ == "__main__":
    unittest.main()
