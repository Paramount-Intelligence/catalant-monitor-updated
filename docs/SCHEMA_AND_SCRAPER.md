# Catalant Monitor — Schema & How It Works

This document describes the Supabase PostgreSQL schema and the production Selenium scraper’s runtime behavior.

## Overview

The monitor logs into Catalant, scans Search Projects, decides which listings are eligible under a **3-day repeat rule**, fetches detail pages, inserts a new `projects` row, then sends email. Storage is **Supabase only** (no MongoDB at runtime).

| Piece | Role |
| --- | --- |
| `monitor.py` | CLI entrypoint |
| `script_clean.py` | Login, Selenium scan, detail fetch, email, main loop |
| `extraction.py` | Category helpers + safe card/detail merge |
| `database.py` | Supabase client + repository functions |
| `supabase/migrations/` | Schema source of truth |

Platform value written by this scraper: **`catalant`**. The same `projects` table is shared with future scrapers (`btg`, `guru`, `hubstaff`, `toptal`, etc.).

---

## How the scraper works

### 1. Startup

1. Load config from `.env` (`CATALANT_*`, `SUPABASE_*`, SMTP, intervals, retries).
2. Initialize Chrome (headless or not).
3. Restore session:
   - Load cookies from Supabase `scraper_sessions` first.
   - Fall back to local `COOKIES_FILE` (`catalant_cookies.json`).
   - Validate cookie domain (`.gocatalant.com`) before restoring into Selenium.
4. If cookies fail, run the normal Catalant login and save cookies to Supabase + local file.
5. Confirm required Supabase tables exist (`ensure_schema_ready`).

### 2. Each monitoring cycle

1. Create a `scraper_runs` row (`status = RUNNING`).
2. Open Search Projects and extract listing cards.
3. **Cold start** (no Catalant rows in `projects` yet):
   - For each valid card: open the detail page, extract fields, merge with card data.
   - Insert each enriched seed as `email_status = SUPPRESSED`, `email_not_sent_reason = COLD_START_SEED`.
   - Do **not** send project emails or create `email_attempts`.
   - One failed detail page still seeds card data (`detail_extraction_status` FAILED/TIMEOUT/PARTIAL) and continues.
   - Mark the run completed only after seeds insert successfully.
4. Otherwise, for each card:
   1. Confirm identity (`project_id`, title, URL).
   2. Run the **3-day eligibility check** (see below).
   3. If not eligible: skip new occurrence; optionally **enrich** the existing incomplete row (update same UUID, no email).
   4. If eligible: fetch the detail page and merge with card data (`merge_project_data`).
   5. **Insert** a new `projects` row (`email_status = PENDING`).
   6. Create an `email_attempts` row (`SENDING`).
   7. Send the project email.
   8. Update the **same** project row and the **same** attempt row.
   9. Update scraper-run counters.
5. Also process due `RETRY_PENDING` emails (same project row; no new occurrence).
6. Complete the run as `COMPLETED` or `PARTIAL`, or `FAILED` / `AUTH_FAILED` on hard failures.

### 3. Three-day repeated-project rule

There is **no** unique constraint on `(platform, project_id)`. The same marketplace project may appear as multiple occurrence rows over time.

Eligibility query:

```text
platform = catalant
project_id = <source id>
order by scraped_at desc
limit 1
```

| Latest row | Decision |
| --- | --- |
| None | Eligible — insert new occurrence + email |
| `now - scraped_at` **> 3 days** | Eligible — insert new occurrence + email |
| `now - scraped_at` **≤ 3 days** (including exactly 3 days) | Skip — no insert, no email |

Comparison uses timezone-aware UTC and **`projects.scraped_at` only** (not website “Posted …” text).

### 4. Email lifecycle

| Step | What happens |
| --- | --- |
| Insert | `email_status = PENDING`, `email_sent = false` |
| Attempt start | `email_attempts` row with `SENDING`; project may move to `SENDING` |
| Success | Project: `SENT`, `email_sent = true`, timestamps + message id. Attempt: `SENT` |
| Failure | Project: `RETRY_PENDING` (or `FAILED` at max retries), sanitized error fields. Attempt: `FAILED` |
| Retry | New `email_attempts` row for the **same** `projects.id`; no 3-day check; no new project row |
| Cold start | `SUPPRESSED` / `COLD_START_SEED` — never emailed as “new” |

Backoff uses `EMAIL_RETRY_BASE_MINUTES` with exponential growth, capped at 24 hours. Max attempts: `EMAIL_MAX_RETRIES`.

### 5. Category extraction order

1. Structured labeled field  
2. Dedicated category / pool selector  
3. Category breadcrumb (multi-segment)  
4. Embedded page JSON  
5. Bounded label-specific text fallback  
6. Empty value + `MISSING`

