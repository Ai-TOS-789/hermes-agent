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
      logs/             # monitor + office logs

Usage:
    python scripts/hermes_local_office.py init     # create folders on F:
    python scripts/hermes_local_office.py start    # background monitor (F: office)
    python scripts/hermes_local_office.py once     # single snapshot to stdout
    python scripts/hermes_local_office.py stop     # remove status file

The monitor reads HERMES_HOME for the Hy3 stream and writes all heavy artifacts
to HERMES_OFFICE (F:). No external resources, no network, no GPU required.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make repo root importable when run via `python scripts/...`.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.weight_stream_monitor import (  # noqa: E402
    _OFFICE, _FORGE, _SIPHON_ROOT, _BLOCK_SYNC, _STATUS, _LOGS,
    cmd_once, cmd_start, cmd_stop,
)


def cmd_init() -> int:
    for d in (_OFFICE, _FORGE, _SIPHON_ROOT, _BLOCK_SYNC, _LOGS):
        d.mkdir(parents=True, exist_ok=True)
    # Write a layout marker so the office is self-describing.
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


def main() -> int:
    action = (sys.argv[1] if len(sys.argv) > 1 else "once").lower()
    if action == "init":
        return cmd_init()
    if action == "start":
        return cmd_start()
    if action == "stop":
        return cmd_stop()
    return cmd_once()


if __name__ == "__main__":
    raise SystemExit(main())
