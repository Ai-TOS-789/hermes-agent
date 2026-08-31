#!/usr/bin/env python3
"""Tests for the multi-model coordinator (agent/model_coordinator.py)."""

from __future__ import annotations

from agent.model_coordinator import (
    CoordinatorConfig,
    ModelCoordinator,
    ModelRef,
    get_coordinator,
    _cache_key,
)


def _ref(role):
    return ModelRef(provider="openrouter", model=f"m-{role}", role=role)


def test_router_picks_role_model():
    c = ModelCoordinator(CoordinatorConfig(models=[
        _ref("coder"), _ref("reasoner"), _ref("fast"), _ref("default"),
    ]))
    assert c.route("code").role == "coder"
    assert c.route("reason").role == "reasoner"
    assert c.route("triage").role == "fast"
    assert c.route("unknown").role == "default"


def test_router_falls_back_to_first():
    c = ModelCoordinator(CoordinatorConfig(models=[_ref("default")]))
    assert c.route("code").role == "default"


def test_parallel_returns_first_good():
    c = ModelCoordinator(CoordinatorConfig(
        models=[_ref("default"), _ref("coder")], mode="parallel"))
    calls = []

    def fake(m, p):
        calls.append(m.role)
        return f"ans-from-{m.role}"

    ans = c.ask("hi", "default", call=fake)
    assert ans.startswith("ans-from-")
    assert len(calls) >= 1  # first good answer wins, may stop early


def test_parallel_skips_erroring_models():
    c = ModelCoordinator(CoordinatorConfig(
        models=[_ref("default"), _ref("coder")], mode="parallel"))

    def fake(m, p):
        if m.role == "default":
            raise RuntimeError("boom")
        return f"ok-{m.role}"

    ans = c.ask("skip-error-prompt-xyz", "default", call=fake)
    assert ans == "ok-coder"


def test_dedupe_reuses_cache():
    c = ModelCoordinator(CoordinatorConfig(
        models=[_ref("default")], dedupe=True))
    seen = []

    def fake(m, p):
        seen.append(p)
        return "cached-answer"

    a1 = c.ask("same prompt", "default", call=fake)
    a2 = c.ask("same   prompt", "default", call=fake)  # normalized equal
    assert a1 == a2 == "cached-answer"
    # Second call should NOT have invoked the model again (dedupe hit).
    assert len(seen) == 1


def test_dedupe_disabled_calls_twice():
    c = ModelCoordinator(CoordinatorConfig(
        models=[_ref("default")], dedupe=False))
    seen = []

    def fake(m, p):
        seen.append(p)
        return "x"

    c.ask("p", "default", call=fake)
    c.ask("p", "default", call=fake)
    assert len(seen) == 2


def test_cache_key_normalizes():
    k1 = _cache_key("Hello   World", "default")
    k2 = _cache_key("hello world", "default")
    assert k1 == k2


def test_singleton():
    a = get_coordinator()
    b = get_coordinator()
    assert a is b
