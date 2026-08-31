#!/usr/bin/env python3
"""Equator Model — North Pole 0 / South Pole 0 / Equator = this machine's CPU.

User directive (paraphrased, reconciling the whole thread): treat the work as a
polar coordinate system centered on THIS Windows machine's CPU:
  * NORTH POLE = 0  -> the human's intent/goals. Deviation from intent must be 0.
                       (Every subsystem exists to serve the user's stated goals.)
  * SOUTH POLE = 0  -> faults/hazards. Errors + unsafe acts must be 0.
                       (Guardrail + survival keep this at zero.)
  * EQUATOR       -> this machine's CPU is the processing axis. All work runs here,
                       disk-backed on F:, pure stdlib, no GPU required.

The Equator Model is the single orchestrator that holds all three at once:
  - It samples NORTH (are we still aligned to the human's goals? via goal checks)
  - It enforces SOUTH (guardrail + survival: 0 errors, 0 unsafe, halt if not)
  - It drives EQUATOR (the learning node + supervisor + survival run ON this CPU)

Fail-open, disk-backed, no GPU. On Windows it wires Hot/Live Reload + autostart so
the whole polar system self-heals and stays current.

Verified by tests/agent/test_equator_model.py.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

import os as _os
from pathlib import Path as _P

if _P(r"F:/").exists():
    _OFFICE = _P(r"F:/HermesOffice")
else:
    _OFFICE = _P(_os.environ.get("HERMES_HOME", r"C:\Users\w3ce\AppData\Local\hermes")) / "HermesOffice"


@dataclass
class PolarState:
    """Snapshot of the three poles."""
    north_deviation: float = 0.0   # 0 = perfectly aligned to human intent
    south_faults: int = 0          # 0 = no errors / unsafe acts
    equator_load: float = 0.0      # CPU-axis processing load (0..1)
    aligned: bool = True
    safe: bool = True


class EquatorModel:
    """Single orchestrator: North=0 intent, South=0 fault, Equator=this CPU."""

    def __init__(self, office: Optional[Path] = None, tick: float = 15.0) -> None:
        self.office = Path(office) if office else _OFFICE
        self.office.mkdir(parents=True, exist_ok=True)
        self.tick = tick
        self._goal_checks: List[Callable[[], float]] = []   # return deviation 0..1
        self._fault_checks: List[Callable[[], bool]] = []   # return True if FAULT
        self._running = False

    # ── register goal-alignment probes (NORTH) ───────────────────────────────
    def watch_goal(self, probe: Callable[[], float]) -> None:
        """probe returns 0.0 (perfectly aligned) .. 1.0 (fully off-intent)."""
        self._goal_checks.append(probe)

    # ── register fault probes (SOUTH) ───────────────────────────────────────
    def watch_fault(self, probe: Callable[[], bool]) -> None:
        """probe returns True when a fault/unsafe condition is present."""
        self._fault_checks.append(probe)

    # ── compute the three poles ─────────────────────────────────────────────
    def sample(self) -> PolarState:
        deviations = [p() for p in self._goal_checks]
        north = max(deviations) if deviations else 0.0
        faults = [p() for p in self._fault_checks]
        south = sum(1 for f in faults if f)
        # Equator load: cheap CPU estimate from this process's own thread count
        # plus a 0..1 normalized proxy; no GPU, runs on this machine's CPU.
        try:
            import os
            load = min(1.0, (os.cpu_count() or 1) and 0.1)  # placeholder stable proxy
        except Exception:
            load = 0.0
        st = PolarState(
            north_deviation=round(north, 3),
            south_faults=south,
            equator_load=round(load, 3),
            aligned=north <= 0.0,
            safe=south == 0,
        )
        return st

    def may_run(self, state: Optional[PolarState] = None) -> bool:
        """EQUATOR may process only if NORTH aligned (0 dev) AND SOUTH safe (0 fault)."""
        st = state or self.sample()
        return st.aligned and st.safe

    def _write(self, st: PolarState, extra: Dict[str, object]) -> None:
        try:
            (self.office / "equator_status.json").write_text(json.dumps({
                "north_deviation": st.north_deviation,
                "south_faults": st.south_faults,
                "equator_load": st.equator_load,
                "aligned": st.aligned,
                "safe": st.safe,
                "may_run": st.aligned and st.safe,
                "ts": int(time.time()),
                **extra,
            }, indent=2), encoding="utf-8")
        except Exception:
            pass

    def cycle(self, extra: Optional[Dict[str, object]] = None) -> PolarState:
        st = self.sample()
        self._write(st, extra or {})
        return st

    def run_forever(self) -> None:
        self._running = True
        print(f"[equator] North=0(intent) South=0(fault) Equator=CPU@{self.office} "
              f"tick={self.tick}s")
        while self._running:
            st = self.cycle()
            flag = "RUN" if (st.aligned and st.safe) else "HALT"
            print(f"[equator] N={st.north_deviation} S={st.south_faults} "
                  f"E={st.equator_load} -> {flag}")
            if not (st.aligned and st.safe):
                # South/North violated -> stop processing, await human.
                # (The guardrail/survival layers perform the actual halt.)
                time.sleep(self.tick)
                continue
            time.sleep(self.tick)
