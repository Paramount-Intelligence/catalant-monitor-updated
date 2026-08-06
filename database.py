"""
Supabase PostgreSQL data-access layer for Catalant (and future marketplace scrapers).

Normal scraper runtime uses only this module for database I/O — no MongoDB.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

PLATFORM_CATALANT = "catalant"
SCRAPER_NAME = "catalant-monitor"
SCRAPER_VERSION = "2.0.0"


def get_occurrence_window_days() -> int:
    """
    Days between eligible re-occurrences for the same platform + project_id.

    Env (first match wins):
      OCCURRENCE_WINDOW_DAYS
      REPOST_MIN_DAYS
    Default: 3
    """
    raw = os.getenv("OCCURRENCE_WINDOW_DAYS")
    if raw is None or str(raw).strip() == "":
        raw = os.getenv("REPOST_MIN_DAYS", "3")
    try:
        days = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise SupabaseConfigError(
            f"OCCURRENCE_WINDOW_DAYS/REPOST_MIN_DAYS must be an integer, got {raw!r}"
        ) from exc
    if days < 0:
        raise SupabaseConfigError(
            f"OCCURRENCE_WINDOW_DAYS/REPOST_MIN_DAYS must be >= 0, got {days}"
        )
    return days


def get_occurrence_window() -> timedelta:
    return timedelta(days=get_occurrence_window_days())


_supabase_client = None


class SupabaseConfigError(RuntimeError):
    """Missing or invalid Supabase configuration."""


class SupabaseNetworkError(RuntimeError):
    """Network / transport failure talking to Supabase."""


class SupabaseAPIError(RuntimeError):
    """Supabase API returned an error or unexpected payload."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime] = None) -> str:
    value = dt or _utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_timestamptz(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SupabaseAPIError(f"Invalid timestamptz value: {text[:64]}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def redact_db_error(value: Any) -> str:
    """Redact secrets from database error strings (no keys, no session payloads)."""
    if value is None:
        return ""
    out = str(value)
    for secret in (
        os.getenv("SUPABASE_SECRET_KEY"),
        os.getenv("SUPABASE_SERVICE_ROLE_KEY"),
        os.getenv("SUPABASE_ACCESS_TOKEN"),
        os.getenv("SUPABASE_DB_PASSWORD"),
        os.getenv("SUPABASE_DB_URL"),
        os.getenv("CATALANT_PASSWORD"),
        os.getenv("SENDER_PASSWORD"),
        os.getenv("MONGO_URI"),
    ):
        if secret:
            out = out.replace(secret, "[REDACTED]")
    out = re.sub(
        r"(?i)(sb_secret_|sb_publishable_|eyJ)[A-Za-z0-9._\-]+",
        "[REDACTED_KEY]",
        out,
    )
    out = re.sub(
        r"(?i)(apikey|authorization|service[_-]?role|secret[_-]?key)\s*[:=]\s*\S+",
        r"\1=[REDACTED]",
        out,
    )
    out = re.sub(
        r"(postgresql(?:\+?\w*)?://)([^:@/\s]+):([^@/\s]+)@",
        r"\1[REDACTED_USER]:[REDACTED_PASSWORD]@",
        out,
        flags=re.IGNORECASE,
    )
    return out


def get_supabase_credentials() -> tuple[str, str, str]:
    """
    Return (url, key, key_source).
    Prefer SUPABASE_SECRET_KEY; fall back to SUPABASE_SERVICE_ROLE_KEY only.
    Never accepts publishable/anonymous keys.
    """
    url = (os.getenv("SUPABASE_URL") or "").strip()
    secret = (os.getenv("SUPABASE_SECRET_KEY") or "").strip()
    legacy = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    publishable = (os.getenv("SUPABASE_PUBLISHABLE_KEY") or os.getenv("SUPABASE_ANON_KEY") or "").strip()

    if not url:
        raise SupabaseConfigError("SUPABASE_URL is required")

    if secret:
        key, source = secret, "SUPABASE_SECRET_KEY"
    elif legacy:
        key, source = legacy, "SUPABASE_SERVICE_ROLE_KEY"
    else:
        raise SupabaseConfigError(
            "SUPABASE_SECRET_KEY is required "
            "(or legacy SUPABASE_SERVICE_ROLE_KEY for compatibility)"
        )

    lowered = key.lower()
    if publishable and key == publishable:
        raise SupabaseConfigError(
            "Publishable/anonymous Supabase keys cannot be used for scraper writes"
        )
    if "publishable" in lowered or lowered.startswith("sb_publishable_"):
        raise SupabaseConfigError(
            "Publishable/anonymous Supabase keys cannot be used for scraper writes"
        )
    if "anon" in source.lower() or lowered.startswith("eyj") and "role\":\"anon" in key:
        # JWT anon keys are rejected; service_role JWTs are allowed via legacy fallback.
        try:
            import base64
            import json as _json

            payload_b64 = key.split(".")[1]
            pad = "=" * (-len(payload_b64) % 4)
            payload = _json.loads(base64.urlsafe_b64decode(payload_b64 + pad))
            if payload.get("role") == "anon":
                raise SupabaseConfigError(
                    "Anonymous Supabase keys cannot be used for scraper writes"
                )
        except SupabaseConfigError:
            raise
        except Exception:
            pass

    return url, key, source


def reset_supabase_client() -> None:
    """Clear the cached client (tests only)."""
    global _supabase_client
    _supabase_client = None


def get_supabase_client():
    """
    Return a reusable Supabase client.
    Raises SupabaseConfigError / SupabaseNetworkError — never returns None.
    """
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client

    url, key, _source = get_supabase_credentials()
    try:
        from supabase import create_client
    except ImportError as exc:
        raise SupabaseConfigError(
            "supabase package is not installed or broken; "
            "run: python -m pip install -r requirements.txt"
        ) from exc

    try:
        _supabase_client = create_client(url, key)
    except Exception as exc:
        msg = redact_db_error(exc).lower()
        if any(tok in msg for tok in ("config", "key", "url", "credential", "invalid api")):
            raise SupabaseConfigError(redact_db_error(exc)) from exc
        raise SupabaseNetworkError(redact_db_error(exc)) from exc

    if _supabase_client is None:
        raise SupabaseConfigError("Supabase client initialization returned None")
    return _supabase_client


def _execute(operation: str, table: str, builder, platform: str = "", project_id: str = ""):
    """Run a PostgREST builder and normalize errors."""
    try:
        response = builder.execute()
    except SupabaseConfigError:
        raise
    except Exception as exc:
        msg = redact_db_error(exc)
        lowered = msg.lower()
        context = (
            f"operation={operation} table={table} "
            f"platform={platform or '-'} project_id={project_id or '-'}: {msg}"
        )
        if any(
            tok in lowered
            for tok in (
                "timeout",
                "timed out",
                "connection",
                "network",
                "name or service not known",
                "temporarily unavailable",
                "failed to establish",
            )
        ):
            raise SupabaseNetworkError(context) from exc
        raise SupabaseAPIError(context) from exc

    if response is None:
        raise SupabaseAPIError(
            f"operation={operation} table={table}: empty response"
        )
    return response


def _rows(response) -> list:
    data = getattr(response, "data", None)
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    raise SupabaseAPIError(f"Unexpected response data type: {type(data).__name__}")


def _one(response, required: bool = True) -> Optional[dict]:
    rows = _rows(response)
    if not rows:
        if required:
            raise SupabaseAPIError("Expected at least one row, got none")
        return None
    row = rows[0]
    if not isinstance(row, dict):
        raise SupabaseAPIError("Expected row dict from Supabase")
    return row


# ---------------------------------------------------------------------------
# Scraper runs
# ---------------------------------------------------------------------------

def create_scraper_run(
    platform: str = PLATFORM_CATALANT,
    scraper_name: str = SCRAPER_NAME,
    scraper_version: str = SCRAPER_VERSION,
    metadata: Optional[dict] = None,
) -> dict:
    client = get_supabase_client()
    payload = {
        "platform": platform,
        "scraper_name": scraper_name,
        "scraper_version": scraper_version,
        "status": "RUNNING",
        "started_at": _iso(),
        "metadata": metadata or {},
    }
    response = _execute(
        "create_scraper_run",
        "scraper_runs",
        client.table("scraper_runs").insert(payload).select("*"),
        platform=platform,
    )
    return _one(response)


def update_scraper_run_counts(run_id: str, **counts) -> dict:
    if not run_id:
        raise ValueError("run_id is required")
    allowed = {
        "cards_found",
        "cards_parsed",
        "cards_failed",
        "details_attempted",
        "details_completed",
        "details_failed",
        "projects_inserted",
        "projects_skipped",
        "emails_sent",
        "emails_failed",
        "emails_suppressed",
        "metadata",
        "status",
        "failure_code",
        "failure_reason",
        "completed_at",
    }
    payload = {k: v for k, v in counts.items() if k in allowed and v is not None}
    if not payload:
        return get_scraper_run(run_id)
    client = get_supabase_client()
    response = _execute(
        "update_scraper_run_counts",
        "scraper_runs",
        client.table("scraper_runs").update(payload).eq("id", run_id).select("*"),
    )
    return _one(response)


def get_scraper_run(run_id: str) -> dict:
    client = get_supabase_client()
    response = _execute(
        "get_scraper_run",
        "scraper_runs",
        client.table("scraper_runs").select("*").eq("id", run_id).limit(1),
    )
    return _one(response)


def complete_scraper_run(run_id: str, status: str = "COMPLETED", **counts) -> dict:
    if status not in ("COMPLETED", "PARTIAL", "CANCELLED"):
        raise ValueError(f"Invalid completion status: {status}")
    payload = dict(counts)
    payload["status"] = status
    payload["completed_at"] = _iso()
    return update_scraper_run_counts(run_id, **payload)


def fail_scraper_run(
    run_id: str,
    failure_code: str,
    failure_reason: str,
    status: str = "FAILED",
    **counts,
) -> dict:
    if status not in ("FAILED", "AUTH_FAILED", "CANCELLED"):
        raise ValueError(f"Invalid failure status: {status}")
    payload = dict(counts)
    payload["status"] = status
    payload["completed_at"] = _iso()
    payload["failure_code"] = failure_code
    payload["failure_reason"] = redact_db_error(failure_reason)[:2000]
    return update_scraper_run_counts(run_id, **payload)


def mark_stale_running_runs(
    platform: str = PLATFORM_CATALANT,
    older_than_hours: int = 6,
) -> int:
    """Best-effort: mark abandoned RUNNING rows as FAILED."""
    client = get_supabase_client()
    cutoff = _iso(_utc_now() - timedelta(hours=max(older_than_hours, 1)))
    response = _execute(
        "mark_stale_running_runs",
        "scraper_runs",
        client.table("scraper_runs")
        .update(
            {
                "status": "FAILED",
                "completed_at": _iso(),
                "failure_code": "STALE_RUNNING",
                "failure_reason": "Run left in RUNNING past threshold",
            }
        )
        .eq("platform", platform)
        .eq("status", "RUNNING")
        .lt("started_at", cutoff)
        .select("id"),
        platform=platform,
    )
    return len(_rows(response))


# ---------------------------------------------------------------------------
# Three-day eligibility
# ---------------------------------------------------------------------------

def get_latest_project_occurrence(platform: str, project_id: str) -> Optional[dict]:
    """
    Latest occurrence for platform + project_id ordered by scraped_at desc.
    Raises on database failure — never treats failure as 'no row'.
    """
    if not platform or not project_id:
        raise ValueError("platform and project_id are required")
    client = get_supabase_client()
    # Include detail fields so existing-row enrichment can skip already-complete rows
    response = _execute(
        "get_latest_project_occurrence",
        "projects",
        client.table("projects")
        .select(
            "id,scraped_at,email_status,email_sent,email_eligible,email_not_sent_reason,"
            "title,source_url,detail_extraction_status,description,location_preference,"
            "project_length,start_date_text,level_of_support,industry,contracting_process,"
            "short_description,budget_text,platform_category"
        )
        .eq("platform", platform)
        .eq("project_id", project_id)
        .order("scraped_at", desc=True)
        .limit(1),
        platform=platform,
        project_id=project_id,
    )
    return _one(response, required=False)


def should_process_project(
    platform: str,
    project_id: str,
    now: Optional[datetime] = None,
) -> tuple[bool, str, Optional[dict]]:
    """
    Repeated-project rule based on projects.scraped_at (UTC).

    Window days from OCCURRENCE_WINDOW_DAYS or REPOST_MIN_DAYS (default 3).
    Returns (eligible, reason, latest_row).
    age > N days → eligible; age <= N days (including exactly N days) → skip.
    """
    latest = get_latest_project_occurrence(platform, project_id)
    if latest is None:
        return True, "first_occurrence", None

    scraped_at = _parse_timestamptz(latest.get("scraped_at"))
    if scraped_at is None:
        raise SupabaseAPIError(
            f"Latest project row missing scraped_at "
            f"(platform={platform} project_id={project_id})"
        )

    current = now or _utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)

    window_days = get_occurrence_window_days()
    window = timedelta(days=window_days)
    age = current - scraped_at
    if age > window:
        return True, f"eligible_after_{age.total_seconds():.0f}s", latest
    return (
        False,
        f"skipped_within_{window_days}_days_age_{age.total_seconds():.0f}s",
        latest,
    )


def platform_has_projects(platform: str) -> bool:
    """Platform-specific cold-start detection. Raises on DB failure."""
    client = get_supabase_client()
    response = _execute(
        "platform_has_projects",
        "projects",
        client.table("projects")
        .select("id")
        .eq("platform", platform)
        .limit(1),
        platform=platform,
    )
    return bool(_rows(response))


# ---------------------------------------------------------------------------
# Projects insert / email lifecycle
# ---------------------------------------------------------------------------

_PLACEHOLDER_STRINGS = {
    "",
    "unknown",
    "unclassified",
    "n/a",
    "na",
    "none",
    "not provided",
    "not specified",
    "tbd",
}


def _is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in _PLACEHOLDER_STRINGS or not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def merge_project_data(card_data: dict, detail_data: Optional[dict] = None) -> dict:
    """
    Merge card + detail extraction without letting empty/placeholder detail
    values overwrite useful card values.
    """
    merged = dict(card_data or {})
    warnings = list(merged.get("extraction_warnings") or [])
    detail_data = detail_data or {}

    for key, detail_val in detail_data.items():
        if key in ("extraction_warnings", "missing_fields", "extraction_metadata"):
            continue
        card_val = merged.get(key)
        if _is_empty_value(detail_val):
            if not _is_empty_value(card_val):
                warnings.append(f"detail_empty_preserved_card:{key}")
            continue
        if isinstance(detail_val, str) and detail_val.strip().lower() == "unclassified":
            if not _is_empty_value(card_val) and str(card_val).strip().lower() != "unclassified":
                warnings.append("detail_rejected_unclassified_category")
                continue
            # Do not invent Unclassified when card also empty
            if _is_empty_value(card_val):
                warnings.append("detail_rejected_unclassified_empty")
                continue
        if isinstance(detail_val, list) and not detail_val and card_val:
            continue
        merged[key] = detail_val

    # Merge metadata dicts
    meta = dict(merged.get("extraction_metadata") or {})
    meta.update(detail_data.get("extraction_metadata") or {})
    if meta:
        merged["extraction_metadata"] = meta

    detail_warnings = detail_data.get("extraction_warnings") or []
    warnings.extend(detail_warnings)
    if warnings:
        merged["extraction_warnings"] = warnings

    missing = []
    for field in (
        "title",
        "project_id",
        "source_url",
        "platform_category",
        "description",
        "budget_text",
    ):
        if _is_empty_value(merged.get(field)) and _is_empty_value(merged.get("id" if field == "project_id" else field)):
            # project_id may live as "id" on card dicts
            if field == "project_id" and not _is_empty_value(merged.get("id")):
                continue
            if field == "source_url" and not _is_empty_value(merged.get("url")):
                continue
            if field == "budget_text" and (
                not _is_empty_value(merged.get("budget"))
                or not _is_empty_value(merged.get("detail_budget"))
            ):
                continue
            missing.append(field)
    merged["missing_fields"] = missing
    return merged


def project_to_row(
    project: dict,
    *,
    scraper_run_id: Optional[str] = None,
    email_status: str = "PENDING",
    email_eligible: bool = True,
    email_sent: bool = False,
    email_not_sent_reason: Optional[str] = None,
    scraped_at: Optional[datetime] = None,
) -> dict:
    """Normalize internal project dict into a projects table insert payload."""
    now = scraped_at or _utc_now()
    platform = (project.get("platform") or PLATFORM_CATALANT).strip().lower()
    project_id = str(project.get("project_id") or project.get("id") or "").strip()
    title = (project.get("title") or "").strip()
    source_url = (
        project.get("source_url")
        or project.get("url")
        or ""
    ).strip()
    if not project_id or not title or not source_url:
        raise ValueError("project_id, title, and source_url are required")

    cat = project.get("platform_category")
    if isinstance(cat, str) and cat.strip().lower() == "unclassified":
        cat = None

    path = project.get("platform_category_path") or []
    if not isinstance(path, list):
        path = []

    skills = project.get("skills") or []
    expertise = project.get("expertise") or []
    deliverables = project.get("deliverables") or []

    budget_text = (
        project.get("budget_text")
        or project.get("budget")
        or project.get("detail_budget")
        or None
    )
    if budget_text is not None:
        budget_text = str(budget_text).strip() or None

    row = {
        "platform": platform,
        "project_id": project_id,
        "source_url": source_url,
        "title": title,
        "short_description": project.get("short_description")
        or (project.get("description") if not project.get("short_description") else None),
        "description": project.get("description"),
        "status": project.get("status"),
        "platform_category": cat or None,
        "platform_category_path": path,
        "platform_category_raw": project.get("platform_category_raw"),
        "platform_category_source": project.get("platform_category_source"),
        "platform_category_confidence": project.get("platform_category_confidence"),
        "platform_category_extraction_status": project.get(
            "platform_category_extraction_status"
        )
        or ("MISSING" if not cat else None),
        "location": project.get("location"),
        "location_preference": project.get("location_preference")
        or project.get("location_pref"),
        "budget_text": budget_text,
        "budget_min": project.get("budget_min"),
        "budget_max": project.get("budget_max"),
        "budget_currency": project.get("budget_currency"),
        "billing_type": project.get("billing_type"),
        "hourly_rate": project.get("hourly_rate"),
        "daily_rate": project.get("daily_rate"),
        "rate_currency": project.get("rate_currency"),
        "budget_source": project.get("budget_source"),
        "budget_confidence": project.get("budget_confidence"),
        "duration_text": project.get("duration_text") or project.get("duration"),
        "project_length": project.get("project_length"),
        "start_date_text": project.get("start_date_text") or project.get("start_date"),
        "source_start_date": project.get("source_start_date"),
        "level_of_support": project.get("level_of_support"),
        "industry": project.get("industry"),
        "contracting_process": project.get("contracting_process")
        or project.get("contracting"),
        "skills": skills if isinstance(skills, list) else [],
        "expertise": expertise if isinstance(expertise, list) else [],
        "deliverables": deliverables if isinstance(deliverables, list) else [],
        "engagement_type": project.get("engagement_type"),
        "project_type": project.get("project_type"),
        "workstream": project.get("workstream"),
        "estimated_hours": project.get("estimated_hours"),
        "weekly_commitment": project.get("weekly_commitment"),
        "remote_or_onsite": project.get("remote_or_onsite"),
        "country_or_region": project.get("country_or_region"),
        "application_deadline": project.get("application_deadline"),
        "time_posted_text": project.get("time_posted_text") or project.get("time_posted"),
        "source_posted_at": project.get("source_posted_at"),
        "source_posted_at_is_estimated": bool(
            project.get("source_posted_at_is_estimated", False)
        ),
        "scraped_at": _iso(now),
        "first_detected_at": _iso(now),
        "last_seen_at": _iso(now),
        "card_extraction_status": project.get("card_extraction_status") or "COMPLETE",
        "detail_extraction_status": project.get("detail_extraction_status")
        or "NOT_ATTEMPTED",
        "detail_last_attempt_at": project.get("detail_last_attempt_at"),
        "detail_attempt_count": int(project.get("detail_attempt_count") or 0),
        "detail_failure_code": project.get("detail_failure_code"),
        "detail_last_error": project.get("detail_last_error"),
        "detail_completed_at": project.get("detail_completed_at"),
        "missing_fields": project.get("missing_fields") or [],
        "extraction_warnings": project.get("extraction_warnings") or [],
        "extraction_metadata": project.get("extraction_metadata") or {},
        "raw_data": project.get("raw_data") or {},
        "email_eligible": bool(email_eligible),
        "email_status": email_status,
        "email_sent": bool(email_sent),
        "email_not_sent_reason": email_not_sent_reason,
        "scraper_run_id": scraper_run_id,
    }

    # Prefer short_description from card when description is the long detail text
    if not project.get("short_description") and project.get("description"):
        # Keep description as-is; short_description may equal card blurb stored separately
        pass

    # Drop None values that would clear NOT NULL DEFAULT columns incorrectly — keep required ones
    cleaned = {}
    for k, v in row.items():
        if v is None and k not in (
            "short_description",
            "description",
            "status",
            "platform_category",
            "platform_category_raw",
            "platform_category_source",
            "platform_category_confidence",
            "platform_category_extraction_status",
            "location",
            "location_preference",
            "budget_text",
            "budget_min",
            "budget_max",
            "budget_currency",
            "duration_text",
            "project_length",
            "start_date_text",
            "source_start_date",
            "level_of_support",
            "industry",
            "contracting_process",
            "engagement_type",
            "project_type",
            "workstream",
            "estimated_hours",
            "weekly_commitment",
            "remote_or_onsite",
            "country_or_region",
            "application_deadline",
            "time_posted_text",
            "source_posted_at",
            "email_not_sent_reason",
            "scraper_run_id",
            "email_failure_code",
            "email_last_error",
            "email_last_attempt_at",
            "email_next_retry_at",
            "email_sent_at",
            "email_message_id",
            "budget_currency",
            "billing_type",
            "hourly_rate",
            "daily_rate",
            "rate_currency",
            "budget_source",
            "budget_confidence",
            "duration_text",
            "project_length",
            "start_date_text",
            "source_start_date",
            "level_of_support",
            "industry",
            "contracting_process",
            "engagement_type",
            "project_type",
            "workstream",
            "estimated_hours",
            "weekly_commitment",
            "remote_or_onsite",
            "country_or_region",
            "application_deadline",
            "time_posted_text",
            "source_posted_at",
            "email_not_sent_reason",
            "scraper_run_id",
            "email_failure_code",
            "email_last_error",
            "email_last_attempt_at",
            "email_next_retry_at",
            "email_sent_at",
            "email_message_id",
            "detail_last_attempt_at",
            "detail_failure_code",
            "detail_last_error",
            "detail_completed_at",
        ):
            continue
        cleaned[k] = v
    return _strip_unavailable_enrichment_columns(cleaned)


DETAIL_ENRICHMENT_COLUMNS = {
    "billing_type",
    "hourly_rate",
    "daily_rate",
    "rate_currency",
    "budget_source",
    "budget_confidence",
    "detail_last_attempt_at",
    "detail_attempt_count",
    "detail_failure_code",
    "detail_last_error",
    "detail_completed_at",
}

_detail_enrichment_schema_ready: Optional[bool] = None
_detail_enrichment_warn_printed = False


def reset_detail_enrichment_schema_cache() -> None:
    global _detail_enrichment_schema_ready, _detail_enrichment_warn_printed
    _detail_enrichment_schema_ready = None
    _detail_enrichment_warn_printed = False


def detail_enrichment_schema_ready() -> bool:
    """True when migration 20260801120000 columns exist in projects."""
    global _detail_enrichment_schema_ready
    if _detail_enrichment_schema_ready is not None:
        return _detail_enrichment_schema_ready
    try:
        client = get_supabase_client()
        _execute(
            "probe_detail_enrichment_columns",
            "projects",
            client.table("projects")
            .select("billing_type,detail_attempt_count,budget_source")
            .limit(1),
        )
        _detail_enrichment_schema_ready = True
    except Exception as exc:
        text = str(exc or "").lower()
        if "pgrst204" in text or "could not find the" in text or "schema cache" in text:
            _detail_enrichment_schema_ready = False
        else:
            # Unexpected error — do not cache forever; treat as not ready for safety
            _detail_enrichment_schema_ready = False
    return bool(_detail_enrichment_schema_ready)


def detail_enrichment_migration_message() -> str:
    return (
        "Detail enrichment columns are missing. Apply the new migration:\n"
        "  1) Open https://supabase.com/dashboard/project/sdaqjqvcxvtxxcblmlev/sql/new\n"
        "  2) Paste contents of "
        "supabase/migrations/20260801120000_add_detail_enrichment_columns.sql\n"
        "  3) Click Run\n"
        "  4) Verify with: python monitor.py --test-supabase"
    )


def warn_detail_enrichment_migration_once() -> None:
    """Print the missing-migration notice at most once per process."""
    global _detail_enrichment_warn_printed
    if detail_enrichment_schema_ready() or _detail_enrichment_warn_printed:
        return
    _detail_enrichment_warn_printed = True
    print(f"⚠️  {detail_enrichment_migration_message()}")


def _strip_unavailable_enrichment_columns(payload: dict) -> dict:
    if detail_enrichment_schema_ready():
        return payload
    return {k: v for k, v in payload.items() if k not in DETAIL_ENRICHMENT_COLUMNS}


DETAIL_UPDATE_ALLOWED = {
    "title",
    "short_description",
    "description",
    "status",
    "platform_category",
    "platform_category_path",
    "platform_category_raw",
    "platform_category_source",
    "platform_category_confidence",
    "platform_category_extraction_status",
    "location",
    "location_preference",
    "budget_text",
    "budget_min",
    "budget_max",
    "budget_currency",
    "billing_type",
    "hourly_rate",
    "daily_rate",
    "rate_currency",
    "budget_source",
    "budget_confidence",
    "duration_text",
    "project_length",
    "start_date_text",
    "source_start_date",
    "level_of_support",
    "industry",
    "contracting_process",
    "skills",
    "expertise",
    "deliverables",
    "engagement_type",
    "project_type",
    "workstream",
    "estimated_hours",
    "weekly_commitment",
    "remote_or_onsite",
    "country_or_region",
    "application_deadline",
    "time_posted_text",
    "source_posted_at",
    "source_posted_at_is_estimated",
    "card_extraction_status",
    "detail_extraction_status",
    "detail_last_attempt_at",
    "detail_attempt_count",
    "detail_failure_code",
    "detail_last_error",
    "detail_completed_at",
    "missing_fields",
    "extraction_warnings",
    "extraction_metadata",
    "raw_data",
    "last_seen_at",
}

DETAIL_UPDATE_FORBIDDEN = {
    "id",
    "platform",
    "project_id",
    "email_status",
    "email_sent",
    "email_eligible",
    "email_not_sent_reason",
    "email_attempt_count",
    "email_sent_at",
    "email_failure_code",
    "email_last_error",
    "email_last_attempt_at",
    "email_next_retry_at",
    "email_message_id",
    "scraped_at",
    "first_detected_at",
    "scraper_run_id",
    "created_at",
}


def update_project_details(project_row_id: str, detail_updates: dict) -> dict:
    """Update enrichment fields for an existing projects.id. Raises on failure."""
    if not project_row_id:
        raise ValueError("project_row_id is required")
    if not isinstance(detail_updates, dict):
        raise ValueError("detail_updates must be a dict")

    forbidden_hit = [k for k in detail_updates if k in DETAIL_UPDATE_FORBIDDEN]
    if forbidden_hit:
        raise ValueError(f"Refusing to update protected columns: {', '.join(forbidden_hit)}")

    payload = {}
    for key, value in detail_updates.items():
        if key not in DETAIL_UPDATE_ALLOWED:
            continue
        if value is None:
            # Allow explicit clears for nullable enrichment fields
            payload[key] = None
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, dict)) and len(value) == 0 and key in (
            "skills", "expertise", "deliverables", "platform_category_path",
            "missing_fields", "extraction_warnings",
        ):
            payload[key] = value
            continue
        payload[key] = value

    if not payload:
        raise ValueError("No valid detail updates provided")

    if "detail_last_error" in payload and payload["detail_last_error"] is not None:
        payload["detail_last_error"] = redact_db_error(payload["detail_last_error"])[:2000]

    payload["last_seen_at"] = payload.get("last_seen_at") or _iso()
    payload = _strip_unavailable_enrichment_columns(payload)
    warn_detail_enrichment_migration_once()

    client = get_supabase_client()
    response = _execute(
        "update_project_details",
        "projects",
        client.table("projects").update(payload).eq("id", project_row_id).select("*"),
    )
    return _one(response)


