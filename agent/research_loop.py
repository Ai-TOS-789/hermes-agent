#!/usr/bin/env python3
"""Research & Self-Improvement loop (Phase 3 of the Desktop/Option-Skills plan).

Hermes sets goals, searches the web + imagery for reference material, and persists
findings as a versioned roadmap — all under the Guardrail (Card/Trezor anchor present).

Design rules:
- Only URLs / notes are stored. Downloaded imagery is a *reference* for the human or
  the desktop/CLI designer, never executed as code (security + YAGNI).
- Goals + references live under ``$HERMES_HOME/roadmap/`` (JSONL, one object per line).
- The loop is a guarded multi-process worker; it never force-applies changes.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
import os as _os

_OFFICE_ENV = _os.environ.get("HERMES_OFFICE", "")
if _OFFICE_ENV:
    _OFFICE = Path(_OFFICE_ENV)
elif Path(r"F:/").exists():
    _OFFICE = Path(r"F:/HermesOffice")
else:
    _OFFICE = Path(_os.environ.get("HERMES_HOME", r"C:\Users\w3ce\AppData\Local\hermes")) / "HermesOffice"

_REF_DIR = _OFFICE / "roadmap"

# Default self-improvement goals (Hermes continues to extend this list at runtime).
_DEFAULT_GOALS = [
    "macOS Human Interface Guidelines design tokens for Hermes Desktop",
    "Windows Fluent / WinUI Mica Acrylic patterns for a CLI + Desktop app",
    "KDE Breeze global-menu and translucency patterns",
    "Supreme AI self-improvement research: continual learning without forgetting",
    "Multi-process agent orchestration patterns (supervisor/worker)",
]


def set_goal(text: str) -> dict:
    """Append a goal to the roadmap; returns the stored goal object."""
    _REF_DIR.mkdir(parents=True, exist_ok=True)
    goal = {"goal": text, "ts": int(time.time()), "status": "active"}
    with (_REF_DIR / "goals.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(goal, ensure_ascii=False) + "\n")
    return goal


def save_reference(url: str, note: str) -> None:
    """Persist a research reference (URL + note). Reference only — never executed."""
    _REF_DIR.mkdir(parents=True, exist_ok=True)
    with (_REF_DIR / "references.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"url": url, "note": note, "ts": int(time.time())}, ensure_ascii=False) + "\n")


def _ensure_default_goals() -> None:
    _REF_DIR.mkdir(parents=True, exist_ok=True)
    path = _REF_DIR / "goals.jsonl"
    if path.exists() and path.stat().st_size > 0:
        return
    for g in _DEFAULT_GOALS:
        set_goal(g)


def research_once() -> int:
    """Run one research sweep over the most recent goals. Returns reference count added."""
    _ensure_default_goals()
    added = 0
    try:
        from agent.web_search_shim import search, search_images
        goals = [json.loads(l) for l in (_REF_DIR / "goals.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        return 0
    for g in goals[-5:]:
        q = g.get("goal", "")
        try:
            for h in search(q)[:5]:
                save_reference(h.get("url", ""), f"goal:{q}")
                added += 1
            for img in search_images(q)[:3]:
                save_reference(img.get("url", ""), f"image-ref:{q}")
                added += 1
        except Exception:
            continue
    return added


def research_loop(office=None, cadence=600.0) -> None:
    """Supervised multi-process worker: periodically RESEARCH goals into the roadmap."""
    root = Path(office) if office else _OFFICE
    _REF_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            from agent import guardrail as _gr
            if not _gr.Guardrail(office=root).may_proceed():
                time.sleep(cadence)
                continue
            research_once()
        except Exception:
            pass
        time.sleep(cadence)
