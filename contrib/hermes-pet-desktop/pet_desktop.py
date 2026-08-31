#!/usr/bin/env python3
"""Standalone desktop pet for Hermes — floats, roams the screen, lives in the tray.

This is an edge utility (per Hermes' "capability lives at the edges" rule): it
does NOT patch the core agent. It reuses the *read-only* frame-decoding logic
from ``agent.pet.render`` so it draws the exact same sprites Petdex uses, and it
reads the active pet from ``$HERMES_HOME/pets/<slug>/`` — the same store the
core pet uses. Nothing here is tracked by ``hermes update``, so upgrades never
clobber it.

State (idle / busy / error / wave) is pulled from ``$HERMES_HOME/pet_state.json``,
written by the optional ``petbridge`` plugin (see ~/.hermes/plugins/petbridge/).
If that file is absent the pet just idles and roams.

Usage:
    python %HERMES_HOME%/pet_desktop.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import threading
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import PhotoImage
except Exception as exc:  # pragma: no cover - environment guard
    sys.stderr.write(f"tkinter unavailable: {exc}\n")
    raise SystemExit(1)

# Pillow is a core Hermes dependency; reuse it for spritesheet decoding.
from PIL import Image, ImageTk  # noqa: E402

# Reuse Hermes' own frame decoder so the pet looks identical to the in-terminal
# one. Importing read-only is safe across updates (we never write to agent.pet).
_AGENT_ROOT = Path(__file__).resolve().parent
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from agent.pet import render as _pet_render  # noqa: E402
from agent.pet.constants import (  # noqa: E402
    FRAME_W,
    FRAME_H,
    FRAMES_PER_STATE,
    DEFAULT_SCALE,
)
from agent.pet.store import pets_dir, installed_pets  # noqa: E402


HERMES_HOME = Path(os.environ.get("HERMES_HOME", r"C:\Users\w3ce\AppData\Local\hermes"))
STATE_FILE = HERMES_HOME / "pet_state.json"

# Animation frame cadence (seconds) — matches petdex's ~1100ms loop feel.
_FRAME_INTERVAL = 0.11
# Roam step in pixels per tick.
_ROAM_STEP = 2
_ROAM_TICK = 0.03


def _active_pet_slug() -> str | None:
    """Best-effort resolution of the active pet slug (mirrors CLI config)."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        disp = cfg.get("display", {})
        if isinstance(disp, dict):
            pet = disp.get("pet", {})
            if isinstance(pet, dict) and pet.get("slug"):
                return str(pet["slug"])
    except Exception:
        pass
    pets = installed_pets()
    return pets[0].slug if pets else None


