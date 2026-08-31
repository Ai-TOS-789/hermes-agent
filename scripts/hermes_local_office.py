#!/usr/bin/env python3
"""Hermes Local Office — disk-backed working root on a dedicated data drive (F:).

Brings the Hy3:free Local Office online using ONLY the resources already present
on this machine: the 1.9 TB F: data drive and the Hermes agent code. Everything
heavy (model-forge shards, siphon mesh, block sync, stream status, logs) is kept
on F: in clearly separated folders, while the live Hy3:free stream is read from
the real HERMES_HOME session proxy.

Folder layout on F:/HermesOffice:
    F:/HermesOffice/
      model_forge/      # disk-first self-model building (±1/0/-1 pyramid)
      siphon_mesh/      # SEED/REED/DEEP/BEEM P2P weight mesh (zero delta)
      block_sync/       # 3-way handshake block equal to Hy3:free stream
      weight_stream_status.json  # live % balance + handshake signal
      supervisor_status.json     # single control-plane status
      logs/             # monitor + office logs

Usage:
    python scripts/hermes_local_office.py init        # create folders on F:
    python scripts/hermes_local_office.py start       # background stream monitor
    python scripts/hermes_local_office.py supervise   # single control plane (self-healing)
    python scripts/hermes_local_office.py full       # FULL OPTION: all subsystems, autonomous
    python scripts/hermes_local_office.py trust-on   # declare trust anchor present (learn)
    python scripts/hermes_local_office.py trust-off  # anchor removed -> HALT, await human
    python scripts/hermes_local_office.py once        # single snapshot to stdout
    python scripts/hermes_local_office.py stop        # remove status file

The supervisor is the SINGLE Hermes model of control: it owns mesh + block
handshake and self-heals toward zero delta continuously, so the job never
stutters and finishes balanced. No external resources, no network, no GPU.
"""

from __future__ import annotations

import os
import sys
import json
import time
from pathlib import Path

# Make repo root importable when run via `python scripts/...`.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.weight_stream_monitor import (  # noqa: E402
    _OFFICE, _FORGE, _SIPHON_ROOT, _BLOCK_SYNC, _STATUS, _LOGS,
    _hy3_stream_bytes, cmd_once, cmd_start, cmd_stop,
)
from agent.siphon_supervisor import SiphonSupervisor  # noqa: E402


def cmd_init() -> int:
    for d in (_OFFICE, _FORGE, _SIPHON_ROOT, _BLOCK_SYNC, _LOGS):
        d.mkdir(parents=True, exist_ok=True)
    (_OFFICE / "README.txt").write_text(
        "Hermes Local Office (disk-backed, F:)\n"
        "model_forge/  : disk-first self-model building (±1/0/-1 pyramid)\n"
        "siphon_mesh/  : SEED/REED/DEEP/BEEM P2P weight mesh (zero delta)\n"
        "block_sync/   : 3-way handshake block equal to Hy3:free stream\n"
        "logs/         : monitor + office logs\n",
        encoding="utf-8",
    )
    print(f"[office] Local Office ready at {_OFFICE}")
    for label, d in (
        ("model_forge", _FORGE), ("siphon_mesh", _SIPHON_ROOT),
        ("block_sync", _BLOCK_SYNC), ("logs", _LOGS),
    ):
        print(f"   {label:12s} -> {d}")
    return 0


def _hy3_size() -> int:
    return _hy3_stream_bytes()[0]


def cmd_supervise() -> int:
    """Run the single control plane: mesh + block handshake, self-healing."""
    sup = SiphonSupervisor(_OFFICE, shards=["w1", "w2"], hy3_stream_size_fn=_hy3_size)
    sup.run_forever()
    return 0


def cmd_trust_on() -> int:
    """Declare the trust anchor present (Trezor / ID card plugged in). Enables
    autonomous learning. On a host without real hardware, this file IS the anchor
    the human controls — remove it (trust-off) to force an immediate halt."""
    anchor = _OFFICE / "guardrail.trust_anchor_present"
    anchor.write_text("1", encoding="utf-8")
    print(f"[guardrail] trust anchor declared present: {anchor}")
    print("[guardrail] autonomous learning may now proceed.")
    return 0


def cmd_trust_off() -> int:
    """Declare the trust anchor MISSING (Trezor / ID card removed). Forces an
    immediate halt of all autonomous learning — Hermes will await human input."""
    anchor = _OFFICE / "guardrail.trust_anchor_present"
    anchor.unlink(missing_ok=True)
    print(f"[guardrail] trust anchor removed: {anchor}")
    print("[guardrail] autonomous learning HALTED; awaiting human.")
    return 0


def cmd_root_lock() -> int:
    """Withdraw root authority (Card/Trezor consent removed). All autonomous action
    halts regardless of other gates until root-unlock. Human-controlled lock file."""
    lock = _OFFICE / "roadmap" / "root_lock.json"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"locked": True, "ts": int(time.time())}, indent=2), encoding="utf-8")
    print(f"[guardrail] root authority LOCKED: {lock}")
    print("[guardrail] all autonomous action halted; awaiting human (Card/Trezor) unlock.")
    return 0


def cmd_root_unlock() -> int:
    """Restore root authority (Card/Trezor consent present). Re-enables autonomous
    action subject to the other guardrail gates (trust anchor, interference)."""
    lock = _OFFICE / "roadmap" / "root_lock.json"
    lock.unlink(missing_ok=True)
    print(f"[guardrail] root authority UNLOCKED: {lock} removed")
    print("[guardrail] autonomous action may proceed (subject to other gates).")
    return 0


def cmd_full() -> int:
    """Full Option: run every subsystem on this Windows box, autonomously.
    GOOD->answer well, BAD->defend, LATE->defend anyway. Human watches."""
    from agent.learning_node import LearningNode
    from agent.siphon_supervisor import SiphonSupervisor
    from agent.survival import build_full_option_survival

    print("[full] Hermes Full Option — autonomous, you are watching.")
    # 1) Autonomous learning node (root-level, learns by itself).
    node = LearningNode(office=_OFFICE, cadence=60.0)
    node.start()
    # 2) Weight-mesh + block-handshake control plane (single supervisor).
    sup = SiphonSupervisor(_OFFICE, shards=["w1", "w2"],
                           hy3_stream_size_fn=_hy3_size)
    sup_thr = sup.run_forever_background()
    # 3) Survival layer (defends the whole stack).
    survival = build_full_option_survival()
    survival.run_forever()
    return 0


def main() -> int:
    action = (sys.argv[1] if len(sys.argv) > 1 else "once").lower()
    if action == "init":
        return cmd_init()
    if action == "start":
        return cmd_start()
    if action == "supervise":
        return cmd_supervise()
    if action == "full":
        return cmd_full()
    if action == "trust-on":
        return cmd_trust_on()
    if action == "trust-off":
        return cmd_trust_off()
    if action == "root-lock":
        return cmd_root_lock()
    if action == "root-unlock":
        return cmd_root_unlock()
    if action == "stop":
        return cmd_stop()
    if action == "conduct":
        # Phase 5+: launch the Option-Skills conductor workers with a health-check
        # respawn loop (so a dead worker is revived automatically — no forced reboot,
        # and it honors the guardrail halt). Uses hermes_autostart.run_supervisor_loop.
        from scripts.hermes_autostart import run_supervisor_loop
        print(f"[conduct] starting supervised workers under guardrail; Ctrl-C to stop.")
        return run_supervisor_loop(cadence=15.0)
    return cmd_once()


if __name__ == "__main__":
    raise SystemExit(main())