def get_projects_needing_detail_enrichment(
    *,
    platform: str = PLATFORM_CATALANT,
    limit: int = 20,
    project_id: Optional[str] = None,
    retry_failed: bool = False,
) -> list:
    """
    Fetch Catalant rows that need detail enrichment.
    Uses broad select + local filter because PostgREST OR emptiness checks are awkward.
    """
    client = get_supabase_client()
    query = (
        client.table("projects")
        .select("*")
        .eq("platform", platform)
        .order("scraped_at", desc=True)
        .limit(max(limit * 5, 50))
    )
    if project_id:
        query = query.eq("project_id", project_id)
    response = _execute(
        "get_projects_needing_detail_enrichment",
        "projects",
        query,
        platform=platform,
        project_id=project_id or "",
    )
    rows = _rows(response)
    selected = []
    for row in rows:
        status = (row.get("detail_extraction_status") or "").upper()
        needs = status in ("NOT_ATTEMPTED", "PARTIAL")
        if retry_failed and status in ("FAILED", "TIMEOUT"):
            needs = True
        for field in (
            "description",
            "location_preference",
            "project_length",
            "start_date_text",
            "level_of_support",
            "industry",
            "contracting_process",
        ):
            val = row.get(field)
            if val is None or (isinstance(val, str) and not val.strip()):
                needs = True
                break
        if needs:
            selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def insert_project_occurrence(
    project: dict,
    scraper_run_id: Optional[str] = None,
    *,
    email_status: str = "PENDING",
    email_eligible: bool = True,
    email_sent: bool = False,
    email_not_sent_reason: Optional[str] = None,
) -> dict:
    """Insert a new occurrence row. Always creates a new row (no upsert)."""
    payload = project_to_row(
        project,
        scraper_run_id=scraper_run_id,
        email_status=email_status,
        email_eligible=email_eligible,
        email_sent=email_sent,
        email_not_sent_reason=email_not_sent_reason,
    )
    client = get_supabase_client()
    response = _execute(
        "insert_project_occurrence",
        "projects",
        client.table("projects").insert(payload).select("*"),
        platform=payload.get("platform", ""),
        project_id=payload.get("project_id", ""),
    )
    return _one(response)


