#!/usr/bin/env python3
"""Tests for the Hermes Sensory & Cognitive layer (agent/sensory_system.py)."""

from __future__ import annotations

import time

from agent.sensory_system import (
    KNOWN_MODALITIES,
    PerceptionBuffer,
    SalienceFilter,
    SensorySystem,
    Stimulus,
    WorkingMemory,
    get_engine,
    reset_engine,
)


def _stim(modality, payload="x", source="test", meta=None):
    return Stimulus(modality=modality, payload=payload, source=source, meta=meta or {})


def test_known_modalities_present():
    # The "sensory organs" the agent was missing.
    for m in ("text", "audio", "vision", "state", "tactile", "proprioception"):
        assert m in KNOWN_MODALITIES, f"missing modality {m}"


def test_aot_compile_builds_pipeline():
    s = SensorySystem()
    assert not s._compiled
    s.compile({"sensory": {"salience_threshold": 0.3}})
    assert s._compiled
    assert s._buffer is not None
    assert s._filter is not None
    assert s._memory is not None
    # default = all modalities active
    assert "vision" in s._active_modalities


def test_jit_routes_only_active_modalities():
    s = SensorySystem()
    s.compile({"sensory": {"modalities": ["text", "vision"]}})
    assert "audio" not in s._active_modalities
    frame = s.perceive([
        _stim("text", "hello"),
        _stim("audio", "voice note"),   # inactive -> dropped at routing
        _stim("vision", "screenshot"),
    ])
    mods = {st.modality for st in frame.stimuli}
    assert "audio" not in mods
    assert "text" in mods and "vision" in mods


def test_salience_gating_drops_low_value():
    s = SensorySystem()
    # Threshold between routine text (0.5) and error state (0.7) so only the
    # error survives — verifies the gate actually filters.
    s.compile({"sensory": {"salience_threshold": 0.6}})
    frame = s.perceive([
        _stim("text", "routine message", meta={}),
        _stim("state", "error", meta={"error": True}),
    ])
    assert all(st.modality != "text" for st in frame.stimuli)
    assert any(st.modality == "state" for st in frame.stimuli)


def test_error_stimulus_max_salience():
    s = SensorySystem()
    s.compile({"sensory": {"salience_threshold": 0.25}})
    frame = s.perceive([_stim("state", "crash", meta={"error": True})])
    # error state: weight 0.7 * content 1.0 = 0.7 -> still passes the gate and
    # is the most salient present.
    assert frame.stimuli[0].salience >= 0.7
    assert frame.stimuli[0].salience <= 1.0


def test_working_memory_capacity_bounded():
    wm = WorkingMemory(capacity=3)
    eng = SensorySystem()
    eng.compile()
    for i in range(5):
        frame = eng.perceive([_stim("text", f"t{i}")])
        wm.commit(frame)
    assert len(wm) == 3  # oldest frames evicted
    assert wm.recent(1)[0].turn_id >= 3


def test_perception_frame_prompt_prefix():
    s = SensorySystem()
    s.compile()
    frame = s.perceive([_stim("vision", "img", source="browser")])
    prefix = frame.to_prompt_prefix()
    assert "perception turn" in prefix
    assert "vision@browser" in prefix


def test_buffer_adaptation_cap():
    b = PerceptionBuffer(capacity=4)
    for i in range(10):
        b.push(_stim("text", f"m{i}"))
    assert len(b) == 4  # oldest dropped


def test_salience_sorted_descending():
    s = SensorySystem()
    s.compile({"sensory": {"salience_threshold": 0.1}})
    frame = s.perceive([
        _stim("text", "routine", meta={}),
        _stim("state", "err", meta={"error": True}),
    ])
    assert frame.stimuli[0].salience >= frame.stimuli[-1].salience


def test_singleton_lazy_compile():
    reset_engine()
    eng = get_engine()
    # Not compiled until first perceive.
    assert not eng._compiled
    eng.perceive([_stim("text", "hi")])
    assert eng._compiled


def test_describe_introspection():
    s = SensorySystem()
    s.compile()
    s.perceive([_stim("text", "hi")])
    d = s.describe()
    assert d["compiled"] is True
    assert "text" in d["active_modalities"]
    assert d["working_memory_len"] >= 1


def test_empty_turn_no_salient_stimuli():
    s = SensorySystem()
    s.compile({"sensory": {"salience_threshold": 0.9}})
    frame = s.perceive([_stim("text", "quiet", meta={})])
    assert frame.stimuli == []
    assert frame.summary == "no salient stimuli this turn"
