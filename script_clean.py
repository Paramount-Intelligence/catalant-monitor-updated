import sys
import time
import smtplib
import json
import os
import re
import html
import socket
import threading
import traceback
import hashlib
import tempfile
import mimetypes
import argparse
from email.message import EmailMessage

# Ensure UTF-8 output on all platforms (fixes Windows emoji crash)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
from datetime import datetime, timezone, timedelta

PKT = timezone(timedelta(hours=5))  # Pakistan Standard Time (UTC+5)
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from dotenv import load_dotenv

import database as db
import extraction

# Load environment variables
load_dotenv()

PLATFORM = db.PLATFORM_CATALANT

# ============================
# CONFIGURATION
# ============================
def _resolve_evidence_dir():
    configured = (os.getenv("EVIDENCE_DIR") or "").strip()
    if configured:
        return configured
    return os.path.join(tempfile.gettempdir(), "catalant-evidence")


class Config:
    """Load configuration from environment variables"""
    CATALANT_EMAIL = os.getenv("CATALANT_EMAIL")
    CATALANT_PASSWORD = os.getenv("CATALANT_PASSWORD")
    SMTP_SERVER = os.getenv("SMTP_SERVER")
    SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
    SENDER_EMAIL = os.getenv("SENDER_EMAIL")
    SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
    RECIPIENT_EMAILS = [e.strip() for e in os.getenv("RECIPIENT_EMAILS", "ahmedghazi459@gmail.com,ahsanuddin3522@gmail.com").split(",") if e.strip()]
    _ERROR_RECIPIENTS_RAW = (
        os.getenv("ERROR_RECIPIENTS")
        or os.getenv("ERROR_RECIPIENT")
        or os.getenv("ERROR_RECIPENT")
        or os.getenv("error_recipent")
        or ""
    )
    ERROR_RECIPIENTS = [
        email.strip()
        for email in _ERROR_RECIPIENTS_RAW.split(",")
        if email.strip()
    ]
    ERROR_EMAIL_COOLDOWN_MINUTES = int(os.getenv("ERROR_EMAIL_COOLDOWN_MINUTES", "30"))
    LOGIN_RETRY_INTERVAL = int(os.getenv("LOGIN_RETRY_INTERVAL", "300"))
    CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 60))
    MAX_AGE_MINUTES = int(os.getenv("MAX_AGE_MINUTES", 60))
    REPOST_MIN_DAYS = int(
        os.getenv("OCCURRENCE_WINDOW_DAYS")
        or os.getenv("REPOST_MIN_DAYS", "3")
    )
    EMAIL_MAX_RETRIES = int(os.getenv("EMAIL_MAX_RETRIES", "5"))
    EMAIL_RETRY_BASE_MINUTES = int(os.getenv("EMAIL_RETRY_BASE_MINUTES", "15"))
    DETAIL_FETCH_DELAY_SECONDS = float(os.getenv("DETAIL_FETCH_DELAY_SECONDS", "2"))
    DETAIL_MAX_ATTEMPTS = int(os.getenv("DETAIL_MAX_ATTEMPTS", "2"))
    TAB_CRASH_MAX_RETRIES = int(os.getenv("TAB_CRASH_MAX_RETRIES", "2"))
    TAB_CRASH_RETRY_DELAY_SECONDS = int(os.getenv("TAB_CRASH_RETRY_DELAY_SECONDS", "120"))
    HEADLESS = os.getenv("HEADLESS", "False").lower() == "true"
    COOKIES_FILE = os.getenv("COOKIES_FILE", "catalant_cookies.json")
    SUPABASE_URL = os.getenv("SUPABASE_URL", "")
    EVIDENCE_DIR = _resolve_evidence_dir()
    EVIDENCE_RETENTION_HOURS = int(os.getenv("EVIDENCE_RETENTION_HOURS", "24"))


# Runtime state for operational alerts (never stores secrets)
_error_alert_lock = threading.Lock()
_error_alert_last_sent = {}
_error_alert_in_progress = False
_monitor_check_count = 0
_monitor_state = "starting"
_last_successful_scan_at = None
_last_login_alert = {"alert_sent": False, "classification": None}
last_scan_issue = None  # set by scan_for_projects; consumed once by main loop
_browser_versions_cache = None
_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
_BLOCKED_ATTACHMENT_NAMES = {
    ".env", "catalant_cookies.json", "cookies.json", "credentials.json",
}

# ============================
# OPERATIONAL ERROR ALERTS
# ============================
def redact_sensitive_text(value):
    """Redact credentials, tokens, cookies, and database secrets from text."""
    if value is None:
        return ""
    out = str(value)
    for secret in (
        Config.CATALANT_PASSWORD,
        Config.SENDER_PASSWORD,
        os.getenv("SUPABASE_SECRET_KEY"),
        os.getenv("SUPABASE_SERVICE_ROLE_KEY"),
        os.getenv("SUPABASE_ACCESS_TOKEN"),
        os.getenv("SUPABASE_DB_PASSWORD"),
        os.getenv("SUPABASE_DB_URL"),
        os.getenv("MONGO_URI"),
    ):
        if secret:
            out = out.replace(secret, "[REDACTED_PASSWORD]")
    out = re.sub(
        r"(mongodb(?:\+srv)?://)([^:@/\s]+):([^@/\s]+)@",
        r"\1[REDACTED_USER]:[REDACTED_PASSWORD]@",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        r"(postgresql(?:\+?\w*)?://)([^:@/\s]+):([^@/\s]+)@",
        r"\1[REDACTED_USER]:[REDACTED_PASSWORD]@",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        r"(?i)(sb_secret_|sb_publishable_)[A-Za-z0-9._\-]+",
        "[REDACTED_KEY]",
        out,
    )
    out = re.sub(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]+=*", "Bearer [REDACTED_TOKEN]", out)
    out = re.sub(
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
        "[REDACTED_JWT]",
        out,
    )
    out = re.sub(
        r"(?i)(cookie|set-cookie|authorization|x-api-key|session[_-]?token)\s*[:=]\s*[^;\s]+",
        r"\1=[REDACTED]",
        out,
    )
    out = re.sub(
        r"(?i)(password|passwd|pwd|token|access_token|refresh_token|secret[_-]?key)\s*[=:]\s*[^\s&]+",
        r"\1=[REDACTED]",
        out,
    )
    return out


def _password_fingerprint(password):
    if not password:
        return ""
    return hashlib.sha256(password.encode("utf-8", errors="replace")).hexdigest()[:12]


def _evidence_dir():
    path = Config.EVIDENCE_DIR
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except Exception:
        fallback = os.path.join(tempfile.gettempdir(), "catalant-evidence")
        os.makedirs(fallback, exist_ok=True)
        return fallback


def clean_old_evidence_files():
    """Delete generated Catalant evidence files older than retention period."""
    root = Config.EVIDENCE_DIR
    try:
        if not os.path.isdir(root):
            return
        cutoff = time.time() - max(Config.EVIDENCE_RETENTION_HOURS, 1) * 3600
        removed = 0
        for name in os.listdir(root):
            if not name.startswith("catalant_"):
                continue
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except Exception:
                pass
        if removed:
            print(f"  🧹 Evidence cleanup: removed {removed} old file(s)")
    except Exception as e:
        print(f"  ⚠️ Evidence cleanup failed: {redact_sensitive_text(e)}")


def get_browser_versions():
    """Cached Chromium / ChromeDriver version strings for diagnostics."""
    global _browser_versions_cache
    if _browser_versions_cache is not None:
        return _browser_versions_cache

    def _run(cmd):
        try:
            import subprocess
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=8)
            return out.decode("utf-8", errors="replace").strip() or "unknown"
        except FileNotFoundError:
            return "not found"
        except Exception as e:
            return f"failed: {redact_sensitive_text(e)}"

    versions = {
        "chromium": _run(["chromium", "--version"]),
        "chromedriver": _run(["chromedriver", "--version"]),
    }
    if versions["chromium"] in ("not found",) or str(versions["chromium"]).startswith("failed"):
        for cmd in (
            ["chromium-browser", "--version"],
            ["google-chrome", "--version"],
            ["google-chrome-stable", "--version"],
        ):
            out = _run(cmd)
            if out != "not found" and not str(out).startswith("failed"):
                versions["chromium"] = out
                break
    _browser_versions_cache = versions
    return versions


def build_error_signature(context, error):
    error_type = type(error).__name__ if error is not None and not isinstance(error, str) else "Error"
    message = str(error or "").strip()
    return f"{context}|{error_type}|{message}"[:1000]


def should_send_error_alert(signature, force=False):
    """Return (ok_to_send, remaining_seconds). Does not update last-sent timestamp."""
    if force:
        return True, 0
    cooldown_s = max(Config.ERROR_EMAIL_COOLDOWN_MINUTES, 0) * 60
    with _error_alert_lock:
        last = _error_alert_last_sent.get(signature)
        if last is None:
            return True, 0
        elapsed = time.time() - last
        if elapsed >= cooldown_s:
            return True, 0
        return False, int(cooldown_s - elapsed)


def _safe_driver_info(driver):
    info = {"current_url": "", "page_title": ""}
    if not driver:
        return info
    try:
        info["current_url"] = driver.current_url or ""
    except Exception:
        pass
    try:
        info["page_title"] = driver.title or ""
    except Exception:
        pass
    return info


def _safe_page_text(driver, limit=2000):
    if not driver:
        return ""
    try:
        text = driver.find_element(By.TAG_NAME, "body").text or ""
        return redact_sensitive_text(text[:limit])
    except Exception:
        return ""


