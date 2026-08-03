# Project Monitor — Shared Schema & Scraper Guide

This document describes the **shared Supabase PostgreSQL schema** used by every marketplace scraper, plus how the Catalant Selenium scraper behaves today.

## Overview

All scrapers (Catalant now; BTG, Guru, Hubstaff, Toptal, etc. later) write into the **same Supabase project** and the **same four tables**. They must map site-specific HTML into the **same `projects` columns**. Do not create a separate database or parallel project table per website.

| Piece | Role |
| --- | --- |
| `monitor.py` | CLI entrypoint (Catalant) |
| `script_clean.py` | Catalant login, Selenium scan, detail fetch, email, main loop |
| `extraction.py` | Shared helpers (merge, budget, posted time, status) + Catalant extractors |
| `database.py` | Shared Supabase client + repository functions for all platforms |
| `supabase/migrations/` | Schema source of truth for every scraper |

Catalant platform value: **`catalant`**. Other scrapers use their own lowercase `platform` string in the same rows.

---

## Building a scraper for another website

Use this checklist so every new marketplace attaches to the **same DB** and fills the **same columns**.

### 1. Same Supabase attachment

- Reuse the existing Supabase project URL + **secret** key (`SUPABASE_*` in `.env`).
- Reuse tables: `projects`, `scraper_runs`, `email_attempts`, `scraper_sessions`.
- Reuse `database.py` (or import it). Do **not** invent a second schema.
- Apply migrations from `supabase/migrations/` once per Supabase project (including enrichment columns).
- Set a unique lowercase `platform` value, e.g. `btg`, `guru`, `hubstaff`, `toptal`.
- Store cookies under that same `platform` in `scraper_sessions`.

### 2. Same occurrence / email rules

Every platform scraper should keep these behaviors identical:

| Rule | Behavior |
| --- | --- |
| Occurrence rows | Insert a **new** `projects` row when eligible (no unique constraint on `platform + project_id`) |
| 3-day rule | Skip insert + email when latest row for `(platform, project_id)` has `scraped_at` age ≤ 3 days |
| Cold start | First successful scan seeds existing listings as `SUPPRESSED` / `COLD_START_SEED` (no project emails) |
| Detail before insert | Fetch detail page (when the site has one) before inserting a seed or eligible occurrence |
| Enrichment | Update the **same** `projects.id` for missing detail fields; never create a row only to fill columns |
| Email | Insert → `PENDING` → `email_attempts` → send → update **same** project + attempt rows |
| Sessions | Save/load cookies via `scraper_sessions` for that platform |

### 3. Required identity fields (every platform)

Always populate:

| Column | Requirement |
| --- | --- |
| `platform` | Your marketplace key |
| `project_id` | Stable source id (string) |
| `source_url` | Canonical project URL |
| `title` | Non-empty title |

Without these three identity fields (`project_id`, `title`, `source_url`), do not insert.

### 4. Shared columns every scraper should target

Map each site into these columns when the data is genuinely visible. Leave null / empty when the site does not expose the field — do not invent values.

#### Listing card (minimum useful set)

| Column | Purpose |
| --- | --- |
| `title` | Card title |
| `short_description` | Card summary / blurb only (not full page body) |
| `source_url` / `project_id` | Link + id |
| `platform_category*` | Category / pool / breadcrumb fields |
| `location` | Card location if shown |
| `budget_text` (+ parsed `budget_min` / `budget_max` / `budget_currency` when possible) | Rate or budget display |
| `duration_text` | Card duration if shown |
| `time_posted_text` | Relative/absolute “posted” text |
| `source_posted_at` / `source_posted_at_is_estimated` | Normalized posted time when parseable |
| `status` | Posted / open / etc. |
| `card_extraction_status` | `COMPLETE` / `PARTIAL` / `FAILED` |
| `missing_fields` / `extraction_warnings` / `extraction_metadata` | Diagnostics |

#### Detail page (fill whenever exposed)

| Column | Purpose |
| --- | --- |
| `description` | Full project description (not title/nav/footer) |
| `location_preference` | Structured location preference |
| `remote_or_onsite` | Remote / hybrid / onsite |
| `country_or_region` | Country or region |
| `project_length` / `duration_text` | Engagement length / timeline |
| `start_date_text` / `source_start_date` | Start date text + parsed `date` when calendar-parseable |
| `budget_text` + billing helpers | Structured budget / rate (`billing_type`, `hourly_rate`, `daily_rate`, `budget_source`, `budget_confidence` when migration applied) |
| `level_of_support` | Expert type / support level |
| `industry` | Industry background |
| `contracting_process` | Contracting process |
| `skills` / `expertise` / `deliverables` | Tag arrays |
| `engagement_type` / `project_type` / `workstream` | Engagement metadata |
| `estimated_hours` / `weekly_commitment` | Effort / weekly load |
| `application_deadline` | Deadline when shown |
| `detail_extraction_status` | `NOT_ATTEMPTED` / `COMPLETE` / `PARTIAL` / `FAILED` / `TIMEOUT` |
| Detail attempt columns | `detail_attempt_count`, `detail_last_attempt_at`, `detail_completed_at`, `detail_failure_code`, `detail_last_error` |