def get_project_by_id(row_id: str) -> Optional[dict]:
    client = get_supabase_client()
    response = _execute(
        "get_project_by_id",
        "projects",
        client.table("projects").select("*").eq("id", row_id).limit(1),
    )
    return _one(response, required=False)


def update_project_email_status(row_id: str, **fields) -> dict:
    if not row_id:
        raise ValueError("row_id is required")
    allowed = {
        "email_eligible",
        "email_status",
        "email_sent",
        "email_not_sent_reason",
        "email_failure_code",
        "email_last_error",
        "email_attempt_count",
        "email_last_attempt_at",
        "email_next_retry_at",
        "email_sent_at",
        "email_message_id",
        "last_seen_at",
    }
    payload = {k: v for k, v in fields.items() if k in allowed}
    if "email_last_error" in payload and payload["email_last_error"] is not None:
        payload["email_last_error"] = redact_db_error(payload["email_last_error"])[:2000]
    client = get_supabase_client()
    response = _execute(
        "update_project_email_status",
        "projects",
        client.table("projects").update(payload).eq("id", row_id).select("*"),
    )
    return _one(response)


def get_retryable_email_projects(
    *,
    max_attempts: int,
    now: Optional[datetime] = None,
    limit: int = 20,
    platform: Optional[str] = None,
) -> list:
    client = get_supabase_client()
    current = _iso(now or _utc_now())
    query = (
        client.table("projects")
        .select("*")
        .eq("email_status", "RETRY_PENDING")
        .lte("email_next_retry_at", current)
        .lt("email_attempt_count", max_attempts)
        .eq("email_eligible", True)
        .order("email_next_retry_at", desc=False)
        .limit(limit)
    )
    if platform:
        query = query.eq("platform", platform)
    response = _execute("get_retryable_email_projects", "projects", query)
    return _rows(response)


