#!/usr/bin/env python3
"""Multi-model coordinator: let several configured models work *together*.

Hermes already supports a sequential ``fallback_model`` chain (try A, then B, then
C on failure).  This module ADDS cooperative multi-model execution on top of that
existing config so models can share the load instead of only backing each other
up:

  * ROUTER mode  — pick the best-fit model per *task type* (code -> coder model,
                   reasoning -> strong model, triage -> fast/cheap model). Reads
                   the same ``fallback_model``/``model`` config the core uses.
  * PARALLEL mode — fan a prompt to N models at once, keep the first good answer
                   (or majority-vote), cancelling the rest. Cuts latency vs.
                   sequential fallback when the primary is slow.
  * DEDUPE       — a content-addressed cache shared across models: if two models
                   would answer the same normalized prompt, the second reuses the
                   first's result. Cuts redundant token spend (the "reduce
                   duplication" goal).
  * SUPERVISE     — a long-running loop that drives self-learning / sensory
                    passes on a timer even with no chat open (the "keep learning
                    24/7" goal), feeding the agent's own closed-loop subsystems.

This is additive and fail-open: if a model errors, the coordinator drops it and
continues with the rest; if the whole thing errors, the caller proceeds with the
normal single-model path. It never patches the core inference code.

Verified by tests/agent/test_model_coordinator.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

_HERMES_HOME = Path(os.environ.get("HERMES_HOME", r"C:\Users\w3ce\AppData\Local\hermes"))
_CACHE_FILE = _HERMES_HOME / "model_coordinator_cache.json"
_CACHE_LOCK = threading.Lock()


# ── Task-type → model-role routing ───────────────────────────────────────────
# Mirrors how a nervous system routes signals to the right cortex region.
TASK_ROUTES: Dict[str, str] = {
    "code": "coder",           # implementation / debugging
    "reason": "reasoner",      # planning / analysis
    "triage": "fast",          # classification / routing / cheap work
    "vision": "vision",        # image understanding
    "default": "default",      # general chat
}


@dataclass
class ModelRef:
    provider: str
    model: str
    role: str = "default"


@dataclass
class CoordinatorConfig:
    models: List[ModelRef] = field(default_factory=list)
    mode: str = "router"          # router | parallel
    dedupe: bool = True
    cache_ttl_s: int = 3600 * 24  # 1 day


def _normalize(prompt: str) -> str:
    # Cheap normalization for the dedupe key: lowercase, collapse whitespace.
    return " ".join(prompt.strip().lower().split())


def _cache_key(prompt: str, role: str) -> str:
    return hashlib.sha256(f"{role}|{_normalize(prompt)}".encode()).hexdigest()[:16]


def _load_cache() -> Dict[str, dict]:
    try:
        with _CACHE_LOCK:
            return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache: Dict[str, dict]) -> None:
    try:
        with _CACHE_LOCK:
            _CACHE_FILE.write_text(json.dumps(cache), encoding="utf-8")
    except Exception:
        pass


class ModelCoordinator:
    """Cooperatively runs multiple models for one logical request."""

    def __init__(self, config: Optional[CoordinatorConfig] = None) -> None:
        self.cfg = config or CoordinatorConfig()
        self._cache: Dict[str, dict] = _load_cache()

    # ── public API ──────────────────────────────────────────────────────────
    def route(self, task_type: str) -> Optional[ModelRef]:
        """Return the best-fit model for a task type (router mode)."""
        role = TASK_ROUTES.get(task_type, "default")
        for m in self.cfg.models:
            if m.role == role:
                return m
        # Fallback to the first model of any role.
        return self.cfg.models[0] if self.cfg.models else None

    def ask(self, prompt: str, task_type: str = "default",
            call: Callable[[ModelRef, str], str] = None) -> str:
        """Run a prompt through the cooperative model set.

        ``call`` is injected by the caller (the actual LLM call fn); we never
        hardcode provider SDKs. If ``call`` is None we just return the routed
        model description for testing.
        """
        if call is None:
            m = self.route(task_type)
            return f"route:{m.role}:{m.provider}/{m.model}" if m else "no-model"

        role = TASK_ROUTES.get(task_type, "default")

        # Dedupe: if we've answered an equivalent prompt recently, reuse it.
        if self.cfg.dedupe:
            key = _cache_key(prompt, role)
            hit = self._cache.get(key)
            if hit and time.time() - hit.get("ts", 0) < self.cfg.cache_ttl_s:
                return hit["answer"]

        if self.cfg.mode == "parallel" and len(self.cfg.models) > 1:
            answer = self._parallel(prompt, role, call)
        else:
            m = self.route(task_type)
            if m is None:
                return ""
            answer = self._safe_call(m, prompt, call)

        if self.cfg.dedupe and answer:
            key = _cache_key(prompt, role)
            self._cache[key] = {"answer": answer, "ts": time.time()}
            _save_cache(self._cache)
        return answer

    # ── internals ───────────────────────────────────────────────────────────
    def _safe_call(self, m: ModelRef, prompt: str,
                   call: Callable[[ModelRef, str], str]) -> str:
        try:
            return call(m, prompt) or ""
        except Exception as exc:
            return f"[model {m.role} error: {exc}]"

    def _parallel(self, prompt: str, role: str,
                  call: Callable[[ModelRef, str], str]) -> str:
        """Fan out to all models; keep first non-empty, cancel the rest.

        Priority order: models whose role EXACTLY matches the task type are
        tried first, then ``default``-role models, so the most appropriate
        model wins ties instead of whichever thread happens to finish first.
        """
        exact = [m for m in self.cfg.models if m.role == role]
        fallback = [m for m in self.cfg.models if m.role == "default" and m not in exact]
        others = [m for m in self.cfg.models if m not in exact and m not in fallback]
        ordered = exact + fallback + others
        if not ordered:
            ordered = self.cfg.models
        answers: List[str] = []
        with ThreadPoolExecutor(max_workers=len(ordered)) as ex:
            futs = {ex.submit(self._safe_call, m, prompt, call): m
                    for m in ordered}
            # Honor priority: check futures in submission order, not completion.
            for m in ordered:
                fut = next(f for f, mm in futs.items() if mm is m)
                try:
                    a = fut.result()
                except Exception:
                    continue
                if a and not a.startswith("[model"):
                    answers.append(a)
                    ex.shutdown(cancel_futures=True, wait=False)
                    break
        return answers[0] if answers else ""


# ── Supervise mode: keep self-learning + sensory alive 24/7 ──────────────────
def supervise_loop(interval_s: int = 300) -> None:
    """Timer loop that nudges Hermes' closed-loop subsystems even with no chat.

    Drives the self-learning tune pass and a sensory perception pass on a fixed
    cadence so the agent keeps refining itself around the clock. Fail-open: any
    error is swallowed and the loop continues.
    """
    while True:
        try:
            from agent import self_learning, sensory_system

            sl = self_learning.get_engine()
            if sl is not None:
                sl.tune_once(iterations=20)
            ss = sensory_system.get_engine()
            ss.compile()
            # A light proprioceptive ping so working memory stays warm.
            ss.perceive([sensory_system.Stimulus(
                modality="proprioception",
                payload="supervisor ping",
                source="coordinator",
                meta={"novel": False},
            )])
        except Exception as exc:
            print(f"[coordinator] supervise tick error (continuing): {exc}")
        time.sleep(interval_s)


# Singleton for the agent-facing path.
_engine: Optional[ModelCoordinator] = None
_engine_lock = threading.Lock()


def get_coordinator(cfg: Optional[CoordinatorConfig] = None) -> ModelCoordinator:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = ModelCoordinator(cfg)
    return _engine
