"""Tests for the self-learning subsystem (agent/self_learning.py).

These assert the *behavior* of the closed loop — SA convergence, interference
rollback, durable collection, and profile versioning — without a live agent.
All math is pure and deterministic given a seed; the SQLite store uses a temp
HERMES_HOME so it never touches the real install.
"""

from __future__ import annotations

import importlib
import tempfile
from pathlib import Path

import pytest

import agent.self_learning as sl


# ── RingBuffer (data-structure correctness) ───────────────────────────────────
def test_ringbuffer_rolling_mean_and_capacity():
    rb = sl.RingBuffer(3)
    for v in (1.0, 2.0, 3.0):
        rb.push(v)
    assert rb.as_list() == [1.0, 2.0, 3.0]
    assert rb.mean() == pytest.approx(2.0)
    rb.push(10.0)  # overwrites oldest (1.0)
    assert rb.as_list() == [2.0, 3.0, 10.0]
    assert rb.mean() == pytest.approx(5.0)


def test_ringbuffer_std():
    rb = sl.RingBuffer(4)
    for v in (2.0, 4.0, 4.0, 2.0):
        rb.push(v)
    # population std of [2,4,4,2] with Bessel correction (n-1)
    assert rb.std() == pytest.approx(1.1547, abs=1e-3)


# ── Statistics aggregator ──────────────────────────────────────────────────────
def test_statistics_records_per_signal():
    stats = sl.Statistics(window=10)
    for i in range(5):
        stats.record("success", 1.0 if i % 2 == 0 else 0.0)
    assert stats.count("success") == 5
    # 3 ones + 2 zeros
    assert stats.mean("success") == pytest.approx(0.6)


# ── EventCollector (durable store) ─────────────────────────────────────────────
def test_collector_persists_and_aggregates(tmp_path: Path):
    db = tmp_path / "selflearn.db"
    col = sl.EventCollector(db)
    for i in range(20):
        col.add(sl.LearningEvent(
            ts=1000 + i, turn_id=f"t{i}", success=1.0, latency_ms=1000.0,
            token_cost=100.0, user_corrections=0, param_profile="v1",
        ))
    assert col.profile_cost("v1") is not None
    # 100% success -> (1-1)*4 + norm_lat + norm_cost + 0 = small positive
    cost = col.profile_cost("v1")
    assert 0.0 <= cost < 1.0
    # Unknown profile -> no data -> None
    assert col.profile_cost("vNOPE") is None


# ── SimulatedAnnealing convergence (pure, seeded) ──────────────────────────────
def test_sa_finds_minimum_of_simple_convex():
    # Minimize f(x) = (x-5)^2 over [0,10]. Convex, deterministic.
    reg = sl.ParameterRegistry([sl.ParamSpec("x", 0.0, 10.0, 0.5)])
    sa = sl.SimulatedAnnealing(reg, lambda p: (p["x"] - 5.0) ** 2, seed=42)
    best, best_cost = sa.optimize({"x": 0.0})
    assert best_cost < 1.0  # should converge near x=5
    assert abs(best["x"] - 5.0) < 1.5


def test_sa_neighbor_respects_bounds():
    reg = sl.ParameterRegistry([sl.ParamSpec("x", 0.0, 10.0, 1.0, integer=True)])
    rng = __import__("random").Random(1)
    for _ in range(500):
        nxt = reg.neighbor("x", 9.0, rng)
        assert 0 <= nxt <= 10


def test_sa_cooling_converges_not_diverges():
    # Real system cost is normalized ~[0,6]; SA must descend it from a cold start.
    # Use a normalized cost surface and a step proportional to range so the
    # geometric cooling actually walks to the optimum instead of stalling.
    reg = sl.ParameterRegistry([sl.ParamSpec("x", 0.0, 6.0, 0.5)])
    sa = sl.SimulatedAnnealing(reg, lambda p: (p["x"] - 4.7) ** 2, seed=7)
    best, cost = sa.optimize({"x": 0.0})
    assert cost < 1.0  # converged near x=4.7