def record_email_attempt(
    project_row_id: str,
    attempt_number: int,
    status: str = "SENDING",
    *,
    recipients: Optional[list] = None,
    provider: str = "smtp",
    message_id: Optional[str] = None,
    failure_code: Optional[str] = None,
    failure_reason: Optional[str] = None,
    metadata: Optional[dict] = None,
    attempt_id: Optional[str] = None,
) -> dict:
    client = get_supabase_client()
    if attempt_id:
        payload = {
            "status": status,
            "completed_at": _iso() if status in ("SENT", "FAILED") else None,
            "message_id": message_id,
            "failure_code": failure_code,
            "failure_reason": redact_db_error(failure_reason) if failure_reason else None,
        }
        if metadata is not None:
            payload["metadata"] = metadata
        response = _execute(
            "update_email_attempt",
            "email_attempts",
            client.table("email_attempts")
            .update(payload)
            .eq("id", attempt_id)
            .select("*"),
        )
        return _one(response)

    payload = {
        "project_id": project_row_id,
        "attempt_number": attempt_number,
        "status": status,
        "attempted_at": _iso(),
        "recipients": recipients or [],
        "provider": provider,
        "message_id": message_id,
        "failure_code": failure_code,
        "failure_reason": redact_db_error(failure_reason) if failure_reason else None,
        "metadata": metadata or {},
    }
    if status in ("SENT", "FAILED"):
        payload["completed_at"] = _iso()
    response = _execute(
        "record_email_attempt",
        "email_attempts",
        client.table("email_attempts").insert(payload).select("*"),
    )
    return _one(response)


