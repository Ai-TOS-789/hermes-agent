#!/usr/bin/env python3
"""Apply desktop-ux reference tokens to real Hermes surfaces (closes gap #3).

Reads the macOS/Windows/KDE design tokens from ``skills/desktop-ux/SKILL.md`` and
materializes them as:
  * a generated DesktopTheme JSON the renderer can load (``apps/desktop/src/themes/
    hermes-ux-generated.json``), and
  * a CLI skin proposal (accent + radius) written to the office for the human to
    adopt via the ``skin`` command.

Hermes does NOT silently restyle the running app — it generates the artifact and a
proposal; the human approves (guardrail/Update menu contract). Reference-only data
from the skill file is parsed, never executed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
import os as _os

_REPO = Path(__file__).resolve().parent.parent
_SKILL = _REPO / "skills" / "desktop-ux" / "SKILL.md"
# Generated theme goes to the Local Office (F:/HermesOffice/themes), NOT into the
# repo's apps/desktop source tree — keeps generated artifacts out of version control
# noise and lets the human copy it into the renderer when they approve.
_OFFICE = Path(_os.environ.get("HERMES_OFFICE", "")) or (
    Path(r"F:/HermesOffice") if Path(r"F:/").exists()
    else Path(_os.environ.get("HERMES_HOME", r"C:\Users\w3ce\AppData\Local\hermes")) / "HermesOffice"
)
_DESKTOP_THEMES = _OFFICE / "themes"


# Curated token table distilled from the skill file (kept in code so this module
# works even if the markdown changes — the skill file is the human-readable source,
# this is the machine-consumable mirror).
_TOKENS = {
    "macos":   {"accent": "#0A84FF", "radius": 8, "surface": "#1E1E1E", "fg": "#FFFFFF", "label": "macOS Vibrancy"},
    "windows": {"accent": "#0078D4", "radius": 4, "surface": "#202020", "fg": "#FFFFFF", "label": "Windows Fluent/Mica"},
    "kde":     {"accent": "#3DAEE9", "radius": 4, "surface": "#2A2A2A", "fg": "#FCFCFC", "label": "KDE Breeze"},
}


def parse_skill_tokens() -> dict:
    """Return the token table (fallback to defaults if the skill file is missing)."""
    if not _SKILL.is_file():
        return dict(_TOKENS)
    text = _SKILL.read_text(encoding="utf-8")
    out = {}
    for plat, base in _TOKENS.items():
        # Allow the skill file to override a hex accent if it names one per platform.
        m = re.search(rf"{plat}.*?accent[:\s]+`?([0-9A-Fa-f]{{6}})", text)
        tok = dict(base)
        if m:
            tok["accent"] = "#" + m.group(1)
        out[plat] = tok
    return out


def build_theme(platform: str = "kde") -> dict:
    """Build a DesktopTheme-shaped dict for the renderer (colors + darkColors)."""
    tok = parse_skill_tokens().get(platform, _TOKENS["kde"])
    colors = {
        "background": tok["surface"],
        "foreground": tok["fg"],
        "accent": tok["accent"],
        "elevated": _mix(tok["surface"], "#FFFFFF", 0.08),
        "sidebar": _mix(tok["surface"], "#000000", 0.10),
        "error": "#FF453A",
        "radius": tok["radius"],
    }
    return {
        "name": f"hermes-ux-{platform}",
        "label": tok["label"],
        "colors": colors,
        "darkColors": colors,
    }


def _mix(a: str, b: str, t: float) -> str:
    def hx(c):
        c = c.lstrip("#")
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    ar, ag, ab = hx(a)
    br, bg, bb = hx(b)
    rr = int(ar + (br - ar) * t)
    gg = int(ag + (bg - ag) * t)
    bb = int(ab + (bb - ab) * t)
    return f"#{rr:02X}{gg:02X}{bb:02X}"


def generate_theme_artifact(platform: str = "kde") -> Path:
    """Write the generated DesktopTheme JSON into the renderer's theme dir."""
    _DESKTOP_THEMES.mkdir(parents=True, exist_ok=True)
    out = _DESKTOP_THEMES / "hermes-ux-generated.json"
    out.write_text(json.dumps(build_theme(platform), indent=2), encoding="utf-8")
    return out


def propose_cli_skin(platform: str = "kde", office=None) -> dict:
    """Write a CLI skin proposal (accent + radius) for the human to apply via `skin`."""
    tok = parse_skill_tokens().get(platform, _TOKENS["kde"])
    root = Path(office) if office else (_REPO / "HermesOffice")
    root.mkdir(parents=True, exist_ok=True)
    prop = {
        "kind": "cli-skin",
        "platform": platform,
        "accent": tok["accent"],
        "radius": tok["radius"],
        "apply_command": f"skin set accent {tok['accent']}",
        "note": "proposal only; human approves via skin command / Update menu",
    }
    (root / "desktop_ux_cli_proposal.json").write_text(json.dumps(prop, indent=2), encoding="utf-8")
    return prop


def apply_all(platform: str = "kde", office=None) -> dict:
    """Generate the renderer theme artifact + CLI skin proposal. Returns a report."""
    theme_path = generate_theme_artifact(platform)
    cli = propose_cli_skin(platform, office=office)
    return {
        "platform": platform,
        "theme_artifact": str(theme_path),
        "theme_written": theme_path.is_file(),
        "cli_proposal": cli,
    }
