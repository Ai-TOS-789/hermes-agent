#!/usr/bin/env python3
"""Always-on conductor with health-check respawn (closes gap #6/#8).

Wraps ``hermes_local_office.py conduct`` so the Option-Skills workers run continuously:
  * ``install`` registers a Windows Task Scheduler task that launches this script at
    user logon (no forced reboot — it only starts the supervised workers).
  * ``run`` supervises the workers and respawns any that die (health-check loop),
    honoring the guardrail (if it HALTs, the supervisor waits, it does not force).

All multi-process; each worker is its own process under learning_node's wing.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import os as _os

_REPO = Path(__file__).resolve().parent.parent
_OFFICE = Path(_os.environ.get("HERMES_OFFICE", "")) or (
    Path(r"F:/HermesOffice") if Path(r"F:/").exists()
    else Path(_os.environ.get("HERMES_HOME", r"C:\Users\w3ce\AppData\Local\hermes")) / "HermesOffice"
)
_TASK_NAME = "HermesOptionSkillsConductor"


def _conduct_cmd() -> list:
    return [sys.executable, str(_REPO / "scripts" / "hermes_local_office.py"), "conduct"]


def install_autostart() -> str:
    """Register a logon Task Scheduler task (no forced reboot)."""
    cmd = (
        f'schtasks /Create /TN "{_TASK_NAME}" /TR "\\"{sys.executable}\\" \\"'
        f'{_REPO / "scripts" / "hermes_autostart.py"}\\" run" /SC ONLOGON '
        f'/F /RL HIGHEST'
    )
    try:
        subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)
        return f"autostart task '{_TASK_NAME}' registered (runs at logon)"
    except subprocess.CalledProcessError as e:
        return f"autostart register failed: {e.stderr[:200]}"


def uninstall_autostart() -> str:
    try:
        subprocess.run(f'schtasks /Delete /TN "{_TASK_NAME}" /F', shell=True,
                       check=True, capture_output=True, text=True)
        return f"autostart task '{_TASK_NAME}' removed"
    except subprocess.CalledProcessError as e:
        return f"autostart remove failed: {e.stderr[:200]}"


def run_supervisor_loop(cadence: float = 15.0, max_respawns: int = 10) -> int:
    """Launch conduct workers and respawn any that die (health-check loop)."""
    from agent.learning_node import run_supervisor_processes
    procs = run_supervisor_processes(office=_OFFICE)
    respawns = 0
    try:
        while True:
            # Guardrail gate: if halted, wait (do NOT force anything).
            try:
                from agent import guardrail as _gr
                if not _gr.Guardrail(office=_OFFICE).may_proceed():
                    time.sleep(cadence)
                    continue
            except Exception:
                pass
            alive = [p for p in procs if p.is_alive()]
            if len(alive) < len(procs):
                dead = [p for p in procs if not p.is_alive()]
                print(f"[supervisor] {len(dead)} worker(s) died; respawning "
                      f"({respawns}/{max_respawns})", flush=True)
                if respawns >= max_respawns:
                    print("[supervisor] max respawns reached; stopping.", flush=True)
                    break
                procs = run_supervisor_processes(office=_OFFICE)
                respawns += 1
            time.sleep(cadence)
    except KeyboardInterrupt:
        for p in procs:
            p.terminate()
    return 0


def main() -> int:
    action = (sys.argv[1] if len(sys.argv) > 1 else "run").lower()
    if action == "install":
        print(install_autostart())
        return 0
    if action == "uninstall":
        print(uninstall_autostart())
        return 0
    return run_supervisor_loop()


if __name__ == "__main__":
    raise SystemExit(main())
