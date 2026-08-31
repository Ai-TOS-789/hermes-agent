---
name: desktop-ux
description: UI/UX reference patterns for macOS, Windows, and KDE — design tokens,
  window behavior, motion, and accessibility, distilled from web + imagery research.
---

# Desktop UX Reference

Reference patterns for making Hermes Desktop and CLI feel world-class, distilled from
web research + imagery (never executed as code — design tokens only). Hermes should
continually study these and propose improvements to `apps/desktop` and `hermes_cli`.

## Design tokens (from research)
- **macOS (Human Interface Guidelines):** vibrancy/translucency, 8px corner radius,
  SF Pro / system font, single system accent color, traffic-light window controls
  (red/amber/green), generous whitespace, centered modal sheets.
- **Windows (Fluent / WinUI):** Mica + Acrylic materials, 4px corner radius, Segoe UI
  Variable, left-aligned title bar with centered command bar, snap layouts (Z-one /
  Z-two / Z-three), subtle reveal hover, content-first density.
- **KDE (Breeze):** 4px corner radius, Noto Sans / system sans, global menu + titlebar
  button option, optional translucency, high-contrast accessibility mode, strong
  keyboard-navigation-first, configurable density.

## Window behavior
- macOS: sheet modals, full-screen Spaces, stage manager grouping.
- Windows: snap layouts + snap groups, focus sessions, compact overlay.
- KDE: tiling/scripted layouts optional, activities, window rules.

## Motion & feel
- 150–250ms ease-out; honor `prefers-reduced-motion`.
- Skeleton/loading shimmer instead of spinners where possible.
- Command palette (Cmd/Ctrl+K) as the primary navigation surface on every platform.

## Accessibility (non-negotiable)
- Full keyboard nav, visible focus rings, ARIA/role parity, 4.5:1 contrast minimum.

## How Hermes uses this
On each research tick (`agent/research_loop.py`), Hermes appends new reference URLs +
notes to `$HERMES_HOME/roadmap/references.jsonl` and may propose concrete token changes
to `apps/desktop` / `hermes_cli`. Proposals stay proposals until the human (with
Card/Trezor anchor present) approves via the Update menu — no forced restyle.
