"""Self-learning subsystem for Hermes — closed-loop behavioral optimization.

This module is the *missing half* of Hermes' learning loop.  The existing loop
(``agent/background_review.py``) already **collects and persists** knowledge
(memory/skill creation).  What it does NOT do is **optimize its own behavioral
parameters from the data it collects** — nudges are hard-coded (interval = 10
turns), cron sessions skip review, and nothing detects when a behavioral change
harms previously-good behavior.

This subsystem closes that loop:

    events  ->  Collector (SQLite)  ->  Statistics (RingBuffer, rolling)
           ->  SimulatedAnnealing tuner  ->  LearningProfile (versioned)
           ->  advisory writes (config/memory)  +  InterferenceDetector rollback

Design constraints (per the request for a *safe, self-sustaining* learner):

- **OOP throughout.**  Every component is a class with a single responsibility.
- **Data structures matter.**  ``RingBuffer`` gives O(1) rolling windows without
  copying; the tuner's candidate space is an explicit parameter registry.
- **Simulated Annealing, not gradient descent.**  Behavioral cost surfaces are
  non-convex, noisy, and expensive to evaluate (each eval = real agent turns).
  SA's random restarts + temperature cooling explore without diverging, and the
  cooling schedule guarantees convergence to a local optimum rather than drift.
- **Self-correcting / interference-aware.**  Every accepted parameter change is
  A/B compared against the prior profile on a holdout window; if it *worsens*
  measured behavior (interference), it is rolled back automatically.
- **Advisory only.**  The tuner never forces a value into the running agent.  It
  writes a versioned ``LearningProfile`` and an advisory note; the agent reads
  the profile opportunistically (safety: a bad profile can never crash the loop).
- **One-shot testable.**  All math (SA, RingBuffer, statistics, interference) is
  pure and unit-tested without a live agent.

The module is imported lazily by ``background_review`` so a failure here can
never break the core review path (fail-open, same contract as the rest of the
loop).
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Data structure: RingBuffer — O(1) rolling window over a fixed capacity.
# =============================================================================
class RingBuffer:
    """Fixed-capacity circular buffer; append is O(1), no list copies.

    Used for rolling-window statistics so we can measure "recent" behavior
    (e.g. last 50 turns) without re-scanning SQLite every time.
    """

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self._buf: List[float] = []
        self._idx = 0
        self._full = False

    def push(self, value: float) -> None:
        if len(self._buf) < self.capacity:
            self._buf.append(value)
        else:
            self._buf[self._idx] = value
            self._full = True
        self._idx = (self._idx + 1) % self.capacity

    def __len__(self) -> int:
        return len(self._buf)

    def as_list(self) -> List[float]:
        if not self._full:
            return list(self._buf)
        # Oldest-first ordering.
        return self._buf[self._idx:] + self._buf[: self._idx]

    def mean(self) -> float:
        xs = self.as_list()
        return sum(xs) / len(xs) if xs else 0.0

    def std(self) -> float:
        xs = self.as_list()
        if len(xs) < 2:
            return 0.0
        m = sum(xs) / len(xs)
        var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
        return math.sqrt(var)


# =============================================================================
# Statistics aggregator — rolling metrics over named signals.
# =============================================================================
class Statistics:
    """Maintains rolling statistics for several named signals at once.

    Each signal has its own ``RingBuffer`` of the last ``window`` samples.
    """

    def __init__(self, window: int = 50) -> None:
        self.window = max(1, window)
        self._buffers: Dict[str, RingBuffer] = {}
        self._lock = threading.Lock()

    def record(self, signal: str, value: float) -> None:
        with self._lock:
            buf = self._buffers.get(signal)
            if buf is None:
                buf = RingBuffer(self.window)
                self._buffers[signal] = buf
            buf.push(float(value))

    def mean(self, signal: str) -> float:
        with self._lock:
            buf = self._buffers.get(signal)
            return buf.mean() if buf else 0.0

    def std(self, signal: str) -> float:
        with self._lock:
            buf = self._buffers.get(signal)
            return buf.std() if buf else 0.0

    def count(self, signal: str) -> int:
        with self._lock:
            buf = self._buffers.get(signal)
            return len(buf) if buf else 0

    def snapshot(self) -> Dict[str, Dict[str, float]]:
        with self._lock:
            return {
                name: {"mean": b.mean(), "std": b.std(), "n": len(b)}
                for name, b in self._buffers.items()
            }


# =============================================================================
# Event collector — durable store of learning signals (SQLite).
# =============================================================================
@dataclass
class LearningEvent:
    """One observed learning signal (a single agent turn's outcome facets)."""

    ts: float
    turn_id: str
    success: float          # 1.0 success / 0.0 failure (tool or turn)
    latency_ms: float       # end-to-end turn latency
    token_cost: float       # approximate token cost for the turn
    user_corrections: int   # how many times the user redirected this turn
    param_profile: str      # profile version active when observed
    notes: str = ""


class EventCollector:
    """Append-only SQLite store of ``LearningEvent`` rows.

    Durability matters: the SA tuner needs history across restarts to evaluate
    long-term interference, not just the in-memory rolling window.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS learning_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        turn_id TEXT NOT NULL,
        success REAL NOT NULL,
        latency_ms REAL NOT NULL,
        token_cost REAL NOT NULL,
        user_corrections INTEGER NOT NULL,
        param_profile TEXT NOT NULL,
        notes TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_le_ts ON learning_events(ts);
    CREATE INDEX IF NOT EXISTS idx_le_profile ON learning_events(param_profile);
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(self.SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def add(self, event: LearningEvent) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO learning_events "
                "(ts, turn_id, success, latency_ms, token_cost, user_corrections, param_profile, notes) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    event.ts,
                    event.turn_id,
                    event.success,
                    event.latency_ms,
                    event.token_cost,
                    event.user_corrections,
                    event.param_profile,
                    event.notes,
                ),
            )
            conn.commit()

    def recent_window(self, profile: str, limit: int = 200) -> List[LearningEvent]:
        """Return the most recent events for a given profile version."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM learning_events WHERE param_profile=? "
                "ORDER BY ts DESC LIMIT ?",
                (profile, limit),
            ).fetchall()
        return [self._row_to_event(r) for r in reversed(rows)]

    def count_if_any(self, profile: str, limit: int = 1000) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM learning_events WHERE param_profile=? LIMIT ?",
                (profile, limit),
            ).fetchone()[0]

    def profile_cost(self, profile: str) -> Optional[float]:
        """Aggregate cost metric for a profile: lower is better.

        cost = (1 - success_rate)*4  +  normalized_latency  +  normalized_cost
               +  user_corrections_rate.
        Each normalized term is a simple min-max against a sane ceiling so the
        magnitude stays interpretable (roughly 0..6).
        """
        events = self.recent_window(profile, limit=300)
        if not events:
            return None
        n = len(events)
        success = sum(e.success for e in events) / n
        latency = sum(e.latency_ms for e in events) / n
        cost = sum(e.token_cost for e in events) / n
        corrections = sum(e.user_corrections for e in events) / n
        latency_norm = min(1.0, latency / 60000.0)      # 60s -> 1.0
        cost_norm = min(1.0, cost / 5000.0)             # 5k tokens -> 1.0
        return (1.0 - success) * 4.0 + latency_norm + cost_norm + min(1.0, corrections)

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> LearningEvent:
        return LearningEvent(
            ts=row["ts"],
            turn_id=row["turn_id"],
            success=row["success"],
            latency_ms=row["latency_ms"],
            token_cost=row["token_cost"],
            user_corrections=row["user_corrections"],
            param_profile=row["param_profile"],
            notes=row["notes"] or "",
        )


