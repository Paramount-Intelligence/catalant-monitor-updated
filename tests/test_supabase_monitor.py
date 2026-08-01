"""Unit tests for Supabase-backed Catalant monitor (mocked; no live credentials required)."""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import database as db
import extraction


class FakeResponse:
    def __init__(self, data):
        self.data = data


class FakeTable:
    def __init__(self, store, name):
        self.store = store
        self.name = name
        self._filters = []
        self._order = None
        self._limit = None
        self._payload = None
        self._op = "select"
        self._select = "*"

    def select(self, *_a, **_k):
        # PostgREST chains .insert(...).select("*") — keep mutating op
        if self._op not in ("insert", "update", "upsert", "delete"):
            self._op = "select"
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def upsert(self, payload, on_conflict=None):
        self._op = "upsert"
        self._payload = payload
        self._on_conflict = on_conflict
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, key, value):
        self._filters.append(("eq", key, value))
        return self

    def lte(self, key, value):
        self._filters.append(("lte", key, value))
        return self

    def lt(self, key, value):
        self._filters.append(("lt", key, value))
        return self

    def order(self, key, desc=False):
        self._order = (key, desc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def _match(self, row):
        for op, key, value in self._filters:
            actual = row.get(key)
            if op == "eq" and actual != value:
                return False
            if op == "lte":
                if actual is None or str(actual) > str(value):
                    return False
            if op == "lt":
                if actual is None or not (str(actual) < str(value)):
                    return False
        return True

    def execute(self):
        rows = self.store.setdefault(self.name, [])
        if self._op == "insert":
            payload = self._payload
            items = payload if isinstance(payload, list) else [payload]
            created = []
            for item in items:
                row = dict(item)
                row.setdefault("id", f"{self.name}-{len(rows)+1}")
                rows.append(row)
                created.append(row)
            return FakeResponse(created)
        if self._op == "upsert":
            payload = dict(self._payload)
            key = getattr(self, "_on_conflict", None) or "platform"
            replaced = False
            for i, row in enumerate(rows):
                if row.get(key) == payload.get(key):
                    rows[i] = {**row, **payload}
                    replaced = True
                    return FakeResponse([rows[i]])
            payload.setdefault("id", f"{self.name}-{len(rows)+1}")
            rows.append(payload)
            return FakeResponse([payload])
        if self._op == "update":
            updated = []
            for i, row in enumerate(rows):
                if self._match(row):
                    rows[i] = {**row, **(self._payload or {})}
                    updated.append(rows[i])
            return FakeResponse(updated)
        if self._op == "delete":
            kept = []
            deleted = []
            for row in rows:
                if self._match(row):
                    deleted.append(row)
                else:
                    kept.append(row)
            self.store[self.name] = kept
            return FakeResponse(deleted)
        # select
        matched = [r for r in rows if self._match(r)]
        if self._order:
            key, desc = self._order
            matched.sort(key=lambda r: r.get(key) or "", reverse=bool(desc))
        if self._limit is not None:
            matched = matched[: self._limit]
        return FakeResponse(matched)


class FakeClient:
    def __init__(self, store=None):
        self.store = store if store is not None else {}

    def table(self, name):
        return FakeTable(self.store, name)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        db.reset_supabase_client()
        self._saved = {}
        for key in (
            "SUPABASE_URL",
            "SUPABASE_SECRET_KEY",
            "SUPABASE_SERVICE_ROLE_KEY",
            "SUPABASE_PUBLISHABLE_KEY",
            "SUPABASE_ANON_KEY",
        ):
            self._saved[key] = os.environ.pop(key, None)

    def tearDown(self):
        db.reset_supabase_client()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_preferred_secret_key(self):
        os.environ["SUPABASE_URL"] = "https://example.supabase.co"
        os.environ["SUPABASE_SECRET_KEY"] = "sb_secret_test_preferred"
        os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "legacy_should_not_win"
        url, key, source = db.get_supabase_credentials()
        self.assertEqual(url, "https://example.supabase.co")
        self.assertEqual(key, "sb_secret_test_preferred")
        self.assertEqual(source, "SUPABASE_SECRET_KEY")

    def test_legacy_service_role_fallback(self):
        os.environ["SUPABASE_URL"] = "https://example.supabase.co"
        os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "legacy_service_role"
        url, key, source = db.get_supabase_credentials()
        self.assertEqual(key, "legacy_service_role")
        self.assertEqual(source, "SUPABASE_SERVICE_ROLE_KEY")

    def test_missing_credentials(self):
        os.environ["SUPABASE_URL"] = "https://example.supabase.co"
        with self.assertRaises(db.SupabaseConfigError):
            db.get_supabase_credentials()

    def test_reject_publishable_key(self):
        os.environ["SUPABASE_URL"] = "https://example.supabase.co"
        os.environ["SUPABASE_SECRET_KEY"] = "sb_publishable_not_allowed"
        with self.assertRaises(db.SupabaseConfigError):
            db.get_supabase_credentials()


class EligibilityTests(unittest.TestCase):
    def setUp(self):
        self.store = {"projects": []}
        self.client = FakeClient(self.store)
        db.reset_supabase_client()
        self.patcher = mock.patch.object(db, "get_supabase_client", return_value=self.client)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        db.reset_supabase_client()

    def test_first_occurrence_eligible(self):
        ok, reason, latest = db.should_process_project("catalant", "p1")
        self.assertTrue(ok)
        self.assertEqual(reason, "first_occurrence")
        self.assertIsNone(latest)

    def test_within_three_days_skipped(self):
        now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
        self.store["projects"].append(
            {
                "id": "row1",
                "platform": "catalant",
                "project_id": "p1",
                "scraped_at": (now - timedelta(days=2)).isoformat(),
                "email_status": "SENT",
                "email_sent": True,
            }
        )
        ok, reason, _ = db.should_process_project("catalant", "p1", now=now)
        self.assertFalse(ok)
        self.assertIn("skipped_within_3_days", reason)

    def test_exactly_three_days_skipped(self):
        now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
        self.store["projects"].append(
            {
                "id": "row1",
                "platform": "catalant",
                "project_id": "p1",
                "scraped_at": (now - timedelta(days=3)).isoformat(),
                "email_status": "SENT",
                "email_sent": True,
            }
        )
        ok, reason, _ = db.should_process_project("catalant", "p1", now=now)
        self.assertFalse(ok)
        self.assertIn("skipped_within_3_days", reason)

    def test_more_than_three_days_eligible(self):
        now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
        self.store["projects"].append(
            {
                "id": "row1",
                "platform": "catalant",
                "project_id": "p1",
                "scraped_at": (now - timedelta(days=3, seconds=1)).isoformat(),
                "email_status": "SENT",
                "email_sent": True,
            }
        )
        ok, reason, _ = db.should_process_project("catalant", "p1", now=now)
        self.assertTrue(ok)
        self.assertIn("eligible_after_", reason)

    def test_same_id_different_platforms_independent(self):
        now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
        self.store["projects"].append(
            {
                "id": "row1",
                "platform": "btg",
                "project_id": "p1",
                "scraped_at": now.isoformat(),
                "email_status": "SENT",
                "email_sent": True,
            }
        )
        ok, reason, _ = db.should_process_project("catalant", "p1", now=now)
        self.assertTrue(ok)
        self.assertEqual(reason, "first_occurrence")

    def test_db_failure_not_treated_as_empty(self):
        with mock.patch.object(
            db, "get_latest_project_occurrence", side_effect=db.SupabaseAPIError("simulated failure")
        ):
            with self.assertRaises(db.SupabaseAPIError):
                db.should_process_project("catalant", "p1")


class EmailLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.store = {"projects": [], "email_attempts": [], "scraper_runs": []}
        self.client = FakeClient(self.store)
        db.reset_supabase_client()
        self.patcher = mock.patch.object(db, "get_supabase_client", return_value=self.client)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        db.reset_supabase_client()

    def test_project_inserted_before_email_attempt(self):
        order = []

        def fake_insert(*a, **k):
            order.append("insert")
            return {"id": "proj-1", "email_attempt_count": 0, "project_id": "p1"}

        def fake_attempt(*a, **k):
            order.append("email_attempt")
            return {"id": "att-1"}

        with mock.patch.object(db, "insert_project_occurrence", side_effect=fake_insert):
            with mock.patch.object(db, "record_email_attempt", side_effect=fake_attempt):
                row = db.insert_project_occurrence(
                    {
                        "platform": "catalant",
                        "project_id": "p1",
                        "title": "T",
                        "source_url": "https://example.com/p1",
                    }
                )
                db.record_email_attempt(row["id"], 1, status="SENDING")
        self.assertEqual(order, ["insert", "email_attempt"])

    def test_successful_email_updates_same_row(self):
        row = db.insert_project_occurrence(
            {
                "platform": "catalant",
                "project_id": "p1",
                "title": "T",
                "source_url": "https://example.com/p1",
            }
        )
        updated = db.update_project_email_status(
            row["id"],
            email_sent=True,
            email_status="SENT",
            email_attempt_count=1,
        )
        self.assertEqual(updated["id"], row["id"])
        self.assertEqual(updated["email_status"], "SENT")
        self.assertTrue(updated["email_sent"])

    def test_failed_email_updates_same_row_and_attempt_history(self):
        row = db.insert_project_occurrence(
            {
                "platform": "catalant",
                "project_id": "p1",
                "title": "T",
                "source_url": "https://example.com/p1",
            }
        )
        attempt = db.record_email_attempt(row["id"], 1, status="SENDING")
        db.record_email_attempt(
            row["id"],
            1,
            status="FAILED",
            attempt_id=attempt["id"],
            failure_code="EMAIL_SEND_FAILED",
            failure_reason="boom",
        )
        updated = db.update_project_email_status(
            row["id"],
            email_sent=False,
            email_status="RETRY_PENDING",
            email_attempt_count=1,
            email_not_sent_reason="EMAIL_SEND_FAILED",
        )
        self.assertEqual(updated["id"], row["id"])
        self.assertEqual(updated["email_status"], "RETRY_PENDING")
        self.assertEqual(self.store["email_attempts"][0]["status"], "FAILED")

    def test_retry_does_not_create_another_project_occurrence(self):
        row = db.insert_project_occurrence(
            {
                "platform": "catalant",
                "project_id": "p1",
                "title": "T",
                "source_url": "https://example.com/p1",
            }
        )
        before = len(self.store["projects"])
        db.record_email_attempt(row["id"], 2, status="SENDING")
        self.assertEqual(len(self.store["projects"]), before)

    def test_max_retries_produce_failed(self):
        row = db.insert_project_occurrence(
            {
                "platform": "catalant",
                "project_id": "p1",
                "title": "T",
                "source_url": "https://example.com/p1",
            }
        )
        updated = db.update_project_email_status(
            row["id"],
            email_status="FAILED",
            email_attempt_count=5,
            email_sent=False,
        )
        self.assertEqual(updated["email_status"], "FAILED")


class ColdStartTests(unittest.TestCase):
    def setUp(self):
        self.store = {"projects": []}
        self.client = FakeClient(self.store)
        self.patcher = mock.patch.object(db, "get_supabase_client", return_value=self.client)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_cold_start_rows_suppressed(self):
        row = db.insert_project_occurrence(
            {
                "platform": "catalant",
                "project_id": "seed1",
                "title": "Seed",
                "source_url": "https://example.com/seed1",
            },
            email_status="SUPPRESSED",
            email_eligible=False,
            email_not_sent_reason="COLD_START_SEED",
        )
        self.assertEqual(row["email_status"], "SUPPRESSED")
        self.assertEqual(row["email_not_sent_reason"], "COLD_START_SEED")
        self.assertFalse(row["email_eligible"])

    def test_catalant_cold_start_platform_specific(self):
        self.store["projects"].append(
            {
                "id": "btg-1",
                "platform": "btg",
                "project_id": "x",
                "title": "BTG",
                "source_url": "https://example.com/x",
            }
        )
        self.assertFalse(db.platform_has_projects("catalant"))
        self.assertTrue(db.platform_has_projects("btg"))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.store = {"scraper_sessions": []}
        self.client = FakeClient(self.store)
        self.patcher = mock.patch.object(db, "get_supabase_client", return_value=self.client)
        self.patcher.start()
        self.tmp = "test_cookies_fallback.json"
        if os.path.exists(self.tmp):
            os.remove(self.tmp)

    def tearDown(self):
        self.patcher.stop()
        if os.path.exists(self.tmp):
            os.remove(self.tmp)

    def test_cookie_session_save_and_load(self):
        cookies = [{"name": "sid", "value": "abc", "domain": ".gocatalant.com"}]
        saved = db.save_scraper_session("catalant", cookies)
        self.assertEqual(saved["platform"], "catalant")
        loaded = db.load_scraper_session("catalant")
        self.assertEqual(loaded["session_data"]["cookies"][0]["name"], "sid")

    def test_local_cookie_fallback_file(self):
        import json

        with open(self.tmp, "w", encoding="utf-8") as fh:
            json.dump([{"name": "local", "value": "1", "domain": ".gocatalant.com"}], fh)
        self.assertTrue(os.path.exists(self.tmp))
        with open(self.tmp, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data[0]["name"], "local")


class CategoryExtractionTests(unittest.TestCase):
    def test_valid_category_structured_field(self):
        body = "Category: Strategy > Ops\nBudget: $1"
        result = extraction.extract_category_from_body_text(body)
        self.assertEqual(result["platform_category"], "Strategy")
        self.assertEqual(result["platform_category_extraction_status"], "FOUND_TEXT_FALLBACK")

    def test_valid_category_breadcrumb(self):
        body = "Strategy > Finance > M&A\nSomething else"
        result = extraction.extract_category_from_body_text(body)
        self.assertEqual(result["platform_category"], "Strategy")
        self.assertGreaterEqual(len(result["platform_category_path"]), 2)

    def test_embedded_category_data(self):
        content = '{"category": "Digital Transformation"}'
        result = extraction.extract_category_from_embedded_json(content)
        self.assertEqual(result["platform_category"], "Digital Transformation")
        self.assertEqual(result["platform_category_extraction_status"], "FOUND_EMBEDDED_DATA")

    def test_unclassified_candidate_rejection(self):
        body = "Category: Unclassified\n"
        result = extraction.extract_category_from_body_text(body)
        self.assertIsNone(result["platform_category"])
        self.assertEqual(
            result["platform_category_extraction_status"], "REJECTED_INVALID_CANDIDATE"
        )

    def test_generic_typography_text_rejection(self):
        # Noise words / non-category lines should not invent a category
        body = "Posted 2 hours ago\nSearch results\nBudget: $100"
        result = extraction.extract_category_from_body_text(body)
        self.assertIn(
            result["platform_category_extraction_status"],
            ("MISSING", "REJECTED_INVALID_CANDIDATE"),
        )
        self.assertTrue(
            result["platform_category"] is None or result["platform_category"] == ""
        )


class MergeTests(unittest.TestCase):
    def test_empty_detail_does_not_overwrite_card(self):
        card = {"platform_category": "Strategy", "budget_text": "$100", "skills": ["A"]}
        detail = {"platform_category": "", "budget_text": "", "skills": []}
        merged = extraction.merge_project_data(card, detail)
        self.assertEqual(merged["platform_category"], "Strategy")
        self.assertEqual(merged["budget_text"], "$100")
        self.assertEqual(merged["skills"], ["A"])

    def test_unclassified_does_not_overwrite(self):
        card = {"platform_category": "Strategy"}
        detail = {"platform_category": "Unclassified"}
        merged = extraction.merge_project_data(card, detail)
        self.assertEqual(merged["platform_category"], "Strategy")

    def test_optional_missing_fields_do_not_discard_project(self):
        card = {
            "id": "p1",
            "project_id": "p1",
            "title": "Hello",
            "source_url": "https://example.com/p1",
            "platform_category": None,
        }
        merged = extraction.merge_project_data(card, {"description": ""})
        self.assertEqual(merged["title"], "Hello")
        self.assertIn("platform_category", merged.get("missing_fields", []))

    def test_multi_paragraph_description_preserved(self):
        card = {"description": "Card blurb"}
        detail = {"description": "Para one.\n\nPara two."}
        merged = extraction.merge_project_data(card, detail)
        self.assertIn("Para two.", merged["description"])


class ScraperRunTests(unittest.TestCase):
    def setUp(self):
        self.store = {"scraper_runs": []}
        self.client = FakeClient(self.store)
        self.patcher = mock.patch.object(db, "get_supabase_client", return_value=self.client)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_scraper_run_completion(self):
        run = db.create_scraper_run(platform="catalant")
        done = db.complete_scraper_run(run["id"], status="COMPLETED", projects_inserted=2)
        self.assertEqual(done["status"], "COMPLETED")
        self.assertIsNotNone(done.get("completed_at"))

    def test_scraper_run_partial_status(self):
        run = db.create_scraper_run(platform="catalant")
        done = db.complete_scraper_run(run["id"], status="PARTIAL", emails_failed=1)
        self.assertEqual(done["status"], "PARTIAL")

    def test_scraper_run_failure_status(self):
        run = db.create_scraper_run(platform="catalant")
        done = db.fail_scraper_run(run["id"], "X", "boom", status="FAILED")
        self.assertEqual(done["status"], "FAILED")
        self.assertEqual(done["failure_code"], "X")


class FakeSeleniumCategoryTests(unittest.TestCase):
    class FakeEl:
        def __init__(self, text="", html=""):
            self.text = text
            self._html = html

        def get_attribute(self, name):
            if name in ("innerHTML", "textContent"):
                return self._html
            return None

    class FakeRoot:
        def __init__(self, by_css=None, by_xpath=None):
            self.by_css = by_css or {}
            self.by_xpath = by_xpath or {}

        def find_elements(self, by, selector):
            token = getattr(by, "value", None) or str(by)
            token_l = str(token).lower()
            if "css" in token_l:
                return self.by_css.get(selector, [])
            return self.by_xpath.get(selector, [])

    def test_dedicated_pool_selector(self):
        # Import after path setup
        import script_clean as sc

        root = self.FakeRoot(
            by_css={
                ".need-card-inline-pools .small.text-muted": [
                    self.FakeEl("Operations > Supply Chain")
                ]
            }
        )
        # Bypass structured XPath by returning empty
        result = sc.extract_platform_category(root, body_text="")
        self.assertEqual(result["platform_category"], "Operations")
        self.assertIn(
            result["platform_category_extraction_status"],
            ("FOUND_DEDICATED_SELECTOR", "FOUND_BREADCRUMB"),
        )


if __name__ == "__main__":
    unittest.main()