def compute_email_next_retry_at(
    attempt_count: int,
    base_minutes: int = 15,
    now: Optional[datetime] = None,
) -> datetime:
    """Bounded exponential backoff: base * 2^(attempt-1), capped at 24h."""
    current = now or _utc_now()
    exponent = max(attempt_count - 1, 0)
    minutes = min(base_minutes * (2 ** exponent), 24 * 60)
    return current + timedelta(minutes=minutes)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def _earliest_cookie_expiry(cookies: list) -> Optional[str]:
    expiries = []
    for cookie in cookies or []:
        if not isinstance(cookie, dict):
            continue
        exp = cookie.get("expiry") or cookie.get("expires")
        if exp is None:
            continue
        try:
            ts = float(exp)
            if ts > 1e12:
                ts = ts / 1000.0
            expiries.append(datetime.fromtimestamp(ts, tz=timezone.utc))
        except (TypeError, ValueError, OSError):
            continue
    if not expiries:
        return None
    return _iso(min(expiries))


def save_scraper_session(
    platform: str,
    cookies: list,
    *,
    metadata: Optional[dict] = None,
) -> dict:
    if not platform:
        raise ValueError("platform is required")
    # Sanitize: only keep JSON-safe cookie dicts; never store passwords
    safe_cookies = []
    for cookie in cookies or []:
        if not isinstance(cookie, dict):
            continue
        safe = {
            k: v
            for k, v in cookie.items()
            if k.lower() not in ("password", "passwd", "smtp_password")
        }
        safe_cookies.append(safe)

    payload = {
        "platform": platform,
        "session_data": {"cookies": safe_cookies},
        "saved_at": _iso(),
        "expires_at": _earliest_cookie_expiry(safe_cookies),
        "session_version": 1,
        "metadata": metadata or {"cookie_count": len(safe_cookies)},
    }
    client = get_supabase_client()
    response = _execute(
        "save_scraper_session",
        "scraper_sessions",
        client.table("scraper_sessions").upsert(payload, on_conflict="platform").select("*"),
        platform=platform,
    )
    return _one(response)


