#!/usr/bin/env python3
"""Parabola Model — North Pole 0 / South Pole 0 / Equator = this machine's CPU.

User directive: process the whole thread as a POLAR/parabolic coordinate system
centered on THIS Windows machine's CPU, and drive it through data-script coding
nodes as a parabola (คณิตศาสตร์พาราโบลา).

Model:
  The Hermes system is a parabola y = a(x - h)^2 + k whose VERTEX (h, k) is the
  Equator = this machine's CPU (the processing axis). x is the deviation axis:
    * +x  -> North Pole (human intent / goals).  North Pole = 0  => intent deviation 0.
    * -x  -> South Pole (faults / hazards).      South Pole = 0  => faults 0.
  With the vertex at the CPU (h=0, k=0 for a balanced system), the curve is
    y = a * x^2
  and both poles sit at y = a * (pole_offset)^2. We DEFINE the poles as zero by
  requiring the *measured* north/south values to be 0; the parabola's job is to
  report how far x has drifted from the CPU axis and the resulting "height" y,
  which the Equator (CPU) must keep at minimum.

  A coding node feeds this model from real data (heartbeat status files, guardrail
  state, survival verdict). The parabola yields:
    * x        = signed drift (north positive, south negative) from CPU axis
    * y        = a * x^2  (cost/height the Equator must process)
    * balanced = (north == 0 and south == 0)  -> x == 0 -> y == 0 (true zero)
  The Equator Model (agent/equator_model.py) is the CPU-axis executor; this module
  is the MATH that governs it. Pure stdlib, disk-backed, no GPU.

Verified by tests/agent/test_parabola_model.py.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import os as _os
from pathlib import Path as _P

if _P(r"F:/").exists():
    _OFFICE = _P(r"F:/HermesOffice")
else:
    _OFFICE = _P(_os.environ.get("HERMES_HOME", r"C:\Users\w3ce\AppData\Local\hermes")) / "HermesOffice"

_STATUS = _OFFICE / "parabola_status.json"


@dataclass
class ParabolaState:
    """A point on the system parabola."""
    a: float                 # curvature (processing cost per unit drift^2)
    x: float                 # signed drift from CPU axis (+north / -south)
    y: float                 # height = a * x^2  (cost the Equator processes)
    north: float             # north-pole value (intent deviation)
    south: float             # south-pole value (fault count)
    balanced: bool           # True when both poles are 0 -> x==0 -> y==0

    def as_dict(self) -> dict:
        return {
            "a": self.a, "x": round(self.x, 4), "y": round(self.y, 4),
            "north": self.north, "south": self.south, "balanced": self.balanced,
        }


def parabola_point(north: float, south: float, a: float = 1.0) -> ParabolaState:
    """Map (north, south) poles onto the CPU-centered parabola.

    The CPU axis is x=0. North is +x, South is -x. The *drift* x is the
    signed imbalance: x = (north - south) / 2  (north pulls +, south pulls -).
    When both poles are 0, x = 0 and y = a*0 = 0 -> perfectly balanced on the
    Equator (CPU). y grows with the square of drift, which the CPU must process.
    """
    x = (north - south) / 2.0
    y = a * x * x
    balanced = (north == 0.0 and south == 0.0)
    return ParabolaState(a=a, x=x, y=y, north=north, south=south, balanced=balanced)


class ParabolaModel:
    """Drives the parabola from real data-script coding nodes."""

    def __init__(self, office: Optional[Path] = None, a: float = 1.0) -> None:
        self.office = Path(office) if office else _OFFICE
        self.office.mkdir(parents=True, exist_ok=True)
        self.a = a

    def _read_poles(self) -> tuple[float, float]:
        """Read north/south from the data-script coding nodes' status files."""
        north, south = 0.0, 0.0
        # NORTH = intent deviation from equator model (0 = aligned)
        eq = self.office / "equator_status.json"
        if eq.is_file():
            try:
                d = json.loads(eq.read_text(encoding="utf-8"))
                north = float(d.get("north_deviation", 0.0))
            except Exception:
                pass
        # SOUTH = faults from survival (0 faults = safe) or guardrail halt
        surv = self.office / "survival_status.json"
        if surv.is_file():
            try:
                d = json.loads(surv.read_text(encoding="utf-8"))
                if d.get("verdict") == "BAD":
                    south += 1.0
            except Exception:
                pass
        gr = self.office / "guardrail_status.json"
        if gr.is_file():
            try:
                d = json.loads(gr.read_text(encoding="utf-8"))
                if d.get("state") == "AWAITING_HUMAN":
                    south += 1.0  # a halt is a southern (unsafe) event
            except Exception:
                pass
        return north, south

    def sample(self) -> ParabolaState:
        north, south = self._read_poles()
        st = parabola_point(north, south, self.a)
        try:
            _STATUS.write_text(json.dumps({
                "ts": int(time.time()), **st.as_dict(),
                "equator": "CPU@" + str(self.office),
            }, indent=2), encoding="utf-8")
        except Exception:
            pass
        return st

    def is_zero(self) -> bool:
        """True when the system sits at the vertex: both poles 0, y 0."""
        return self.sample().balanced


def run_once(office: Optional[Path] = None, a: float = 1.0) -> ParabolaState:
    return ParabolaModel(office, a).sample()
