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


def self_heal(office: Path | None = None) -> dict:
    """One watchdog pass. If Hermes stalled, DO NOT force-restart it (per the rule:
    a reboot/update must go through the Update menu, never an automatic forced
    restart). Instead report the stall so the human can act via the menu.

    Returns what it observed."""
    if hermes_alive(office):
        return {"action": "ok", "alive": True}
    # Stalled: raise a visible signal and let the human reboot via the Update menu.
    try:
        _OFFICE_STATUS = (Path(office) if office else _OFFICE) / "tray_stall.json"
        _OFFICE_STATUS.write_text(json.dumps({
            "state": "STALLED",
            "ts": int(time.time()),
            "note": "Hermes stalled — awaiting human reboot via Update menu (no auto-restart)",
        }, indent=2), encoding="utf-8")
    except Exception:
        pass
    return {"action": "stalled-awaiting-human", "alive": False}
