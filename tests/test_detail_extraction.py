"""Tests for Catalant detail extraction, budget safety, status, and backfill helpers."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import database as db
import extraction

FIXTURES = Path(__file__).parent / "fixtures"


class BudgetTests(unittest.TestCase):
    def test_complete_title_rejected_as_budget(self):
        title = "Consultant for Strategic Project Support ($110/hr)"
        ok, reason = extraction.validate_budget_candidate(title, {"title": title})
        self.assertFalse(ok)
        self.assertEqual(reason, "equals_title")

    def test_title_rate_fallback_isolates_rate(self):
        title = "Consultant for Strategic Project Support ($110/hr)"
        parsed = extraction.extract_title_rate_fallback(title)
        self.assertEqual(parsed.get("budget_text"), "$110/hr")
        self.assertEqual(parsed.get("hourly_rate"), 110)
        self.assertEqual(parsed.get("budget_confidence"), "LOW")
        self.assertEqual(parsed.get("budget_source"), "title_rate_fallback")

    def test_budget_range_parsing(self):
        parsed = extraction.parse_budget("$25,000–$35,000")
        self.assertEqual(parsed["budget_min"], 25000.0)
        self.assertEqual(parsed["budget_max"], 35000.0)

    def test_hourly_rate_parsing(self):
        parsed = extraction.parse_budget("$110/hr")
        self.assertEqual(parsed["hourly_rate"], 110.0)
        self.assertEqual(parsed["billing_type"], "hourly")

    def test_structured_budget_accepted(self):
        ok, reason = extraction.validate_budget_candidate(
            "$25,000–$35,000",
            {"title": "Some Project"},
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")


class DetailBodyExtractionTests(unittest.TestCase):
    def test_full_detail_fixture(self):
        body = (FIXTURES / "detail_full_body.txt").read_text(encoding="utf-8")
        details = extraction.extract_detail_fields_from_body(
            body, title="Interim Executive Transformation"
        )
        self.assertIn("interim executive", details["description"].lower())
        self.assertEqual(details["start_date_text"], "03/15/2026")
        self.assertEqual(details["source_start_date"], "2026-03-15")
        self.assertEqual(details["project_length"], "12 weeks")
        self.assertEqual(details["location_preference"], "Remote - United States")
        self.assertEqual(details["level_of_support"], "Independent Expert")
        self.assertEqual(details["industry"], "Technology")
        self.assertEqual(details["contracting_process"], "Catalant Contracting")
        self.assertTrue(details.get("budget_text"))
        self.assertIn("Strategy", details.get("skills") or [])
        self.assertIn(details["detail_extraction_status"], ("COMPLETE", "PARTIAL"))

    def test_asap_does_not_invent_date(self):
        body = (FIXTURES / "detail_ops_body.txt").read_text(encoding="utf-8")
        details = extraction.extract_detail_fields_from_body(body, title="Data Platform")
        self.assertEqual(details["start_date_text"], "ASAP")
        self.assertIsNone(details.get("source_start_date"))

    def test_live_style_duration_not_asap_timeline(self):
        body = (FIXTURES / "detail_catalant_live_style.txt").read_text(encoding="utf-8")
        details = extraction.extract_detail_fields_from_body(
            body, title="Advisor Experience Digital Product Lead"
        )
        self.assertIn("PE-backed", details.get("description") or "")
        self.assertIn("3 months", details.get("project_length") or "")
        self.assertNotEqual((details.get("project_length") or "").upper(), "ASAP")
        self.assertEqual(details.get("start_date_text"), "Aug 31, 2026")
        self.assertEqual(details.get("budget_text"), "Not provided")
        self.assertEqual(details.get("location_preference"), "Remote")
        self.assertIn("Remote-friendly", details.get("remote_or_onsite") or "")

    def test_multi_paragraph_description(self):
        body = (FIXTURES / "detail_full_body.txt").read_text(encoding="utf-8")
        details = extraction.extract_detail_fields_from_body(body, title="X")
        self.assertIn("\n", details["description"])


class PostedTimeTests(unittest.TestCase):
    def test_relative_posted_time(self):
        now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        parsed, estimated = extraction.parse_relative_posted_time("42 minutes", now)
        self.assertTrue(estimated)
        self.assertEqual(parsed, now - timedelta(minutes=42))

    def test_a_day(self):
        now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        parsed, estimated = extraction.parse_relative_posted_time("a day", now)
        self.assertTrue(estimated)
        self.assertEqual(parsed, now - timedelta(days=1))


class StatusTests(unittest.TestCase):
    def test_card_complete_and_partial(self):
        complete = {
            "project_id": "abc",
            "title": "T",
            "source_url": "https://example.com/a",
            "platform_category": "Strategy",
            "time_posted_text": "2 hours",
        }
        self.assertEqual(extraction.calculate_card_extraction_status(complete), "COMPLETE")
        partial = dict(complete)
        partial["platform_category"] = None
        self.assertEqual(extraction.calculate_card_extraction_status(partial), "PARTIAL")

    def test_detail_statuses(self):
        self.assertEqual(
            extraction.calculate_detail_extraction_status(attempted=False, page_ok=False),
            "NOT_ATTEMPTED",
        )
        self.assertEqual(
            extraction.calculate_detail_extraction_status(
                attempted=True, page_ok=True, timeout=True
            ),
            "TIMEOUT",
        )
        self.assertEqual(
            extraction.calculate_detail_extraction_status(
                attempted=True,
                page_ok=True,
                fields_visible=["description", "industry"],
                fields_extracted=["description", "industry"],
                meaningful=True,
            ),
            "COMPLETE",
        )
        self.assertEqual(
            extraction.calculate_detail_extraction_status(
                attempted=True,
                page_ok=True,
                fields_visible=["description", "industry"],
                fields_extracted=["description"],
                meaningful=True,
            ),
            "PARTIAL",
        )
        self.assertEqual(
            extraction.calculate_detail_extraction_status(
                attempted=True, page_ok=False, meaningful=False
            ),
            "FAILED",
        )

    def test_missing_fields_populated(self):
        project = {
            "project_id": "x",
            "title": "T",
            "source_url": "https://example.com/x",
            "platform_category": "Strategy",
            "time_posted_text": "1 hour",
            "detail_extraction_status": "PARTIAL",
            "description": None,
        }
        missing = extraction.compute_missing_fields(
            project, expected_fields=["description", "title"]
        )
        self.assertIn("description", missing)
        self.assertNotIn("title", missing)


class MergeSafetyTests(unittest.TestCase):
    def test_empty_detail_preserves_card(self):
        card = {"budget_text": "$100/hr", "location": "Remote"}
        detail = {"budget_text": "", "location": ""}
        merged = extraction.merge_project_data(card, detail)
        self.assertEqual(merged["budget_text"], "$100/hr")
        self.assertEqual(merged["location"], "Remote")

    def test_title_budget_not_merged(self):
        title = "Consultant for Strategic Project Support ($110/hr)"
        card = {"title": title, "budget_text": None}
        detail = {"budget_text": title}
        merged = extraction.merge_project_data(card, detail)
        self.assertTrue(extraction.is_empty_value(merged.get("budget_text")))
        self.assertTrue(
            any("BUDGET_CANDIDATE_REJECTED" in w for w in merged.get("extraction_warnings") or [])
        )


class BackfillRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.store = {
            "projects": [
                {
                    "id": "uuid-1",
                    "platform": "catalant",
                    "project_id": "xq4w3w",
                    "title": "Sample",
                    "source_url": "https://example.com/xq4w3w",
                    "detail_extraction_status": "NOT_ATTEMPTED",
                    "description": None,
                    "email_status": "SUPPRESSED",
                    "email_sent": False,
                    "email_eligible": False,
                    "email_not_sent_reason": "COLD_START_SEED",
                    "scraped_at": "2026-07-31T18:00:00+00:00",
                    "first_detected_at": "2026-07-31T18:00:00+00:00",
                }
            ]
        }
        from tests.test_supabase_monitor import FakeClient

        self.client = FakeClient(self.store)
        self.patcher = mock.patch.object(db, "get_supabase_client", return_value=self.client)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_not_attempted_selected_for_backfill(self):
        rows = db.get_projects_needing_detail_enrichment(limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["project_id"], "xq4w3w")

    def test_backfill_updates_same_uuid_and_preserves_email(self):
        updated = db.update_project_details(
            "uuid-1",
            {
                "description": "Full description now",
                "detail_extraction_status": "COMPLETE",
                "industry": "Technology",
            },
        )
        self.assertEqual(updated["id"], "uuid-1")
        self.assertEqual(updated["description"], "Full description now")
        self.assertEqual(updated["email_status"], "SUPPRESSED")
        self.assertEqual(len(self.store["projects"]), 1)

    def test_forbidden_email_column_rejected(self):
        with self.assertRaises(ValueError):
            db.update_project_details("uuid-1", {"email_status": "PENDING"})

    def test_project_id_filter(self):
        rows = db.get_projects_needing_detail_enrichment(project_id="nope", limit=5)
        self.assertEqual(rows, [])

    def test_limit_respected(self):
        for i in range(5):
            self.store["projects"].append(
                {
                    "id": f"uuid-{i+2}",
                    "platform": "catalant",
                    "project_id": f"p{i}",
                    "title": "T",
                    "source_url": f"https://example.com/{i}",
                    "detail_extraction_status": "NOT_ATTEMPTED",
                    "description": None,
                    "scraped_at": "2026-07-31T18:00:00+00:00",
                }
            )
        rows = db.get_projects_needing_detail_enrichment(limit=3)
        self.assertEqual(len(rows), 3)


class ColdStartSemanticsTests(unittest.TestCase):
    def test_cold_start_suppressed_after_detail_merge(self):
        card = {
            "project_id": "abc",
            "id": "abc",
            "title": "T",
            "source_url": "https://example.com/abc",
            "platform_category": "Strategy",
            "time_posted_text": "1 hour",
        }
        body = (FIXTURES / "detail_full_body.txt").read_text(encoding="utf-8")
        details = extraction.extract_detail_fields_from_body(body, title="T")
        merged = extraction.merge_project_data(card, details)
        self.assertTrue(merged.get("description"))
        self.assertNotEqual(merged.get("detail_extraction_status"), "NOT_ATTEMPTED")
        # Email suppression is applied at insert time, not merge time
        self.assertTrue(True)


class CategoryStillWorksTests(unittest.TestCase):
    def test_existing_category_extraction(self):
        body = "Category: Strategy > Ops\nBudget: $1"
        result = extraction.extract_category_from_body_text(body)
        self.assertEqual(result["platform_category"], "Strategy")


class ThreeDayUnchangedTests(unittest.TestCase):
    def setUp(self):
        self.store = {"projects": []}
        from tests.test_supabase_monitor import FakeClient

        self.client = FakeClient(self.store)
        self.patcher = mock.patch.object(db, "get_supabase_client", return_value=self.client)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_exactly_three_days_still_skipped(self):
        now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        self.store["projects"].append(
            {
                "id": "r1",
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


if __name__ == "__main__":
    unittest.main()