def create_error_email_html(
    context,
    error,
    details="",
    traceback_text="",
    diagnostics=None,
):
    err_type = type(error).__name__ if error is not None and not isinstance(error, str) else "Error"
    err_msg = html.escape(redact_sensitive_text(str(error) if error is not None else ""))
    versions = get_browser_versions()
    hostname = socket.gethostname()
    now = datetime.now(PKT).strftime("%Y-%m-%d %H:%M:%S PKT")
    diag = diagnostics or {}

    rows = [
        ("Context", html.escape(str(context))),
        ("Exception", f"{html.escape(err_type)}: {err_msg}"),
        ("Timestamp", html.escape(now)),
        ("Hostname", html.escape(hostname)),
        ("Check #", html.escape(str(_monitor_check_count or "—"))),
        ("Headless", html.escape(str(Config.HEADLESS))),
        ("Chromium", html.escape(versions.get("chromium", "unknown"))),
        ("ChromeDriver", html.escape(versions.get("chromedriver", "unknown"))),
        ("Current URL", html.escape(redact_sensitive_text(diag.get("current_url", "")))),
        ("Page title", html.escape(redact_sensitive_text(diag.get("page_title", "")))),
        ("Account email", html.escape(Config.CATALANT_EMAIL or "(not set)")),
        ("Monitor state", html.escape(str(_monitor_state))),
    ]
    for label, key in (
        ("Configured password length", "configured_password_length"),
        ("Typed password length", "typed_password_length"),
        ("Password fingerprint (configured)", "configured_password_fingerprint"),
        ("Password fingerprint (typed)", "typed_password_fingerprint"),
        ("Password values match", "password_values_match"),
        ("Email field found", "email_field_found"),
        ("Password field found", "password_field_found"),
        ("Submit button found", "submit_button_found"),
        ("Submitted", "submitted"),
        ("CAPTCHA detected", "captcha_detected"),
        ("MFA detected", "mfa_detected"),
        ("Project ID", "project_id"),
        ("Project title", "project_title"),
        ("Project URL", "project_url"),
        ("Selector", "selector"),
        ("Database", "database"),
        ("Collection", "collection"),
        ("Operation", "operation"),
        ("Record count", "record_count"),
    ):
        if key in diag and diag[key] not in (None, ""):
            rows.append((label, html.escape(redact_sensitive_text(str(diag[key])))))

    if details:
        rows.append((
            "Details",
            f"<pre style='white-space:pre-wrap;margin:0;font-size:12px;'>"
            f"{html.escape(redact_sensitive_text(details))}</pre>",
        ))
    if traceback_text:
        rows.append((
            "Traceback",
            f"<pre style='white-space:pre-wrap;margin:0;font-size:11px;color:#7f1d1d;'>"
            f"{html.escape(redact_sensitive_text(traceback_text[:8000]))}</pre>",
        ))

    body_rows = "".join(
        f"<tr>"
        f"<td style='padding:10px 14px;width:200px;background:#fef2f2;border-bottom:1px solid #fecaca;"
        f"font-weight:bold;color:#7f1d1d;vertical-align:top;'>{label}</td>"
        f"<td style='padding:10px 14px;border-bottom:1px solid #fecaca;color:#111;vertical-align:top;'>{value}</td>"
        f"</tr>"
        for label, value in rows
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;">
  <div style="max-width:720px;margin:24px auto;background:#fff;border-radius:8px;overflow:hidden;
       box-shadow:0 4px 14px rgba(0,0,0,0.12);">
    <div style="background:linear-gradient(135deg,#b91c1c,#ef4444);padding:20px 24px;color:#fff;">
      <p style="margin:0;font-size:11px;letter-spacing:1px;text-transform:uppercase;opacity:0.85;">
        Catalant Project Monitor</p>
      <h2 style="margin:6px 0 0;font-size:22px;">Operational Error Alert</h2>
    </div>
    <div style="padding:18px 20px 24px;">
      <table style="width:100%;border-collapse:collapse;border:1px solid #fecaca;">{body_rows}</table>
      <p style="margin:16px 0 0;font-size:12px;color:#6b7280;">
        This alert was sent only to configured error recipients.
        Passwords, cookies and tokens are never included.
      </p>
    </div>
  </div>
</body></html>"""


def _attachment_allowed(path):
    name = os.path.basename(path).lower()
    if name in _BLOCKED_ATTACHMENT_NAMES or name.endswith(".env"):
        return False
    if "cookie" in name and name.endswith((".json", ".txt")):
        return False
    lower = path.replace("\\", "/").lower()
    if "/.env" in lower or lower.endswith(".env"):
        return False
    return True


def send_error_notification(
    context,
    error,
    details="",
    traceback_text="",
    force=False,
    attachments=None,
    diagnostics=None,
):
    """Send operational error email to ERROR_RECIPIENTS only. Never raises."""
    global _error_alert_in_progress

    try:
        if _error_alert_in_progress:
            print("  ⚠️ Error-email function failed recursively — alert suppressed")
            return False

        if not Config.ERROR_RECIPIENTS:
            print("  ⚠️ Error alert skipped — no ERROR_RECIPIENTS / aliases configured")
            return False

        if not Config.SENDER_EMAIL or not Config.SENDER_PASSWORD or not Config.SMTP_SERVER:
            print("  ⚠️ Error alert skipped — SMTP sender configuration incomplete")
            return False

        signature = build_error_signature(context, error)
        ok, remaining = should_send_error_alert(signature, force=force)
        if not ok:
            print(f"⏳ Error alert suppressed (cooldown {remaining}s remaining): {context}")
            return False

        _error_alert_in_progress = True
        try:
            safe_context = str(context or "UNKNOWN")[:120]
            html_body = create_error_email_html(
                safe_context,
                error,
                details=details,
                traceback_text=traceback_text,
                diagnostics=diagnostics,
            )
            err_type = type(error).__name__ if error is not None and not isinstance(error, str) else "Error"
            plain = (
                f"Catalant Project Monitor — Operational Error Alert\n\n"
                f"Context: {safe_context}\n"
                f"Exception: {err_type}: {redact_sensitive_text(error)}\n"
                f"Timestamp: {datetime.now(PKT).strftime('%Y-%m-%d %H:%M:%S PKT')}\n"
                f"Hostname: {socket.gethostname()}\n"
                f"Check #: {_monitor_check_count or '—'}\n"
                f"Monitor state: {_monitor_state}\n\n"
                f"{redact_sensitive_text(details)}\n\n"
                f"{redact_sensitive_text(traceback_text[:4000])}\n"
            )

            msg = MIMEMultipart("mixed")
            msg["Subject"] = f"🚨 Catalant Monitor Error: {safe_context}"
            msg["From"] = Config.SENDER_EMAIL
            msg["To"] = ", ".join(Config.ERROR_RECIPIENTS)

            alt = MIMEMultipart("alternative")
            alt.attach(MIMEText(plain, "plain", "utf-8"))
            alt.attach(MIMEText(html_body, "html", "utf-8"))
            msg.attach(alt)

            attached = []
            for path in attachments or []:
                if not path or not os.path.isfile(path):
                    continue
                if not _attachment_allowed(path):
                    print(f"  ⚠️ Skipped blocked attachment: {os.path.basename(path)}")
                    continue
                try:
                    size = os.path.getsize(path)
                    if size > _MAX_ATTACHMENT_BYTES:
                        print(f"  ⚠️ Skipped oversized attachment ({size} bytes): {os.path.basename(path)}")
                        continue
                    ctype, _ = mimetypes.guess_type(path)
                    if not ctype:
                        ctype = "application/octet-stream"
                    maintype, subtype = ctype.split("/", 1)
                    with open(path, "rb") as fh:
                        part = MIMEBase(maintype, subtype)
                        part.set_payload(fh.read())
                    encoders.encode_base64(part)
                    part.add_header(
                        "Content-Disposition",
                        f'attachment; filename="{os.path.basename(path)}"',
                    )
                    msg.attach(part)
                    attached.append(os.path.basename(path))
                except Exception as attach_err:
                    print(f"  ⚠️ Could not attach {os.path.basename(path)}: {redact_sensitive_text(attach_err)}")

            with smtplib.SMTP(Config.SMTP_SERVER, Config.SMTP_PORT, timeout=30) as server:
                server.starttls()
                server.login(Config.SENDER_EMAIL, Config.SENDER_PASSWORD)
                server.send_message(msg)

            with _error_alert_lock:
                _error_alert_last_sent[signature] = time.time()

            suffix = f"\n(attachments: {', '.join(attached)})" if attached else ""
            print(f"📧 Error alert sent to configured error recipient{suffix}")
            return True
        except Exception as smtp_err:
            print(f"⚠️ Failed to send Catalant error alert: {redact_sensitive_text(smtp_err)}")
            return False
        finally:
            _error_alert_in_progress = False
    except Exception as outer:
        print(f"⚠️ Failed to send Catalant error alert: {redact_sensitive_text(outer)}")
        _error_alert_in_progress = False
        return False


def validate_error_email_configuration():
    """Return (ok, missing_names) for operational alert SMTP config."""
    missing = []
    if not Config.SMTP_SERVER:
        missing.append("SMTP_SERVER")
    if not Config.SMTP_PORT:
        missing.append("SMTP_PORT")
    if not Config.SENDER_EMAIL:
        missing.append("SENDER_EMAIL")
    if not Config.SENDER_PASSWORD:
        missing.append("SENDER_PASSWORD")
    if not Config.ERROR_RECIPIENTS:
        missing.extend(["ERROR_RECIPIENTS", "ERROR_RECIPIENT", "ERROR_RECIPENT", "error_recipent"])
    return (len(missing) == 0), missing


def print_startup_banner():
    print("=" * 50)
    print("Catalant Project Monitor")
    print(f"Account: {Config.CATALANT_EMAIL or '(not set)'}")
    print(f"Interval: {Config.CHECK_INTERVAL}s")
    print(f"Repeat occurrence gap: > {Config.REPOST_MIN_DAYS} days (scraped_at)")
    print(f"Supabase URL set: {bool((Config.SUPABASE_URL or '').strip())}")
    print(f"Project recipients: {', '.join(Config.RECIPIENT_EMAILS) if Config.RECIPIENT_EMAILS else '(none)'}")
    if Config.ERROR_RECIPIENTS:
        print(f"Error recipients: {', '.join(Config.ERROR_RECIPIENTS)}")
        print(f"Error alerts: {', '.join(Config.ERROR_RECIPIENTS)}")
    else:
        print("Error recipients: (none)")
        print("Error alerts: disabled")
    print(f"Error cooldown: {Config.ERROR_EMAIL_COOLDOWN_MINUTES} minutes")
    print(f"Headless: {Config.HEADLESS}")
    print("=" * 50)
    ok, missing = validate_error_email_configuration()
    if not ok:
        print(f"⚠️ Operational alerts disabled — missing: {', '.join(missing)}")


def run_test_error_email():
    """Force-send a test operational alert; skip Selenium/Supabase."""
    print_startup_banner()
    ok, missing = validate_error_email_configuration()
    if not ok:
        print(f"❌ Cannot send test — missing: {', '.join(missing)}")
        return 2
    success = send_error_notification(
        context="TEST_ERROR_NOTIFICATION",
        error=RuntimeError("This is a test Catalant operational error notification."),
        details=(
            "Generated by --test-error-email. "
            "No scraper failure occurred."
        ),
        force=True,
        diagnostics={"monitor_state": "test"},
    )
    print("✅ Test error email sent" if success else "❌ Test error email failed")
    return 0 if success else 1


def save_login_failure_evidence(driver, diagnostics=None, prefix="catalant_login_failure"):
    """Save screenshot + HTML + safe JSON. Returns list of existing file paths."""
    ts = datetime.now(PKT).strftime("%Y%m%d_%H%M%S")
    base = os.path.join(_evidence_dir(), f"{prefix}_{ts}")
    paths = {"png": f"{base}.png", "html": f"{base}.html", "json": f"{base}.json"}
    out = []
    if driver:
        try:
            driver.save_screenshot(paths["png"])
            out.append(paths["png"])
        except Exception as e:
            print(f"  ⚠️ Screenshot failed: {redact_sensitive_text(e)}")
        try:
            with open(paths["html"], "w", encoding="utf-8", errors="replace") as fh:
                fh.write(driver.page_source or "")
            out.append(paths["html"])
        except Exception as e:
            print(f"  ⚠️ HTML capture failed: {redact_sensitive_text(e)}")
    try:
        safe = diagnostics or {}
        with open(paths["json"], "w", encoding="utf-8") as fh:
            json.dump(safe, fh, indent=2, default=str)
        out.append(paths["json"])
    except Exception as e:
        print(f"  ⚠️ JSON diagnostics failed: {redact_sensitive_text(e)}")
    return out


def classify_login_failure(driver, exc=None):
    """Best-effort classification of Catalant login failure."""
    text = ""
    url = ""
    title = ""
    if driver:
        try:
            url = (driver.current_url or "").lower()
        except Exception:
            pass
        try:
            title = (driver.title or "").lower()
        except Exception:
            pass
        text = (_safe_page_text(driver, 4000) or "").lower()
    blob = f"{text} {url} {title} {str(exc or '').lower()}"
    if isinstance(exc, TimeoutException) or "timeout" in blob:
        # Prefer more specific page signals when present
        pass
    if any(w in blob for w in ("captcha", "recaptcha", "hcaptcha", "verify you are human")):
        return "CAPTCHA_REQUIRED"
    if any(w in blob for w in ("two-factor", "2fa", "mfa", "verification code", "one-time")):
        return "MFA_REQUIRED"
    if any(w in blob for w in ("locked", "disabled", "suspended")):
        return "ACCOUNT_LOCKED"
    if any(w in blob for w in ("access denied", "forbidden", "not authorized")):
        return "ACCESS_DENIED"
    if any(w in blob for w in ("cors", "preflight")):
        return "CORS_PREFLIGHT_FAILED"
    if any(w in blob for w in ("invalid", "incorrect", "wrong password", "authentication failed", "login failed")):
        return "INVALID_CREDENTIALS_RESPONSE"
    if isinstance(exc, TimeoutException) or "timeout" in str(exc or "").lower():
        return "LOGIN_TIMEOUT"
    return "UNKNOWN"


# ============================
# SESSION MANAGEMENT
# ============================
def save_cookies(driver):
    """Save session cookies to Supabase AND local file as fallback."""
    cookies = driver.get_cookies()
    cookie_count = len(cookies) if cookies is not None else 0
    try:
        db.save_scraper_session(PLATFORM, cookies or [])
        print(f"  Saved {cookie_count} cookie(s) to Supabase scraper_sessions")
    except Exception as e:
        print(f"  ⚠️ Could not save cookies to Supabase: {redact_sensitive_text(e)}")
        send_error_notification(
            "COOKIE_SAVE:SUPABASE",
            e,
            details=f"cookie_count={cookie_count}\nsource=supabase table=scraper_sessions",
            traceback_text=traceback.format_exc(),
            diagnostics={
                **_safe_driver_info(driver),
                "operation": "cookie_save_supabase",
                "record_count": cookie_count,
                "platform": PLATFORM,
            },
        )
    try:
        with open(Config.COOKIES_FILE, 'w') as f:
            json.dump(cookies, f)
    except Exception as e:
        print(f"  ⚠️ Could not save cookies to local file: {redact_sensitive_text(e)}")
        send_error_notification(
            "COOKIE_SAVE:LOCAL_FILE",
            e,
            details=f"path={Config.COOKIES_FILE}\ncookie_count={cookie_count}",
            traceback_text=traceback.format_exc(),
            diagnostics={**_safe_driver_info(driver), "operation": "cookie_save_file", "record_count": cookie_count},
        )
    return True

def load_cookies(driver):
    """Load cookies from Supabase first, fall back to local file."""
    cookies = None
    source = None
    try:
        session = db.load_scraper_session(PLATFORM)
        if session:
            session_data = session.get("session_data") or {}
            if isinstance(session_data, str):
                try:
                    session_data = json.loads(session_data)
                except Exception:
                    session_data = {}
            loaded = session_data.get("cookies") if isinstance(session_data, dict) else None
            if loaded:
                cookies = loaded
                source = "supabase"
                print("  Loaded cookies from Supabase")
    except Exception as e:
        err_text = redact_sensitive_text(e)
        print(f"  ⚠️ Could not load cookies from Supabase: {err_text}")
        # Missing schema is a setup issue — fall back to local cookies without alert spam.
        if not db.is_missing_schema_error(e):
            send_error_notification(
                "COOKIE_LOAD:SUPABASE",
                e,
                details="source=supabase table=scraper_sessions",
                traceback_text=traceback.format_exc(),
                diagnostics={
                    **_safe_driver_info(driver),
                    "operation": "cookie_load_supabase",
                    "platform": PLATFORM,
                },
            )
        else:
            print("  ℹ️ Supabase schema not applied yet; using local cookie fallback")
    if not cookies:
        if not os.path.exists(Config.COOKIES_FILE):
            return False
        try:
            with open(Config.COOKIES_FILE, 'r') as f:
                cookies = json.load(f)
            source = "local_file"
            print("  Loaded cookies from local file")
        except Exception as e:
            print(f"  ⚠️ Could not load cookies from local file: {redact_sensitive_text(e)}")
            send_error_notification(
                "COOKIE_LOAD:LOCAL_FILE",
                e,
                details=f"path={Config.COOKIES_FILE}",
                traceback_text=traceback.format_exc(),
                diagnostics={**_safe_driver_info(driver), "operation": "cookie_load_file"},
            )
            return False
    if not cookies:
        return False
    try:
        driver.get("https://app.gocatalant.com")
        time.sleep(2)
        driver.delete_all_cookies()
        for cookie in cookies:
            if 'domain' in cookie and '.gocatalant.com' in cookie['domain']:
                try:
                    driver.add_cookie(cookie)
                except Exception:
                    pass
        return True
    except Exception as e:
        print(f"  ⚠️ Could not restore cookies in browser: {redact_sensitive_text(e)}")
        send_error_notification(
            "COOKIE_RESTORE:BROWSER",
            e,
            details=f"source={source}\ncookie_count={len(cookies)}",
            traceback_text=traceback.format_exc(),
            diagnostics={**_safe_driver_info(driver), "operation": "cookie_restore", "record_count": len(cookies)},
        )
        return False

def perform_login(driver):
    """Perform login to Catalant. Returns dict with success/classification/alert_sent."""
    global _last_login_alert
    _last_login_alert = {"alert_sent": False, "classification": None}
    email_found = password_found = submit_found = submitted = False
    typed_password = Config.CATALANT_PASSWORD or ""
    configured_password = Config.CATALANT_PASSWORD or ""
    try:
        driver.get("https://app.gocatalant.com/c/_/u/0/dashboard/")
        time.sleep(3)

        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.NAME, "email"))
        )
        email_found = True
        password_found = True
        driver.find_element(By.NAME, "email").send_keys(Config.CATALANT_EMAIL)
        driver.find_element(By.NAME, "password").send_keys(typed_password)
        submit = driver.find_element(By.XPATH, "//button[contains(text(), 'Login') or @type='submit']")
        submit_found = True
        submit.click()
        submitted = True

        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".need-card-inline-name"))
        )

        save_cookies(driver)
        _navigate_to_search(driver)
        print("Login successful -> Search Projects")
        return {"success": True, "classification": None, "alert_sent": False, "message": "ok"}
    except Exception as e:
        print(f"❌ Login failed: {redact_sensitive_text(e)}")
        classification = classify_login_failure(driver, e)
        context = f"LOGIN_FAILURE:{classification}"
        info = _safe_driver_info(driver)
        diagnostics = {
            **info,
            "result": classification,
            "headless": Config.HEADLESS,
            "email_field_found": email_found,
            "password_field_found": password_found,
            "submit_button_found": submit_found,
            "submitted": submitted,
            "configured_password_length": len(configured_password),
            "typed_password_length": len(typed_password),
            "configured_password_fingerprint": _password_fingerprint(configured_password),
            "typed_password_fingerprint": _password_fingerprint(typed_password),
            "password_values_match": configured_password == typed_password,
            "captcha_detected": classification == "CAPTCHA_REQUIRED",
            "mfa_detected": classification == "MFA_REQUIRED",
            "visible_error": _safe_page_text(driver, 500),
        }
        attachments = []
        try:
            attachments = save_login_failure_evidence(driver, diagnostics=diagnostics)
        except Exception as ev_err:
            print(f"  ⚠️ Evidence capture failed: {redact_sensitive_text(ev_err)}")
        details = (
            f"Login classification: {classification}\n"
            f"Current URL: {info.get('current_url')}\n"
            f"Page title: {info.get('page_title')}\n"
            f"Email field found: {email_found}\n"
            f"Password field found: {password_found}\n"
            f"Submit button found: {submit_found}\n"
            f"Submitted: {submitted}\n"
            f"Configured password length: {len(configured_password)}\n"
            f"Typed password length: {len(typed_password)}\n"
            f"Password fingerprints match: {configured_password == typed_password}\n"
            f"Evidence files: {', '.join(attachments) if attachments else 'none'}\n"
        )
        alert_sent = send_error_notification(
            context,
            e,
            details=details,
            traceback_text=traceback.format_exc(),
            attachments=attachments,
            diagnostics=diagnostics,
        )
        _last_login_alert = {"alert_sent": bool(alert_sent), "classification": classification}
        return {
            "success": False,
            "classification": classification,
            "alert_sent": bool(alert_sent),
            "message": redact_sensitive_text(e),
        }

# ============================
# PROJECT EXTRACTION
# ============================
def _first_platform_category(cat_text):
    path = extraction.category_path_from_text(cat_text)
    return path[0] if path else ""


def _extract_platform_category_from_text(text):
    cat, _path, _raw, rejected = extraction.normalize_category_candidate(
        text, allow_single=True
    )
    if rejected or not cat:
        return ""
    return cat


def extract_platform_category(root, body_text=""):
    """
    Priority:
      1. Verified structured labeled field
      2. Verified dedicated category selector
      3. Verified category breadcrumb
      4. Verified embedded structured page data
      5. Bounded label-specific text fallback
      6. Empty + MISSING
    Never invents Unclassified.
    """
    # 1) Structured labeled field
    try:
        for el in root.find_elements(
            By.XPATH,
            ".//*[contains(translate(normalize-space(.),"
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'category:') "
            "or contains(translate(normalize-space(.),"
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'practice area:') "
            "or contains(translate(normalize-space(.),"
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'functional area:')]",
        ):
            raw = (el.text or "").strip()
            if not raw or len(raw) > 180:
                continue
            m = re.search(
                r"(?:Category|Practice Area|Functional Area)\s*:\s*(.+)$",
                raw,
                re.IGNORECASE | re.DOTALL,
            )
            if not m:
                continue
            cat, path, cleaned, rejected = extraction.normalize_category_candidate(
                m.group(1), allow_single=True
            )
            if rejected == "invalid_placeholder":
                return extraction.category_result(
                    None, path, cleaned, "structured_label", None, "REJECTED_INVALID_CANDIDATE"
                )
            if cat:
                return extraction.category_result(
                    cat, path, cleaned, "structured_label", "HIGH", "FOUND_STRUCTURED"
                )
    except Exception:
        pass

    dedicated_selectors = (
        ".need-card-inline-pools .small.text-muted",
        ".need-card-inline-pools .text-muted",
        "[class*='need-card'] [class*='pool']",
        "[class*='category']",
        "[class*='breadcrumb']",
    )
    for sel in dedicated_selectors:
        try:
            for el in root.find_elements(By.CSS_SELECTOR, sel):
                raw = (el.text or "").strip()
                if not raw:
                    continue
                allow_single = "pool" in sel.lower() or "categor" in sel.lower()
                cat, path, cleaned, rejected = extraction.normalize_category_candidate(
                    raw, allow_single=allow_single
                )
                if rejected == "invalid_placeholder":
                    return extraction.category_result(
                        None, path, cleaned, f"selector:{sel}", None, "REJECTED_INVALID_CANDIDATE"
                    )
                if cat and (len(path) > 1 or allow_single):
                    return extraction.category_result(
                        cat, path, cleaned, f"selector:{sel}", "HIGH", "FOUND_DEDICATED_SELECTOR"
                    )
        except Exception:
            pass

    for sel in (
        ".text-gray.text-size-14.line-height-170",
        "[class*='line-height-170']",
    ):
        try:
            for el in root.find_elements(By.CSS_SELECTOR, sel):
                raw = (el.text or "").strip()
                if not raw:
                    continue
                cat, path, cleaned, rejected = extraction.normalize_category_candidate(
                    raw, allow_single=False
                )
                if rejected == "invalid_placeholder":
                    return extraction.category_result(
                        None, path, cleaned, f"breadcrumb:{sel}", None, "REJECTED_INVALID_CANDIDATE"
                    )
                if cat and len(path) >= 2:
                    return extraction.category_result(
                        cat, path, cleaned, f"breadcrumb:{sel}", "HIGH", "FOUND_BREADCRUMB"
                    )
        except Exception:
            pass

    try:
        for el in root.find_elements(
            By.XPATH,
            ".//*[contains(@class,'breadcrumb') or contains(@class,'pool')]",
        ):
            raw = (el.text or "").strip()
            if not raw or len(raw) > 160 or raw.count("\n") > 2:
                continue
            cat, path, cleaned, rejected = extraction.normalize_category_candidate(
                raw, allow_single=False
            )
            if rejected == "invalid_placeholder":
                return extraction.category_result(
                    None, path, cleaned, "breadcrumb_node", None, "REJECTED_INVALID_CANDIDATE"
                )
            if cat and len(path) >= 2:
                return extraction.category_result(
                    cat, path, cleaned, "breadcrumb_node", "MEDIUM", "FOUND_BREADCRUMB"
                )
    except Exception:
        pass

    try:
        scripts = root.find_elements(By.CSS_SELECTOR, "script[type='application/ld+json'], script")
        for script in scripts[:30]:
            content = script.get_attribute("innerHTML") or script.get_attribute("textContent") or ""
            result = extraction.extract_category_from_embedded_json(content)
            if result.get("platform_category_extraction_status") not in (None, "MISSING"):
                if result.get("platform_category") or result.get(
                    "platform_category_extraction_status"
                ) == "REJECTED_INVALID_CANDIDATE":
                    return result
    except Exception:
        pass

    text_result = extraction.extract_category_from_body_text(body_text or "")
    if text_result.get("platform_category") or text_result.get(
        "platform_category_extraction_status"
    ) == "REJECTED_INVALID_CANDIDATE":
        return text_result
    return extraction.category_result(None, [], None, None, None, "MISSING")


def _extract_platform_category(driver, body_text=""):
    result = extract_platform_category(driver, body_text=body_text)
    return result.get("platform_category") or ""


def extract_project_data(card):
    """Extract data from a project card - returns None if invalid"""
    try:
        title_elem = card.find_element(By.CSS_SELECTOR, ".need-card-inline-name .line-clamp-2")
        title = title_elem.text.strip()
        if not title:
            return None

        try:
            like_button = card.find_element(By.CSS_SELECTOR, "[data-ajax-post*='need/']")
            match = re.search(r'/need/([^/]+)/', like_button.get_attribute("data-ajax-post"))
            if not match:
                return None
            project_id = match.group(1)
        except Exception:
            return None

        category_info = extraction.category_result(None, [], None, None, None, "MISSING")
        try:
            category_info = extract_platform_category(card)
        except Exception:
            pass

        description = ""
        try:
            description = card.find_element(
                By.CSS_SELECTOR, ".need-card-inline-details .line-clamp-2"
            ).text.strip()
        except Exception:
            pass
        # Reject noise short descriptions
        if description and (
            description.lower() == title.lower()
            or description == (category_info.get("platform_category") or "")
            or description.lower().startswith("posted")
            or "$" in description and len(description) > 60
        ):
            description = ""

        location = ""
        try:
            loc_text = card.find_element(
                By.CSS_SELECTOR, ".text-gray-25.font-weight-semibold"
            ).text.strip()
            location = loc_text if loc_text else ""
        except Exception:
            pass

        time_posted = "Unknown"
        try:
            time_elems = card.find_elements(
                By.XPATH,
                ".//div[contains(@class, 'small') and contains(@class, 'text-gray-20') "
                "and contains(@class, 'mt-1')]//span[contains(text(), 'Posted')]",
            )
            if time_elems:
                time_posted = time_elems[0].text.replace("Posted", "").replace("ago", "").strip()
        except Exception:
            pass

        budget = ""
        try:
            budget = card.find_element(By.CSS_SELECTOR, ".need-card-inline-budget").text.strip()
        except Exception:
            pass
        # Do NOT scan arbitrary $ text — that falsely captured titles containing ($110/hr)
        budget_fields = {}
        if budget:
            ok, reason = extraction.validate_budget_candidate(budget, {"title": title})
            if ok:
                budget_fields = extraction.parse_budget(budget)
                budget_fields["budget_source"] = "card_dedicated_selector"
                budget_fields["budget_confidence"] = "HIGH"
            else:
                budget = ""
                budget_fields = {"extraction_warnings": [f"BUDGET_CANDIDATE_REJECTED_{reason.upper()}"]}
        if not budget:
            fallback = extraction.extract_title_rate_fallback(title)
            if fallback.get("budget_text"):
                budget_fields = fallback
                budget = fallback["budget_text"]

        duration = ""
        try:
            duration = card.find_element(By.CSS_SELECTOR, ".need-card-inline-duration").text.strip()
        except Exception:
            pass

        status = "Posted"
        try:
            card.find_element(By.CSS_SELECTOR, ".badge-success")
            status = "New Project"
        except Exception:
            pass

        url = f"https://app.gocatalant.com/c/_/u/0/need/{project_id}/"
        try:
            link = card.find_element(By.CSS_SELECTOR, "a[href*='need']")
            href = link.get_attribute("href") or ""
            if href and "need" in href:
                url = href
        except Exception:
            pass

        scraped_at = datetime.now(timezone.utc)
        source_posted_at = None
        source_posted_at_is_estimated = False
        if time_posted and time_posted != "Unknown":
            parsed, estimated = extraction.parse_relative_posted_time(time_posted, scraped_at)
            if parsed is not None:
                source_posted_at = parsed.isoformat()
                source_posted_at_is_estimated = estimated

        project = {
            "id": project_id,
            "project_id": project_id,
            "platform": PLATFORM,
            "title": title,
            "short_description": description or None,
            "description": None,  # full description comes from detail page
            "location": location or None,
            "budget": budget or None,
            "budget_text": budget_fields.get("budget_text") or (budget or None),
            "budget_min": budget_fields.get("budget_min"),
            "budget_max": budget_fields.get("budget_max"),
            "budget_currency": budget_fields.get("budget_currency"),
            "billing_type": budget_fields.get("billing_type"),
            "hourly_rate": budget_fields.get("hourly_rate"),
            "daily_rate": budget_fields.get("daily_rate"),
            "rate_currency": budget_fields.get("rate_currency"),
            "budget_source": budget_fields.get("budget_source"),
            "budget_confidence": budget_fields.get("budget_confidence"),
            "duration": duration or None,
            "duration_text": duration or None,
            "platform_category": category_info.get("platform_category"),
            "platform_category_path": category_info.get("platform_category_path") or [],
            "platform_category_raw": category_info.get("platform_category_raw"),
            "platform_category_source": category_info.get("platform_category_source"),
            "platform_category_confidence": category_info.get("platform_category_confidence"),
            "platform_category_extraction_status": category_info.get(
                "platform_category_extraction_status"
            ),
            "time_posted": time_posted,
            "time_posted_text": time_posted,
            "source_posted_at": source_posted_at,
            "source_posted_at_is_estimated": source_posted_at_is_estimated,
            "status": status,
            "url": url,
            "source_url": url,
            "detail_extraction_status": "NOT_ATTEMPTED",
            "detected_at": datetime.now(PKT).strftime("%Y-%m-%d %H:%M:%S"),
            "raw_data": {"card_time_posted": time_posted},
            "extraction_warnings": list(budget_fields.get("extraction_warnings") or []),
        }
        if not description:
            # Optional card summary — do not fail card for missing optional field
            pass
        project["card_extraction_status"] = extraction.calculate_card_extraction_status(project)
        project["missing_fields"] = extraction.compute_missing_fields(
            project, expected_fields=list(extraction.CARD_REQUIRED_FIELDS)
        )
        return project
    except Exception:
        return None


def scan_for_projects(driver):
    """Scan Search Projects page for project cards - returns only valid projects.
    Sets last_scan_issue when a classified scan failure occurs (one alert owner).
    """
    global last_scan_issue, _last_successful_scan_at
    last_scan_issue = None
    selector = ".need-card-inline-name"
    try:
        WebDriverWait(driver, 25).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
        )

        # Get all card blocks that contain a project title
        all_cards = driver.find_elements(By.CSS_SELECTOR, "div.card-block")
        project_cards = [c for c in all_cards if c.find_elements(By.CSS_SELECTOR, ".need-card-inline-name")]

        projects = []
        for card in project_cards:
            project = extract_project_data(card)
            if project and project.get('title') and project.get('id'):
                projects.append(project)

        print(f"✅ Extracted {len(projects)} valid projects")
        if projects:
            _last_successful_scan_at = datetime.now(PKT).strftime("%Y-%m-%d %H:%M:%S PKT")
        elif project_cards:
            # Cards present but extraction failed for all — selector/structure issue
            err = RuntimeError("Project cards present but no valid projects extracted")
            last_scan_issue = {
                "classification": "SCAN:SELECTOR_FAILURE",
                "error": err,
                "alert_sent": False,
                "selector": selector,
            }
            last_scan_issue["alert_sent"] = send_error_notification(
                "SCAN:SELECTOR_FAILURE",
                err,
                details=f"card_blocks={len(all_cards)} named_cards={len(project_cards)}",
                diagnostics={**_safe_driver_info(driver), "selector": selector},
            )
        return projects
    except TimeoutException as e:
        print("⏳ Timeout waiting for projects")
        last_scan_issue = {
            "classification": "SCAN:TIMEOUT",
            "error": e,
            "alert_sent": False,
            "selector": selector,
        }
        last_scan_issue["alert_sent"] = send_error_notification(
            "SCAN:TIMEOUT",
            e,
            details=f"selector={selector}\npage_text={_safe_page_text(driver, 1500)}",
            traceback_text=traceback.format_exc(),
            diagnostics={**_safe_driver_info(driver), "selector": selector},
        )
        return []
    except Exception as e:
        print(f"❌ Error scanning: {redact_sensitive_text(e)}")
        last_scan_issue = {
            "classification": "SCAN:PROJECT_LIST_FAILED",
            "error": e,
            "alert_sent": False,
            "selector": selector,
        }
        last_scan_issue["alert_sent"] = send_error_notification(
            "SCAN:PROJECT_LIST_FAILED",
            e,
            details=f"selector={selector}\npage_text={_safe_page_text(driver, 1500)}",
            traceback_text=traceback.format_exc(),
            diagnostics={**_safe_driver_info(driver), "selector": selector},
        )
        return []

# ============================
# PROJECT DATABASE (Supabase)
# ============================
def init_db():
    """Validate Supabase connectivity and required schema."""
    db.ensure_schema_ready()


def db_is_cold_start():
    """True when this platform has no projects rows yet."""
    return not db.platform_has_projects(PLATFORM)


def should_process_project(project_id, now=None):
    return db.should_process_project(PLATFORM, project_id, now=now)


def notify_db_error(context, error, *, project_id=None, operation=None):
    send_error_notification(
        context,
        error,
        details=(
            f"operation={operation or context}\n"
            f"platform={PLATFORM}\n"
            f"project_id={project_id or '-'}\n"
            f"table=projects"
        ),
        traceback_text=traceback.format_exc(),
        diagnostics={
            "database": "supabase",
            "platform": PLATFORM,
            "project_id": project_id,
            "operation": operation or context,
        },
    )


# ============================
# DETAIL PAGE FETCH
# ============================
def classify_detail_page(driver, url=""):
    """Return classification for wrong/empty detail pages."""
    try:
        current = (driver.current_url or "").lower()
    except Exception:
        current = ""
    try:
        title = (driver.title or "").lower()
    except Exception:
        title = ""
    text = (_safe_page_text(driver, 2500) or "").lower()
    blob = f"{current} {title} {text}"
    if "login" in current or "sign in" in text[:500]:
        return "DETAIL_PAGE_LOGIN_REDIRECT"
    if any(tok in blob for tok in ("access denied", "forbidden", "not authorized")):
        return "DETAIL_PAGE_ACCESS_DENIED"
    if any(tok in blob for tok in ("not found", "page doesn't exist", "doesn't exist")):
        return "DETAIL_PAGE_NOT_FOUND"
    if "/search/" in current and "/need/" not in current:
        return "DETAIL_PAGE_SEARCH_INSTEAD"
    if len(text.strip()) < 80:
        return "DETAIL_PAGE_EMPTY_SHELL"
    return None


def wait_for_project_detail_page(driver, timeout=25):
    """Wait for verified Catalant detail indicators (not only sleep)."""
    end = time.time() + timeout
    last_err = None

    def _detail_ready(drv):
        bad = classify_detail_page(drv)
        if bad in (
            "DETAIL_PAGE_LOGIN_REDIRECT",
            "DETAIL_PAGE_ACCESS_DENIED",
            "DETAIL_PAGE_NOT_FOUND",
            "DETAIL_PAGE_SEARCH_INSTEAD",
        ):
            raise TimeoutException(bad)
        body = ""
        try:
            body = (drv.find_element(By.TAG_NAME, "body").text or "").lower()
        except Exception:
            return False
        markers = (
            "contracting process",
            "project logistics",
            "project budget",
            "expert preferences",
            "start date:",
            "timeline:",
        )
        if any(m in body for m in markers):
            return True
        for sel in (
            ".need-description",
            ".description-body",
            "[class*='need-description']",
        ):
            try:
                if drv.find_elements(By.CSS_SELECTOR, sel):
                    return True
            except Exception:
                pass
        return False

    while time.time() < end:
        try:
            WebDriverWait(driver, 3).until(_detail_ready)
            time.sleep(1.2)  # brief stabilize
            return True
        except TimeoutException as e:
            last_err = e
            if str(e).startswith("DETAIL_PAGE_"):
                raise
            time.sleep(0.5)
        except Exception as e:
            last_err = e
            time.sleep(0.5)
    raise TimeoutException(str(last_err or "DETAIL_PAGE_TIMEOUT"))


def fetch_project_details(driver, url, *, title="", max_attempts=None):
    """Navigate to a Catalant project detail page and extract full information."""
    max_attempts = max_attempts or Config.DETAIL_MAX_ATTEMPTS
    details = {
        "detail_extraction_status": "NOT_ATTEMPTED",
        "detail_attempt_count": 0,
        "extraction_warnings": [],
        "extraction_metadata": {},
    }
    last_error = None
    for attempt in range(1, max_attempts + 1):
        details["detail_attempt_count"] = attempt
        details["detail_last_attempt_at"] = db._iso()
        try:
            driver.get(url)
            wait_for_project_detail_page(driver)
            bad = classify_detail_page(driver, url)
            if bad:
                details["detail_extraction_status"] = "FAILED"
                details["detail_failure_code"] = bad
                details["detail_last_error"] = bad
                details["extraction_warnings"] = list(set((details.get("extraction_warnings") or []) + [bad]))
                details.setdefault("extraction_metadata", {})["retries"] = attempt
                last_error = bad
                if attempt < max_attempts:
                    continue
                return details

            # Expand truncated Project Description ("More")
            try:
                for xpath in (
                    "//a[normalize-space()='More' or contains(normalize-space(),'More')]",
                    "//button[normalize-space()='More' or contains(normalize-space(),'More')]",
                    "//*[self::a or self::button][contains(@class,'more')]",
                ):
                    links = driver.find_elements(By.XPATH, xpath)
                    for el in links:
                        try:
                            txt = (el.text or "").strip().lower()
                            if txt in ("more", "show more", "read more") or txt.endswith("more"):
                                if el.is_displayed():
                                    driver.execute_script("arguments[0].click();", el)
                                    time.sleep(0.9)
                                    break
                        except Exception:
                            continue
                    else:
                        continue
                    break
            except Exception:
                pass

            # Prefer dedicated description container when present
            desc_from_dom = ""
            for sel in (
                ".need-description",
                ".description-body",
                "[class*='description-body']",
                ".need-detail-description",
                ".project-description",
                "[class*='need-description']",
                "[class*='project-description']",
            ):
                try:
                    el = driver.find_element(By.CSS_SELECTOR, sel)
                    t = extraction.normalize_visible_text(el.text)
                    if len(t) > 50 and extraction.validate_extracted_value(t, title=title):
                        desc_from_dom = t
                        break
                except Exception:
                    pass

            # Fallback: section under "Project Description" heading via XPath
            if not desc_from_dom:
                try:
                    heading = driver.find_element(
                        By.XPATH,
                        "//*[self::h1 or self::h2 or self::h3 or self::h4 or self::div]"
                        "[contains(normalize-space(),'Project Description')]",
                    )
                    parent = heading.find_element(By.XPATH, "./ancestor::*[self::section or self::div][1]")
                    t = extraction.normalize_visible_text(parent.text)
                    # Strip the heading itself
                    t = re.sub(r"(?i)^project description\s*", "", t).strip()
                    if len(t) > 50 and extraction.validate_extracted_value(t, title=title):
                        desc_from_dom = t
                except Exception:
                    pass

            body_text = driver.find_element(By.TAG_NAME, "body").text
            body_text = extraction.normalize_visible_text(body_text).replace("\n ", "\n")
            # Keep newlines for section parsing
            body_text = body_text.replace("\u00a0", " ")

            parsed = extraction.extract_detail_fields_from_body(body_text, title=title)
            if desc_from_dom:
                parsed["description"] = desc_from_dom
                meta = parsed.get("extraction_metadata") or {}
                extracted = list(meta.get("fields_extracted") or [])
                if "description" not in extracted:
                    extracted.append("description")
                visible = list(meta.get("fields_visible_on_page") or [])
                if "description" not in visible:
                    visible.append("description")
                # Remove from missing/not_exposed if present
                meta["fields_missing_but_visible"] = [
                    f for f in (meta.get("fields_missing_but_visible") or []) if f != "description"
                ]
                meta["fields_not_exposed"] = [
                    f for f in (meta.get("fields_not_exposed") or []) if f != "description"
                ]
                meta["fields_extracted"] = extracted
                meta["fields_visible_on_page"] = visible
                parsed["extraction_metadata"] = meta
                parsed["detail_extraction_status"] = extraction.calculate_detail_extraction_status(
                    attempted=True,
                    page_ok=True,
                    fields_visible=visible,
                    fields_extracted=extracted,
                    meaningful=True,
                )
                parsed["missing_fields"] = [
                    f for f in visible if f not in extracted
                ]

            # Category from DOM/body (preserve working extractor)
            if not parsed.get("platform_category"):
                cat_info = extract_platform_category(driver, body_text)
                parsed.update(cat_info)
                if parsed.get("platform_category"):
                    print(
                        f"      platform_category → {parsed['platform_category']} "
                        f"({parsed.get('platform_category_extraction_status')})"
                    )
                else:
                    print(
                        f"      ⚠️ platform_category "
                        f"{parsed.get('platform_category_extraction_status') or 'MISSING'}"
                    )

            details.update(parsed)
            details["detail_failure_code"] = None
            details["detail_last_error"] = None
            details["detail_completed_at"] = db._iso()
            details.setdefault("extraction_metadata", {})["detail_attempts"] = attempt
            return details
        except TimeoutException as e:
            last_error = e
            code = str(e)
            if not code.startswith("DETAIL_PAGE_"):
                code = "DETAIL_PAGE_TIMEOUT"
            print(f"  ⚠️ Detail fetch timeout (attempt {attempt}): {redact_sensitive_text(e)}")
            details["detail_extraction_status"] = "TIMEOUT"
            details["detail_failure_code"] = code
            details["detail_last_error"] = redact_sensitive_text(e)[:500]
            details["extraction_warnings"] = list(set((details.get("extraction_warnings") or []) + [code]))
            if attempt >= max_attempts:
                send_error_notification(
                    "PROJECT_DETAIL:TIMEOUT",
                    e,
                    details=f"url={url}\nattempt={attempt}",
                    traceback_text=traceback.format_exc(),
                    diagnostics={
                        **_safe_driver_info(driver),
                        "project_url": url,
                        "operation": "fetch_project_details",
                    },
                )
        except Exception as e:
            last_error = e
            print(f"  ⚠️ Detail fetch failed (attempt {attempt}): {redact_sensitive_text(e)}")
            details["detail_extraction_status"] = "FAILED"
            details["detail_failure_code"] = "DETAIL_FETCH_FAILED"
            details["detail_last_error"] = redact_sensitive_text(e)[:500]
            details["extraction_warnings"] = list(set(
                (details.get("extraction_warnings") or []) + ["DETAIL_FETCH_FAILED"]
            ))
            if attempt >= max_attempts:
                send_error_notification(
                    "PROJECT_DETAIL:FETCH_FAILED",
                    e,
                    details=f"url={url}\nattempt={attempt}",
                    traceback_text=traceback.format_exc(),
                    diagnostics={
                        **_safe_driver_info(driver),
                        "project_url": url,
                        "operation": "fetch_project_details",
                    },
                )
        if attempt < max_attempts:
            time.sleep(1)
    if last_error and not details.get("detail_extraction_status"):
        details["detail_extraction_status"] = "FAILED"
    return details


def enrich_project_with_details(driver, project, *, delay=None):
    """Fetch details for a card dict and merge. Returns merged project."""
    url = project.get("source_url") or project.get("url")
    title = project.get("title") or ""
    details = fetch_project_details(driver, url, title=title)
    merged = extraction.merge_project_data(project, details)
    if delay is None:
        delay = Config.DETAIL_FETCH_DELAY_SECONDS
    if delay and delay > 0:
        time.sleep(delay)
    return merged, details


# ============================
# EMAIL NOTIFICATIONS
# ============================
def _esc(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _section_header(icon, title, color):
    return (
        f'<tr><td colspan="2" style="padding:14px 16px 6px;background:{color};'
        f'color:#fff;font-size:12px;font-weight:bold;'
        f'text-transform:uppercase;letter-spacing:1px;">'
        f'{icon}&nbsp; {title}</td></tr>'
    )


def _row(label, value, alt=False, bold_value=False):
    if not value:
        return ""
    bg   = "background:#f8f9fa;" if alt else "background:#fff;"
    bold = "font-weight:bold;" if bold_value else ""
    return (
        f"<tr>"
        f"<td style='padding:9px 16px;color:#555;width:200px;{bg}border-bottom:1px solid #eee;'>"
        f"<strong>{_esc(label)}</strong></td>"
        f"<td style='padding:9px 16px;{bg}{bold}border-bottom:1px solid #eee;'>{_esc(str(value))}</td>"
        f"</tr>"
    )


def create_email_html(project):
    title         = project.get('title', 'Untitled Project')
    url           = project.get('url') or project.get('source_url') or 'https://app.gocatalant.com/c/_/u/0/dashboard/'
    time_posted   = project.get('time_posted') or project.get('time_posted_text') or ''
    status        = project.get('status', '')
    detected_at   = project.get('detected_at', '')
    project_id    = project.get('id') or project.get('project_id') or ''
    description   = project.get('description', '')
    start_date    = project.get('start_date') or project.get('start_date_text') or ''
    proj_length   = project.get('project_length', '') or project.get('duration', '') or project.get('duration_text', '')
    location_pref = project.get('location_pref', '') or project.get('location_preference', '') or project.get('location', '')
    contracting   = project.get('contracting', '') or project.get('contracting_process', '')
    budget        = project.get('budget', '') or project.get('budget_text', '') or project.get('detail_budget', '') or 'Not provided'
    support_level = project.get('level_of_support', '')
    industry      = project.get('industry', '')

    hdr_grad   = "linear-gradient(135deg,#1a6b3c,#27ae60)"
    sec_desc   = "#1a6b3c"
    sec_logist = "#166534"
    sec_budget = "#1d4ed8"
    sec_expert = "#7c3aed"
    btn_color  = "#27ae60"

    badge = ""
    if status == "New Project":
        badge = ("<span style='display:inline-block;background:#e74c3c;color:#fff;"
                 "padding:4px 12px;border-radius:3px;font-size:12px;font-weight:bold;"
                 "margin-bottom:12px;'>🆕 New Project</span>")

    desc_html = ""
    if description:
        paragraphs = _esc(description).replace("\n\n", "|||").replace("\n", " ")
        paras = [f"<p style='margin:0 0 10px;'>{p}</p>" for p in paragraphs.split("|||")]
        desc_html = "".join(paras)

    desc_section = ""
    if desc_html:
        desc_section = (
            _section_header('📋', 'Description', sec_desc) +
            f"<tr><td colspan='2' style='padding:14px 16px;background:#f9fafb;"
            f"font-size:14px;line-height:1.75;color:#333;border-bottom:2px solid #e5e7eb;'>"
            f"{desc_html}</td></tr>"
        )

    logistics_rows = (
        _row("Start Date",              start_date or "TBD",              alt=False) +
        _row("Expected Project Length", proj_length or "Not specified",   alt=True) +
        _row("Location Preference",     location_pref or "Not specified", alt=False) +
        _row("Contracting Process",     contracting or "Standard",        alt=True)
    )
    logistics_section = _section_header('📦', 'Project Logistics', sec_logist) + logistics_rows

    budget_section = (
        _section_header('💰', 'Budget', sec_budget) +
        _row("Project Budget", budget, bold_value=bool(project.get('budget')))
    )

    expert_rows = (
        _row("Level of Support",            support_level or "Not specified", alt=False) +
        _row("Desired Industry Background", industry      or "Not specified", alt=True)
    )
    expert_section = _section_header('👤', 'Expert Preferences', sec_expert) + expert_rows

    meta_rows = (
        _row("Posted",      f"{time_posted} ago" if time_posted and time_posted != "Unknown" else "—", alt=False) +
        _row("Detected at", detected_at, alt=True) +
        _row("Project ID",  project_id, alt=False)
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:Arial,Helvetica,sans-serif;color:#333;">
  <div style="max-width:700px;margin:30px auto;background:#fff;border-radius:10px;
       overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,0.12);">

    <div style="background:{hdr_grad};padding:24px 28px;">
      <p style="margin:0;color:rgba(255,255,255,0.75);font-size:11px;
          letter-spacing:1.5px;text-transform:uppercase;">Catalant Project Monitor</p>
      <h2 style="margin:6px 0 0;color:#fff;font-size:24px;font-weight:700;">🚀 New Project Alert</h2>
    </div>

    <div style="padding:22px 28px 4px;">
      <h3 style="margin:0 0 10px;color:#1a252f;font-size:20px;line-height:1.4;">{_esc(title)}</h3>
      {badge}
    </div>

    <div style="padding:0 28px 28px;">
      <table style="width:100%;border-collapse:collapse;font-size:14px;
             border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">
        {desc_section}
        {logistics_section}
        {budget_section}
        {expert_section}
        {_section_header('🕒', 'Detection Info', '#6b7280')}
        {meta_rows}
      </table>
      <div style="text-align:center;margin-top:28px;">
        <a href="{url}" style="display:inline-block;background:{btn_color};color:#fff;
                  padding:14px 36px;text-decoration:none;border-radius:6px;
                  font-weight:bold;font-size:15px;letter-spacing:0.3px;">
          View Full Project on Catalant →
        </a>
      </div>
    </div>

    <div style="background:#f8f9fa;padding:14px 28px;border-top:1px solid #eee;
         font-size:12px;color:#999;text-align:center;">
      Catalant Project Monitor &nbsp;|&nbsp; Automated alert &nbsp;|&nbsp; {detected_at}
    </div>
  </div>
</body></html>"""

def classify_email_failure(error):
    msg = str(error or "").lower()
    if "auth" in msg or "login" in msg or "credential" in msg:
        return "SMTP_AUTH_FAILED"
    if "timeout" in msg:
        return "SMTP_TIMEOUT"
    if "recipient" in msg or "mailbox" in msg:
        return "SMTP_RECIPIENT_REJECTED"
    return "EMAIL_SEND_FAILED"


def send_notification(project):
    """Send email notification for a new project.

    Returns dict: {ok, message_id, error, failure_code}.
    """
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"🔔 Catalant: {project.get('title', 'New Project')}"
        msg["From"] = Config.SENDER_EMAIL
        msg["To"] = ", ".join(Config.RECIPIENT_EMAILS)
        msg_id_header = msg["Message-ID"] if msg["Message-ID"] else None

        msg.attach(MIMEText(create_email_html(project), "html"))

        with smtplib.SMTP(Config.SMTP_SERVER, Config.SMTP_PORT) as server:
            server.starttls()
            server.login(Config.SENDER_EMAIL, Config.SENDER_PASSWORD)
            server.send_message(msg)
            try:
                msg_id_header = msg["Message-ID"]
            except Exception:
                pass

        print(f"📧 Email sent: {project.get('title', 'Unknown')[:50]}...")
        return {
            "ok": True,
            "message_id": msg_id_header,
            "error": None,
            "failure_code": None,
        }
    except Exception as e:
        print(f"❌ Email failed: {redact_sensitive_text(e)}")
        code = classify_email_failure(e)
        send_error_notification(
            "PROJECT_EMAIL:SEND_FAILED",
            e,
            details=(
                f"project_id={project.get('id') or project.get('project_id')}\n"
                f"title={project.get('title')}\n"
                f"url={project.get('url') or project.get('source_url')}\n"
                f"recipients={', '.join(Config.RECIPIENT_EMAILS)}\n"
                f"smtp={Config.SMTP_SERVER}:{Config.SMTP_PORT}"
            ),
            traceback_text=traceback.format_exc(),
            diagnostics={
                "project_id": project.get("id") or project.get("project_id"),
                "project_title": project.get("title"),
                "project_url": project.get("url") or project.get("source_url"),
                "operation": "project_email",
                "failure_code": code,
            },
        )
        return {
            "ok": False,
            "message_id": None,
            "error": redact_sensitive_text(e),
            "failure_code": code,
        }


def row_to_email_project(row):
    """Map a projects table row back to the email template shape."""
    return {
        "id": row.get("project_id"),
        "project_id": row.get("project_id"),
        "title": row.get("title"),
        "description": row.get("description") or row.get("short_description"),
        "url": row.get("source_url"),
        "source_url": row.get("source_url"),
        "time_posted": row.get("time_posted_text"),
        "status": row.get("status"),
        "detected_at": row.get("first_detected_at") or row.get("scraped_at"),
        "start_date": row.get("start_date_text"),
        "project_length": row.get("project_length") or row.get("duration_text"),
        "duration": row.get("duration_text"),
        "location_pref": row.get("location_preference") or row.get("location"),
        "location": row.get("location"),
        "contracting": row.get("contracting_process"),
        "budget": row.get("budget_text"),
        "level_of_support": row.get("level_of_support"),
        "industry": row.get("industry"),
        "platform_category": row.get("platform_category"),
    }


def process_project_email(row, project_payload=None, dry_run=False):
    """
    Insert-order safe email attempt against an existing projects row.
    Creates email_attempts, sends, updates the same projects row.
    """
    row_id = row["id"]
    attempt_number = int(row.get("email_attempt_count") or 0) + 1
    recipients = list(Config.RECIPIENT_EMAILS)
    payload = project_payload or row_to_email_project(row)

    if dry_run:
        print(f"  [dry-run] would email project row {row_id} attempt={attempt_number}")
        return {"ok": True, "dry_run": True}

    attempt = db.record_email_attempt(
        row_id,
        attempt_number,
        status="SENDING",
        recipients=recipients,
        provider="smtp",
    )
    db.update_project_email_status(
        row_id,
        email_status="SENDING",
        email_last_attempt_at=db._iso(),
    )
    result = send_notification(payload)
    now_iso = db._iso()
    if result["ok"]:
        db.record_email_attempt(
            row_id,
            attempt_number,
            status="SENT",
            attempt_id=attempt["id"],
            message_id=result.get("message_id"),
        )
        updated = db.update_project_email_status(
            row_id,
            email_sent=True,
            email_status="SENT",
            email_sent_at=now_iso,
            email_last_attempt_at=now_iso,
            email_attempt_count=attempt_number,
            email_not_sent_reason=None,
            email_failure_code=None,
            email_last_error=None,
            email_message_id=result.get("message_id"),
            email_next_retry_at=None,
        )
        return {"ok": True, "row": updated, "attempt": attempt}
    next_retry = db.compute_email_next_retry_at(
        attempt_number, base_minutes=Config.EMAIL_RETRY_BASE_MINUTES
    )
    status = (
        "FAILED"
        if attempt_number >= Config.EMAIL_MAX_RETRIES
        else "RETRY_PENDING"
    )
    db.record_email_attempt(
        row_id,
        attempt_number,
        status="FAILED",
        attempt_id=attempt["id"],
        failure_code=result.get("failure_code"),
        failure_reason=result.get("error"),
    )
    updated = db.update_project_email_status(
        row_id,
        email_sent=False,
        email_status=status,
        email_last_attempt_at=now_iso,
        email_attempt_count=attempt_number,
        email_next_retry_at=db._iso(next_retry) if status == "RETRY_PENDING" else None,
        email_not_sent_reason="EMAIL_SEND_FAILED",
        email_failure_code=result.get("failure_code"),
        email_last_error=result.get("error"),
    )
    return {"ok": False, "row": updated, "attempt": attempt, "result": result}


def retry_pending_emails(dry_run=False, limit=20):
    """Bounded retry worker for RETRY_PENDING project emails."""
    rows = db.get_retryable_email_projects(
        max_attempts=Config.EMAIL_MAX_RETRIES,
        limit=limit,
        platform=PLATFORM,
    )
    print(f"📬 Retryable emails: {len(rows)}")
    sent = failed = 0
    for row in rows:
        if row.get("email_status") in ("SUPPRESSED", "NOT_REQUIRED", "SENT"):
            continue
        if row.get("email_not_sent_reason") == "COLD_START_SEED":
            continue
        outcome = process_project_email(row, dry_run=dry_run)
        if outcome.get("ok"):
            sent += 1
        else:
            failed += 1
    return {"sent": sent, "failed": failed, "considered": len(rows)}


# ============================
# DRIVER INITIALIZATION
# ============================
def _find_binary(env_var, candidates):
    """Return the first executable from env var or candidate paths."""
    import shutil

    configured = os.getenv(env_var, "").strip()
    if configured:
        if os.path.exists(configured):
            return configured
        found = shutil.which(configured)
        if found:
            return found

    for path in candidates:
        if os.path.exists(path):
            return path
    return ""


def initialize_driver():
    """Initialize Chrome WebDriver"""
    from selenium.webdriver.chrome.service import Service

    options = Options()

    if Config.HEADLESS:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-setuid-sandbox")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")

    chrome_bin = _find_binary("CHROME_BIN", [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ])
    if chrome_bin:
        options.binary_location = chrome_bin

    chromedriver_path = _find_binary("CHROMEDRIVER_PATH", [
        "/usr/bin/chromedriver",
        "/usr/lib/chromium/chromedriver",
        "/usr/lib/chromium-browser/chromedriver",
    ])
    versions = get_browser_versions()
    try:
        if chromedriver_path:
            service = Service(chromedriver_path)
            driver = webdriver.Chrome(service=service, options=options)
        else:
            try:
                from webdriver_manager.chrome import ChromeDriverManager
                service = Service(ChromeDriverManager().install())
                driver = webdriver.Chrome(service=service, options=options)
            except Exception:
                # Selenium 4.6+ built-in manager as last resort
                driver = webdriver.Chrome(options=options)

        driver.execute_cdp_cmd('Network.setUserAgentOverride', {
            "userAgent": 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        return driver
    except Exception as e:
        ctx = "BROWSER_STARTUP:CHROMEDRIVER"
        msg = str(e).lower()
        if "chrome" in msg and "binary" in msg:
            ctx = "BROWSER_STARTUP:CHROMIUM"
        send_error_notification(
            ctx,
            e,
            details=(
                f"chrome_bin={chrome_bin or '(auto)'}\n"
                f"chromedriver_path={chromedriver_path or '(auto)'}\n"
                f"headless={Config.HEADLESS}\n"
                f"hostname={socket.gethostname()}\n"
                f"chromium={versions.get('chromium')}\n"
                f"chromedriver={versions.get('chromedriver')}"
            ),
            traceback_text=traceback.format_exc(),
            diagnostics={"operation": "initialize_driver"},
            force=True,
        )
        raise


DASHBOARD_URL = "https://app.gocatalant.com/c/_/u/0/dashboard/"
SEARCH_URL = "https://app.gocatalant.com/c/_/u/0/search/?form_name=SearchForm&enable_pagination=True&enable_facets=True&card_action_show_need=True&use_recommended=y&display_result_count=True"


def safe_quit_driver(driver):
    """Quit ChromeDriver without dumping urllib3/ConnectionRefused noise on Ctrl+C."""
    if driver is None:
        return
    try:
        driver.quit()
    except (KeyboardInterrupt, ConnectionRefusedError, ConnectionError, OSError):
        pass
    except Exception:
        pass
    try:
        process = getattr(getattr(driver, "service", None), "process", None)
        if process is not None:
            process.kill()
    except Exception:
        pass


_RECOVERABLE_BROWSER_CRASH_MARKERS = (
    "tab crashed",
    "session deleted because of page crash",
    "chrome not reachable",
    "disconnected: not connected to devtools",
)


def is_recoverable_browser_crash(exc: Exception) -> bool:
    """True for Chromium/Selenium renderer/session crashes that warrant a fresh driver."""
    parts = [str(exc or "")]
    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        parts.append(str(cause))
    ctx = getattr(exc, "__context__", None)
    if ctx is not None and ctx is not cause:
        parts.append(str(ctx))
    message = " ".join(parts).lower()
    return any(marker in message for marker in _RECOVERABLE_BROWSER_CRASH_MARKERS)


def _sleep_interruptible(seconds: float, *, chunk_seconds: float = 1.0) -> None:
    """Sleep in short chunks so KeyboardInterrupt can stop recovery promptly."""
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        step = min(chunk_seconds, remaining)
        time.sleep(step)
        remaining -= step


def recreate_authenticated_driver(old_driver=None):
    """
    Quit a broken driver (if any), create a fresh ChromeDriver, restore Catalant session.
    Returns (driver, session_result).
    """
    if old_driver is not None:
        try:
            safe_quit_driver(old_driver)
            print(
                "event=browser_driver_quit_attempted "
                "operation=browser_crash_recovery status=ok"
            )
        except Exception as quit_err:
            print(
                "event=browser_driver_quit_attempted "
                f"operation=browser_crash_recovery status=failed "
                f"error={redact_sensitive_text(quit_err)}"
            )
    print("event=browser_driver_create_start operation=browser_crash_recovery")
    driver = initialize_driver()
    print("event=browser_driver_create_complete operation=browser_crash_recovery")
    print("event=browser_auth_restore_start operation=browser_crash_recovery")
    session = setup_session(driver)
    if session.get("success"):
        print(
            "event=browser_auth_restore_complete "
            "operation=browser_crash_recovery status=ok "
            f"method={session.get('message') or 'session'}"
        )
    else:
        print(
            "event=browser_auth_restore_complete "
            "operation=browser_crash_recovery status=failed "
            f"classification={session.get('classification')}"
        )
    return driver, session


def run_monitoring_cycle_with_browser_recovery(
    driver,
    *,
    cold_start_pending,
    dry_run=False,
    debug_extraction=False,
    run_once=False,
    check_number=0,
):
    """
    Run one monitoring cycle; on recoverable Chromium crashes recreate the driver,
    restore auth, and retry the full cycle up to TAB_CRASH_MAX_RETRIES times.

    Returns (driver, cold_start_pending).
    Raises the last crash (or auth failure) after retries are exhausted.
    Does not send MONITORING_CYCLE:FAILED — caller handles final alerting.
    """
    global _monitor_state
    max_retries = max(0, int(Config.TAB_CRASH_MAX_RETRIES))
    delay_seconds = max(0, int(Config.TAB_CRASH_RETRY_DELAY_SECONDS))
    last_crash = None

    for attempt in range(max_retries + 1):
        try:
            cold_start_pending = run_monitoring_cycle(
                driver,
                cold_start_pending=cold_start_pending,
                dry_run=dry_run,
                debug_extraction=debug_extraction,
                run_once=run_once,
            )
            if attempt > 0:
                print(
                    "event=browser_crash_recovery_success "
                    "operation=monitoring_cycle "
                    f"check_number={check_number} "
                    f"recovery_attempt={attempt} "
                    f"max_recovery_attempts={max_retries}"
                )
            return driver, cold_start_pending
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            if not is_recoverable_browser_crash(exc):
                raise
            last_crash = exc
            sanitized = redact_sensitive_text(exc)
            print(
                "event=browser_crash_detected "
                "operation=monitoring_cycle "
                f"check_number={check_number} "
                f"recovery_attempt={attempt + 1} "
                f"max_recovery_attempts={max_retries} "
                f"retry_delay_seconds={delay_seconds} "
                f"error={sanitized}"
            )
            if attempt >= max_retries:
                print(
                    "event=browser_crash_recovery_exhausted "
                    "operation=monitoring_cycle "
                    f"check_number={check_number} "
                    f"recovery_attempts={max_retries} "
                    "recovery_exhausted=true "
                    f"last_browser_error={sanitized}"
                )
                raise

            print(
                "event=browser_crash_recovery_delay_start "
                "operation=monitoring_cycle "
                f"retry_delay_seconds={delay_seconds}"
            )
            previous_monitor_state = _monitor_state
            _monitor_state = "browser_recovery"
            try:
                _sleep_interruptible(delay_seconds)
            finally:
                _monitor_state = previous_monitor_state

            print(
                "event=browser_crash_recovery_recreate "
                "operation=monitoring_cycle "
                "driver_recreated=true"
            )
            driver, session = recreate_authenticated_driver(driver)
            if not session.get("success"):
                auth_err = RuntimeError(
                    session.get("message")
                    or session.get("classification")
                    or "Authentication restore failed after browser crash"
                )
                if not session.get("alert_sent"):
                    send_error_notification(
                        "BROWSER_RECOVERY:AUTH_FAILED",
                        auth_err,
                        details=(
                            f"check_number={check_number}\n"
                            f"recovery_attempt={attempt + 1}\n"
                            f"classification={session.get('classification')}"
                        ),
                        diagnostics={
                            **_safe_driver_info(driver),
                            "operation": "browser_crash_auth_restore",
                        },
                    )
                raise auth_err from last_crash

            print(
                "event=browser_crash_recovery_retry_start "
                "operation=monitoring_cycle "
                f"check_number={check_number} "
                f"recovery_attempt={attempt + 1}"
            )

    if last_crash is not None:
        raise last_crash
    return driver, cold_start_pending


def _navigate_to_search(driver):
    """Navigate to Search Projects page. Loads dashboard first so the AJAX session is active."""
    driver.get(DASHBOARD_URL)
    time.sleep(4)
    driver.get(SEARCH_URL)
    time.sleep(8)

def setup_session(driver):
    """Setup browser session with cookies or login.
    Returns dict: {success, classification, alert_sent, message}.
    Does not re-alert when perform_login already sent a classified login alert.
    """
    if load_cookies(driver):
        _navigate_to_search(driver)
        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".need-card-inline-name"))
            )
            print("Logged in via cookies -> Search Projects")
            return {"success": True, "classification": None, "alert_sent": False, "message": "cookies"}
        except Exception:
            pass
    result = perform_login(driver)
    if isinstance(result, dict):
        if not result.get("success") and not result.get("alert_sent"):
            send_error_notification(
                "SESSION_SETUP:FAILED",
                RuntimeError(result.get("message") or "Session setup failed"),
                details=f"classification={result.get('classification')}",
                diagnostics={**_safe_driver_info(driver), "operation": "setup_session"},
            )
            result["alert_sent"] = True
        return result
    return {"success": bool(result), "classification": None, "alert_sent": False, "message": ""}

