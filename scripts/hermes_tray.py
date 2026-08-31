#!/usr/bin/env python3
"""Hermes Tray System (Windows) — lives in the system tray, auto-raises itself
if the Hermes process stalls or drops.

User directive: set up a Tray System, and if it drops/crashes, pop itself back up
automatically. This is an EDGE utility (per Hermes' "capability lives at the edges"
rule): it does NOT patch the core agent. Pure logic lives in agent/tray_core.py
(tested headless); this file adds the tkinter tray UI and the self-heal watchdog.

The self-heal watchdog checks heartbeat status files on the Local Office (written
by every supervised subsystem). If they go stale, it restarts the always-on
supervisor AND raises the tray window so the human sees recovery happening — Hermes
is never left invisible/dead.

Usage:
    python scripts/hermes_tray.py start    # tray + self-heal watchdog
    python scripts/hermes_tray.py once     # one self-heal check (for cron/tests)
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

# Make repo root importable when run via `python scripts/...`.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agent.tray_core import hermes_alive, self_heal

_AGENT_ROOT = Path(__file__).resolve().parent.parent


def cmd_once() -> int:
    print(f"[tray] self-heal: {self_heal()}")
    return 0


def cmd_start() -> int:
    # Guard the import so headless/test environments don't fail at load.
    try:
        import tkinter as tk  # noqa: F401
    except Exception as exc:
        print(f"[tray] tkinter unavailable ({exc}); running self-heal loop visibly.")
        return _run_selfheal_loop()

    import tkinter as tk
    from tkinter import ttk  # noqa: F401

    root = tk.Tk()
    root.title("Hermes Tray")
    root.geometry("340x180")

    status_var = tk.StringVar(value="Hermes Tray active")

    def refresh():
        res = self_heal()
        if res["action"] == "ok":
            status_var.set("Hermes running — healthy")
        elif res["action"] == "restarted":
            status_var.set("Hermes stalled — restarted automatically")
        else:
            status_var.set(f"Restart failed: {res.get('error', '?')}")

    def watching():
        status_var.set("You are watching. Hermes continues autonomously.")

    tk.Label(root, text="Hermes Tray System", font=("Segoe UI", 14, "bold")).pack(pady=10)
    tk.Label(root, textvariable=status_var, wraplength=300).pack(pady=6)
    tk.Button(root, text="I'm watching (Open)", command=watching).pack(pady=4)
    tk.Button(root, text="Restart Hermes", command=lambda: (self_heal(), refresh())).pack(pady=4)

    def _watchdog():
        while True:
            time.sleep(10)
            res = self_heal()
            if res["action"] != "ok":
                # Pop the window up so the human witnesses recovery.
                try:
                    root.deiconify()
                    root.lift()
                    root.attributes("-topmost", True)
                    root.after(0, refresh)
                except Exception:
                    pass

    threading.Thread(target=_watchdog, daemon=True, name="tray-watchdog").start()
    refresh()
    root.mainloop()
    return 0


def _run_selfheal_loop() -> int:
    print("[tray] self-heal watchdog running (visible fallback). Ctrl-C to stop.")
    try:
        while True:
            print(f"[tray] {self_heal()}")
            time.sleep(10)
    except KeyboardInterrupt:
        print("\n[tray] stopped.")
    return 0


def main() -> int:
    action = (sys.argv[1] if len(sys.argv) > 1 else "start").lower()
    if action == "once":
        return cmd_once()
    return cmd_start()


if __name__ == "__main__":
    raise SystemExit(main())
