"""Chromium process hygiene: reap leftovers, worker lock, recycle helpers."""

from __future__ import annotations

import atexit
import os
import signal
import socket
import subprocess
import sys
import time
from typing import Iterable, Optional

# Distinct exit code so start.sh can log intentional recycle vs crash.
PROCESS_RECYCLE_EXIT_CODE = int(os.getenv("PROCESS_RECYCLE_EXIT_CODE", "75"))

_WORKER_LOCK_FH = None
_PROCESS_STARTED_AT = time.time()


def process_started_at() -> float:
    return _PROCESS_STARTED_AT


def process_uptime_seconds() -> float:
    return max(0.0, time.time() - _PROCESS_STARTED_AT)


def process_recycle_hours() -> float:
    try:
        return max(0.0, float(os.getenv("PROCESS_RECYCLE_HOURS", "3")))
    except (TypeError, ValueError):
        return 3.0


def should_recycle_process() -> bool:
    hours = process_recycle_hours()
    if hours <= 0:
        return False
    return process_uptime_seconds() >= hours * 3600.0


def is_browser_process_exhaustion(exc: BaseException) -> bool:
    """True for OS-level fork/spawn exhaustion (Railway Cannot fork)."""
    parts = [str(exc or "")]
    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        parts.append(str(cause))
    ctx = getattr(exc, "__context__", None)
    if ctx is not None and ctx is not cause:
        parts.append(str(ctx))
    message = " ".join(parts).lower()
    markers = (
        "cannot fork",
        "posix_spawn",
        "resource temporarily unavailable",
        "errno 11",
        "errno 35",
        "too many processes",
        "unable to create new process",
    )
    return any(m in message for m in markers)


def _worker_lock_path() -> str:
    return os.getenv(
        "WORKER_LOCK_PATH",
        os.path.join(os.getenv("TMPDIR") or os.getenv("TEMP") or "/tmp", "catalant-monitor.worker.lock"),
    )


def acquire_worker_lock() -> bool:
    """
    Best-effort exclusive lock so only one monitor worker runs per container.
    Returns True if lock held (or unsupported platform).
    """
    global _WORKER_LOCK_FH
    if _WORKER_LOCK_FH is not None:
        return True
    path = _worker_lock_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fh = open(path, "a+", encoding="utf-8")
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                fh.close()
                print(f"event=worker_lock_busy path={path}")
                return False
        else:
            import fcntl

            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                print(f"event=worker_lock_busy path={path}")
                return False
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} host={socket.gethostname()} started={_PROCESS_STARTED_AT}\n")
        fh.flush()
        _WORKER_LOCK_FH = fh
        atexit.register(release_worker_lock)
        print(f"event=worker_lock_acquired path={path}")
        return True
    except Exception as exc:
        print(f"event=worker_lock_failed error={exc}")
        return True  # do not block monitor if lock fails


def release_worker_lock() -> None:
    global _WORKER_LOCK_FH
    fh = _WORKER_LOCK_FH
    _WORKER_LOCK_FH = None
    if fh is None:
        return
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()
        print("event=worker_lock_released")
    except Exception as exc:
        print(f"event=worker_lock_release_failed error={exc}")


def _pgrep_patterns() -> tuple[str, ...]:
    return (
        "chromium",
        "chrome",
        "chromedriver",
        "crashpad",
        "chrome_crashpad",
    )


def _list_matching_pids(patterns: Iterable[str]) -> list[int]:
    """Return PIDs whose cmdline matches any pattern (Unix). Excludes current process."""
    if os.name == "nt":
        return []
    pids: list[int] = []
    my_pid = os.getpid()
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == my_pid:
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    cmdline = fh.read().decode("utf-8", errors="ignore").lower().replace("\x00", " ")
            except OSError:
                continue
            if any(pat in cmdline for pat in patterns):
                # Avoid killing unrelated system chrome if user desktop — Railway containers
                # only run the monitor's Chromium. Still skip our own python interpreter.
                if "python" in cmdline and "monitor" in cmdline:
                    continue
                pids.append(pid)
    except Exception:
        pass
    return pids


def reap_leftover_browser_processes(*, reason: str = "cleanup") -> int:
    """
    Best-effort kill of leftover Chromium / ChromeDriver / crashpad processes.
    Safe no-op on Windows / when /proc is unavailable.
    Returns number of PIDs signaled.
    """
    patterns = _pgrep_patterns()
    pids = _list_matching_pids(patterns)
    if not pids:
        print(f"event=browser_reap reason={reason} killed=0")
        return 0

    killed = 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
        except Exception:
            pass

    time.sleep(0.5)
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except Exception:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass

    print(f"event=browser_reap reason={reason} killed={killed} pids={pids}")
    return killed


def recycle_and_exit(
    *,
    driver=None,
    quit_driver_fn=None,
    message: str = "process recycle",
) -> None:
    """Quit browser, reap leftovers, release lock, exit for start.sh relaunch."""
    hours = process_recycle_hours()
    uptime = process_uptime_seconds()
    print(
        "event=process_recycle "
        f"uptime_seconds={int(uptime)} "
        f"recycle_hours={hours} "
        f"exit_code={PROCESS_RECYCLE_EXIT_CODE} "
        f"message={message}"
    )
    if driver is not None and quit_driver_fn is not None:
        try:
            quit_driver_fn(driver)
        except Exception:
            pass
    try:
        reap_leftover_browser_processes(reason="process_recycle")
    except Exception as exc:
        print(f"event=browser_reap_failed error={exc}")
    release_worker_lock()
    sys.exit(PROCESS_RECYCLE_EXIT_CODE)
