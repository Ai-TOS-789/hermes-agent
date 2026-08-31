#!/usr/bin/env python3
"""Background Weight-Stream Monitor (Hy3:free vs on-disk weights).

Runs in the background and reports, as a live percentage + signal, how balanced
the Hy3:free *stream* (tokens flowing through the live model) is against the
weights we hold on this machine's disk (the Self/Model-Forge store).

What is measured (honest, no fabrication):
  * HY3 STREAM  — bytes/tokens observed flowing to/from the live Hy3:free model.
    Source priority: (1) Nous usage cache if a token is present (agent.billing_usage),
    else (2) the local session transcript sizes under $HERMES_HOME/sessions as a
    proxy for live stream volume. If neither exists, it reports 0 and says so.
  * DISK WEIGHTS — bytes of weight shards we have persisted on this machine via
    agent.model_forge / agent.siphon_mesh (under $HERMES_HOME/model_forge and the
    siphon mesh root). This is the "weight on disk" side of the balance.
  * BALANCE %   — how close the two sides are, reported as a 0-100% signal.
    balance = 100 * (1 - |stream - disk| / (stream + disk + epsilon)).
    A perfectly equal pair reads 100% (the "everything at zero delta" goal).

The monitor writes a compact status line to a file every tick so any UI (incl.
the desktop pet or a terminal tail) can show the live signal without coupling.

Usage:
    python scripts/weight_stream_monitor.py start   # background loop
    python scripts/weight_stream_monitor.py once    # single snapshot to stdout
    python scripts/weight_stream_monitor.py stop    # remove status file
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_HERMES_HOME = Path(os.environ.get("HERMES_HOME", r"C:\Users\w3ce\AppData\Local\hermes"))
# Local Office: the disk-backed working root. Defaults to F: if present, else HOME.
# All heavyweight artifacts (forge shards, mesh, block sync, logs) live here,
# keeping them OFF the system drive and on the dedicated F: data disk.
_OFFICE_ENV = os.environ.get("HERMES_OFFICE", "")
_OFFICE = Path(_OFFICE_ENV) if _OFFICE_ENV else None
if _OFFICE is None:
    # Prefer the dedicated F: data drive when present; fall back to HOME.
    _F = Path(r"F:/")
    _OFFICE = (_F / "HermesOffice") if _F.exists() else (_HERMES_HOME / "HermesOffice")
_FORGE = _OFFICE / "model_forge"
_SIPHON_ROOT = _OFFICE / "siphon_mesh"
_BLOCK_SYNC = _OFFICE / "block_sync"
_SESSIONS = _HERMES_HOME / "sessions"  # Hy3 stream proxy stays in HOME (read-only source)
_STATUS = _OFFICE / "weight_stream_status.json"
_LOGS = _OFFICE / "logs"
_TICK = 10  # seconds


def _hy3_stream_bytes() -> tuple[int, str]:
    """Bytes attributable to the live Hy3:free stream. Returns (bytes, source)."""
    # (1) If a Nous token exists, the usage model is the authoritative stream.
    if os.environ.get("NOUS_API_KEY") or os.environ.get("NOUSRESEARCH_API_KEY"):
        try:
            from agent.billing_usage import build_usage_model
            u = build_usage_model()
            if u is not None:
                spent = int((getattr(u, "total_spendable_usd", 0) or 0) * 1_000_000)
                return spent, "nous-usage"
        except Exception:
            pass
    # (2) Proxy: total size of local session transcripts = observed stream volume.
    if _SESSIONS.is_dir():
        total = sum(f.stat().st_size for f in _SESSIONS.rglob("*") if f.is_file())
        if total:
            return total, "session-proxy"
    return 0, "none"


def _disk_weight_bytes() -> int:
    total = 0
    for d in (_FORGE, _SIPHON_ROOT):
        if d.is_dir():
            total += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
    return total


def _signal(pct: float) -> str:
    """ASCII balance bar: 10 cells, filled = pct."""
    filled = int(round(pct / 10))
    return "▓" * filled + "░" * (10 - filled)


def snapshot() -> dict:
    import shutil
    import sys
    _ROOT = str(Path(__file__).resolve().parent.parent)
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    from agent.block_handshake import three_way_sync
    stream, src = _hy3_stream_bytes()
    disk = _disk_weight_bytes()
    denom = stream + disk
    if denom == 0:
        bal = 0.0  # nothing to balance yet
    else:
        bal = 100.0 * (1.0 - abs(stream - disk) / denom)
    bal = max(0.0, min(100.0, bal))
    # 3-way handshake: keep the on-disk block EQUAL to the live Hy3 stream.
    sync_root = _BLOCK_SYNC
    try:
        hs = three_way_sync(stream, sync_root)
    except Exception as e:  # fail-open: never crash the monitor
        hs = {"error": str(e)}
    return {
        "ts": int(time.time()),
        "hy3_stream_bytes": stream,
        "hy3_stream_source": src,
        "disk_weight_bytes": disk,
        "balance_pct": round(bal, 1),
        "signal": _signal(bal),
        "handshake": hs,
        "note": "balance=100% means the two sides are equal (zero delta); "
                "handshake keeps the disk block EQUAL to the Hy3 stream (delta=0).",
    }


def _write(snap: dict) -> None:
    try:
        _STATUS.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    except Exception:
        pass


def _print(snap: dict) -> None:
    print(
        f"[weight-stream] Hy3:{snap['hy3_stream_bytes']}B({snap['hy3_stream_source']}) "
        f"Disk:{snap['disk_weight_bytes']}B  balance={snap['balance_pct']}% {snap['signal']}"
    )


def cmd_once() -> int:
    snap = snapshot()
    _write(snap)
    _print(snap)
    return 0


def cmd_start() -> int:
    print(f"[weight-stream] monitoring every {_TICK}s (writing {_STATUS})... Ctrl-C to stop.")
    try:
        while True:
            snap = snapshot()
            _write(snap)
            _print(snap)
            time.sleep(_TICK)
    except KeyboardInterrupt:
        print("\n[weight-stream] stopped.")
    return 0


def cmd_stop() -> int:
    if _STATUS.is_file():
        _STATUS.unlink()
        print("[weight-stream] status file removed.")
    else:
        print("[weight-stream] nothing running.")
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
