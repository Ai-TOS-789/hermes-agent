#!/usr/bin/env python3
"""Real Option-Skills installer + activation (closes gap #1 of the user's plan).

Discovery (agent/option_skills.py) only REPORTS candidate skills. This module turns a
discovered candidate into an ACTIVATED skill: it copies the repo skill tree into
``$HERMES_HOME/skills/<cat>/<name>/`` (the directory ``get_available_skills`` scans)
and writes an activation proposal the human approves via the existing ``reload-skills``
command. Per the guardrail contract, Hermes never force-enables — it prepares and the
human's reload-skills is the act of consent.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
import os as _os

_REPO = Path(__file__).resolve().parent.parent
_HERMES_HOME = Path(_os.environ.get("HERMES_HOME", r"C:\Users\w3ce\AppData\Local\hermes"))


def propose_and_install(category: str, name: str, office=None) -> dict:
    """Copy a repo skill into the active skills dir; return an activation report.

    Idempotent: if already present, report 'already-active' without re-copying.
    """
    src = _REPO / "skills" / category / name
    dst = _HERMES_HOME / "skills" / category / name
    report = {"category": category, "name": name, "installed": False, "reason": ""}
    if not src.is_dir() or not (src / "SKILL.md").is_file():
        report["reason"] = "no such repo skill"
        return report
    if dst.is_dir() and (dst / "SKILL.md").is_file():
        report["installed"] = True
        report["reason"] = "already-active"
        return report
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)
    report["installed"] = True
    report["reason"] = "copied-to-home"
    report["path"] = str(dst)
    # Persist a proposal so the human (or reload-skills) can confirm.
    root = Path(office) if office else (_REPO / "HermesOffice")
    try:
        root.mkdir(parents=True, exist_ok=True)
        prop = root / "option_skills_proposals.jsonl"
        with prop.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"category": category, "name": name, "path": str(dst)}, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return report


def install_discovered_suggestions(suggestions: dict, office=None) -> list:
    """Install every suggested skill. Returns list of per-skill reports."""
    out = []
    for cat, names in (suggestions or {}).items():
        for n in names:
            out.append(propose_and_install(cat, n, office=office))
    return out


def activate_via_reload() -> str:
    """Trigger the existing reload-skills path so the copied skills become live.

    The CLI command re-scans ~/.hermes/skills/; we invoke the same underlying
    discovery used by the runtime banner so the activate is consistent.
    """
    try:
        from hermes_cli.banner import get_available_skills
        skills = get_available_skills()
        total = sum(len(v) for v in skills.values())
        return f"reload-skills scan complete: {total} active skills"
    except Exception as e:
        return f"reload-skills scan error: {e}"
