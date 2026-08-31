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
                # No external stream wired: do NOT fabricate a fake shard every tick
                # (that just spams the pyramid). Instead, harvest only if a real
                # self-dialogue source file exists, else skip silently.
                from agent.model_forge import harvest_self_dialogue
                src = self.office / "self_dialogue.jsonl"
                if src.is_file():
                    turns = [json.loads(l) for l in src.read_text(encoding="utf-8").splitlines() if l.strip()]
                    if turns:
                        report["harvested"] = harvest_self_dialogue(turns)
                else:
                    report["harvested"] = 0
        except Exception as e:  # noqa: BLE001
            report["harvest_error"] = str(e)
        # 1b) SELF-EXTEND: synthesize a +1 meta/insight from recent 0/-1 shards, AND
        #     auto-cross-link it back to the shards it was derived from (+ connect 0
        #     core facts to related prior facts). The node grows +1/0/-1 *and* wires
        #     the pyramid together on its own — no human prompt required.
        try:
            from agent.model_forge import PyramidStore
            store = PyramidStore()
            # Wire a real Hy3 summarizer if the runtime exposes one; otherwise the
            # compact fallback (recent prompt/answer concat) is used. This makes the
            # +1 shard real learning rather than a bare count.
            summarize_fn = self._hy3_summarize() if self._hy3_summarize() else None
            synth = self._synthesize_and_link(store, summarize_fn=summarize_fn)
            report["synthesized"] = synth
        except Exception as e:  # noqa: BLE001
            report["synthesize_error"] = str(e)
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
        # 4) Option-Skills discovery — suggest (never auto-install) skills the repo
        #    ships but the session hasn't activated yet. Report-only, guardrail-gated.
        try:
            from agent.option_skills import discover_once
            rep = discover_once()
            report["option_skills"] = {
                "suggestions": rep.get("suggestions", {}),
                "active_count": sum(len(v) for v in rep.get("active", {}).values()),
            }
        except Exception as e:  # noqa: BLE001
            report["option_skills_error"] = str(e)
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

    def _synthesize_and_link(self, store: "object", summarize_fn=None) -> dict:
        """Self-extend the pyramid on the node's own initiative.

        - Adds a 0 core fact from the current tick.
        - Synthesizes a +1 meta/insight. If ``summarize_fn`` is supplied (e.g. a Hy3
          call), it produces a NATURAL-LANGUAGE summary of recent 0/-1 shards so the
          +1 shard is real learning, not just a count. Without it, a compact summary
          of recent prompts/answers is used.
        - Auto-links the +1 insight back to recent 0/-1 shards it summarizes.
        - Auto-links the new 0 fact to related prior 0/-1 facts by keyword-cluster
          overlap (semantic-ish, no embeddings needed).
        Returns a summary of what was added/linked.
        """
        added = {}
        counts = {l: store.count(l) for l in ("+1", "0", "-1")}
        # Gather recent 0/-1 content to summarize into a +1 insight.
        recent = []
        for lvl in ("0", "-1"):
            for p in list(store.iter_paths(lvl))[-5:]:
                try:
                    rec = json.loads(Path(p).read_text(encoding="utf-8"))
                    recent.append(f"[{lvl}] {rec.get('prompt','')[:120]} -> {rec.get('answer','')[:120]}")
                except Exception:
                    continue
        if summarize_fn is not None and recent:
            try:
                summary = summarize_fn(recent)
            except Exception:
                summary = None
        else:
            summary = None
        if not summary:
            summary = (f"levels: +1={counts['+1']} 0={counts['0']} -1={counts['-1']}; "
                       f"recent: {' | '.join(recent[-3:])}" if recent else
                       f"levels: +1={counts['+1']} 0={counts['0']} -1={counts['-1']}")
        meta_path = store.add("+1", {
            "type": "synthesized-insight",
            "topic": f"autonomous-tick-{self.ticks}",
            "prompt": "what does the recent pyramid say?",
            "answer": summary,
            "ts": int(time.time()),
        })
        # Cross-link +1 back to the most recent 0/-1 shards it summarizes.
        link_n = 0
        for lvl in ("0", "-1"):
            for p in list(store.iter_paths(lvl))[-3:]:
                if store.link(meta_path, str(p)):
                    link_n += 1
        # 0 core fact: a crisp self-dialogue turn derived from this tick.
        fact_path = store.add("0", {
            "topic": f"node-fact-{self.ticks}",
            "prompt": f"autonomous node observation #{self.ticks}",
            "answer": f"node extended pyramid; +1 insight +{link_n} back-links",
            "ts": int(time.time()),
        })
        # Auto-link the new 0 fact to related prior 0/-1 facts (self-connection).
        auto_n = store.auto_link_recent(fact_path, "0", max_links=3)
        auto_n += store.auto_link_recent(fact_path, "-1", max_links=2)
        added = {
            "plus1": meta_path,
            "plus1_backlinks": link_n,
            "zero": fact_path,
            "zero_autolinks": auto_n,
            "counts": counts,
            "summary": summary[:200],
        }
        return added


    def _hy3_summarize(self):
        """Return a Hy3-backed summarizer callable, or None if unavailable.

        Best-effort: if the runtime can call the live model, summarize recent shards
        into a natural-language +1 insight. Otherwise return None and the node falls
        back to the compact concat summary (still real data, no fabrication).
        """
        try:
            from hermes_tools import chat_completion
            def _sum(recent_lines):
                prompt = ("Summarize the following agent observations into one concise "
                          "insight (1-2 sentences):\n" + "\n".join(recent_lines))
                try:
                    resp = chat_completion(prompt, max_tokens=120)
                    return resp.strip()
                except Exception:
                    return None
            return _sum
        except Exception:
            return None


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


# ── Multi-process worker harness (Phase 0 of the Desktop/Option-Skills plan) ──
import multiprocessing as _mp


def run_worker(func, *args, office=None):
    """Spawn ``func(*args)`` as a supervised, daemonized child PROCESS.

    Used so the Option-Skills conductor, research loop, and discovery loop each run
    in their own process (multi-process isolation) under the learning node's wing.
    Returns the live Process so callers can join/wait if needed.
    """
    p = _mp.Process(target=func, args=args, name="hermes-worker", daemon=True)
    p.start()
    return p


def run_supervisor_processes(office=None, cadence=60.0):
    """Conductor entry: launch the discover + research workers as separate processes."""
    from agent import option_skills as _osk
    root = office or _OFFICE
    procs = [
        run_worker(_osk.discover_loop, root, cadence),
        run_worker(_osk.research_loop, root, cadence),
    ]
    return procs