# ============================
# MAIN MONITORING LOOP
# ============================
def process_eligible_project(driver, project, scraper_run_id, *, dry_run=False, debug_extraction=False):
    """Eligibility already confirmed. Fetch details, insert, then email."""
    merged, details = enrich_project_with_details(driver, project)
    if debug_extraction:
        print(
            f"     category={merged.get('platform_category')!r} "
            f"status={merged.get('platform_category_extraction_status')} "
            f"source={merged.get('platform_category_source')}"
        )
        print(
            f"     detail_status={merged.get('detail_extraction_status')} "
            f"missing={merged.get('missing_fields')}"
        )
        print(f"     description_len={len(merged.get('description') or '')}")

    if dry_run:
        print(f"  [dry-run] would insert+email: {merged.get('title', '')[:60]}")
        return {
            "inserted": False,
            "emailed": False,
            "skipped": False,
            "dry_run": True,
            "merged": merged,
            "details": details,
        }

    row = db.insert_project_occurrence(
        merged,
        scraper_run_id=scraper_run_id,
        email_status="PENDING",
        email_eligible=True,
    )
    outcome = process_project_email(row, project_payload=merged, dry_run=False)
    return {
        "inserted": True,
        "emailed": bool(outcome.get("ok")),
        "row": outcome.get("row") or row,
        "skipped": False,
        "merged": merged,
        "details": details,
    }