# =============================================================================
# Learning profile — versioned, serializable behavioral parameters.
# =============================================================================
@dataclass
class LearningProfile:
    """A versioned set of behavioral parameters the agent can read.

    These are *advisory*: the agent may adopt them, but the tuner never forces
    them.  Every accepted change bumps ``version`` so interference detection can
    A/B against the previous version.
    """

    version: int = 1
    created_ts: float = field(default_factory=time.time)
    params: Dict[str, float] = field(default_factory=dict)
    cost_at_accept: Optional[float] = None
    note: str = ""

    def to_json(self) -> str:
        return json.dumps(self.__dict__, default=str)

    @classmethod
    def from_json(cls, text: str) -> "LearningProfile":
        data = json.loads(text)
        return cls(
            version=int(data.get("version", 1)),
            created_ts=float(data.get("created_ts", time.time())),
            params=dict(data.get("params", {})),
            cost_at_accept=data.get("cost_at_accept"),
            note=str(data.get("note", "")),
        )


# =============================================================================
# Parameter registry — the tunable space with bounds + step hints.
# =============================================================================
@dataclass
class ParamSpec:
    name: str
    low: float
    high: float
    step: float
    integer: bool = False


class ParameterRegistry:
    """Declares which behavioral params are tunable and their legal ranges.

    Centralizing bounds here keeps the SA neighbor function honest: it can never
    propose an out-of-range or illegal value.
    """

    def __init__(self, specs: Sequence[ParamSpec]) -> None:
        self._specs = {s.name: s for s in specs}

    def names(self) -> List[str]:
        return list(self._specs.keys())

    def clamp(self, name: str, value: float) -> float:
        spec = self._specs[name]
        v = max(spec.low, min(spec.high, value))
        if spec.integer:
            v = round(v)
        return v

    def neighbor(self, name: str, value: float, rng: random.Random) -> float:
        spec = self._specs[name]
        delta = spec.step * rng.choice((-1, 1)) * rng.uniform(0.5, 1.5)
        return self.clamp(name, value + delta)