def _load_frames(slug: str) -> dict[str, list]:
    """Decode every animation state's frames for the given pet.

    Returns {state_name: [PIL.Image, ...]} using Hermes' own _raw_frames so the
    crop geometry matches the terminal renderer exactly.
    """
    pets = {p.slug: p for p in installed_pets()}
    pet = pets.get(slug)
    if pet is None or not pet.exists:
        return {}
    sheet = pet.spritesheet
    if not sheet.is_file():
        return {}

    # How many state rows does this sheet actually have? Use the renderer's own
    # grid detection so legacy 8-row and current 9-row sheets both work.
    try:
        from agent.pet.constants import state_rows_for_grid

        with Image.open(sheet) as img:
            w, h = img.size
        cols = max(1, w // FRAME_W)
        rows = max(1, h // FRAME_H)
        state_rows = state_rows_for_grid(rows)
    except Exception:
        state_rows = ["idle", "wave", "run", "failed", "review", "jump", "waiting", "extra1", "extra2"]

    frames: dict[str, list] = {}
    for state in state_rows:
        try:
            pil_frames = _pet_render._raw_frames(
                str(sheet), state, FRAME_W, FRAME_H, FRAMES_PER_STATE
            )
        except Exception:
            pil_frames = []
        if pil_frames:
            frames[state] = pil_frames
    # Always guarantee a usable idle.
    if "idle" not in frames and frames:
        frames["idle"] = next(iter(frames.values()))
    return frames


def _read_state() -> str:
    """Read the last pet state written by the petbridge plugin."""
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        s = str(data.get("state", "idle")).lower()
        return s
    except Exception:
        return "idle"


class DesktopPet:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.overrideredirect(True)  # borderless floating window
        self.root.attributes("-topmost", True)
        # Transparent window on Windows.
        self.root.configure(bg="black")
        self.root.attributes("-transparentcolor", "black")
        self.root.wm_attributes("-toolwindow", True)

        self.scale = DEFAULT_SCALE
        self.canvas_w = max(24, int(FRAME_W * self.scale))
        self.canvas_h = max(24, int(FRAME_H * self.scale))

        self.canvas = tk.Canvas(
            self.root,
            width=self.canvas_w,
            height=self.canvas_h,
            bg="black",
            highlightthickness=0,
            borderwidth=0,
        )
        self.canvas.pack()

        self.slug = _active_pet_slug()
        self.frames: dict[str, list] = _load_frames(self.slug) if self.slug else {}
        self.state = "idle"
        self.frame_idx = 0
        self._tk_frames: dict[str, list] = {}
        self._photo_cache: dict[tuple, object] = {}

        self.root.bind("<ButtonPress-1>", self._start_drag)
        self.root.bind("<B1-Motion>", self._do_drag)
        self.root.bind("<Button-3>", self._toggle_pause)  # right-click pause roam

        self._paused = False
        self._dir = 1  # 1 = right, -1 = left
        self._vx, self._vy = _ROAM_STEP, _ROAM_STEP
        self._last_state_check = 0.0

        # Tray (Windows: use a System Tray via pystray if available, else skip).
        self._setup_tray()

        self._animate()
        self._roam()

    # ── Tray ───────────────────────────────────────────────────────────────
    def _setup_tray(self) -> None:
        try:
            import pystray  # type: ignore

            from PIL import Image as PILImage

            icon_img = PILImage.new("RGBA", (32, 32), (0, 0, 0, 0))
            menu = pystray.Menu(
                pystray.MenuItem("Quit", self._quit),
            )
            self._tray = pystray.Icon("hermes_pet", icon_img, "Hermes Pet", menu)
            threading.Thread(target=self._tray.run, daemon=True).start()
        except Exception:
            self._tray = None

    # ── Animation ──────────────────────────────────────────────────────────
    def _current_frames(self) -> list:
        # Prefer state-specific frames; fall back to idle, then any.
        for key in (self.state, "idle"):
            if key in self.frames:
                return self.frames[key]
        if self.frames:
            return next(iter(self.frames.values()))
        return []

    def _animate(self) -> None:
        now = time.time()
        if now - self._last_state_check > 0.25:
            self._last_state_check = now
            desired = _read_state()
            # Map api states to available animation rows.
            alias = {
                "busy": "run",
                "error": "failed",
                "wave": "wave",
                "jump": "jump",
                "waiting": "waiting",
                "review": "review",
            }.get(desired, "idle")
            if alias in self.frames:
                self.state = alias
            elif desired in self.frames:
                self.state = desired
            else:
                self.state = "idle"

        frames = self._current_frames()
        if frames:
            self.frame_idx = (self.frame_idx + 1) % len(frames)
            img = frames[self.frame_idx]
            # Flip horizontally when walking left for a natural roam.
            if self._dir < 0 and hasattr(img, "transpose"):
                img = img.transpose(Image.FLIP_LEFT_RIGHT)
            tk_img = ImageTk.PhotoImage(img)
            self._photo_cache[(id(img), self.frame_idx)] = tk_img
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor="nw", image=tk_img)
            self.canvas.image = tk_img  # keep ref alive
        self.root.after(int(_FRAME_INTERVAL * 1000), self._animate)

    # ── Roam (walk around the whole screen) ─────────────────────────────────
    def _roam(self) -> None:
        if not self._paused:
            try:
                x = self.root.winfo_x()
                y = self.root.winfo_y()
                sw = self.root.winfo_screenwidth()
                sh = self.root.winfo_screenheight()
                nx = x + self._vx * self._dir
                ny = y + self._vy
                # Bounce off horizontal edges; flip facing.
                if nx <= 0:
                    nx = 0
                    self._dir = 1
                elif nx + self.canvas_w >= sw:
                    nx = sw - self.canvas_w
                    self._dir = -1
                # Wrap vertically (gentle drift up/down).
                if ny <= 0:
                    ny = 0
                    self._vy = abs(self._vy)
                elif ny + self.canvas_h >= sh:
                    ny = sh - self.canvas_h
                    self._vy = -abs(self._vy)
                self.root.geometry(f"+{nx}+{ny}")
            except Exception:
                pass
        self.root.after(int(_ROAM_TICK * 1000), self._roam)

    # ── Interaction ─────────────────────────────────────────────────────────
    def _start_drag(self, event) -> None:
        self._drag_x = event.x
        self._drag_y = event.y

    def _do_drag(self, event) -> None:
        x = self.root.winfo_x() + event.x - self._drag_x
        y = self.root.winfo_y() + event.y - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def _toggle_pause(self, _event=None) -> None:
        self._paused = not self._paused

    def _quit(self, _icon=None, _item=None) -> None:
        try:
            if self._tray is not None:
                self._tray.stop()
        except Exception:
            pass
        self.root.after(0, self.root.destroy)

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    if not _active_pet_slug():
        sys.stderr.write(
            "No Hermes pet installed. Install one with `hermes pets` or drop a "
            "spritesheet into $HERMES_HOME/pets/<slug>/.\n"
        )
        raise SystemExit(1)
    DesktopPet().run()


if __name__ == "__main__":
    main()