def seed_cold_start(driver, projects, scraper_run_id, *, dry_run=False, debug_extraction=False):
    """
    Cold-start: fetch detail pages, insert SUPPRESSED rows, no emails / no email_attempts.
    One failed detail does not abort the cycle.
    """
    inserted = 0
    details_attempted = details_completed = details_failed = details_partial = 0
    coverage_fields = (
        "description",
        "location_preference",
        "budget_text",
        "project_length",
        "start_date_text",
        "level_of_support",
        "industry",
        "contracting_process",
        "platform_category",
        "skills",
        "remote_or_onsite",
    )
    coverage = {f: 0 for f in coverage_fields}
    # Snapshot primitive card dicts before navigation
    card_snapshots = [dict(p) for p in projects if p.get("id") or p.get("project_id")]

    for project in card_snapshots:
        pid = project.get("project_id") or project.get("id")
        print(f"  → seeding {pid}: {str(project.get('title') or '')[:55]}...")
        details_attempted += 1
        try:
            merged, details = enrich_project_with_details(driver, project)
            status = (merged.get("detail_extraction_status") or "").upper()
            if status == "COMPLETE":
                details_completed += 1
            elif status == "PARTIAL":
                details_partial += 1
                details_completed += 1  # PARTIAL still counts as attempted success path
            elif status in ("FAILED", "TIMEOUT"):
                details_failed += 1
            else:
                details_completed += 1
            for f in coverage_fields:
                if not extraction.is_empty_value(merged.get(f)):
                    coverage[f] += 1
            if debug_extraction:
                print(
                    f"     detail={status} desc_len={len(merged.get('description') or '')} "
                    f"budget={merged.get('budget_text')!r} "
                    f"missing={merged.get('missing_fields')}"
                )
            if dry_run:
                print(f"  [dry-run] would seed enriched {pid}")
                inserted += 1
                continue
            db.insert_project_occurrence(
                merged,
                scraper_run_id=scraper_run_id,
                email_status="SUPPRESSED",
                email_eligible=False,
                email_sent=False,
                email_not_sent_reason="COLD_START_SEED",
            )
            inserted += 1
        except Exception as e:
            details_failed += 1
            print(f"  ⚠️ Seed detail failed for {pid}: {redact_sensitive_text(e)}")
            # Still save card-only if possible
            if dry_run:
                continue
            try:
                fallback = dict(project)
                fallback["detail_extraction_status"] = "FAILED"
                fallback["detail_failure_code"] = "COLD_START_DETAIL_FAILED"
                fallback["detail_last_error"] = redact_sensitive_text(e)[:500]
                fallback["extraction_warnings"] = list(
                    set((fallback.get("extraction_warnings") or []) + ["COLD_START_DETAIL_FAILED"])
                )
                fallback["card_extraction_status"] = extraction.calculate_card_extraction_status(fallback)
                fallback["missing_fields"] = extraction.compute_missing_fields(fallback)
                db.insert_project_occurrence(
                    fallback,
                    scraper_run_id=scraper_run_id,
                    email_status="SUPPRESSED",
                    email_eligible=False,
                    email_sent=False,
                    email_not_sent_reason="COLD_START_SEED",
                )
                inserted += 1
            except Exception as insert_err:
                notify_db_error(
                    "DATABASE:COLD_START_SEED_FAILED",
                    insert_err,
                    project_id=pid,
                    operation="cold_start_seed_item",
                )

    if debug_extraction:
        total = max(details_attempted, 1)
        print("\n--- debug extraction summary ---")
        print(f"cards found/parsed: {len(card_snapshots)}")
        print(f"detail pages attempted: {details_attempted}")
        print(f"detail pages complete: {details_completed - details_partial}")
        print(f"detail pages partial: {details_partial}")
        print(f"detail pages failed: {details_failed}")
        print("field-level extraction coverage:")
        for f in coverage_fields:
            print(f"  {f}: {coverage[f]}/{details_attempted}")

    return {
        "inserted": inserted,
        "details_attempted": details_attempted,
        "details_completed": details_completed,
        "details_failed": details_failed,
        "details_partial": details_partial,
        "field_coverage": coverage,
    }


