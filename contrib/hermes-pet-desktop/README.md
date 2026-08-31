# Hermes Pet Desktop — Tray + Roaming Desktop Pet

A standalone desktop pet for [Hermes Agent](https://github.com/NousResearch/hermes-agent).
It floats a transparent window on your screen, walks around the edges (roam),
sits in the system tray, and reacts to Hermes activity (busy = walking,
idle = resting, error = fallen).

This is an **edge utility** — it does NOT patch the Hermes core. It reuses the
read-only frame-decoding logic from `agent.pet.render` so it draws the exact
same sprites Petdex uses, and it reads the active pet from
`$HERMES_HOME/pets/<slug>/` — the same store the in-terminal pet uses. Because
nothing here lives in the tracked core repo, `hermes update` never clobbers it.

## Why a separate repo?

Per the Hermes contribution guidance, third-party / user extensions belong in a
standalone plugin repo installed into `~/.hermes/plugins/` (or pip entry point),
not embedded in the core tree. This keeps the core maintainable and your pet
survives upgrades.

## Install

```bash
# 1. Copy the viewer anywhere you like (e.g. your HERMES_HOME)
cp pet_desktop.py ~/.hermes/pet_desktop.py

# 2. Install the state-bridge plugin into the USER plugin dir
mkdir -p ~/.hermes/plugins/petbridge
cp plugins/petbridge/__init__.py ~/.hermes/plugins/petbridge/__init__.py
```

Requirements (all already present in a normal Hermes install):
- Python 3.10+ with Tkinter (ships with CPython on Windows/macOS/Linux)
- Pillow (`pip install pillow`) — a core Hermes dependency
- Optional: `pystray` for a real system-tray icon (`pip install pystray`)

## Run

```bash
# From a terminal, with HERMES_HOME pointed at your install:
python ~/.hermes/pet_desktop.py
```

The pet appears as a small transparent sprite that wanders the screen.
Controls:
- **Left-drag** — move it.
- **Right-click** — pause/resume roaming.
- **Tray icon** (if pystray installed) — Quit.

## How it syncs with Hermes

The `petbridge` plugin hooks agent lifecycle events
(`pre_api_request`, `post_api_request`, `api_request_error`, `subagent_start`,
`subagent_stop`, `on_session_start`, `on_session_end`) and writes a tiny
`$HERMES_HOME/pet_state.json` file. `pet_desktop.py` reads that file to choose
its pose. If the file is absent the pet simply idles and roams.

## Notes

- Uses the same spritesheet (`~/.hermes/pets/<slug>/spritesheet.webp`) as the
  terminal pet, so installing a pet via `hermes pets` makes it available here
  automatically.
- State mapping is best-effort: `busy` → `run`, `error` → `failed`,
  `idle` → `idle`. Unknown states fall back to idle.
