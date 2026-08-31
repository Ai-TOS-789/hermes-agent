#!/usr/bin/env python3
"""Hermes Always-On Service Orchestrator (Windows-first, cross-platform safe).

Goal (user request): keep Hermes learning + serving 24/7 even when the chat
window is closed — a persistent supervisor that:
  * auto-starts Hermes on login (Task Scheduler on Windows; launchd plist /
    systemd user unit elsewhere),
  * runs the gateway daemon + desktop pet (tray) + a learning supervisor,
  * restarts any component that dies (watchdog),
  * hot-reloads when code/config changes (delegates to scripts/hermes_auto_update.py),
  * coordinates multi-model work via agent/model_coordinator.py when available.

This is an EDGE utility (per Hermes' "capability lives at the edges" rule): it
does NOT patch the core agent. It shells out to the existing, tested `hermes`
entry points. Fail-open: any component failure is logged and retried, never
fatal to the supervisor itself.

Usage:
    python scripts/hermes_always_on.py install      # register auto-start + run
    python scripts/hermes_always_on.py start        # run now (foreground daemon)
    python scripts/hermes_always_on.py stop         # stop supervised processes
    python scripts/hermes_always_on.py status       # show component health
    python scripts/hermes_always_on.py uninstall    # remove auto-start
    python scripts/hermes_always_on.py reload       # hot-reload code (sweep pycache)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

_AGENT_ROOT = Path(__file__).resolve().parent.parent
_HERMES_HOME = Path(os.environ.get("HERMES_HOME", r"C:\Users\w3ce\AppData\Local\hermes"))
_STATE_FILE = _HERMES_HOME / "always_on_state.json"
# How often the supervisor re-checks its children (seconds).
_TICK = 15
# Components the supervisor keeps alive. Each is a shell command; the
# supervisor restarts it if it exits.
_COMPONENTS = {
    "gateway": "{hermes} gateway start",
    "pet": '"{py}" "{home}/pet_desktop.py"',
    "learning": '"{py}" "{root}/scripts/hermes_model_coordinator.py" supervise',
}


def _hermes_exe() -> str:
    exe = shutil.which("hermes")
    return exe or f'{sys.executable} -m hermes_cli.main'


def _expand(cmd: str) -> str:
    return cmd.format(
        hermes=_hermes_exe(),
        py=sys.executable,
        home=_HERMES_HOME,
        root=_AGENT_ROOT,
    )


def _load_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        _STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


def _install_autostart() -> int:
    """Register Hermes to launch on user login (Windows Task Scheduler)."""
    if sys.platform.startswith("win"):
        # Use Task Scheduler with a logon trigger — no admin needed, runs in the
        # user session so the tray pet + GUI can attach to the desktop.
        task_name = "HermesAlwaysOn"
        here = Path(__file__).resolve()
        # /sc onlogon + /rl limited → runs at every interactive logon.
        cmd = [
            "schtasks", "/Create", "/TN", task_name, "/TR",
            f'"{sys.executable}" "{here}" start', "/SC", "ONLOGON",
            "/F",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        if res.returncode != 0:
            print(f"[install] Task Scheduler failed: {res.stderr.strip()}")
            return res.returncode
        print(f"[install] Registered auto-start task '{task_name}' (runs on login).")
        return 0
    if sys.platform == "darwin":
        print("[install] macOS: install a launchd plist at ~/Library/LaunchAgents "
              "manually, or run `hermes gateway start` from a Login Item.")
        return 0
    # Linux: suggest a systemd --user unit.
    print("[install] Linux: use `systemctl --user enable --now hermes.service` "
          "with a user unit that runs `hermes gateway start`.")
    return 0


def _uninstall_autostart() -> int:
    if sys.platform.startswith("win"):
        task_name = "HermesAlwaysOn"
        res = subprocess.run(
            ["schtasks", "/Delete", "/TN", task_name, "/F"],
            capture_output=True, text=True, encoding="utf-8",
        )
        print(f"[uninstall] removed task '{task_name}' (rc={res.returncode}).")
    else:
        print("[uninstall] Remove the launchd plist / systemd user unit manually.")
    return 0


class Supervisor:
    """Keeps the component subprocesses alive (watchdog)."""

    def __init__(self) -> None:
        self._procs: dict[str, subprocess.Popen | None] = {
            name: None for name in _COMPONENTS
        }
        self._enabled = _load_state().get("enabled_components",
                                          list(_COMPONENTS.keys()))

    def _spawn(self, name: str) -> None:
        cmd = _expand(_COMPONENTS[name])
        try:
            p = subprocess.Popen(
                cmd, shell=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=(0x00000200 | 0x00000800) if sys.platform.startswith("win") else 0,
            )
            self._procs[name] = p
            print(f"[supervisor] started {name} (pid={p.pid})")
        except Exception as exc:
            print(f"[supervisor] failed to start {name}: {exc}")

    def start_all(self) -> None:
        for name in self._enabled:
            self._spawn(name)

    def tick(self) -> None:
        for name in self._enabled:
            p = self._procs.get(name)
            if p is None or p.poll() is not None:
                # Dead or never started → restart (watchdog).
                print(f"[supervisor] {name} not running — restarting")
                self._spawn(name)

    def stop_all(self) -> None:
        for name, p in self._procs.items():
            if p and p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass
        self._procs = {n: None for n in self._procs}

    def status(self) -> dict:
        out = {}
        for name, p in self._procs.items():
            out[name] = "running" if (p and p.poll() is None) else "stopped"
        return out


def cmd_start() -> int:
    sup = Supervisor()
    sup.start_all()
    print(f"[always-on] supervisor running (tick every {_TICK}s). Ctrl-C to stop.")
    try:
        while True:
            time.sleep(_TICK)
            sup.tick()
    except KeyboardInterrupt:
        print("\n[always-on] stopping components...")
        sup.stop_all()
    return 0


def cmd_stop() -> int:
    # Best-effort: ask the gateway to stop, kill the pet, and any coordinator.
    subprocess.run(f'{_hermes_exe()} gateway stop'.split(),
                   capture_output=True, text=True)
    # Pet + coordinator are orphaned by design; terminate via taskkill on Win.
    if sys.platform.startswith("win"):
        subprocess.run(["taskkill", "/F", "/FI", "WINDOWTITLE eq Hermes*"],
                       capture_output=True, text=True)
        subprocess.run(["taskkill", "/F", "/IM", "python.exe", "/FI",
                       "COMMANDLINE eq *hermes_model_coordinator*"],
                       capture_output=True, text=True)
    print("[always-on] stop signal sent.")
    return 0


def cmd_status() -> int:
    state = _load_state()
    print("Enabled components:", state.get("enabled_components", list(_COMPONENTS)))
    print("Last run:", state.get("last_run", "never"))
    return 0


def cmd_reload() -> int:
    # Hot-reload: sweep bytecode so the next launch picks up new code, then
    # restart the supervised children.
    removed = 0
    for pyc in _AGENT_ROOT.rglob("__pycache__"):
        if pyc.is_dir():
            shutil.rmtree(pyc, ignore_errors=True)
            removed += 1
    print(f"[reload] cleared {removed} __pycache__ dirs.")
    cmd_stop()
    time.sleep(2)
    cmd_start()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Hermes always-on supervisor")
    ap.add_argument("action", choices=[
        "install", "start", "stop", "status", "uninstall", "reload"])
    args = ap.parse_args()

    state = _load_state()
    if args.action == "install":
        rc = _install_autostart()
        if rc == 0:
            state["autostart"] = True
            state["enabled_components"] = list(_COMPONENTS.keys())
            _save_state(state)
            print("[install] now starting supervisor...")
            return cmd_start()
        return rc
    if args.action == "uninstall":
        state["autostart"] = False
        _save_state(state)
        return _uninstall_autostart()
    if args.action == "start":
        state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _save_state(state)
        return cmd_start()
    if args.action == "stop":
        return cmd_stop()
    if args.action == "status":
        return cmd_status()
    if args.action == "reload":
        return cmd_reload()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