def run_monitoring_cycle(
    driver,
    *,
    cold_start_pending,
    dry_run=False,
    debug_extraction=False,
    run_once=False,
):
    """One scan cycle. Returns updated cold_start_pending flag."""
    run = None
    counts = {
        "cards_found": 0,
        "cards_parsed": 0,
        "cards_failed": 0,
        "details_attempted": 0,
        "details_completed": 0,
        "details_failed": 0,
        "projects_inserted": 0,
        "projects_skipped": 0,
        "emails_sent": 0,
        "emails_failed": 0,
        "emails_suppressed": 0,
    }
    partial = False
    try:
        if not dry_run:
            try:
                db.mark_stale_running_runs(PLATFORM)
            except Exception as stale_err:
                print(f"  ⚠️ Stale-run cleanup skipped: {redact_sensitive_text(stale_err)}")
            run = db.create_scraper_run(platform=PLATFORM)
        else:
            run = {"id": None}
            print("  [dry-run] scraper_run not persisted")

        _navigate_to_search(driver)
        all_projects = scan_for_projects(driver)
        counts["cards_found"] = len(all_projects)
        counts["cards_parsed"] = len(all_projects)

        if not all_projects:
            print("⚠️ No projects found")
            if run and run.get("id") and not dry_run:
                db.complete_scraper_run(run["id"], status="PARTIAL", **counts)
            return cold_start_pending

        if cold_start_pending:
            print("⚙️  Seeding first successful scan (detail fetch ON, emails suppressed)...")
            try:
                seed_stats = seed_cold_start(
                    driver,
                    all_projects,
                    run.get("id") if run else None,
                    dry_run=dry_run,
                    debug_extraction=debug_extraction,
                )
                counts["projects_inserted"] = seed_stats["inserted"]
                counts["emails_suppressed"] = seed_stats["inserted"]
                counts["details_attempted"] = seed_stats["details_attempted"]
                counts["details_completed"] = seed_stats["details_completed"]
                counts["details_failed"] = seed_stats["details_failed"]
                if seed_stats["inserted"] <= 0 and not dry_run:
                    raise RuntimeError("Cold-start produced zero seeded rows")
                if run and run.get("id") and not dry_run:
                    status = "PARTIAL" if seed_stats["details_failed"] else "COMPLETED"
                    db.complete_scraper_run(run["id"], status=status, **counts)
                print(
                    f"✅ Seeded {seed_stats['inserted']} existing project(s) "
                    f"(details ok={seed_stats['details_completed']} "
                    f"failed={seed_stats['details_failed']}). "
                    "Only qualifying future posts will trigger emails.\n"
                )
                return False
            except Exception as seed_err:
                notify_db_error(
                    "DATABASE:COLD_START_SEED_FAILED",
                    seed_err,
                    operation="cold_start_seed",
                )
                if run and run.get("id") and not dry_run:
                    db.fail_scraper_run(
                        run["id"],
                        "COLD_START_SEED_FAILED",
                        redact_sensitive_text(seed_err),
                        **counts,
                    )
                print(
                    "⚠️  Initial seed was not confirmed; project emails remain "
                    "suppressed and seeding will retry next cycle.\n"
                )
                return True

        # Failed-email retries (same row, no new occurrence)
        try:
            retry_stats = retry_pending_emails(dry_run=dry_run)
            counts["emails_sent"] += retry_stats.get("sent", 0)
            counts["emails_failed"] += retry_stats.get("failed", 0)
        except Exception as retry_err:
            partial = True
            print(f"  ⚠️ Email retry worker failed: {redact_sensitive_text(retry_err)}")
            notify_db_error(
                "DATABASE:EMAIL_RETRY_FAILED",
                retry_err,
                operation="retry_pending_emails",
            )

        new_count = 0
        for project in all_projects:
            project_id = project.get("project_id") or project.get("id")
            if not project_id:
                counts["cards_failed"] += 1
                partial = True
                continue
            try:
                eligible, reason, latest = should_process_project(project_id)
            except Exception as lookup_err:
                partial = True
                notify_db_error(
                    "DATABASE:PROJECT_LOOKUP_FAILED",
                    lookup_err,
                    project_id=project_id,
                    operation="should_process_project",
                )
                raise

            if not eligible:
                counts["projects_skipped"] += 1
                age_note = reason
                print(f"  Skipping {project_id}: {age_note}")
                if debug_extraction and latest:
                    print(f"     latest scraped_at={latest.get('scraped_at')}")
                # Existing-row enrichment: fill missing details without new occurrence/email
                if latest and not dry_run and _row_needs_detail_enrichment(latest):
                    try:
                        print(f"     Enriching incomplete existing row {latest.get('id')}...")
                        counts["details_attempted"] += 1
                        cardish = dict(project)
                        cardish["source_url"] = (
                            project.get("source_url")
                            or project.get("url")
                            or latest.get("source_url")
                        )
                        merged, _details = enrich_project_with_details(driver, cardish)
                        payload = _detail_update_payload(latest, merged)
                        db.update_project_details(latest["id"], payload)
                        status = (payload.get("detail_extraction_status") or "").upper()
                        if status in ("FAILED", "TIMEOUT"):
                            counts["details_failed"] += 1
                        else:
                            counts["details_completed"] += 1
                        print(
                            f"     ✅ enriched detail_status={status} "
                            f"desc_len={len(payload.get('description') or '')}"
                        )
                    except Exception as enrich_err:
                        counts["details_failed"] += 1
                        partial = True
                        print(
                            f"     ⚠️ Existing-row enrichment failed: "
                            f"{redact_sensitive_text(enrich_err)}"
                        )
                continue

            print(f"  → {project.get('title', '')[:60]}... ({reason})")
            print("     Fetching full project details...")
            counts["details_attempted"] += 1
            try:
                result = process_eligible_project(
                    driver,
                    project,
                    run.get("id") if run else None,
                    dry_run=dry_run,
                    debug_extraction=debug_extraction,
                )
                if result.get("inserted"):
                    counts["projects_inserted"] += 1
                    new_count += 1
                    counts["details_completed"] += 1
                    if result.get("emailed"):
                        counts["emails_sent"] += 1
                    else:
                        counts["emails_failed"] += 1
                        partial = True
                elif result.get("dry_run"):
                    new_count += 1
                    counts["details_completed"] += 1
            except Exception as proc_err:
                partial = True
                counts["details_failed"] += 1
                print(f"  ⚠️ Project processing failed: {redact_sensitive_text(proc_err)}")
                notify_db_error(
                    "DATABASE:PROJECT_INSERT_FAILED",
                    proc_err,
                    project_id=project_id,
                    operation="process_eligible_project",
                )

        if new_count:
            print(f"🎯 Processed {new_count} NEW project(s)!")
        else:
            print("⏳ No new projects")

        print(
            f"📊 Stats: {len(all_projects)} visible, "
            f"inserted={counts['projects_inserted']} skipped={counts['projects_skipped']}"
        )

        if run and run.get("id") and not dry_run:
            status = "PARTIAL" if partial or counts["emails_failed"] or counts["details_failed"] else "COMPLETED"
            db.complete_scraper_run(run["id"], status=status, **counts)
        return cold_start_pending
    except Exception as cycle_err:
        if run and run.get("id") and not dry_run:
            try:
                db.fail_scraper_run(
                    run["id"],
                    "MONITORING_CYCLE_FAILED",
                    redact_sensitive_text(cycle_err),
                    **counts,
                )
            except Exception:
                pass
        raise


