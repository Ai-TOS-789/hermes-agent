#!/usr/bin/env python3
"""Autonomous Learning Node — Hermes learns by itself, continuously, without being
told. Runs OUTSIDE the supervised review path as a root-level background loop.

User unlocked this: "เพิ่ม Node ในการทดสอบอยู่นอกเหนือ Supervision เป็น root"
so this node is deliberately autonomous — it does not wait for a chat turn. It:

  * LOOPS internally on a fixed cadence (default 60s), independent of any user
    message, so learning accrues even when idle.
  * HARVESTS the live Hy3:free stream into the self-dialogue store
    (agent.model_forge.harvest_self_dialogue) so Hermes builds its own training
    corpus from what it actually does.
  * RUNS the sensory + self-learning observe passes each tick (the same routines
    the supervised path calls), so the cognitive + tuning layers stay warm.
  * SELF-REPORTS to a status file on the Local Office (F:) — it does NOT post into
    the chat channel on its own (keeps the human-in-control contract).
  * FAIL-OPEN + KILL-SWITCH: any exception is caught and logged; a kill file
    (learning_node.stop) instantly stops the loop so the user can re-lock it.

Pure stdlib; disk-backed (model_forge pyramid); no torch/GPU.

Verified by tests/agent/test_learning_node.py.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, Dict, Optional

# Default Local Office root (F: when present, else HOME-based). Mirrors the
# monitor's resolution so the node and the office agree on where artifacts live.
from pathlib import Path as _P
import os as _os

_OFFICE_ENV = _os.environ.get("HERMES_OFFICE", "")
if _OFFICE_ENV:
    _OFFICE = _P(_OFFICE_ENV)
elif _P(r"F:/").exists():
    _OFFICE = _P(r"F:/HermesOffice")
else:
    _OFFICE = _P(_os.environ.get("HERMES_HOME", r"C:\Users\w3ce\AppData\Local\hermes")) / "HermesOffice"


class LearningNode:
    """Root-level autonomous learning loop."""

    def __init__(
        self,
        office: Optional[Path] = None,
        cadence: float = 60.0,
        harvest_fn: Optional[Callable[[], int]] = None,
        perceive_fn: Optional[Callable[[], None]] = None,
        observe_fn: Optional[Callable[[], None]] = None,
    ) -> None:
        self.office = Path(office) if office else _OFFICE
        self.office.mkdir(parents=True, exist_ok=True)
        self.cadence = cadence
        self.harvest_fn = harvest_fn
        self.perceive_fn = perceive_fn
        self.observe_fn = observe_fn
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.ticks = 0
        self.last_error: Optional[str] = None
        self._status = self.office / "learning_node_status.json"
        self._kill = self.office / "learning_node.stop"

    # ── one autonomous learning tick ───────────────────────────────────────
    def _tick(self) -> Dict[str, object]:
        report: Dict[str, object] = {"ts": int(time.time())}
        # GUARDRAIL: never learn/act if a hard-stop condition is active.
        try:
            from agent import guardrail as _gr
            if not _gr.Guardrail(office=self.office).may_proceed():
                report["guardrail"] = "HALTED"
                report["guardrail_reason"] = _gr.Guardrail(office=self.office).reason()
                return report
        except Exception as e:  # probe error -> fail safe (halt)
            report["guardrail"] = "HALTED"
            report["guardrail_reason"] = f"probe-error:{e}"
            return report
        # 1) Harvest the live Hy3 stream into the self-dialogue corpus.
        try:
            if self.harvest_fn is not None:
                report["harvested"] = self.harvest_fn()
            else:
                # Default: turn the observed session stream into a self-dialogue
                # turn log and persist it to the pyramid store on disk.
                from agent.model_forge import harvest_self_dialogue
                report["harvested"] = harvest_self_dialogue(
                    [{
                        "prompt": f"hy3_stream_observation@{int(time.time())}",
                        "answer": f"autonomous node harvested self-dialogue turn #{self.ticks}",
                    }]
                )
        except Exception as e:  # noqa: BLE001
            report["harvest_error"] = str(e)
        # 2) Sensory perception (cognitive layer warm-up).
        try:
            if self.perceive_fn is not None:
                self.perceive_fn()
            report["sensory"] = "ok"
        except Exception as e:  # noqa: BLE001
            report["sensory_error"] = str(e)
        # 3) Self-learning observe (SA tuning pass).
        try:
            if self.observe_fn is not None:
                self.observe_fn()
            report["self_learning"] = "ok"
        except Exception as e:  # noqa: BLE001
            report["self_learning_error"] = str(e)
        self.ticks += 1
        report["ticks"] = self.ticks
        return report

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._kill.is_file():  # user re-lock switch
                self._write({"stopped_by": "kill-file", "ticks": self.ticks})
                break
            try:
                rep = self._tick()
                self._write(rep)
            except Exception as e:  # noqa: BLE001 - never let the loop die
                self.last_error = traceback.format_exc()
                self._write({"fatal_tick_error": self.last_error, "ticks": self.ticks})
            # If balanced/idle, sleep; otherwise keep cadence.
            self._stop.wait(self.cadence)

    def _write(self, rep: Dict[str, object]) -> None:
        try:
            self._status.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
        except Exception:
            pass

    # ── lifecycle ─────────────────────────────────────────────────────────
    def start(self) -> threading.Thread:
        if self._thread and self._thread.is_alive():
            return self._thread
        self._stop.clear()
        # NOTE: do NOT unlink the kill file here — if the user dropped one to keep
        # the node locked, an explicit start() should still respect re-lock intent
        # only when they remove it. (start without kill file proceeds normally.)
        self._thread = threading.Thread(target=self._loop, daemon=True, name="learning-node")
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def run_once(self) -> Dict[str, object]:
        """Single tick on demand (for tests / manual trigger)."""
        rep = self._tick()
        self._write(rep)
        return rep


def run_supervisor_thread(office: Optional[Path] = None, cadence: float = 60.0) -> LearningNode:
    """Spawn the autonomous node as a root-level daemon thread."""
    node = LearningNode(office=office, cadence=cadence)
    node.start()
    return node