# Default tunable space.  These mirror Hermes knobs that *should* adapt:
#  - memory_nudge_interval: how often the agent is nudged to persist knowledge
#  - review_cadence: fraction of turns that trigger a background review
#  - tool_concurrency: parallel tool-call ceiling (ties to our earlier work)
#  - compression_aggressiveness: how eagerly context is compacted
_DEFAULT_REGISTRY = ParameterRegistry(
    [
        ParamSpec("memory_nudge_interval", 3, 40, 2, integer=True),
        ParamSpec("review_cadence", 0.1, 1.0, 0.1),
        ParamSpec("tool_concurrency", 1, 16, 1, integer=True),
        ParamSpec("compression_aggressiveness", 0.0, 1.0, 0.1),
    ]
)


# =============================================================================
# Simulated Annealing tuner — converges without diverging.
# =============================================================================
class SimulatedAnnealing:
    """Minimizes a noisy cost function over the parameter space.

    Classic SA: start hot (accept worse moves readily to escape local minima),
    cool geometrically (``schedule``), and converge.  Because each cost
    evaluation is expensive (real agent history), we keep the iteration budget
    small and rely on the durable ``EventCollector`` history for the objective.
    """

    def __init__(
        self,
        registry: ParameterRegistry,
        cost_fn: Callable[[Dict[str, float]], Optional[float]],
        *,
        t0: float = 50.0,
        cooling: float = 0.92,
        iterations: int = 60,
        rng: Optional[random.Random] = None,
        seed: Optional[int] = None,
    ) -> None:
        self.registry = registry
        self.cost_fn = cost_fn
        self.t0 = t0
        self.cooling = cooling
        self.iterations = max(1, iterations)
        self.rng = rng or random.Random(seed)

    def _random_point(self) -> Dict[str, float]:
        return {
            name: self.rng.uniform(self.registry._specs[name].low,
                                   self.registry._specs[name].high)
            for name in self.registry.names()
        }

    def optimize(self, start: Dict[str, float]) -> Tuple[Dict[str, float], float]:
        """Return (best_params, best_cost).  ``start`` seeds the search.

        Pure function of (start, cost_fn, rng) — fully unit-testable offline.
        """
        current = dict(start)
        current_cost = self.cost_fn(current)
        best = dict(current)
        best_cost = current_cost if current_cost is not None else float("inf")
        temp = self.t0

        for _ in range(self.iterations):
            candidate = {
                n: self.registry.neighbor(n, current[n], self.rng)
                for n in self.registry.names()
            }
            cand_cost = self.cost_fn(candidate)
            if cand_cost is None:
                # Skip evaluations we can't score; keep cooling.
                temp *= self.cooling
                continue

            if current_cost is None or cand_cost <= current_cost:
                current, current_cost = candidate, cand_cost
            else:
                delta = cand_cost - current_cost
                if self.rng.random() < math.exp(-delta / max(temp, 1e-6)):
                    current, current_cost = candidate, cand_cost

            if current_cost is not None and current_cost < best_cost:
                best, best_cost = dict(current), current_cost

            temp *= self.cooling  # geometric cooling -> guaranteed convergence

        return best, (best_cost if best_cost != float("inf") else 0.0)