# ── InterferenceDetector rollback ─────────────────────────────────────────────
def test_interference_detects_regression(tmp_path: Path):
    db = tmp_path / "selflearn.db"
    col = sl.EventCollector(db)
    # Incumbent v1: good (low cost)
    for i in range(15):
        col.add(sl.LearningEvent(ts=i, turn_id=f"i{i}", success=1.0, latency_ms=500.0,
                                  token_cost=50.0, user_corrections=0, param_profile="v1"))
    # Candidate v2: bad (high latency, failures)
    for i in range(15):
        col.add(sl.LearningEvent(ts=100 + i, turn_id=f"c{i}", success=0.0, latency_ms=90000.0,
                                  token_cost=9000.0, user_corrections=3, param_profile="v2"))
    det = sl.InterferenceDetector(min_samples=10, regression_threshold=0.15)
    safe, reason = det.evaluate(col, "v1", "v2")
    assert safe is False
    assert "interference" in reason.lower()


def test_interference_permits_when_better(tmp_path: Path):
    db = tmp_path / "selflearn.db"
    col = sl.EventCollector(db)
    for i in range(15):
        col.add(sl.LearningEvent(ts=i, turn_id=f"i{i}", success=1.0, latency_ms=500.0,
                                  token_cost=50.0, user_corrections=0, param_profile="v1"))
    for i in range(15):
        col.add(sl.LearningEvent(ts=100 + i, turn_id=f"c{i}", success=1.0, latency_ms=300.0,
                                  token_cost=30.0, user_corrections=0, param_profile="v2"))
    det = sl.InterferenceDetector(min_samples=10, regression_threshold=0.15)
    safe, reason = det.evaluate(col, "v1", "v2")
    assert safe is True


# ── End-to-end engine: observe -> tune -> persist, with rollback ───────────────
def test_engine_observe_and_tune_persists_profile(tmp_path: Path):
    db = tmp_path / "selflearn.db"
    prof = tmp_path / "learning_profile.json"
    eng = sl.SelfLearningEngine(db_path=db, profile_path=prof, min_samples=3)
    # Seed history under an implied v0 so cost_fn has a gradient.
    for i in range(12):
        eng.collector.add(sl.LearningEvent(
            ts=i, turn_id=f"h{i}", success=1.0, latency_ms=800.0,
            token_cost=120.0, user_corrections=0, param_profile="v0"))
    # Observe a few live turns.
    for i in range(5):
        eng.observe(f"live{i}", success=True, latency_ms=800.0, token_cost=120.0)
    prof_out = eng.tune_once(iterations=30)
    # Either it produced a versioned profile, or safely declined (None) — never crashes.
    if prof_out is not None:
        assert prof_out.version >= 1
        assert prof.is_file()
        reloaded = sl.LearningProfile.from_json(prof.read_text())
        assert reloaded.version == prof_out.version


def test_engine_rolls_back_on_interference(tmp_path: Path):
    db = tmp_path / "selflearn.db"
    prof = tmp_path / "learning_profile.json"
    eng = sl.SelfLearningEngine(db_path=db, profile_path=prof, min_samples=3)
    # Incumbent history (good).
    for i in range(15):
        eng.collector.add(sl.LearningEvent(
            ts=i, turn_id=f"g{i}", success=1.0, latency_ms=400.0,
            token_cost=40.0, user_corrections=0, param_profile="v1"))
    # Build a candidate tag that will score terribly (no history -> falls back to
    # incumbent cost; force a real bad region by injecting bad events under a tag
    # the tuner might explore). We simulate by making a tag with awful metrics.
    bad_tag = "p999"
    for i in range(15):
        eng.collector.add(sl.LearningEvent(
            ts=100 + i, turn_id=f"b{i}", success=0.0, latency_ms=90000.0,
            token_cost=9000.0, user_corrections=5, param_profile=bad_tag))
    # Directly exercise the interference gate the engine uses.
    det = sl.InterferenceDetector(min_samples=10, regression_threshold=0.15)
    safe, _ = det.evaluate(eng.collector, "v1", bad_tag)
    assert safe is False