`Unclassified` / `Unknown` are rejected (`REJECTED_INVALID_CANDIDATE`). Empty detail values never overwrite useful card values.

### 6. Useful CLI commands

```bash
python monitor.py
python monitor.py --test-supabase
python monitor.py --test-error-email
python monitor.py --inspect-project
python monitor.py --dry-run --run-once --debug-extraction
python monitor.py --retry-pending-emails --dry-run
```

Dry-run uses the real Catalant page when authenticated, prints extraction/eligibility decisions, and does **not** insert permanent project rows or send project emails.

---

## Table: `projects` (shared main table)

One row = one **occurrence** of a marketplace project for a platform.

### Identity

| Column | Type | Meaning |
| --- | --- | --- |
| `id` | uuid | Internal Supabase primary key for this occurrence row |
| `platform` | text | Lowercase marketplace id (`catalant`, `btg`, `guru`, …). Not constrained to a fixed list |
| `project_id` | text | Source/marketplace project id (Catalant need id) |
| `source_url` | text | Canonical project URL on the marketplace |

### Content

| Column | Type | Meaning |
| --- | --- | --- |
| `title` | text | Project title (required) |
| `short_description` | text | Listing-card blurb / short summary |
| `description` | text | Full detail-page description when available |
| `status` | text | Listing status text (e.g. Posted / New Project) |

### Category

| Column | Type | Meaning |
| --- | --- | --- |
| `platform_category` | text | Top-level category label, or null if missing/rejected |
| `platform_category_path` | text[] | Full breadcrumb segments, e.g. `{Strategy,Finance}` |
| `platform_category_raw` | text | Raw captured string before normalization |
| `platform_category_source` | text | Where it came from (selector name, structured label, embedded JSON, …) |
| `platform_category_confidence` | text | `HIGH` / `MEDIUM` / `LOW` |
| `platform_category_extraction_status` | text | `FOUND_*`, `MISSING`, or `REJECTED_INVALID_CANDIDATE` |

### Location & commercial fields

| Column | Type | Meaning |
| --- | --- | --- |
| `location` | text | Location from the card when present |
| `location_preference` | text | Detail-page location preference |
| `budget_text` | text | Budget as displayed text |
| `budget_min` / `budget_max` | numeric | Parsed numeric bounds when available |
| `budget_currency` | text | Currency code/symbol when available |
| `duration_text` | text | Duration string from the card |
| `project_length` | text | Timeline / expected length from detail |
| `start_date_text` | text | Start date as shown on the site |
| `source_start_date` | date | Parsed calendar start date when determinable |
| `level_of_support` | text | Expert type / support level |
| `industry` | text | Desired industry background |
| `contracting_process` | text | Contracting process text |

### Skills & engagement (shared columns for multi-platform use)

| Column | Type | Meaning |
| --- | --- | --- |
| `skills` | text[] | Skill tags |
| `expertise` | text[] | Expertise tags |
| `deliverables` | text[] | Deliverable tags |
| `engagement_type` | text | Engagement model when exposed |
| `project_type` | text | Project type when exposed |
| `workstream` | text | Workstream label when exposed |
| `estimated_hours` | numeric | Estimated hours when exposed |
| `weekly_commitment` | text | Weekly commitment text |
| `remote_or_onsite` | text | Remote / onsite preference |
| `country_or_region` | text | Country or region |
| `application_deadline` | timestamptz | Deadline when exposed |

### Timing

| Column | Type | Meaning |
| --- | --- | --- |
| `time_posted_text` | text | Website “Posted …” display text (not used for 3-day rule) |
| `source_posted_at` | timestamptz | Parsed absolute posted time when available |
| `source_posted_at_is_estimated` | boolean | True if posted time was estimated |
| `scraped_at` | timestamptz | When this occurrence was scraped — **used for the 3-day rule** |
| `first_detected_at` | timestamptz | First detection time for this occurrence row |
| `last_seen_at` | timestamptz | Last time this occurrence was observed |

### Extraction quality

| Column | Type | Meaning |
| --- | --- | --- |
| `card_extraction_status` | text | `COMPLETE` / `PARTIAL` / `FAILED` |
| `detail_extraction_status` | text | `NOT_ATTEMPTED` / `COMPLETE` / `PARTIAL` / `FAILED` / `TIMEOUT` |
| `missing_fields` | text[] | Important fields still empty after merge |
| `extraction_warnings` | text[] | Non-fatal extraction/merge warnings |
| `extraction_metadata` | jsonb | Extra extractor diagnostics |
| `raw_data` | jsonb | Platform-specific leftovers / migration payload |

### Email lifecycle