def main(run_once=False, dry_run=False, debug_extraction=False):
    """Main monitoring loop"""
    global _monitor_check_count, _monitor_state, last_scan_issue

    print_startup_banner()
    clean_old_evidence_files()
    _monitor_state = "starting"

    driver = None
    try:
        driver = initialize_driver()
    except Exception as e:
        print(f"❌ Browser startup failed: {redact_sensitive_text(e)}")
        _monitor_state = "fatal"
        return

    try:
        session = setup_session(driver)
        if not session.get("success"):
            print("❌ Failed to establish session")
            if not session.get("alert_sent") and not _last_login_alert.get("alert_sent"):
                send_error_notification(
                    "SESSION_SETUP:FAILED",
                    RuntimeError(session.get("message") or "Failed to establish session"),
                    details=f"classification={session.get('classification')}",
                    diagnostics={**_safe_driver_info(driver), "operation": "session_setup"},
                )
            if session.get("classification"):
                print(f"⏳ Login retry in {Config.LOGIN_RETRY_INTERVAL}s...")
                safe_quit_driver(driver)
                driver = None
                time.sleep(Config.LOGIN_RETRY_INTERVAL)
                try:
                    driver = initialize_driver()
                    session = setup_session(driver)
                except Exception as recovery_err:
                    send_error_notification(
                        "MONITORING_RECOVERY:FAILED",
                        recovery_err,
                        traceback_text=traceback.format_exc(),
                        diagnostics={"operation": "login_retry"},
                    )
                    return
                if not session.get("success"):
                    try:
                        if not dry_run:
                            run = db.create_scraper_run(platform=PLATFORM)
                            db.fail_scraper_run(
                                run["id"],
                                session.get("classification") or "AUTH_FAILED",
                                session.get("message") or "Auth failed",
                                status="AUTH_FAILED",
                            )
                    except Exception:
                        pass
                    return
            else:
                return

        _monitor_state = "running"
        try:
            cold_start_pending = db_is_cold_start()
            init_db()
        except Exception as db_err:
            send_error_notification(
                "DATABASE:INITIALIZATION_FAILED",
                db_err,
                traceback_text=traceback.format_exc(),
                diagnostics={"database": "supabase", "platform": PLATFORM},
                force=True,
            )
            raise

        print(f"📁 Supabase ready — platform={PLATFORM}\n")
        if cold_start_pending:
            print(
                "⚙️  First run detected — the first successful scan will be "
                "seeded without sending project emails.\n"
            )

        check_count = 0
        while True:
            try:
                check_count += 1
                _monitor_check_count = check_count
                if check_count % 20 == 0:
                    clean_old_evidence_files()
                print(f"\n{'='*30}")
                print(f"🔄 Check #{check_count} - {datetime.now(PKT).strftime('%H:%M:%S')} PKT")
                print(f"{'='*30}")

                driver, cold_start_pending = run_monitoring_cycle_with_browser_recovery(
                    driver,
                    cold_start_pending=cold_start_pending,
                    dry_run=dry_run,
                    debug_extraction=debug_extraction,
                    run_once=run_once,
                    check_number=check_count,
                )

                if run_once or dry_run:
                    print("✅ Run-once / dry-run complete")
                    break

                print(f"\n⏳ Next check in {Config.CHECK_INTERVAL} seconds...")
                time.sleep(Config.CHECK_INTERVAL)

            except KeyboardInterrupt:
                raise
            except Exception as loop_err:
                sanitized = redact_sensitive_text(loop_err)
                crash = is_recoverable_browser_crash(loop_err)
                print(
                    f"⚠️ Check failed: {sanitized} — "
                    f"retrying in {Config.CHECK_INTERVAL}s..."
                )
                alert_details = (
                    f"check_number={check_count}\n"
                    f"monitor_state={_monitor_state}\n"
                    f"last_successful_scan={_last_successful_scan_at}"
                )
                if crash:
                    alert_details += (
                        f"\nrecovery_attempts={Config.TAB_CRASH_MAX_RETRIES}\n"
                        "recovery_exhausted=true\n"
                        f"last_browser_error={sanitized}"
                    )
                send_error_notification(
                    "MONITORING_CYCLE:FAILED",
                    loop_err,
                    details=alert_details,
                    traceback_text=traceback.format_exc(),
                    diagnostics={
                        **_safe_driver_info(driver),
                        "operation": "monitoring_cycle",
                        "recovery_exhausted": crash,
                        "recovery_attempts": (
                            Config.TAB_CRASH_MAX_RETRIES if crash else 0
                        ),
                    },
                )
                if run_once or dry_run:
                    break
                safe_quit_driver(driver)
                driver = None
                time.sleep(Config.CHECK_INTERVAL)
                try:
                    driver = initialize_driver()
                    session = setup_session(driver)
                    if not session.get("success"):
                        print("Re-login failed -- will retry next cycle")
                        if not session.get("alert_sent"):
                            send_error_notification(
                                "BROWSER_RECOVERY:FAILED",
                                RuntimeError(session.get("message") or "Re-login failed"),
                                diagnostics={"operation": "browser_recovery"},
                            )
                except Exception as recovery_err:
                    print(f"⚠️ Recovery failed: {redact_sensitive_text(recovery_err)}")
                    send_error_notification(
                        "MONITORING_RECOVERY:FAILED",
                        recovery_err,
                        traceback_text=traceback.format_exc(),
                        diagnostics={"operation": "browser_recovery"},
                    )

    except KeyboardInterrupt:
        print("\n\n⏹️ Stopped by user")
        _monitor_state = "stopped"
    except Exception as e:
        print(f"\n❌ Error: {redact_sensitive_text(e)}")
        _monitor_state = "fatal"
        if not _last_login_alert.get("alert_sent"):
            send_error_notification(
                "MONITORING_LOOP:FATAL_ERROR",
                e,
                traceback_text=traceback.format_exc(),
                diagnostics={"operation": "main"},
                force=True,
            )
    finally:
        safe_quit_driver(driver)
        print("✅ Monitor stopped")