def load_scraper_session(platform: str) -> Optional[dict]:
    client = get_supabase_client()
    response = _execute(
        "load_scraper_session",
        "scraper_sessions",
        client.table("scraper_sessions").select("*").eq("platform", platform).limit(1),
        platform=platform,
    )
    return _one(response, required=False)


def delete_scraper_session(platform: str) -> bool:
    client = get_supabase_client()
    _execute(
        "delete_scraper_session",
        "scraper_sessions",
        client.table("scraper_sessions").delete().eq("platform", platform),
        platform=platform,
    )
    return True


def is_missing_schema_error(error) -> bool:
    text = str(error or "").lower()
    return "pgrst205" in text or "could not find the table" in text or "schema cache" in text


def schema_missing_message(table: str = "projects") -> str:
    return (
        f"Supabase table '{table}' is missing. Apply the migration first:\n"
        f"  1) Open https://supabase.com/dashboard/project/sdaqjqvcxvtxxcblmlev/sql/new\n"
        f"  2) Paste contents of supabase/migrations/20260331120000_create_project_monitor_schema.sql\n"
        f"  3) Click Run\n"
        f"  4) Verify with: python monitor.py --test-supabase"
    )


def list_required_tables() -> list[str]:
    return ["projects", "scraper_runs", "email_attempts", "scraper_sessions"]


