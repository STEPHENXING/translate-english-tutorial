"""
Small cross-platform lock helper for pipeline steps.

Each heavy step writes an exclusive lock file before touching shared outputs.
If a previous process crashed, stale locks are removed when their PID is no
longer alive.
"""
from contextlib import contextmanager
import json
import os
import subprocess
import time


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False

    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
        )
        return str(pid) in result.stdout

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_lock(lock_path: str) -> dict:
    try:
        with open(lock_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


@contextmanager
def file_lock(lock_path: str, label: str):
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "label": label,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "lock_path": os.path.abspath(lock_path),
    }

    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            break
        except FileExistsError:
            existing = _read_lock(lock_path)
            existing_pid = int(existing.get("pid") or 0)
            if existing_pid and not _pid_alive(existing_pid):
                os.remove(lock_path)
                continue
            raise RuntimeError(
                f"{label} is already running or a lock file exists.\n"
                f"Lock: {lock_path}\n"
                f"Existing: {existing or 'unreadable lock file'}\n"
                "If no pipeline process is running, delete the lock file and retry."
            )

    try:
        yield
    finally:
        current = _read_lock(lock_path)
        if int(current.get("pid") or 0) == os.getpid():
            try:
                os.remove(lock_path)
            except FileNotFoundError:
                pass
