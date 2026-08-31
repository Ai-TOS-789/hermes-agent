#!/usr/bin/env python3
"""Option-Skills Conductor (Phase 1 of the Desktop/Option-Skills plan).

Hermes autonomously DISCOVERS Option Skills that exist in the repo but are not yet
active, and (separately) RESEARCHES goals from the web — both run as supervised
multi-process workers gated by the Guardrail (so nothing acts without Card/Trezor
confirmation present in the office root).

This module is intentionally disk-backed and side-effect-light: discovery only
*reports* suggestions (it never auto-installs — enabling a skill still requires the
human + `reload-skills` path). Research only stores URLs/notes as references; it
never executes downloaded content.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
import os as _os

# Default Local Office root — mirrors learning_node / monitor resolution so every
# subsystem agrees on where artifacts live.
_OFFICE_ENV = _os.environ.get("HERMES_OFFICE", "")
if _OFFICE_ENV:
    _OFFICE = Path(_OFFICE_ENV)
elif Path(r"F:/").exists():
    _OFFICE = Path(r"F:/HermesOffice")
else:
    _OFFICE = Path(_os.environ.get("HERMES_HOME", r"C:\Users\w3ce\AppData\Local\hermes")) / "HermesOffice"

# Repo root (.../hermes-agent) so we can read the source `skills/` tree.
_REPO = Path(__file__).resolve().parent.parent


def _active_skills() -> dict:
    """Return {category: [skill_names]} of currently ACTIVE skills."""
    try:
        from hermes_cli.banner import get_available_skills
        return get_available_skills() or {}
    except Exception:
        return {}


def _repo_skill_tree() -> dict:
    """Return {category: [skill_names]} of skills present in the repo `skills/` tree."""
    tree: dict = {}
    skills_root = _REPO / "skills"
    if not skills_root.is_dir():
        return tree
    for cat in sorted(p.name for p in skills_root.iterdir() if p.is_dir()):
        cat_dir = skills_root / cat
        names = [p.parent.name for p in sorted(cat_dir.rglob("SKILL.md"))]
        if names:
            tree[cat] = names
    return tree


def discover_once() -> dict:
    """List Option Skills available in the repo but not yet active.

    Returns:
        {"suggestions": {category: [names]}, "active": {category: [names]},
         "ts": int}
    """
    active = _active_skills()
    tree = _repo_skill_tree()
    active_names = {n for ns in active.values() for n in ns}
    suggestions: dict = {}
    for cat, names in tree.items():
        diff = [n for n in names if n not in active_names]
        if diff:
            suggestions[cat] = diff
    return {
        "suggestions": suggestions,
        "active": active,
        "ts": int(time.time()),
    }


def discover_loop(office=None, cadence=300.0) -> None:
    """Supervised multi-process worker: periodically DISCOVER Option Skill suggestions.

    Writes `option_skills_discover.json` to the office root. Halts (writes nothing
    new) when the Guardrail blocks (Card/Trezor absent).
    """
    root = Path(office) if office else _OFFICE
    root.mkdir(parents=True, exist_ok=True)
    out = root / "option_skills_discover.json"
    while True:
        try:
            from agent import guardrail as _gr
            if not _gr.Guardrail(office=root).may_proceed():
                time.sleep(cadence)
                continue
            rep = discover_once()
            rep["ts"] = int(time.time())
            out.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
        except Exception as e:  # noqa: BLE001 - never let the worker die
            try:
                out.write_text(json.dumps({"error": str(e)}, default=str), encoding="utf-8")
            except Exception:
                pass
        time.sleep(cadence)


# Re-export the real research loop so the conductor has one import surface
# (agent/research_loop.py is the single implementation — no duplicated logic).
from agent.research_loop import research_loop  # noqa: E402,F401