| Column | Type | Meaning |
| --- | --- | --- |
| `email_eligible` | boolean | Whether this occurrence may be emailed |
| `email_status` | text | `PENDING`, `SENDING`, `SENT`, `RETRY_PENDING`, `FAILED`, `SUPPRESSED`, `NOT_REQUIRED` |
| `email_sent` | boolean | Convenience flag for successful send |
| `email_not_sent_reason` | text | Why email was skipped/failed (e.g. `COLD_START_SEED`, `EMAIL_SEND_FAILED`) |
| `email_failure_code` | text | Classified failure code |
| `email_last_error` | text | Sanitized last error (secrets redacted) |
| `email_attempt_count` | integer | Number of send attempts so far |
| `email_last_attempt_at` | timestamptz | Last attempt time |
| `email_next_retry_at` | timestamptz | When retry becomes due |
| `email_sent_at` | timestamptz | Successful send time |
| `email_message_id` | text | Provider/message id when available |

### Run link & audit

| Column | Type | Meaning |
| --- | --- | --- |
| `scraper_run_id` | uuid | FK to `scraper_runs.id` (nullable on delete set null) |
| `created_at` | timestamptz | Row creation time |
| `updated_at` | timestamptz | Auto-updated by trigger |

---

## Table: `scraper_runs`

One row per monitoring cycle.

| Column | Type | Meaning |
| --- | --- | --- |
| `id` | uuid | Run id |
| `platform` | text | Platform being scraped |
| `scraper_name` | text | Scraper identifier (`catalant-monitor`) |
| `scraper_version` | text | Version string |
| `started_at` / `completed_at` | timestamptz | Run window |
| `status` | text | `RUNNING`, `COMPLETED`, `PARTIAL`, `FAILED`, `AUTH_FAILED`, `CANCELLED` |
| `cards_found` | integer | Cards seen on the listing page |
| `cards_parsed` | integer | Cards successfully parsed |
| `cards_failed` | integer | Cards that failed extraction/identity |
| `details_attempted` / `details_completed` / `details_failed` | integer | Detail-page fetch stats |
| `projects_inserted` | integer | New occurrence rows written |
| `projects_skipped` | integer | Skipped by 3-day rule (or similar) |
| `emails_sent` / `emails_failed` / `emails_suppressed` | integer | Email outcome counters |
| `failure_code` / `failure_reason` | text | Sanitized failure info when status is failed |
| `metadata` | jsonb | Extra run context |
| `created_at` / `updated_at` | timestamptz | Audit timestamps |

`PARTIAL` means the scan mostly worked but at least one card, detail, insert, or email failed.

---

## Table: `email_attempts`

Auditable history of each send attempt against a **project occurrence** (`projects.id`).

| Column | Type | Meaning |
| --- | --- | --- |
| `id` | uuid | Attempt id |
| `project_id` | uuid | FK to `projects.id` (cascade delete) |
| `attempt_number` | integer | 1-based attempt number for that project row |
| `status` | text | `SENDING`, `SENT`, `FAILED` |
| `attempted_at` / `completed_at` | timestamptz | Attempt timing |
| `recipients` | text[] | Recipient list used for this attempt |
| `provider` | text | Transport (`smtp`) |
| `message_id` | text | Provider message id when available |
| `failure_code` / `failure_reason` | text | Classified + sanitized failure |
| `metadata` | jsonb | Extra attempt context |
| `created_at` | timestamptz | Row creation time |

---

## Table: `scraper_sessions`

One session row per platform (primary key = `platform`). Used for cookie restore. **Not** readable by authenticated dashboard users.

| Column | Type | Meaning |
| --- | --- | --- |
| `platform` | text | Platform key (`catalant`) |
| `session_data` | jsonb | Sanitized Selenium cookies JSON (`{"cookies":[...]}`) |
| `saved_at` | timestamptz | When session was saved |
| `expires_at` | timestamptz | Earliest cookie expiry when determinable |
| `session_version` | integer | Session format version |
| `metadata` | jsonb | Safe metadata (e.g. cookie count) — never passwords |
| `created_at` / `updated_at` | timestamptz | Audit timestamps |

Passwords (Catalant / SMTP) are never stored here. Raw cookies are never logged or attached to operational emails.

---

## Security & RLS summary

- RLS enabled on all four tables.
- `authenticated` can **read** `projects`, `scraper_runs`, and `email_attempts`.
- `anon` / `authenticated` cannot insert/update/delete those tables.
- `scraper_sessions` has **no** authenticated/anonymous policies; only the trusted backend secret-key client should access it.
- Scraper uses `SUPABASE_SECRET_KEY` (or legacy `SUPABASE_SERVICE_ROLE_KEY`). Publishable/anon keys are rejected.

---

## Optional Mongo → Supabase migration

If old Mongo records must be preserved, use (manual only):

```bash
python scripts/migrate_mongo_to_supabase.py --dry-run
python scripts/migrate_mongo_to_supabase.py --batch-size 100
```

Requires `MONGO_URI` plus Supabase credentials. The production scraper never imports this script.