#### Always set by the shared repository / lifecycle

| Column | Purpose |
| --- | --- |
| `scraped_at` / `first_detected_at` / `last_seen_at` | Occurrence timing (3-day rule uses `scraped_at`) |
| `email_*` | Email lifecycle fields |
| `scraper_run_id` | Link to `scraper_runs` |

### 5. What to customize vs reuse

| Reuse as-is | Customize per website |
| --- | --- |
| `database.py` | Login / cookie domain |
| Migrations + table names | Listing selectors and card parsers |
| 3-day eligibility helpers | Detail-page wait + field selectors |
| Email attempt lifecycle APIs | HTML email formatting for site-specific labels |
| `merge_project_data` / budget / posted-time helpers | `PLATFORM = "your_platform"` |
| Cold-start suppression semantics | CLI entrypoint / Railway service for that scraper |

### 6. Suggested new-scraper layout

```text
your-site-monitor/
  monitor.py              # CLI → main loop
  script_clean.py         # or site-specific module
  extraction.py           # site selectors + shared helpers import
  database.py             # copy or shared package — same Supabase schema
  .env                    # SUPABASE_* shared; site login + SMTP own
  supabase/migrations/    # same migrations (already applied on shared DB)
```

Example env for a second scraper (same DB, different platform credentials):

```env
SUPABASE_URL=https://<same-project>.supabase.co
SUPABASE_SECRET_KEY=<same-secret-key>
# site-specific
BTG_EMAIL=...
BTG_PASSWORD=...
COOKIES_FILE=btg_cookies.json
```

In code, always write:

```python
platform = "btg"   # never reuse "catalant" for another site
```

### 7. Verification query (any platform)

```sql
select
  platform,
  project_id,
  title,
  length(description) as description_length,
  location_preference,
  budget_text,
  project_length,
  start_date_text,
  industry,
  contracting_process,
  detail_extraction_status,
  email_status,
  scraped_at
from public.projects
where platform = 'your_platform'
order by scraped_at desc
limit 50;
```

---

## How the Catalant scraper works

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
platform = <your_platform>   -- e.g. catalant
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
python monitor.py --backfill-missing-details --dry-run --limit 5
python monitor.py --retry-pending-emails --dry-run
```

Dry-run uses the real Catalant page when authenticated, prints extraction/eligibility decisions, and does **not** insert permanent project rows or send project emails.

---

## Table: `projects` (shared main table)

One row = one **occurrence** of a marketplace project for a platform. **All scrapers share this table.**

### Identity

| Column | Type | Meaning |
| --- | --- | --- |
| `id` | uuid | Internal Supabase primary key for this occurrence row |
| `platform` | text | Lowercase marketplace id (`catalant`, `btg`, `guru`, …). Not constrained to a fixed list |
| `project_id` | text | Source/marketplace project id |
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
| `billing_type` | text | `hourly` / `daily` / `fixed` / `fixed_range` when parsed |
| `hourly_rate` / `daily_rate` | numeric | Parsed rates when available |
| `rate_currency` | text | Currency for rate fields |
| `budget_source` | text | e.g. structured label, `title_rate_fallback` |
| `budget_confidence` | text | `HIGH` / `MEDIUM` / `LOW` |
| `duration_text` | text | Duration string |
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
| `detail_attempt_count` | integer | Detail fetch attempts for this row |
| `detail_last_attempt_at` | timestamptz | Last detail attempt |
| `detail_completed_at` | timestamptz | Last successful/finished detail extraction |
| `detail_failure_code` | text | Classified detail failure |
| `detail_last_error` | text | Sanitized detail error |
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

One row per monitoring cycle (any platform).

| Column | Type | Meaning |
| --- | --- | --- |
| `id` | uuid | Run id |
| `platform` | text | Platform being scraped |
| `scraper_name` | text | Scraper identifier (`catalant-monitor`, `btg-monitor`, …) |
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

Auditable history of each send attempt against a **project occurrence** (`projects.id`). Shared across platforms.

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
| `platform` | text | Platform key (`catalant`, `btg`, …) |
| `session_data` | jsonb | Sanitized Selenium cookies JSON (`{"cookies":[...]}`) |
| `saved_at` | timestamptz | When session was saved |
| `expires_at` | timestamptz | Earliest cookie expiry when determinable |
| `session_version` | integer | Session format version |
| `metadata` | jsonb | Safe metadata (e.g. cookie count) — never passwords |
| `created_at` / `updated_at` | timestamptz | Audit timestamps |

Passwords are never stored here. Raw cookies are never logged or attached to operational emails.

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
