#!/usr/bin/env python3
"""Tray core logic (pure, no GUI dependency) for the Hermes Tray System.

Separated from the tkinter UI so the self-heal watchdog can be tested and run
headless. The tray UI (scripts/hermes_tray.py) imports these.

Self-heal rule (user directive): if Hermes drops/stalled, pop itself back up
automatically. Liveness is inferred from recent heartbeat status files on the
Local Office (F:/HermesOffice/*_status.json), which every supervised subsystem
writes — no fragile tasklist/wmic calls.

Verified by tests/agent/test_tray_core.py.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import os as _os
from pathlib import Path as _P

# Detect the Local Office root without a blocking exists() on a drive letter under
# MSYS-style Python (which can hang on r"F:/"). Use os.path.exists (native) instead.
if _os.path.exists(r"F:\\") or _os.path.exists("F:"):
    _OFFICE = _P(r"F:/HermesOffice")
else:
    _OFFICE = _P(_os.environ.get("HERMES_HOME", r"C:\Users\w3ce\AppData\Local\hermes")) / "HermesOffice"

# Status files that prove a supervised Hermes process is heartbeating.
_HEARTBEATS = (
    "equator_status.json",
    "survival_status.json",
    "learning_node_status.json",
    "guardrail_status.json",
    "supervisor_status.json",
)
_STALL_SECONDS = 60


def hermes_alive(office: Path | None = None, stall: int = _STALL_SECONDS) -> bool:
    """True if a supervised Hermes process wrote a heartbeat within `stall` secs."""
    office = Path(office) if office else _OFFICE
    now = time.time()
    for name in _HEARTBEATS:
        p = office / name
        try:
            if p.is_file() and (now - p.stat().st_mtime) < stall:
                return True
        except Exception:
            continue
    return False


def _revive_conductor(office: Path) -> bool:
    """Bring the Option-Skills conductor workers back up if they are not running.

    This is NOT a forced restart of Hermes itself (that must still go through the
    Update menu per the user's rule). It only re-launches the supervised conductor
    workers so the autonomous learning/option-skills system self-recovers ("if it
    drops, pop itself back up"). Fails silently if it cannot spawn.
    """
    try:
        repo = Path(__file__).resolve().parent.parent
        script = repo / "scripts" / "hermes_autostart.py"
        if not script.is_file():
            return False
        # Launch detached so it survives this watchdog's lifetime.
        subprocess.Popen(
            [sys.executable, str(script), "run"],
            cwd=str(repo),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
        )
        return True
    except Exception:
        return False


def self_heal(office: Path | None = None) -> dict:
    """One watchdog pass.

    If Hermes stalled: do NOT force-restart Hermes itself (a reboot/update must go
    through the Update menu). Instead, per the user's later directive ("if it drops,
    pop itself back up"), re-launch the supervised conductor workers so the
    autonomous systems recover on their own, and record the event.

    Returns what it observed."""
    if hermes_alive(office):
        return {"action": "ok", "alive": True}
    # Stalled: revive the supervised workers (not a forced Hermes restart), and
    # record that we auto-recovered so the human stays informed.
    revived = _revive_conductor(Path(office) if office else _OFFICE)
    try:
        _OFFICE_STATUS = (Path(office) if office else _OFFICE) / "tray_stall.json"
        _OFFICE_STATUS.write_text(json.dumps({
            "state": "STALLED",
            "ts": int(time.time()),
            "auto_revived_conductor": revived,
            "note": "Hermes stalled — conductor workers auto-revived; full reboot still via Update menu",
        }, indent=2), encoding="utf-8")
    except Exception:
        pass
    return {"action": "stalled-auto-revived" if revived else "stalled-awaiting-human",
            "alive": False, "revived": revived}
