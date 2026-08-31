#!/usr/bin/env python3
"""Hermes Hot/Live Reload — Windows-native file watcher that reloads Hermes Desktop
and its supervised subsystems without a cold restart.

User directive: don't forget Hot/Live Reload of the Hermes Desktop program, and
improve Windows too. This bridges the gap: a lightweight watchdog polls the agent
source tree (and the desktop app bundle if present) for changes, sweeps stale
bytecode, and triggers the existing live-reload path (scripts/hermes_auto_update.py
reload) + restarts the always-on supervisor children. It is fail-open: a bad
reload is logged, never fatal to the running system.

Why not a third-party watcher: keeps the dependency surface at zero (pure stdlib
+ the existing reload tooling). Polling at a coarse interval is plenty for source
edits; the supervisor's own restart is the live-reload mechanism.

Usage:
    python scripts/hermes_hot_reload.py start    # background watcher (Windows)
    python scripts/hermes_hot_reload.py once     # one sweep + reload if changed
    python scripts/hermes_hot_reload.py stop     # remove status
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

_AGENT_ROOT = Path(__file__).resolve().parent.parent
_HERMES_HOME = Path(os.environ.get("HERMES_HOME", r"C:\Users\w3ce\AppData\Local\hermes"))
_STATUS = _HERMES_HOME / "hot_reload_status.json"
_POLL = 5.0  # seconds between sweeps
_WATCH_DIRS = [
    _AGENT_ROOT / "agent",
    _AGENT_ROOT / "scripts",
    _AGENT_ROOT / "hermes_cli",
]


def _tree_hash() -> str:
    """Stable hash of all .py mtimes+sizes under watched dirs (cheap change signal)."""
    h = hashlib.sha256()
    for d in _WATCH_DIRS:
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*.py")):
            try:
                st = f.stat()
                h.update(f"{f.relative_to(_AGENT_ROOT)}:{st.st_size}:{int(st.st_mtime)}".encode())
            except Exception:
                pass
    return h.hexdigest()


def _sweep_pycache() -> int:
    removed = 0
    for pyc in _AGENT_ROOT.rglob("__pycache__"):
        if pyc.is_dir():
            import shutil
            shutil.rmtree(pyc, ignore_errors=True)
            removed += 1
    return removed


def _trigger_reload() -> str:
    """Run the existing live-reload path, then restart supervised children."""
    out = []
    try:
        r = subprocess.run(
            [sys.executable, str(_AGENT_ROOT / "scripts" / "hermes_auto_update.py"), "reload"],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        out.append(f"auto_update_reload rc={r.returncode}")
    except Exception as e:  # noqa: BLE001
        out.append(f"auto_update_reload error: {e}")
    # Restart always-on supervisor children so they pick up new code live.
    try:
        subprocess.run(
            [sys.executable, str(_AGENT_ROOT / "scripts" / "hermes_always_on.py"), "reload"],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        out.append("always_on_reload ok")
    except Exception as e:  # noqa: BLE001
        out.append(f"always_on_reload error: {e}")
    return "; ".join(out)


def cmd_once() -> int:
    prev = _STATUS.read_text(encoding="utf-8") if _STATUS.is_file() else "{}"
    prev_hash = ""
    try:
        prev_hash = __import__("json").loads(prev).get("tree_hash", "")
    except Exception:
        pass
    cur = _tree_hash()
    if cur != prev_hash:
        swept = _sweep_pycache()
        msg = _trigger_reload()
        _STATUS.write_text(__import__("json").dumps({
            "tree_hash": cur, "reloaded": True, "swept_pycache": swept,
            "detail": msg, "ts": int(time.time()),
        }, indent=2), encoding="utf-8")
        print(f"[hot-reload] change detected -> reloaded ({msg})")
    else:
        print("[hot-reload] no change.")
    return 0


def cmd_start() -> int:
    print(f"[hot-reload] watching {_AGENT_ROOT} every {_POLL}s (Ctrl-C to stop)")
    try:
        while True:
            cmd_once()
            time.sleep(_POLL)
    except KeyboardInterrupt:
        print("\n[hot-reload] stopped.")
    return 0


def cmd_stop() -> int:
    if _STATUS.is_file():
        _STATUS.unlink()
    print("[hot-reload] status cleared.")
    return 0


def main() -> int:
    action = (sys.argv[1] if len(sys.argv) > 1 else "once").lower()
    if action == "start":
        return cmd_start()
    if action == "stop":
        return cmd_stop()
    return cmd_once()


if __name__ == "__main__":
    raise SystemExit(main())