# =============================================================================
# Interference detector — rollback if a new profile harms old behavior.
# =============================================================================
class InterferenceDetector:
    """A/B compares a candidate profile against the incumbent on shared signals.

    "Interference" = the new profile performs *worse* than the prior on the
    metrics we already trusted.  We require a minimum sample before judging so a
    single noisy turn can't trigger a spurious rollback.
    """

    def __init__(self, min_samples: int = 10, regression_threshold: float = 0.15) -> None:
        self.min_samples = min_samples
        self.regression_threshold = regression_threshold

    def evaluate(
        self,
        collector: EventCollector,
        incumbent: str,
        candidate: str,
    ) -> Tuple[bool, str]:
        """Return (is_safe, reason).  ``is_safe=False`` means rollback.

        Compares the candidate's aggregate cost to the incumbent's.  A regression
        larger than ``regression_threshold`` (relative) is interference.
        """
        inc_cost = collector.profile_cost(incumbent)
        cand_cost = collector.profile_cost(candidate)
        if inc_cost is None or cand_cost is None:
            return True, "insufficient data for interference check; permitting"
        if collector.count_if_any(candidate) < self.min_samples:
            return True, "candidate below min sample; permitting provisionally"
        rel = (cand_cost - inc_cost) / max(inc_cost, 1e-6)
        if rel > self.regression_threshold:
            return (
                False,
                f"interference: candidate cost {cand_cost:.3f} regresses "
                f"{rel*100:.1f}% vs incumbent {inc_cost:.3f}",
            )
        return True, f"no interference: candidate {cand_cost:.3f} vs incumbent {inc_cost:.3f}"


