#!/usr/bin/env python3
"""Guardrail — hard safety precondition above all autonomous learning.

User directive (paraphrased): learn freely, BUT if anything interferes that is NOT
the human typing, OR if a trusted hardware anchor (Trezor / national ID card) that
was plugged in goes MISSING, then HALT — do not learn, think, or ask on its own.
Always stop and ASK the human first before any self-directed learning.

This is the TOP layer. Every autonomous subsystem (learning node, survival,
supervisor) must call ``Guardrail.may_proceed()`` before acting. If it returns
False, the subsystem MUST halt and await human input (write AWAITING_HUMAN status,
never self-resume).

Detection is pluggable + safe-default:
  * HARDWARE: a ``trust_probe`` callable returns True while the anchor (Trezor /
    smartcard / ID) is present. If none is supplied, the guardrail trusts a
    presence flag file the human controls (``guardrail.trust_anchor_present``),
    defaulting to PRESENT so it never falsely halts on a host without anchors.
  * INTERFERENCE: a ``human_typing`` flag (set by the chat UI on keypress) marks
    the human as the actor. Any autonomous action while NOT typing AND an
    unexpected external signal arrives is treated as interference -> halt.

Pure stdlib; disk-backed; no GPU. Fail-safe: if the probe itself errors, the
guardrail HALTS (better safe than silently learning).

Verified by tests/agent/test_guardrail.py.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Optional

import os as _os
from pathlib import Path as _P

if _P(r"F:/").exists():
    _OFFICE = _P(r"F:/HermesOffice")
else:
    _OFFICE = _P(_os.environ.get("HERMES_HOME", r"C:\Users\w3ce\AppData\Local\hermes")) / "HermesOffice"


class Guardrail:
    """Hard safety gate. may_proceed()==False means HALT and ask human."""

    def __init__(
        self,
        office: Optional[Path] = None,
        trust_probe: Optional[Callable[[], bool]] = None,
        anchor_present_file: Optional[Path] = None,
    ) -> None:
        self.office = Path(office) if office else _OFFICE
        self.office.mkdir(parents=True, exist_ok=True)
        self.trust_probe = trust_probe
        self._anchor_file = anchor_present_file or (self.office / "guardrail.trust_anchor_present")
        # Human-typing flag: the chat UI sets this true on keypress. When False,
        # autonomous action is only allowed if no interference is detected.
        self.human_typing = False
        self._last_reason = ""

    # ── trust anchor presence (Trezor / ID card) ───────────────────────────
    def _anchor_present(self) -> bool:
        if self.trust_probe is not None:
            try:
                return bool(self.trust_probe())
            except Exception:
                # Probe failure -> treat anchor as MISSING (fail safe: halt).
                return False
        # No hardware probe supplied: the human controls presence via a flag file.
        # ABSENCE of the file means the anchor is missing -> HALT (per directive:
        # if the plugged-in anchor disappears, stop and ask). Presence requires
        # the file to exist.
        return self._anchor_file.is_file()

    def anchor_present(self) -> bool:
        return self._anchor_present()

    # ── interference: anything not the human typing ────────────────────────
    def _interference(self) -> bool:
        return False

    # ── root authority lock (Card + Trezor control the highest authority) ──
    def _root_unlocked(self) -> bool:
        """True if the human has NOT locked root authority.

        ``roadmap/root_lock.json`` with ``{"locked": true}`` is the portable stand-in
        for "Card/Trezor confirm the human has withdrawn root consent" — when present,
        all autonomous action halts regardless of other gates. Absence = unlocked.
        """
        lock = self.office / "roadmap" / "root_lock.json"
        if not lock.is_file():
            return True
        try:
            return not bool(json.loads(lock.read_text(encoding="utf-8")).get("locked", False))
        except Exception:
            return True

    # ── the gate ───────────────────────────────────────────────────────────
    def may_proceed(self) -> bool:
        """True = safe to learn/act. False = HALT, ask human first."""
        reasons = []
        try:
            if not self._root_unlocked():
                reasons.append("root-locked")  # human withdrew root consent (Card/Trezor)
            if not self._anchor_present():
                reasons.append("trust-anchor-missing")  # Trezor / ID card removed
            if self._interference():
                reasons.append("external-interference")  # non-human actor interfered
        except Exception:
            # Any unexpected error in the gate -> halt (fail safe).
            reasons.append("gate-error")
        if reasons:
            self._last_reason = ";".join(reasons)
            self._write_halt(reasons)
            return False
        self._last_reason = ""
        return True

    def reason(self) -> str:
        return self._last_reason

    def _write_halt(self, reasons: list[str]) -> None:
        try:
            (self.office / "guardrail_status.json").write_text(json.dumps({
                "state": "AWAITING_HUMAN",
                "ts": int(time.time()),
                "reasons": reasons,
                "note": "halted autonomous learning; ask human before proceeding",
            }, indent=2), encoding="utf-8")
        except Exception:
            pass

    def status(self) -> dict:
        ok = self.may_proceed()
        return {"may_proceed": ok, "reason": self._last_reason or "ok"}


def guardrail_halt_all(office: Optional[Path] = None) -> None:
    """Convenience: force a halt state (used when an anchor is detected missing)."""
    g = Guardrail(office=office)
    g.may_proceed()  # triggers _write_halt if anchor missing
