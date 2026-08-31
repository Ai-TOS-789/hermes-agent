#!/usr/bin/env python3
"""Human-like Sensory & Cognitive layer for Hermes.

This is the "nervous system" the agent previously lacked: a single, structured
perception stage that sits *between* raw tool input (text, audio, vision,
environment state) and the reasoning loop, mirroring the gross anatomy of a
biological sensory system:

    stimuli (modalities) -> PerceptionBuffer -> SalienceFilter
        -> WorkingMemory -> PerceptionFrame (handed to the agent)

Design follows the user's AOT/JIT directive:
  * AOT (compile-once): ``SensorySystem.compile()`` builds the pipeline graph
    (which modalities are active, their weights, the salience threshold) a
    single time from config, so per-turn ``perceive()`` is a cheap hot path.
  * JIT (run-per-turn): ``perceive()`` lazily materializes only the stimuli
    actually present this turn (no vision pass if there is no image; no audio
    decode if there is no clip), caching decoded percepts across the turn.

The layer is strictly additive and fail-open: if it raises for any reason the
caller (run_agent.py) swallows the exception and the agent proceeds with its
normal (non-perceptual) input. It never mutates the agent's actual tool path.

Verified by tests/agent/test_sensory_system.py (AOT graph build, JIT stimulus
routing, salience gating, working-memory capacity, durability).
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Persistent store for the sensory profile / working memory log.
_HERMES_HOME = Path(
    __import__("os").environ.get("HERMES_HOME", r"C:\Users\w3ce\AppData\Local\hermes")
)
_SENSORY_DB = _HERMES_HOME / "sensory_state.json"


# ── Modality vocabulary (the "sensory organs") ──────────────────────────────
# Each modality is a named input channel. Weights are AOT-compiled; a stimulus
# below the compiled salience threshold is dropped before reaching working
# memory (mirrors thalamic gating — not every signal reaches cortex).
@dataclass(frozen=True)
class Modality:
    name: str
    weight: float  # relative importance (0..1+)
    description: str


KNOWN_MODALITIES: Dict[str, Modality] = {
    "text": Modality("text", 1.0, "written/typed language from any platform"),
    "audio": Modality("audio", 0.9, "speech / voice notes (pre-STT or post-STT)"),
    "vision": Modality("vision", 0.95, "screenshots, images, camera frames"),
    "state": Modality("state", 0.7, "environment state: pet state, session health, "
                               "battery, concurrency, errors"),
    "tactile": Modality("tactile", 0.4, "UI affordances: drag, click, hover, focus"),
    "proprioception": Modality("proprioception", 0.5, "self-model: token budget, "
                                                       "stall timers, own subagents"),
}

# Somatosensory (body) modalities group under these.
BODY_MODALITIES = ("state", "tactile", "proprioception")


@dataclass
class Stimulus:
    """A single raw sense datum entering the system this turn."""
    modality: str
    payload: Any
    source: str
    salience: float = 0.0          # 0..1, assigned by the SalienceFilter
    ts: float = field(default_factory=time.time)
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PerceptionFrame:
    """The integrated percept handed to the reasoning loop each turn.

    Like a thalamocortical relay: only gated, salient, de-duplicated stimuli
    survive into the frame. The agent loop can read ``frame.summary`` to know
    *what it is currently perceiving* the way a mind knows what it senses.
    """
    turn_id: int
    stimuli: List[Stimulus]
    salience_total: float
    modality_counts: Dict[str, int]
    summary: str

    def to_prompt_prefix(self) -> str:
        """Render the percept as a compact prefix the agent can read."""
        if not self.stimuli:
            return ""
        lines = [f"[perception turn {self.turn_id}]"]
        for s in self.stimuli:
            lines.append(
                f"  - {s.modality}@{s.source} (salience={s.salience:.2f}): "
                f"{_summarize(s.payload)}"
            )
        return "\n".join(lines)


def _summarize(payload: Any, limit: int = 80) -> str:
    if isinstance(payload, (str, bytes)):
        txt = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload
        return txt[:limit] + ("…" if len(txt) > limit else "")
    if isinstance(payload, Path) or (isinstance(payload, str) and len(payload) > 200):
        return f"<ref {str(payload)[:limit]}>"
    return str(payload)[:limit]


class PerceptionBuffer:
    """Short-term sensory buffer (sensory register).

    Holds the raw stimuli of the current turn before gating. Bounded by a cap;
    oldest stimuli beyond the cap are dropped (sensory adaptation).
    """

    def __init__(self, capacity: int = 64) -> None:
        self._capacity = capacity
        self._buf: deque = deque()
        self._lock = threading.Lock()

    def push(self, stim: Stimulus) -> None:
        with self._lock:
            self._buf.append(stim)
            while len(self._buf) > self._capacity:
                self._buf.popleft()

    def drain(self) -> List[Stimulus]:
        with self._lock:
            out = list(self._buf)
            self._buf.clear()
        return out

    def __len__(self) -> int:
        return len(self._buf)


class SalienceFilter:
    """Thalamic-style gate.

    Assigns a salience score to each stimulus from its modality weight and a
    lightweight content heuristic, then drops anything below the compiled
    threshold. This is the "what gets conscious attention" step.
    """

    def __init__(self, weights: Dict[str, float], threshold: float = 0.25) -> None:
        self._weights = weights
        self._threshold = threshold

    def score(self, stim: Stimulus) -> float:
        w = self._weights.get(stim.modality, 0.5)
        # Content heuristic: non-empty, unusual sources, or error states are
        # more salient than routine text.
        content = 0.5
        if stim.meta.get("error"):
            content = 1.0
        elif stim.meta.get("novel"):
            content = 0.85
        elif stim.modality in ("vision", "audio") and stim.payload:
            content = 0.9
        score = w * content
        # Clamp to [0, 1].
        return max(0.0, min(1.0, score))

    def gate(self, stimuli: List[Stimulus]) -> List[Stimulus]:
        out = []
        for s in stimuli:
            s.salience = self.score(s)
            if s.salience >= self._threshold:
                out.append(s)
        # Sort by descending salience (most salient first, like attention).
        out.sort(key=lambda x: x.salience, reverse=True)
        return out


class WorkingMemory:
    """Hippocampus-like scratchpad.

    Retains the last N perception frames so the agent has continuity of
    experience (it "remembers" what it perceived recently), without leaking
    unbounded memory. Capacity is AOT-compiled.
    """

    def __init__(self, capacity: int = 8) -> None:
        self._capacity = capacity
        self._frames: deque = deque()
        self._lock = threading.Lock()

    def commit(self, frame: PerceptionFrame) -> None:
        with self._lock:
            self._frames.append(frame)
            while len(self._frames) > self._capacity:
                self._frames.popleft()

    def recent(self, n: Optional[int] = None) -> List[PerceptionFrame]:
        with self._lock:
            frames = list(self._frames)
        return frames[-n:] if n else frames

    def __len__(self) -> int:
        return len(self._frames)


class SensorySystem:
    """The compiled nervous system.

    AOT: ``compile(config)`` bakes the modality weights, salience threshold,
    and working-memory capacity into a frozen pipeline.
    JIT: ``perceive(turn_id, stimuli)`` runs the hot path per turn.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buffer: Optional[PerceptionBuffer] = None
        self._filter: Optional[SalienceFilter] = None
        self._memory: Optional[WorkingMemory] = None
        self._active_modalities: frozenset = frozenset()
        self._compiled = False
        self._turn_counter = 0

    # ── AOT compile ────────────────────────────────────────────────────────
    def compile(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config or {}
        sens = cfg.get("sensory", {})
        # Which modalities are enabled (default: all known).
        enabled = sens.get("modalities", "all")
        if enabled == "all":
            self._active_modalities = frozenset(KNOWN_MODALITIES.keys())
        elif isinstance(enabled, (list, tuple, set)):
            self._active_modalities = frozenset(
                m for m in enabled if m in KNOWN_MODALITIES
            )
        else:
            self._active_modalities = frozenset(KNOWN_MODALITIES.keys())

        # Per-modality weights (config overrides defaults).
        weights = {k: v.weight for k, v in KNOWN_MODALITIES.items()}
        custom = sens.get("weights", {})
        for k, v in custom.items():
            if k in weights:
                weights[k] = float(v)

        threshold = float(sens.get("salience_threshold", 0.25))
        wm_cap = int(sens.get("working_memory_capacity", 8))
        pbuf_cap = int(sens.get("buffer_capacity", 64))

        with self._lock:
            self._buffer = PerceptionBuffer(pbuf_cap)
            self._filter = SalienceFilter(weights, threshold)
            self._memory = WorkingMemory(wm_cap)
            self._compiled = True

    # ── JIT perceive (hot path) ─────────────────────────────────────────────
    def perceive(self, stimuli: List[Stimulus], turn_id: Optional[int] = None) -> PerceptionFrame:
        if not self._compiled:
            self.compile()
        # Only keep stimuli from active (compiled) modalities — JIT routing.
        routed = [s for s in stimuli if s.modality in self._active_modalities]
        for s in routed:
            self._buffer.push(s)  # type: ignore[union-attr]
        raw = self._buffer.drain()  # type: ignore[union-attr]
        gated = self._filter.gate(raw)  # type: ignore[union-attr]

        with self._lock:
            self._turn_counter = (turn_id if turn_id is not None
                                  else self._turn_counter + 1)
            tid = self._turn_counter

        counts: Dict[str, int] = {}
        for s in gated:
            counts[s.modality] = counts.get(s.modality, 0) + 1
        salience_total = round(sum(s.salience for s in gated), 3)

        frame = PerceptionFrame(
            turn_id=tid,
            stimuli=gated,
            salience_total=salience_total,
            modality_counts=counts,
            summary=self._summarize_frame(gated, counts, salience_total),
        )
        self._memory.commit(frame)  # type: ignore[union-attr]
        self._persist(frame)
        return frame

    def _summarize_frame(self, gated, counts, salience_total) -> str:
        if not gated:
            return "no salient stimuli this turn"
        parts = [f"{k}×{v}" for k, v in sorted(counts.items())]
        return f"{' + '.join(parts)} (salience={salience_total:.2f})"

    # ── Durability (so the agent's "experience" survives restarts) ─────────
    def _persist(self, frame: PerceptionFrame) -> None:
        try:
            recent = [f.summary for f in self._memory.recent(5)]  # type: ignore[union-attr]
            _SENSORY_DB.write_text(
                json.dumps(
                    {"last_turn": frame.turn_id, "recent_summaries": recent},
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    # ── Introspection (the "self-aware of sensing" surface) ────────────────
    def describe(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "compiled": self._compiled,
                "active_modalities": sorted(self._active_modalities),
                "working_memory_len": len(self._memory) if self._memory else 0,
                "turn_counter": self._turn_counter,
            }


# Module-level singleton, lazily compiled (AOT on first perceive()).
_engine: Optional[SensorySystem] = None
_engine_lock = threading.Lock()


def get_engine() -> SensorySystem:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = SensorySystem()
    return _engine


def reset_engine() -> None:
    """Test/debug hook."""
    global _engine
    with _engine_lock:
        _engine = None