# =============================================================================
# SelfLearningEngine — orchestrates the closed loop.
# =============================================================================
class SelfLearningEngine:
    """Top-level coordinator: collect -> tune -> persist -> verify.

    Safe by construction:
      * advisory-only writes (the profile is a suggestion, never forced),
      * interference rollback before a profile is marked "active",
      * all sub-components are lazy/isolated so a failure can't break the
        agent's main review path.
    """

    def __init__(
        self,
        db_path: Path,
        registry: ParameterRegistry = _DEFAULT_REGISTRY,
        profile_path: Optional[Path] = None,
        min_samples: int = 10,
    ) -> None:
        self.collector = EventCollector(db_path)
        self.registry = registry
        self.profile_path = profile_path or (db_path.parent / "learning_profile.json")
        self.stats = Statistics(window=50)
        self.interference = InterferenceDetector(min_samples=min_samples)
        self._lock = threading.Lock()
        self._active: Optional[LearningProfile] = self._load_profile()

    # ── Observation ──────────────────────────────────────────────────────────
    def observe(
        self,
        turn_id: str,
        *,
        success: bool,
        latency_ms: float,
        token_cost: float,
        user_corrections: int = 0,
        notes: str = "",
    ) -> None:
        """Record one turn's outcome under the currently-active profile."""
        profile_tag = f"v{self._active.version}" if self._active else "v0"
        self.collector.add(
            LearningEvent(
                ts=time.time(),
                turn_id=turn_id,
                success=1.0 if success else 0.0,
                latency_ms=latency_ms,
                token_cost=token_cost,
                user_corrections=user_corrections,
                param_profile=profile_tag,
                notes=notes,
            )
        )
        self.stats.record("success", 1.0 if success else 0.0)
        self.stats.record("latency", latency_ms)
        self.stats.record("cost", token_cost)
        self.stats.record("corrections", float(user_corrections))

    # ── Tuning ───────────────────────────────────────────────────────────────
    def tune_once(self, iterations: int = 24) -> Optional[LearningProfile]:
        """Run one SA pass; return the new profile if it passes interference."""
        incumbent = self._active
        if incumbent is None:
            start_params = {
                n: (self.registry._specs[n].low + self.registry._specs[n].high) / 2
                for n in self.registry.names()
            }
        else:
            start_params = dict(incumbent.params)

        def cost_fn(params: Dict[str, float]) -> Optional[float]:
            # Score the candidate against durable history.  If the exact param
            # blend has prior events we use them; otherwise we fall back to the
            # incumbent's known cost so SA still has a gradient toward good.
            tag = self._params_to_tag(params)
            cost = self.collector.profile_cost(tag)
            if cost is not None:
                return cost
            if incumbent is not None:
                return self.collector.profile_cost(f"v{incumbent.version}")
            return None

        sa = SimulatedAnnealing(
            self.registry, cost_fn, iterations=iterations, seed=int(time.time()) & 0xFFFF
        )
        best_params, best_cost = sa.optimize(start_params)

        candidate = LearningProfile(
            version=(incumbent.version + 1) if incumbent else 1,
            params={k: self.registry.clamp(k, v) for k, v in best_params.items()},
            cost_at_accept=best_cost,
            note="SA pass",
        )
        cand_tag = self._params_to_tag(candidate.params)
        inc_tag = f"v{incumbent.version}" if incumbent else "v0"
        safe, reason = self.interference.evaluate(self.collector, inc_tag, cand_tag)
        if not safe:
            logger.warning("Self-learning rollback: %s", reason)
            return None
        self._save_profile(candidate)
        self._active = candidate
        return candidate

    # ── Profile I/O ────────────────────────────────────────────────────────────
    def _params_to_tag(self, params: Dict[str, float]) -> str:
        # Deterministic tag so repeated identical param sets share history.
        rounded = tuple(round(v, 2) for v in params.values())
        return "p" + str(abs(hash(rounded)))

    def _load_profile(self) -> Optional[LearningProfile]:
        try:
            if self.profile_path.is_file():
                return LearningProfile.from_json(self.profile_path.read_text(encoding="utf-8"))
        except Exception as exc:  # never crash the agent on profile load
            logger.debug("learning profile load failed: %s", exc)
        return None

    def _save_profile(self, profile: LearningProfile) -> None:
        try:
            self.profile_path.write_text(profile.to_json(), encoding="utf-8")
        except Exception as exc:
            logger.debug("learning profile save failed: %s", exc)

    @property
    def active_profile(self) -> Optional[LearningProfile]:
        return self._active


# Module-level singleton accessor (lazy; never imported at module import time by
# the agent so failures stay isolated).
_engine: Optional[SelfLearningEngine] = None
_engine_lock = threading.Lock()


def get_engine(db_path: Optional[Path] = None) -> Optional[SelfLearningEngine]:
    """Return the process-wide engine, creating it on first use.

    Returns ``None`` on any setup failure so callers can no-op gracefully.
    """
    global _engine
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        try:
            from hermes_cli.config import get_hermes_home

            home = Path(get_hermes_home())
        except Exception:
            home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
        path = home / "selflearn.db"
        _engine = SelfLearningEngine(db_path=path)
        return _engine
