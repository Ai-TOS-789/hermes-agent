"""petbridge — expose Hermes activity to the standalone desktop pet viewer.

This plugin lives in the USER plugin directory (~/.hermes/plugins/petbridge/),
NOT in the tracked core repo, so ``hermes update`` never overwrites it. It is a
thin bridge: it watches agent lifecycle hooks and writes a tiny JSON state file
(``$HERMES_HOME/pet_state.json``) that ``pet_desktop.py`` reads to drive the
pet's pose (idle / busy / error). Nothing here touches core agent code.

Supported hook names (all observers — return values ignored):
    on_session_start   -> busy
    on_session_end     -> idle
    subagent_start     -> busy
    subagent_stop      -> idle
    pre_api_request    -> busy
    post_api_request   -> idle
    api_request_error  -> error
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

_STATE_LOCK = threading.Lock()
_STATE_PATH = None


def _state_path() -> Path:
    global _STATE_PATH
    if _STATE_PATH is None:
        home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
        _STATE_PATH = Path(home) / "pet_state.json"
    return _STATE_PATH


def _write(state: str) -> None:
    """Write the current pet state (best-effort, never raises)."""
    try:
        path = _state_path()
        with _STATE_LOCK:
            data = {
                "state": state,
                "ts": time.time(),
            }
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, path)
    except Exception:
        pass


# ── Hook callbacks ──────────────────────────────────────────────────────────
def _on_busy(**_kwargs) -> None:
    _write("busy")


def _on_idle(**_kwargs) -> None:
    _write("idle")


def _on_error(**_kwargs) -> None:
    _write("error")


def register(ctx) -> None:
    """Plugin entry — called by the plugin loader at startup."""
    # Session lifecycle
    ctx.register_hook("on_session_start", _on_busy)
    ctx.register_hook("on_session_end", _on_idle)
    # Subagent (delegation) lifecycle
    ctx.register_hook("subagent_start", _on_busy)
    ctx.register_hook("subagent_stop", _on_idle)
    # Per-LLM-call lifecycle (the finest-grained busy/idle signal)
    ctx.register_hook("pre_api_request", _on_busy)
    ctx.register_hook("post_api_request", _on_idle)
    ctx.register_hook("api_request_error", _on_error)
