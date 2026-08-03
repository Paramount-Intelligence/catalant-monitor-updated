"""
Robust Catalant field extraction helpers (no database I/O).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

_CATEGORY_NOISE = (
    "posted", "login", "search", "budget", "location", "timeline",
    "start date", "contracting", "industry", "description", "summary",
    "apply", "save", "share", "filter", "sort", "results", "recommended",
)
_INVALID_CATEGORY_VALUES = {"unclassified", "unknown", "n/a", "na", "none", "null"}

_PLACEHOLDER_STRINGS = {
    "",
    "unknown",
    "unclassified",
    "n/a",
    "na",
    "none",
    "not specified",
    "tbd",
}

CARD_REQUIRED_FIELDS = ("project_id", "title", "source_url", "platform_category", "time_posted_text")
CORE_DETAIL_FIELDS = (
    "description",
    "location_preference",
    "project_length",
    "start_date_text",
    "budget_text",
    "level_of_support",
    "industry",
    "contracting_process",
)


def category_path_from_text(cat_text: str) -> list[str]:
    if not cat_text:
        return []
    text = (
        str(cat_text)
        .replace("\u00a0", " ")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
        .strip()
    )
    text = re.sub(r"\s*[›»→]\s*", " > ", text)
    text = re.sub(r"\s*\|\s*", " > ", text)
    return [p.strip() for p in re.split(r"\s*>\s*", text) if p and p.strip()]


def normalize_category_candidate(raw, *, allow_single: bool = False):
    """Return (category, path, raw, rejected_reason)."""
    if not raw:
        return None, [], "", "empty"
    text = str(raw).replace("\u00a0", " ").strip()
    if not text or len(text) > 200:
        return None, [], text, "too_long_or_empty"
    path = category_path_from_text(text)
    if not path and allow_single:
        if "\n" in text or ":" in text:
            return None, [], text, "looks_like_label"
        if not (3 <= len(text) <= 80):
            return None, [], text, "length"
        lowered = text.lower()
        if any(n in lowered for n in _CATEGORY_NOISE):
            return None, [], text, "noise"
        path = [text.strip()]
    if not path:
        return None, [], text, "no_path"
    top = path[0].strip()
    if top.lower() in _INVALID_CATEGORY_VALUES:
        return None, path, text, "invalid_placeholder"
    if any(n in top.lower() for n in _CATEGORY_NOISE):
        return None, path, text, "noise"
    return top, path, text, None


def category_result(category, path, raw, source, confidence, status):
    return {
        "platform_category": category or None,
        "platform_category_path": path or [],
        "platform_category_raw": raw or None,
        "platform_category_source": source,
        "platform_category_confidence": confidence,
        "platform_category_extraction_status": status,
    }


def extract_category_from_body_text(body_text: str) -> dict:
    if not body_text:
        return category_result(None, [], None, None, None, "MISSING")

    for m in re.finditer(
        r"(?im)^(?:Category|Practice Area|Functional Area|Pools?)\s*:\s*(.+)$",
        body_text,
    ):
        cat, path, cleaned, rejected = normalize_category_candidate(
            m.group(1), allow_single=True
        )
        if rejected == "invalid_placeholder":
            return category_result(
                None, path, cleaned, "text_label", None, "REJECTED_INVALID_CANDIDATE"
            )
        if cat:
            return category_result(
                cat, path, cleaned, "text_label", "LOW", "FOUND_TEXT_FALLBACK"
            )

    for m in re.finditer(
        r"(?m)^([A-Za-z][^\n:]{2,60}?)\s*>\s*([^\n]{2,80})$",
        body_text,
    ):
        line = m.group(0).strip()
        if len(line) > 120:
            continue
        cat, path, cleaned, rejected = normalize_category_candidate(
            line, allow_single=False
        )
        if rejected == "invalid_placeholder":
            return category_result(
                None, path, cleaned, "text_breadcrumb", None, "REJECTED_INVALID_CANDIDATE"
            )
        if cat and len(path) >= 2:
            return category_result(
                cat, path, cleaned, "text_breadcrumb", "LOW", "FOUND_TEXT_FALLBACK"
            )

    if re.search(r"(?im)^\s*unclassified\s*$", body_text):
        return category_result(
            None, [], "Unclassified", "text_reject", None, "REJECTED_INVALID_CANDIDATE"
        )

    return category_result(None, [], None, None, None, "MISSING")


def extract_category_from_embedded_json(content: str) -> dict:
    if not content or len(content) > 200000:
        return category_result(None, [], None, None, None, "MISSING")
    if not any(tok in content.lower() for tok in ("category", "breadcrumb", "practice")):
        return category_result(None, [], None, None, None, "MISSING")
    for pattern in (
        r'"category"\s*:\s*"([^"]{2,120})"',
        r'"practiceArea"\s*:\s*"([^"]{2,120})"',
        r'"functionalArea"\s*:\s*"([^"]{2,120})"',
    ):
        m = re.search(pattern, content, re.IGNORECASE)
        if not m:
            continue
        cat, path, cleaned, rejected = normalize_category_candidate(
            m.group(1), allow_single=True
        )
        if rejected == "invalid_placeholder":
            return category_result(
                None, path, cleaned, "embedded_json", None, "REJECTED_INVALID_CANDIDATE"
            )
        if cat:
            return category_result(
                cat, path, cleaned, "embedded_json", "MEDIUM", "FOUND_EMBEDDED_DATA"
            )
    return category_result(None, [], None, None, None, "MISSING")


def is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in _PLACEHOLDER_STRINGS or not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def normalize_visible_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def field_result(value=None, *, raw_value=None, source=None, selector_or_label=None, confidence=None):
    return {
        "value": value,
        "raw_value": raw_value if raw_value is not None else value,
        "source": source,
        "selector_or_label": selector_or_label,
        "confidence": confidence,
    }


def validate_extracted_value(value: Any, *, title: str = "", category: str = "") -> bool:
    if is_empty_value(value):
        return False
    if not isinstance(value, str):
        return True
    text = normalize_visible_text(value)
    if not text or len(text) < 2:
        return False
    if title and text.strip().lower() == title.strip().lower():
        return False
    if category and text.strip().lower() == category.strip().lower():
        return False
    return True


def extract_value_by_label(body_text: str, labels, *, take_last: bool = False) -> dict:
    """Extract 'Label: Value' from normalized body text."""
    if not body_text:
        return field_result()
    label_alt = "|".join(re.escape(lbl) for lbl in labels)
    pattern = rf"(?im)^(?:{label_alt})\s*:\s*(.+)$"
    matches = list(re.finditer(pattern, body_text))
    if not matches:
        return field_result()
    m = matches[-1] if take_last else matches[0]
    value = normalize_visible_text(m.group(1))
    if not value:
        return field_result()
    return field_result(
        value,
        source="label_value",
        selector_or_label=m.group(0).split(":", 1)[0].strip(),
        confidence="HIGH",
    )


def extract_section_text(body_text: str, start_labels, end_labels) -> dict:
    if not body_text:
        return field_result()
    start_alt = "|".join(re.escape(s) for s in start_labels)
    end_alt = "|".join(re.escape(s) for s in end_labels)
    pattern = (
        rf"(?is)(?:^|\n)\s*(?:{start_alt})\s*\n+"
        rf"(.+?)"
        rf"(?=\n\s*(?:{end_alt})\b|\Z)"
    )
    m = re.search(pattern, body_text)
    if not m:
        return field_result()
    value = normalize_visible_text(m.group(1))
    if len(value) < 30:
        return field_result()
    return field_result(
        value,
        source="section_text",
        selector_or_label=start_labels[0],
        confidence="MEDIUM",
    )


def extract_list_values(body_text: str, labels) -> dict:
    result = extract_value_by_label(body_text, labels)
    if is_empty_value(result.get("value")):
        return field_result(value=[], source=None)
    raw = str(result["value"])
    parts = [p.strip() for p in re.split(r"[,;/|•\n]+", raw) if p.strip()]
    return field_result(
        parts,
        raw_value=raw,
        source=result.get("source"),
        selector_or_label=result.get("selector_or_label"),
        confidence=result.get("confidence") or "MEDIUM",
    )


def extract_embedded_json(content: str, keys) -> dict:
    if not content:
        return field_result()
    for key in keys:
        m = re.search(rf'"{re.escape(key)}"\s*:\s*"([^"]{{2,500}})"', content, re.I)
        if m:
            return field_result(
                normalize_visible_text(m.group(1)),
                source="embedded_json",
                selector_or_label=key,
                confidence="MEDIUM",
            )
    return field_result()


def validate_budget_candidate(candidate: str, project: Optional[dict] = None) -> tuple[bool, str]:
    project = project or {}
    title = normalize_visible_text(project.get("title") or "")
    text = normalize_visible_text(candidate)
    if not text:
        return False, "empty"
    if text.lower() in ("not provided", "n/a", "none", "not available", "tbd"):
        return True, "ok_not_provided"
    if title and text.lower() == title.lower():
        return False, "equals_title"
    if title and title.lower() in text.lower() and len(text) > len(title) * 0.8:
        return False, "contains_title"
    if len(text) > 80:
        return False, "too_long"
    if not re.search(r"(\$|usd|eur|gbp|\d)", text, re.I):
        return False, "no_money_token"
    # Reject long descriptive prose with many words and no clear rate pattern
    if len(text.split()) > 12 and not re.search(r"\$?\d[\d,]*(?:\.\d+)?\s*/?\s*(hr|hour|day|mo|month)?", text, re.I):
        return False, "looks_like_prose"
    return True, "ok"


def _is_non_duration_timeline(value: str) -> bool:
    """True when Timeline is a start cue (ASAP) rather than a length."""
    s = normalize_visible_text(value).lower()
    return s in ("asap", "immediately", "immediate", "tbd", "flexible", "as soon as possible")


def _derive_remote_or_onsite(location_value: str) -> Optional[str]:
    """
    Map Catalant Location / Location Preference values onto remote_or_onsite
    when the site does not expose a separate work-arrangement label.
    """
    text = normalize_visible_text(location_value)
    if not text:
        return None
    lower = text.lower()
    if re.search(r"\bhybrid\b", lower):
        return "Hybrid"
    if re.search(r"\bremote[- ]friendly\b|\bremote\b", lower) and not re.search(
        r"\bon[- ]?site\b|\bin[- ]?person\b", lower
    ):
        # Prefer the site's own wording when short; otherwise normalize
        if lower.strip() in ("remote", "fully remote", "remote only"):
            return "Remote"
        if "remote" in lower and len(text.split()) <= 8:
            return text
        return "Remote"
    if re.search(r"\bon[- ]?site\b|\bin[- ]?person\b", lower) and "remote" not in lower:
        if lower.strip() in ("on-site", "onsite", "on site", "in-person", "in person"):
            return "On-site"
        if len(text.split()) <= 8:
            return text
        return "On-site"
    return None


def parse_budget(candidate: str) -> dict:
    text = normalize_visible_text(candidate)
    out = {
        "budget_text": text or None,
        "budget_min": None,
        "budget_max": None,
        "budget_currency": None,
        "billing_type": None,
        "hourly_rate": None,
        "daily_rate": None,
        "rate_currency": None,
    }
    if not text or text.lower() in ("not provided", "n/a", "none"):
        out["budget_text"] = None if not text else text
        return out

    if re.search(r"\bUSD\b|\$", text, re.I):
        out["budget_currency"] = "USD"
        out["rate_currency"] = "USD"

    hourly = re.search(r"\$?\s*([\d,]+(?:\.\d+)?)\s*/\s*(?:hr|hour)\b", text, re.I)
    if hourly:
        out["hourly_rate"] = float(hourly.group(1).replace(",", ""))
        out["billing_type"] = "hourly"
        out["budget_text"] = f"${hourly.group(1)}/hr"
        return out

    daily = re.search(r"\$?\s*([\d,]+(?:\.\d+)?)\s*/\s*day\b", text, re.I)
    if daily:
        out["daily_rate"] = float(daily.group(1).replace(",", ""))
        out["billing_type"] = "daily"
        out["budget_text"] = f"${daily.group(1)}/day"
        return out

    rng = re.search(
        r"\$?\s*([\d,]+(?:\.\d+)?)\s*[kK]?\s*[–\-to]+\s*\$?\s*([\d,]+(?:\.\d+)?)\s*[kK]?",
        text,
    )
    if rng:
        lo = float(rng.group(1).replace(",", ""))
        hi = float(rng.group(2).replace(",", ""))
        if "k" in text.lower():
            if lo < 1000:
                lo *= 1000
            if hi < 1000:
                hi *= 1000
        out["budget_min"] = lo
        out["budget_max"] = hi
        out["billing_type"] = "fixed_range"
        return out

    single = re.search(r"\$?\s*([\d,]+(?:\.\d+)?)\s*([kK])?\b", text)
    if single:
        amount = float(single.group(1).replace(",", ""))
        if single.group(2):
            amount *= 1000
        out["budget_min"] = amount
        out["budget_max"] = amount
        out["billing_type"] = "fixed"
    return out


def extract_title_rate_fallback(title: str) -> dict:
    """Low-confidence: isolate $N/hr from title without storing the whole title."""
    title = normalize_visible_text(title)
    m = re.search(r"(\$\s*[\d,]+(?:\.\d+)?\s*/\s*(?:hr|hour|day))", title, re.I)
    if not m:
        return {}
    parsed = parse_budget(m.group(1))
    parsed["budget_source"] = "title_rate_fallback"
    parsed["budget_confidence"] = "LOW"
    return parsed


def parse_relative_posted_time(text: str, scraped_at: Optional[datetime] = None):
    """Return (source_posted_at, is_estimated) or (None, False)."""
    scraped_at = scraped_at or datetime.now(timezone.utc)
    if scraped_at.tzinfo is None:
        scraped_at = scraped_at.replace(tzinfo=timezone.utc)
    s = normalize_visible_text(text).lower()
    if not s or s in ("unknown",):
        return None, False
    if any(tok in s for tok in ("just now", "moment", "second")):
        return scraped_at, True
    if re.search(r"\ba\s+minute\b", s):
        return scraped_at - timedelta(minutes=1), True
    if re.search(r"\ban?\s+hour\b", s):
        return scraped_at - timedelta(hours=1), True
    if re.search(r"\ba\s+day\b", s):
        return scraped_at - timedelta(days=1), True
    if re.search(r"\ba\s+week\b", s):
        return scraped_at - timedelta(weeks=1), True
    m = re.search(r"(\d+)\s*(minute|hour|day|week|month)s?", s)
    if not m:
        return None, False
    n = int(m.group(1))
    unit = m.group(2)
    delta = {
        "minute": timedelta(minutes=n),
        "hour": timedelta(hours=n),
        "day": timedelta(days=n),
        "week": timedelta(weeks=n),
        "month": timedelta(days=30 * n),
    }[unit]
    return scraped_at - delta, True


def parse_source_start_date(text: str):
    text = normalize_visible_text(text)
    if not text:
        return None
    if re.search(r"(?i)\b(asap|immediately|tbd|flexible)\b", text):
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text[:40], fmt).date().isoformat()
        except ValueError:
            continue
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2))).date().isoformat()
        except ValueError:
            return None
    return None


def calculate_card_extraction_status(project: dict) -> str:
    pid = project.get("project_id") or project.get("id")
    title = project.get("title")
    url = project.get("source_url") or project.get("url")
    if is_empty_value(pid) or is_empty_value(title) or is_empty_value(url):
        return "FAILED"
    missing_expected = []
    for field in CARD_REQUIRED_FIELDS:
        val = project.get(field)
        if field == "project_id":
            val = pid
        if field == "source_url":
            val = url
        if field == "time_posted_text":
            val = project.get("time_posted_text") or project.get("time_posted")
        if is_empty_value(val) or (field == "time_posted_text" and str(val).lower() == "unknown"):
            missing_expected.append(field)
    if missing_expected:
        return "PARTIAL"
    return "COMPLETE"


def calculate_detail_extraction_status(
    *,
    attempted: bool,
    page_ok: bool,
    timeout: bool = False,
    fields_visible: Optional[list] = None,
    fields_extracted: Optional[list] = None,
    meaningful: bool = False,
) -> str:
    if not attempted:
        return "NOT_ATTEMPTED"
    if timeout:
        return "TIMEOUT"
    if not page_ok:
        return "FAILED"
    fields_visible = fields_visible or []
    fields_extracted = fields_extracted or []
    if not meaningful and not fields_extracted:
        return "FAILED"
    missing_visible = [f for f in fields_visible if f not in fields_extracted]
    if missing_visible:
        return "PARTIAL"
    if fields_visible and all(f in fields_extracted for f in fields_visible):
        return "COMPLETE"
    if meaningful:
        return "COMPLETE" if not missing_visible else "PARTIAL"
    return "PARTIAL"


def compute_missing_fields(project: dict, *, expected_fields: Optional[list] = None) -> list:
    expected = expected_fields or list(CARD_REQUIRED_FIELDS) + [
        f for f in CORE_DETAIL_FIELDS if project.get("detail_extraction_status") not in (None, "NOT_ATTEMPTED")
    ]
    missing = []
    for field in expected:
        val = project.get(field)
        if field == "project_id":
            val = project.get("project_id") or project.get("id")
        if field == "source_url":
            val = project.get("source_url") or project.get("url")
        if field == "time_posted_text":
            val = project.get("time_posted_text") or project.get("time_posted")
        if field == "project_length":
            val = project.get("project_length") or project.get("duration_text") or project.get("duration")
        if field == "budget_text":
            val = project.get("budget_text") or project.get("budget")
        if field == "location_preference":
            val = project.get("location_preference") or project.get("location_pref")
        if is_empty_value(val) or (field == "time_posted_text" and str(val).lower() == "unknown"):
            missing.append(field)
    return missing


def extract_detail_fields_from_body(body_text: str, *, title: str = "", project: Optional[dict] = None) -> dict:
    """Pure-text detail extraction used by Selenium wrapper and unit tests."""
    project = project or {}
    warnings = []
    metadata = {
        "fields_visible_on_page": [],
        "fields_extracted": [],
        "fields_missing_but_visible": [],
        "fields_not_exposed": [],
    }
    details: dict[str, Any] = {}

    # Description — Catalant uses "Project Description" (often truncated until More)
    desc = extract_section_text(
        body_text,
        ["Project Description", "Description", "Summary"],
        [
            "Project Logistics",
            "Other Details",
            "Budget",
            "Expert Preferences",
            "Contracting",
            "Skills",
            "Your Pitch",
            "Bookmark Project",
        ],
    )
    if desc.get("value") and validate_extracted_value(desc["value"], title=title):
        details["description"] = desc["value"]
        metadata["fields_extracted"].append("description")
        metadata["fields_visible_on_page"].append("description")
    elif re.search(r"(?im)^(?:Project )?Description\s*$", body_text or ""):
        metadata["fields_visible_on_page"].append("description")
        metadata["fields_missing_but_visible"].append("description")
        warnings.append("DESCRIPTION_CONTAINER_NOT_FOUND")
    else:
        metadata["fields_not_exposed"].append("description")

    # Duration / Timeline — prefer Duration; Timeline may be ASAP (start cue) or a length
    duration_item = extract_value_by_label(body_text, ["Duration"])
    timeline_items = []
    for m in re.finditer(
        r"(?im)^(?:Timeline|Project Length|Expected Duration|Engagement Length)\s*:\s*(.+)$",
        body_text or "",
    ):
        timeline_items.append(normalize_visible_text(m.group(1)))

    if duration_item.get("value") and validate_extracted_value(duration_item["value"], title=title):
        details["project_length"] = duration_item["value"]
        details["duration_text"] = duration_item["value"]
        metadata["fields_extracted"].append("project_length")
        metadata["fields_visible_on_page"].append("project_length")
    else:
        length_candidate = None
        for t in reversed(timeline_items):
            if t and not _is_non_duration_timeline(t):
                length_candidate = t
                break
        if length_candidate:
            details["project_length"] = length_candidate
            details["duration_text"] = length_candidate
            metadata["fields_extracted"].append("project_length")
            metadata["fields_visible_on_page"].append("project_length")
        elif timeline_items:
            metadata["fields_visible_on_page"].append("project_length")
            metadata["fields_missing_but_visible"].append("project_length")
            warnings.append("TIMELINE_NOT_A_DURATION")
        else:
            metadata["fields_not_exposed"].append("project_length")

    # Structured label fields (duration/timeline handled above)
    label_map = {
        "start_date_text": ["Start Date", "Expected Start"],
        "level_of_support": ["Expert Type", "Level of Support"],
        "industry": ["Industry", "Desired Industry Background"],
        "contracting_process": ["Contracting Process"],
        "engagement_type": ["Engagement Type"],
        "project_type": ["Project Type"],
        "workstream": ["Workstream"],
        "weekly_commitment": ["Weekly Commitment", "Hours per Week"],
        "remote_or_onsite": [
            "In-person vs. Remote",
            "In-person vs Remote",
            "Remote or Onsite",
            "Work Arrangement",
        ],
        "country_or_region": ["Country", "Region", "Country or Region"],
        "application_deadline": ["Application Deadline"],
    }
    for field, labels in label_map.items():
        item = extract_value_by_label(body_text, labels)
        visible = bool(re.search(
            rf"(?im)^(?:{'|'.join(re.escape(x) for x in labels)})\s*:",
            body_text or "",
        ))
        if visible:
            metadata["fields_visible_on_page"].append(field)
        if item.get("value") and validate_extracted_value(item["value"], title=title):
            details[field] = item["value"]
            metadata["fields_extracted"].append(field)
            if field == "start_date_text":
                details["source_start_date"] = parse_source_start_date(item["value"])
        elif visible:
            metadata["fields_missing_but_visible"].append(field)
            warnings.append(f"VISIBLE_FIELD_NOT_EXTRACTED:{field}")
        else:
            metadata["fields_not_exposed"].append(field)

    # Location preference — last Location: line (sidebar), not prose
    loc = extract_value_by_label(body_text, ["Location Preference", "Location"], take_last=True)
    if loc.get("value") and validate_extracted_value(loc["value"], title=title):
        # Reject values that look like paragraphs
        if len(str(loc["value"]).split()) <= 20:
            details["location_preference"] = loc["value"]
            details["location_pref"] = loc["value"]
            details.setdefault("location", loc["value"])
            metadata["fields_extracted"].append("location_preference")
            metadata["fields_visible_on_page"].append("location_preference")
            # Catalant often exposes work mode only as Location Preference = Remote|On-site|Hybrid
            if is_empty_value(details.get("remote_or_onsite")):
                derived = _derive_remote_or_onsite(loc["value"])
                if derived:
                    details["remote_or_onsite"] = derived
                    metadata["fields_extracted"].append("remote_or_onsite")
                    metadata["fields_visible_on_page"].append("remote_or_onsite")
                    # Was marked not_exposed earlier from label_map — correct that
                    metadata["fields_not_exposed"] = [
                        f for f in metadata["fields_not_exposed"] if f != "remote_or_onsite"
                    ]
        else:
            warnings.append("LOCATION_AMBIGUOUS")
            metadata["fields_visible_on_page"].append("location_preference")
            metadata["fields_missing_but_visible"].append("location_preference")
    elif re.search(r"(?im)^Location(?: Preference)?\s*:", body_text or ""):
        metadata["fields_visible_on_page"].append("location_preference")
        metadata["fields_missing_but_visible"].append("location_preference")
        warnings.append("VISIBLE_FIELD_NOT_EXTRACTED:location_preference")
    else:
        metadata["fields_not_exposed"].append("location_preference")

    # Budget — structured only
    budget_item = None
    m = re.search(r"(?im)^Project Budget:\s*$", body_text or "")
    if m:
        # value on following non-empty line
        after = (body_text or "")[m.end():]
        line = ""
        for raw_line in after.splitlines():
            if raw_line.strip():
                line = normalize_visible_text(raw_line)
                break
        if line:
            budget_item = field_result(line, source="label_next_line", selector_or_label="Project Budget", confidence="HIGH")
    if not budget_item or is_empty_value(budget_item.get("value")):
        budget_item = extract_value_by_label(body_text, ["Project Budget", "Budget", "Hourly Rate", "Rate"])

    if budget_item.get("value"):
        metadata["fields_visible_on_page"].append("budget_text")
        ok, reason = validate_budget_candidate(budget_item["value"], {"title": title, **project})
        if ok:
            parsed = parse_budget(budget_item["value"])
            parsed["budget_source"] = budget_item.get("source") or "structured"
            parsed["budget_confidence"] = budget_item.get("confidence") or "HIGH"
            details.update({k: v for k, v in parsed.items() if v is not None})
            metadata["fields_extracted"].append("budget_text")
        else:
            warnings.append(f"BUDGET_CANDIDATE_REJECTED_{reason.upper()}")
            metadata["fields_missing_but_visible"].append("budget_text")
    else:
        # Optional low-confidence title rate fallback
        fallback = extract_title_rate_fallback(title)
        if fallback.get("budget_text"):
            details.update(fallback)
            metadata["fields_extracted"].append("budget_text")
            warnings.append("BUDGET_FROM_TITLE_RATE_FALLBACK")
        else:
            metadata["fields_not_exposed"].append("budget_text")

    # Skills / expertise / deliverables
    for field, labels in (
        ("skills", ["Skills", "Required Skills"]),
        ("expertise", ["Expertise", "Expert Preferences"]),
        ("deliverables", ["Deliverables"]),
    ):
        item = extract_list_values(body_text, labels)
        if item.get("value"):
            details[field] = item["value"]
            metadata["fields_extracted"].append(field)
            metadata["fields_visible_on_page"].append(field)
        else:
            metadata["fields_not_exposed"].append(field)

    # Expert type standalone line
    if is_empty_value(details.get("level_of_support")):
        m = re.search(
            r"(?im)^(Independent Expert|Open to Both|Consulting Firm|Both)$",
            body_text or "",
        )
        if m:
            details["level_of_support"] = m.group(1)
            metadata["fields_extracted"].append("level_of_support")
            metadata["fields_visible_on_page"].append("level_of_support")

    meaningful = any(
        not is_empty_value(details.get(f))
        for f in ("description", "location_preference", "project_length", "industry", "contracting_process", "budget_text", "level_of_support")
    )
    status = calculate_detail_extraction_status(
        attempted=True,
        page_ok=True,
        fields_visible=metadata["fields_visible_on_page"],
        fields_extracted=metadata["fields_extracted"],
        meaningful=meaningful,
    )
    details["detail_extraction_status"] = status
    details["extraction_metadata"] = metadata
    details["extraction_warnings"] = warnings
    details["missing_fields"] = [
        f for f in metadata["fields_visible_on_page"] if f not in metadata["fields_extracted"]
    ]
    return details


def merge_project_data(card_data: dict, detail_data: Optional[dict] = None) -> dict:
    """Safe card/detail merge — empty/placeholder detail values do not overwrite card."""
    merged = dict(card_data or {})
    warnings = list(merged.get("extraction_warnings") or [])
    detail_data = detail_data or {}

    for key, detail_val in detail_data.items():
        if key in ("extraction_warnings", "missing_fields", "extraction_metadata"):
            continue
        card_val = merged.get(key)
        if is_empty_value(detail_val):
            if not is_empty_value(card_val):
                warnings.append(f"detail_empty_preserved_card:{key}")
            continue
        if isinstance(detail_val, str) and detail_val.strip().lower() == "unclassified":
            if not is_empty_value(card_val) and str(card_val).strip().lower() != "unclassified":
                warnings.append("detail_rejected_unclassified_category")
                continue
            if is_empty_value(card_val):
                warnings.append("detail_rejected_unclassified_empty")
                continue
        if isinstance(detail_val, list) and not detail_val and card_val:
            continue
        # Never let a rejected title-budget overwrite nothing useful incorrectly
        if key == "budget_text":
            ok, reason = validate_budget_candidate(str(detail_val), merged)
            if not ok:
                warnings.append(f"BUDGET_CANDIDATE_REJECTED_{reason.upper()}")
                continue
        merged[key] = detail_val

    meta = dict(merged.get("extraction_metadata") or {})
    meta.update(detail_data.get("extraction_metadata") or {})
    if meta:
        merged["extraction_metadata"] = meta

    warnings.extend(detail_data.get("extraction_warnings") or [])
    if warnings:
        # de-dupe while preserving order
        seen = set()
        deduped = []
        for w in warnings:
            if w not in seen:
                seen.add(w)
                deduped.append(w)
        merged["extraction_warnings"] = deduped

    # Posted time normalize
    posted = merged.get("time_posted_text") or merged.get("time_posted")
    if posted and is_empty_value(merged.get("source_posted_at")):
        scraped = datetime.now(timezone.utc)
        parsed, estimated = parse_relative_posted_time(str(posted), scraped)
        if parsed is not None:
            merged["source_posted_at"] = parsed.isoformat()
            merged["source_posted_at_is_estimated"] = estimated

    # Short description vs description
    short = merged.get("short_description")
    title = merged.get("title") or ""
    if short and (
        short.strip().lower() == title.strip().lower()
        or short == merged.get("platform_category")
        or short.lower().startswith("posted")
    ):
        warnings.append("SHORT_DESCRIPTION_REJECTED_NOISE")
        merged["short_description"] = None

    merged["card_extraction_status"] = calculate_card_extraction_status(merged)
    if "detail_extraction_status" not in merged:
        merged["detail_extraction_status"] = detail_data.get("detail_extraction_status") or "NOT_ATTEMPTED"

    expected = list(CARD_REQUIRED_FIELDS)
    if merged.get("detail_extraction_status") not in (None, "NOT_ATTEMPTED"):
        expected.extend([f for f in CORE_DETAIL_FIELDS if f in (meta.get("fields_visible_on_page") or CORE_DETAIL_FIELDS)])
    # Prefer detail missing_fields when present
    if detail_data.get("missing_fields"):
        merged["missing_fields"] = list(dict.fromkeys(
            list(detail_data.get("missing_fields") or []) + compute_missing_fields(merged, expected_fields=CARD_REQUIRED_FIELDS)
        ))
    else:
        merged["missing_fields"] = compute_missing_fields(merged, expected_fields=expected)

    if warnings and "extraction_warnings" not in merged:
        merged["extraction_warnings"] = warnings
    return merged