def ensure_schema_ready() -> None:
    """Raise a clear error if required tables are missing."""
    client = get_supabase_client()
    for table in list_required_tables():
        try:
            _execute(
                "ensure_schema",
                table,
                client.table(table)
                .select("id" if table != "scraper_sessions" else "platform")
                .limit(1),
            )
        except Exception as exc:
            if is_missing_schema_error(exc):
                raise SupabaseConfigError(schema_missing_message(table)) from exc
            raise
    if not detail_enrichment_schema_ready():
        warn_detail_enrichment_migration_once()


def test_supabase_connection(cleanup: bool = True) -> dict:
    """
    Validate credentials and tables with a reversible temporary insert.
    Never prints keys. Returns a summary dict.
    """
    client = get_supabase_client()
    availability = {}
    for table in list_required_tables():
        try:
            _execute(
                "test_table",
                table,
                client.table(table).select(
                    "id" if table != "scraper_sessions" else "platform"
                ).limit(1),
            )
            availability[table] = True
        except Exception as exc:
            availability[table] = False
            if is_missing_schema_error(exc):
                raise SupabaseConfigError(schema_missing_message(table)) from exc
            raise SupabaseAPIError(
                f"Table check failed for {table}: {redact_db_error(exc)}"
            ) from exc

    run = create_scraper_run(
        platform="catalant",
        scraper_name="catalant-monitor-test",
        metadata={"temporary_test": True},
    )
    run_id = run["id"]
    project = None
    try:
        project = insert_project_occurrence(
            {
                "platform": "catalant",
                "project_id": f"__test_{run_id[:8]}__",
                "title": "Temporary Supabase connectivity test",
                "source_url": "https://example.invalid/test",
                "description": "auto-deleted",
                "raw_data": {"temporary_test": True},
            },
            scraper_run_id=run_id,
            email_status="NOT_REQUIRED",
            email_eligible=False,
            email_not_sent_reason="CONNECTIVITY_TEST",
        )
        complete_scraper_run(run_id, status="COMPLETED", projects_inserted=1)
    finally:
        if cleanup:
            if project and project.get("id"):
                _execute(
                    "cleanup_test_project",
                    "projects",
                    client.table("projects").delete().eq("id", project["id"]),
                )
            _execute(
                "cleanup_test_run",
                "scraper_runs",
                client.table("scraper_runs").delete().eq("id", run_id),
            )

    return {
        "ok": True,
        "tables": availability,
        "test_run_id": run_id,
        "cleaned_up": cleanup,
    }