def run_test_supabase():
    """Validate Supabase credentials and schema with a reversible test."""
    print_startup_banner()
    try:
        summary = db.test_supabase_connection(cleanup=True)
    except Exception as e:
        print(f"❌ Supabase test failed: {redact_sensitive_text(e)}")
        return 1
    print("Supabase tables:")
    for name, ok in (summary.get("tables") or {}).items():
        print(f"  - {name}: {'OK' if ok else 'MISSING'}")
    print("✅ Supabase connectivity test passed (temporary rows cleaned up)")
    return 0


def _detail_update_payload(existing_row, merged):
    """Build enrichment payload for update_project_details."""
    allowed = db.DETAIL_UPDATE_ALLOWED
    payload = {}
    for key in allowed:
        if key not in merged:
            continue
        val = merged.get(key)
        # Preserve existing useful values when merge left empty
        if extraction.is_empty_value(val) and not extraction.is_empty_value(existing_row.get(key)):
            continue
        payload[key] = val
    # Always sync status/diagnostics from merged
    for key in (
        "detail_extraction_status",
        "detail_attempt_count",
        "detail_last_attempt_at",
        "detail_completed_at",
        "detail_failure_code",
        "detail_last_error",
        "missing_fields",
        "extraction_warnings",
        "extraction_metadata",
        "card_extraction_status",
    ):
        if key in merged:
            payload[key] = merged.get(key)
    return payload


