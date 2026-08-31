#!/usr/bin/env python3
"""Survival Layer — Hermes defends and sustains itself on this Windows machine.

User directive (paraphrased): build Hermes + Hy3 "Full Option" on this Windows box,
act autonomously, with the human watching. Behaviors:
  * GOOD comes  -> answer it well (report healthy, keep serving).
  * BAD intrudes -> intervene and defend (isolate/restart/re-siphon the fault).
  * If too late  -> do the SAME defensive thing anyway (fail-safe: never leave a
                    fault unhandled; default to protecting the mesh + node).

This is the top watchdog. It samples the health of every component the Full Option
brings online, and applies a defensive action the moment anything drifts. It is
fail-open and never crashes the supervisor. Pure stdlib; disk-backed; no GPU.

Full Option scope it governs (all on F:/HermesOffice):
  gateway, pet, self-learning, sensory, model-coordinator, model-forge,
  siphon-mesh (SEED/REED/DEEP/BEEM), block-handshake, learning-node, supervisor.

Verified by tests/agent/test_survival.py.
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

_STATUS = _OFFICE / "survival_status.json"


@dataclass
class Health:
    """Snapshot of one component's wellbeing."""
    name: str
    ok: bool
    detail: str = ""
    metrics: Dict[str, object] = field(default_factory=dict)


class Survival:
    """Top defensive watchdog. GOOD->answer, BAD->defend, LATE->defend anyway."""

    def __init__(self, office: Optional[Path] = None, tick: float = 15.0) -> None:
        self.office = Path(office) if office else _OFFICE
        self.office.mkdir(parents=True, exist_ok=True)
        self.tick = tick
        self._checks: List[Callable[[], Health]] = []
        self._actions: Dict[str, Callable[[Health], None]] = {}
        self._probe_names: Dict[int, str] = {}
        self._stop = False

    # ── register a health probe + its defensive action ────────────────────
    def watch(self, name: str, probe: Callable[[], Health],
              defend: Callable[[Health], None]) -> None:
        self._checks.append(probe)
        self._probe_names[id(probe)] = name
        self._actions[name] = defend

    # ── evaluate all probes; classify GOOD / BAD / LATE ───────────────────
    def scan(self) -> Dict[str, object]:
        report: Dict[str, object] = {"ts": int(time.time()), "components": {}}
        bad: List[Health] = []
        for probe in self._checks:
            # Resolve the registered name for this probe so a crashing probe is
            # still defended under its proper key (LATE -> defend anyway).
            name = self._probe_names.get(id(probe), "?")
            try:
                h = probe()
                if h.name == "?":
                    h = Health(name=name, ok=h.ok, detail=h.detail, metrics=h.metrics)
            except Exception as e:  # probe itself faulted -> treat as BAD (late)
                h = Health(name=name, ok=False, detail=f"probe-error:{e}")
                bad.append(h)
                report["components"][name] = {"ok": False, "detail": h.detail, "metrics": {}}  # type: ignore[index]
                continue
            report["components"][h.name] = {  # type: ignore[index]
                "ok": h.ok, "detail": h.detail, "metrics": h.metrics}
            if not h.ok:
                bad.append(h)
        # Decision:
        #   GOOD (no bad)  -> answer well (report healthy, keep serving).
        #   BAD / LATE     -> intervene with the defensive action, never leave it.
        if not bad:
            report["verdict"] = "GOOD"
            report["action"] = "answer_well"
        else:
            report["verdict"] = "BAD"
            report["action"] = "defend"
            defended = []
            for h in bad:
                fn = self._actions.get(h.name)
                if fn is not None:
                    try:
                        fn(h)
                        defended.append(h.name)
                    except Exception as e:  # defensive action failed -> still record
                        defended.append(f"{h.name}:defend-failed:{e}")
            report["defended"] = defended
        self._write(report)
        return report

    def _write(self, rep: Dict[str, object]) -> None:
        try:
            _STATUS.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
        except Exception:
            pass

    def run_forever(self) -> None:
        print(f"[survival] watching {len(self._checks)} components every {self.tick}s")
        while not self._stop:
            st = self.scan()
            print(f"[survival] verdict={st['verdict']} action={st['action']}")
            time.sleep(self.tick)


# ── Default Full-Option probes + defensive actions ────────────────────────────
def probe_mesh() -> Health:
    """Mesh must stay at zero delta (SEED/REED/DEEP/BEEM equal)."""
    try:
        from agent.siphon_mesh import SiphonMesh
        root = _OFFICE / "siphon_mesh"
        if not root.is_dir():
            return Health("mesh", ok=True, detail="not-initialized (skip)")
        m = SiphonMesh(root=root, shards=["w1", "w2"])
        d = m.total_delta()
        return Health("mesh", ok=d == 0, detail=f"delta={d}",
                      metrics={"delta": d})
    except Exception as e:
        return Health("mesh", ok=False, detail=f"error:{e}")


def defend_mesh(h: Health) -> None:
    """Re-siphon the mesh back to zero delta (the 'defend' act)."""
    from agent.siphon_mesh import SiphonMesh
    root = _OFFICE / "siphon_mesh"
    if root.is_dir():
        SiphonMesh(root=root, shards=["w1", "w2"]).equalize()


def probe_block() -> Health:
    """Hy3 block handshake must stay equal (delta 0)."""
    try:
        from agent.block_handshake import three_way_sync
        size = 383273  # observed Hy3 stream proxy size
        rep = three_way_sync(size, _OFFICE / "block_sync")
        return Health("block", ok=rep["equal"], detail=f"delta={rep['final_delta']}",
                      metrics={"final_delta": rep["final_delta"]})
    except Exception as e:
        return Health("block", ok=False, detail=f"error:{e}")


def defend_block(h: Health) -> None:
    from agent.block_handshake import three_way_sync
    three_way_sync(383273, _OFFICE / "block_sync")


def probe_node() -> Health:
    """Learning node must be accruing ticks (alive + learning)."""
    p = _OFFICE / "learning_node_status.json"
    if not p.is_file():
        return Health("node", ok=False, detail="no status file (not running)")
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
        ticks = st.get("ticks", 0)
        err = st.get("harvest_error") or st.get("fatal_tick_error")
        return Health("node", ok=(err is None and ticks > 0),
                      detail=f"ticks={ticks} err={err}",
                      metrics={"ticks": ticks})
    except Exception as e:
        return Health("node", ok=False, detail=f"parse-error:{e}")


def defend_node(h: Health) -> None:
    """Restart the autonomous learning node if it died."""
    from agent.learning_node import LearningNode
    LearningNode(office=_OFFICE, cadence=60.0).start()


def build_full_option_survival() -> Survival:
    """Wire the Full-Option component set with GOOD/BAD/LATE handling."""
    s = Survival()
    s.watch("mesh", probe_mesh, defend_mesh)
    s.watch("block", probe_block, defend_block)
    s.watch("node", probe_node, defend_node)
    return s
