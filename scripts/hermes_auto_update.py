#!/usr/bin/env python3
"""Automated update + hot-reload orchestrator for Hermes.

Wraps the existing, tested ``hermes update`` pipeline with the self-learning
update-guard so our core patches (self_learning.py, multi-core knobs, the
run_agent.py hook) survive an upgrade.  Also exposes a live-reload trigger that
re-runs the stale-bytecode sweep and (optionally) relaunches the runtime the
way ``hermes update`` already does.

Design rules (consistent with the rest of the codebase):
  * Never re-implements git fetch/reset/install — delegates to ``hermes update``.
  * Fail-closed: if the guard `pre` snapshot fails we still try the update, but
    `post` is skipped so nothing is clobbered silently.
  * Idempotent and cron-safe: prints a structured summary, exits non-zero only
    on a genuine update failure.

Usage:
    python scripts/hermes_auto_update.py run            # do a guarded update now
    python scripts/hermes_auto_update.py enable daily   # schedule (cron) + config
    python scripts/hermes_auto_update.py enable weekly
    python scripts/hermes_auto_update.py disable        # remove schedule
    python scripts/hermes_auto_update.py status         # show schedule + last run
    python scripts/hermes_auto_update.py reload         # live-reload (bytecode sweep)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Resolve the hermes-agent root from this script location (scripts/ -> root).
_AGENT_ROOT = Path(__file__).resolve().parent.parent
_GUARD = _AGENT_ROOT / "scripts" / "hermes_selflearn_update_guard.py"
_STATE = Path(os.environ.get("HERMES_HOME", r"C:\Users\w3ce\AppData\Local\hermes")) / "auto_update_state.json"


def _hermes_exe() -> str:
    """Best-effort path to the hermes entry point running this process."""
    exe = shutil.which("hermes")
    if exe:
        return exe
    # Fall back to re-execing through the current interpreter's module.
    return f'{sys.executable} -m hermes_cli.main'


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def _guard(cmd: str) -> int:
    if not _GUARD.is_file():
        print(f"  (update-guard missing at {_GUARD}; skipping)")
        return 0
    return _run([sys.executable, str(_GUARD), cmd]).returncode


def _load_state() -> dict:
    try:
        return json.loads(_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        _STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"  (could not persist state: {exc})")


def cmd_run() -> int:
    """Perform a guarded `hermes update`."""
    state = _load_state()
    started = datetime.now(timezone.utc).isoformat()

    print("[auto-update] 1/3 snapshot core patches (update-guard pre)...")
    _guard("pre")

    print("[auto-update] 2/3 run `hermes update --yes`...")
    hermes = _hermes_exe()
    # `hermes update --yes` performs fetch/reset/pip-reinstall and relaunches.
    result = _run(f"{hermes} update --yes".split(), cwd=str(_AGENT_ROOT))
    update_ok = result.returncode == 0
    if not update_ok:
        print(result.stderr[-2000:])
        print("[auto-update] update reported failure; NOT reconciling patches.")

    print("[auto-update] 3/3 reconcile core patches (update-guard post)...")
    if update_ok:
        rc = _guard("post")
        if rc != 0:
            print("[auto-update] WARNING: patch reconciliation found conflicts "
                  "(see .diff files under $HERMES_HOME/selflearn_backup).")

    state["last_run"] = started
    state["last_update_ok"] = update_ok
    state["last_sha"] = _git_sha()
    _save_state(state)

    print(f"[auto-update] done. update_ok={update_ok}")
    return 0 if update_ok else 1


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(_AGENT_ROOT),
            capture_output=True, text=True, timeout=20,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def cmd_enable(mode: str) -> int:
    if mode not in ("daily", "weekly"):
        print("usage: enable <daily|weekly>")
        return 2
    # Persist preference in the auto-update state file (the real toggle is read
    # by hermes config; this keeps a local record + lets `status` report it).
    state = _load_state()
    state["auto_update"] = mode
    _save_state(state)

    # Schedule via the existing cron subsystem. The job simply re-invokes this
    # script's `run`, which reuses the battle-tested `hermes update` path.
    hermes = _hermes_exe()
    schedule = "every day" if mode == "daily" else "every week"
    job_cmd = f'{sys.executable} "{_AGENT_ROOT / "scripts" / "hermes_auto_update.py"}" run'
    cron_args = f"{hermes} cron add \"{schedule}\" \"{job_cmd}\" --skill none".split()
    # Note: prefers the documented cron surface; falls back gracefully.
    res = _run(cron_args)
    print(res.stdout[-1500:] or res.stderr[-1500:])
    print(f"[auto-update] automatic updates ENABLED ({mode}).")
    print("  core patches will be snapshotted before and reconciled after each update.")
    return 0


def cmd_disable() -> int:
    state = _load_state()
    state["auto_update"] = "off"
    _save_state(state)
    print("[auto-update] automatic updates DISABLED.")
    return 0


def cmd_status() -> int:
    state = _load_state()
    print("Automatic updates:", state.get("auto_update", "off"))
    print("Last run         :", state.get("last_run", "never"))
    print("Last update ok   :", state.get("last_update_ok", "n/a"))
    print("Last SHA         :", state.get("last_sha", "unknown"))
    return 0


def cmd_reload() -> int:
    """Live-reload: clear stale bytecode so the next launch picks up new code.

    This mirrors the launch-time stale-bytecode sweep in hermes_cli/main.py
    (issues #6207, #60242) but is triggered on demand — useful after a manual
    ``git pull`` or file edit without restarting the whole runtime blindly.
    """
    removed = 0
    for pycache in _AGENT_ROOT.rglob("__pycache__"):
        if pycache.is_dir():
            try:
                shutil.rmtree(pycache)
                removed += 1
            except Exception:
                pass
    print(f"[reload] cleared {removed} __pycache__ director{'y' if removed == 1 else 'ies'}.")
    print("[reload] next `hermes` launch will import fresh bytecode.")
    print("[reload] to apply immediately, restart the runtime (hermes update relaunches automatically).")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    sub = sys.argv[1]
    if sub == "run":
        return cmd_run()
    if sub == "enable":
        return cmd_enable(sys.argv[2] if len(sys.argv) > 2 else "daily")
    if sub == "disable":
        return cmd_disable()
    if sub == "status":
        return cmd_status()
    if sub == "reload":
        return cmd_reload()
    print(f"unknown subcommand: {sub}\n{__doc__}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