def _row_needs_detail_enrichment(row: dict) -> bool:
    """True when an existing Catalant row still lacks core detail fields."""
    if not row:
        return False
    status = (row.get("detail_extraction_status") or "").upper()
    if status in ("NOT_ATTEMPTED", "PARTIAL", "FAILED", "TIMEOUT"):
        return True
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
            return True
    return False


def run_backfill_missing_details(
    *,
    dry_run=False,
    limit=20,
    project_id=None,
    retry_failed=False,
):
    """Enrich existing Catalant rows without new occurrences or emails."""
    print_startup_banner()
    init_db()
    rows = db.get_projects_needing_detail_enrichment(
        platform=PLATFORM,
        limit=limit,
        project_id=project_id,
        retry_failed=retry_failed,
    )
    print(f"Backfill candidates: {len(rows)} (limit={limit})")
    if not rows:
        return 0

    driver = None
    updated = failed = 0
    try:
        driver = initialize_driver()
        session = setup_session(driver)
        if not session.get("success"):
            print("❌ Cannot backfill — auth/session failed")
            return 2
        for row in rows:
            pid = row.get("project_id")
            url = row.get("source_url")
            print(f"\n→ Backfill {pid} ({row.get('id')})")
            cardish = {
                "id": pid,
                "project_id": pid,
                "platform": PLATFORM,
                "title": row.get("title"),
                "short_description": row.get("short_description"),
                "description": row.get("description"),
                "location": row.get("location"),
                "location_preference": row.get("location_preference"),
                "budget_text": row.get("budget_text"),
                "duration_text": row.get("duration_text"),
                "project_length": row.get("project_length"),
                "platform_category": row.get("platform_category"),
                "platform_category_path": row.get("platform_category_path") or [],
                "platform_category_raw": row.get("platform_category_raw"),
                "platform_category_source": row.get("platform_category_source"),
                "platform_category_confidence": row.get("platform_category_confidence"),
                "platform_category_extraction_status": row.get("platform_category_extraction_status"),
                "time_posted_text": row.get("time_posted_text"),
                "source_url": url,
                "url": url,
                "status": row.get("status"),
                "raw_data": row.get("raw_data") or {},
                "extraction_warnings": row.get("extraction_warnings") or [],
                "extraction_metadata": row.get("extraction_metadata") or {},
            }
            try:
                merged, details = enrich_project_with_details(driver, cardish)
                payload = _detail_update_payload(row, merged)
                # Preserve suppressed email fields by never including them
                if dry_run:
                    preview = {
                        k: payload.get(k)
                        for k in (
                            "description",
                            "location_preference",
                            "budget_text",
                            "project_length",
                            "start_date_text",
                            "level_of_support",
                            "industry",
                            "contracting_process",
                            "detail_extraction_status",
                            "missing_fields",
                        )
                        if k in payload
                    }
                    print(f"  [dry-run] would update {row['id']}: {preview}")
                    updated += 1
                    continue
                db.update_project_details(row["id"], payload)
                print(
                    f"  ✅ updated detail_status={payload.get('detail_extraction_status')} "
                    f"desc_len={len(payload.get('description') or '')}"
                )
                updated += 1
            except Exception as e:
                failed += 1
                print(f"  ❌ backfill failed: {redact_sensitive_text(e)}")
                notify_db_error(
                    "DATABASE:DETAIL_BACKFILL_FAILED",
                    e,
                    project_id=pid,
                    operation="backfill_missing_details",
                )
    finally:
        safe_quit_driver(driver)
    print(f"\nBackfill complete: updated={updated} failed={failed} dry_run={dry_run}")
    return 0 if failed == 0 else 1


def run_inspect_project(target=None, *, debug=True):
    """Inspect one project detail page and print field-level report."""
    print_startup_banner()
    driver = None
    try:
        driver = initialize_driver()
        session = setup_session(driver)
        if not session.get("success"):
            print("❌ Auth/session failed")
            return 2

        title = ""
        url = None
        project_id = None
        if target and str(target).startswith("http"):
            url = target
            m = re.search(r"/need/([^/]+)/?", url)
            project_id = m.group(1) if m else None
        elif target:
            project_id = str(target).strip()
            url = f"https://app.gocatalant.com/c/_/u/0/need/{project_id}/"
        else:
            # Fall back to first listing card
            _navigate_to_search(driver)
            cards = scan_for_projects(driver)
            if not cards:
                print("No projects found to inspect")
                return 1
            project_id = cards[0].get("project_id")
            url = cards[0].get("source_url")
            title = cards[0].get("title") or ""
            print(f"Inspecting first listing card: {project_id}")

        print(f"Project ID: {project_id}")
        print(f"URL: {url}")
        details = fetch_project_details(driver, url, title=title)
        print(f"Detail page loaded: {details.get('detail_extraction_status') not in ('TIMEOUT', 'FAILED', 'NOT_ATTEMPTED')}")
        print(f"detail_extraction_status: {details.get('detail_extraction_status')}")
        for field in (
            "description",
            "location_preference",
            "budget_text",
            "project_length",
            "start_date_text",
            "level_of_support",
            "industry",
            "contracting_process",
            "skills",
        ):
            val = details.get(field)
            meta = (details.get("extraction_metadata") or {})
            extracted = field in (meta.get("fields_extracted") or [])
            visible = field in (meta.get("fields_visible_on_page") or [])
            status = "FOUND" if extracted or not extraction.is_empty_value(val) else (
                "MISSING" if visible else "NOT_EXPOSED"
            )
            print(f"\n{field}:")
            print(f"  status: {status}")
            if field == "description" and val:
                print(f"  characters: {len(val)}")
            elif val:
                print(f"  value: {str(val)[:120]}")
            if visible and extraction.is_empty_value(val):
                print("  visible label found: yes")

        # Safe evidence
        try:
            ts = datetime.now(PKT).strftime("%Y%m%d_%H%M%S")
            base = os.path.join(_evidence_dir(), f"catalant_inspect_{project_id or 'unknown'}_{ts}")
            driver.save_screenshot(f"{base}.png")
            safe_json = {
                "project_id": project_id,
                "url": url,
                "detail_extraction_status": details.get("detail_extraction_status"),
                "fields": {
                    k: details.get(k)
                    for k in (
                        "description",
                        "location_preference",
                        "budget_text",
                        "project_length",
                        "start_date_text",
                        "level_of_support",
                        "industry",
                        "contracting_process",
                        "platform_category",
                    )
                },
                "missing_fields": details.get("missing_fields"),
                "extraction_warnings": details.get("extraction_warnings"),
                "extraction_metadata": details.get("extraction_metadata"),
            }
            with open(f"{base}.json", "w", encoding="utf-8") as fh:
                json.dump(safe_json, fh, indent=2, default=str)
            print(f"\nEvidence saved: {base}.png / {base}.json")
        except Exception as e:
            print(f"Evidence save skipped: {redact_sensitive_text(e)}")
        return 0
    finally:
        safe_quit_driver(driver)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Catalant Project Monitor")
    parser.add_argument("--test-error-email", action="store_true")
    parser.add_argument("--test-supabase", action="store_true")
    parser.add_argument(
        "--inspect-project",
        nargs="?",
        const="",
        default=None,
        help="Inspect a project URL or project id (implies authenticated session)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--run-once", action="store_true")
    parser.add_argument("--debug-extraction", action="store_true")
    parser.add_argument("--retry-pending-emails", action="store_true")
    parser.add_argument("--backfill-missing-details", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--retry-failed", action="store_true")
    return parser.parse_args(argv)


def cli_main(argv=None):
    args = parse_args(argv)
    if args.test_error_email:
        return run_test_error_email()
    if args.test_supabase:
        return run_test_supabase()
    if args.backfill_missing_details:
        return run_backfill_missing_details(
            dry_run=args.dry_run,
            limit=args.limit,
            project_id=args.project_id,
            retry_failed=args.retry_failed,
        )
    if args.inspect_project is not None:
        target = args.inspect_project or args.project_id
        return run_inspect_project(target)
    if args.retry_pending_emails:
        print_startup_banner()
        try:
            init_db()
            stats = retry_pending_emails(dry_run=args.dry_run)
            print(f"Retry complete: {stats}")
            return 0
        except Exception as e:
            print(f"❌ Retry failed: {redact_sensitive_text(e)}")
            return 1
    dry_run = args.dry_run
    run_once = args.run_once or dry_run
    debug_extraction = args.debug_extraction
    main(run_once=run_once, dry_run=dry_run, debug_extraction=debug_extraction)
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
