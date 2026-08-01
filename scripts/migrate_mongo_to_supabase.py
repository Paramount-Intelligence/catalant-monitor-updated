#!/usr/bin/env python
"""
Optional one-time MongoDB -> Supabase migration.

Never imported by the production scraper.
Usage:
  python scripts/migrate_mongo_to_supabase.py --dry-run
  python scripts/migrate_mongo_to_supabase.py --batch-size 100
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow importing repository modules
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import database as db  # noqa: E402


def _parse_dt(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def map_mongo_doc(doc: dict) -> dict:
    project_id = str(doc.get("project_id") or doc.get("id") or "").strip()
    title = (doc.get("title") or "").strip() or f"Migrated project {project_id}"
    source_url = (doc.get("url") or doc.get("source_url") or "").strip()
    if not source_url and project_id:
        source_url = f"https://app.gocatalant.com/c/_/u/0/need/{project_id}/"

    emailed = bool(doc.get("emailed"))
    email_status = "SENT" if emailed else "NOT_REQUIRED"
    email_not_sent_reason = None
    # Conservative mapping for ambiguous false
    if not emailed:
        email_status = "NOT_REQUIRED"
        email_not_sent_reason = "MONGO_MIGRATION_AMBIGUOUS"

    scraped = (
        _parse_dt(doc.get("scraped_at"))
        or _parse_dt(doc.get("detected_at"))
        or datetime.now(timezone.utc)
    )

    raw = {k: v for k, v in doc.items() if k not in {"_id", "password", "cookies"}}
    # Make raw JSON-safe-ish
    for key, value in list(raw.items()):
        if isinstance(value, datetime):
            raw[key] = value.isoformat()
        else:
            try:
                raw[key] = value
            except Exception:
                raw[key] = str(value)

    migration_key = str(doc.get("dedupe_key") or doc.get("_id") or f"{project_id}|{scraped.isoformat()}")

    return {
        "platform": (doc.get("platform") or "catalant").strip().lower(),
        "project_id": project_id,
        "title": title,
        "source_url": source_url,
        "short_description": doc.get("description"),
        "description": doc.get("description"),
        "status": doc.get("status"),
        "platform_category": doc.get("platform_category") or None,
        "location": doc.get("location"),
        "location_preference": doc.get("location_pref") or doc.get("location_preference"),
        "budget_text": doc.get("budget") or doc.get("budget_text"),
        "duration_text": doc.get("duration") or doc.get("duration_text"),
        "project_length": doc.get("project_length"),
        "start_date_text": doc.get("start_date") or doc.get("start_date_text"),
        "level_of_support": doc.get("level_of_support"),
        "industry": doc.get("industry"),
        "contracting_process": doc.get("contracting") or doc.get("contracting_process"),
        "time_posted_text": doc.get("time_posted") or doc.get("time_posted_text"),
        "scraped_at": scraped.isoformat(),
        "first_detected_at": scraped.isoformat(),
        "last_seen_at": scraped.isoformat(),
        "email_status": email_status,
        "email_sent": emailed,
        "email_eligible": emailed,
        "email_not_sent_reason": email_not_sent_reason,
        "raw_data": {
            "mongo_migration_key": migration_key,
            "mongo_document": raw,
        },
        "extraction_metadata": {"migrated_from": "mongodb"},
        "platform_category_extraction_status": (
            "FOUND_STRUCTURED" if doc.get("platform_category") else "MISSING"
        ),
    }


def already_migrated(client, migration_key: str) -> bool:
    # Best-effort idempotency via raw_data key scan (bounded)
    response = (
        client.table("projects")
        .select("id,raw_data")
        .contains("raw_data", {"mongo_migration_key": migration_key})
        .limit(1)
        .execute()
    )
    data = getattr(response, "data", None) or []
    return bool(data)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--limit", type=int, default=0, help="0 = no limit")
    args = parser.parse_args(argv)

    mongo_uri = os.getenv("MONGO_URI")
    if not mongo_uri:
        print("MONGO_URI is required for this optional migration script")
        return 2

    try:
        from pymongo import MongoClient
    except ImportError:
        print("pymongo is required only for this migration script: pip install pymongo")
        return 2

    client = db.get_supabase_client()
    mongo = MongoClient(mongo_uri)
    coll = mongo["office_monitor"]["projects"]

    read = inserted = skipped = failed = 0
    cursor = coll.find({})
    if args.limit:
        cursor = cursor.limit(args.limit)

    batch = []
    for doc in cursor:
        read += 1
        try:
            mapped = map_mongo_doc(doc)
            migration_key = mapped["raw_data"]["mongo_migration_key"]
            if already_migrated(client, migration_key):
                skipped += 1
                continue
            if args.dry_run:
                print(f"[dry-run] would insert {mapped['platform']}/{mapped['project_id']}")
                inserted += 1
                continue
            batch.append(mapped)
            if len(batch) >= args.batch_size:
                for item in batch:
                    db.insert_project_occurrence(
                        item,
                        email_status=item["email_status"],
                        email_eligible=item["email_eligible"],
                        email_sent=item["email_sent"],
                        email_not_sent_reason=item.get("email_not_sent_reason"),
                    )
                    inserted += 1
                batch = []
        except Exception as exc:
            failed += 1
            print(f"failed: {db.redact_db_error(exc)}")

    if batch and not args.dry_run:
        for item in batch:
            try:
                db.insert_project_occurrence(
                    item,
                    email_status=item["email_status"],
                    email_eligible=item["email_eligible"],
                    email_sent=item["email_sent"],
                    email_not_sent_reason=item.get("email_not_sent_reason"),
                )
                inserted += 1
            except Exception as exc:
                failed += 1
                print(f"failed: {db.redact_db_error(exc)}")

    print(
        f"Migration summary: read={read} inserted={inserted} "
        f"skipped={skipped} failed={failed} dry_run={args.dry_run}"
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
