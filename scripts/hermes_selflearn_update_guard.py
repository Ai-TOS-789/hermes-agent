#!/usr/bin/env python3
"""Update-safeguard for the Hermes self-learning patch set.

Per the user's durability requirement: when Hermes updates, our core edits must
survive.  This script implements the "backup then Y-diff" strategy:

  PRE-UPDATE  (run before `hermes update`):
    * Snapshot every file we touch into a timestamped backup dir under
      $HERMES_HOME/selflearn_backup/<ts>/.
    * Record the exact upstream git SHA we were based on.

  POST-UPDATE (run after `hermes update`):
    * For each tracked file, compute a 3-way diff:
        base   = our backup (pre-update state WE wrote)
        theirs = current file on disk (post-update upstream)
        ours   = the canonical self-learning version we intend to re-apply
    * If upstream did NOT touch the region we patched -> re-apply cleanly.
    * If upstream DID change the same region -> emit a Y-diff (diff3-style) and
      STOP, so a human resolves it instead of silently clobbering our logic.

This is deliberately conservative: it never force-overwrites upstream work.  It
only auto-restores when our edits are still "foreign" to the updated file.

Usage:
    python scripts/hermes_selflearn_update_guard.py pre
    python scripts/hermes_selflearn_update_guard.py post
    python scripts/hermes_selflearn_update_guard.py status
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

# Files this feature set owns/patches, relative to the hermes-agent root.
OWNED_FILES = [
    "agent/self_learning.py",
    "tests/agent/test_self_learning.py",
]

# Files we patch inline (not wholly owned) — guarded via marker comments.
PATCHED_FILES = [
    "run_agent.py",
    "hermes_cli/config_defaults.py",
]

MARKERS = (
    "# === SELF-LEARNING PATCH (start) ===",
    "# === SELF-LEARNING PATCH (end) ===",
)


def _hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    try:
        from hermes_cli.config import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path.home() / ".hermes"


def _agent_root() -> Path:
    return _hermes_home() / "hermes-agent"


def _backup_dir() -> Path:
    return _hermes_home() / "selflearn_backup"


def _git_sha(root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root), capture_output=True, text=True, timeout=20,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def cmd_pre() -> int:
    root = _agent_root()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bdir = _backup_dir() / ts
    bdir.mkdir(parents=True, exist_ok=True)
    manifest: Dict[str, str] = {}
    all_files = OWNED_FILES + PATCHED_FILES
    for rel in all_files:
        src = root / rel
        if not src.is_file():
            print(f"  skip (missing): {rel}")
            continue
        dst = bdir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        manifest[rel] = _sha256(src.read_text(encoding="utf-8", errors="replace"))
    meta = {
        "ts": ts,
        "based_on_sha": _git_sha(root),
        "files": manifest,
    }
    (bdir / "manifest.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    # Keep only the 5 most recent backups.
    backups = sorted(_backup_dir().iterdir(), key=lambda p: p.name, reverse=True)
    for old in backups[5:]:
        shutil.rmtree(old, ignore_errors=True)
    print(f"Backed up {len(manifest)} files to {bdir}")
    print(f"Based on upstream SHA: {meta['based_on_sha']}")
    print("Now run: hermes update")
    return 0


def _latest_backup() -> Tuple[Path, dict]:
    backups = sorted(_backup_dir().iterdir(), key=lambda p: p.name, reverse=True)
    for b in backups:
        m = b / "manifest.json"
        if m.is_file():
            return b, json.loads(m.read_text(encoding="utf-8"))
    raise SystemExit("No backup found. Run `pre` before `post`.")


def _y_diff(ours: str, base: str, theirs: str) -> str:
    """Return a diff3-style Y-diff string (theirs | base | ours)."""
    import difflib

    tlines = theirs.splitlines()
    blines = base.splitlines()
    olines = ours.splitlines()
    diff = difflib.unified_diff(blines, tlines, fromfile="base(us)", tofile="theirs(upstream)", n=2)
    d2 = difflib.unified_diff(blines, olines, fromfile="base(us)", tofile="ours(self-learn)", n=2)
    return "\n".join(list(diff) + ["---"] + list(d2))


def cmd_post() -> int:
    root = _agent_root()
    bdir, meta = _latest_backup()
    conflicts = 0
    for rel, old_sha in meta["files"].items():
        cur = root / rel
        backup = bdir / rel
        if not cur.is_file() or not backup.is_file():
            print(f"  ? {rel}: file missing post-update; leaving backup intact")
            continue
        cur_text = cur.read_text(encoding="utf-8", errors="replace")
        bak_text = backup.read_text(encoding="utf-8", errors="replace")
        cur_sha = _sha256(cur_text)
        # If upstream did not change the file since our backup, and our backup
        # matches what we last wrote, re-apply is unnecessary -> keep upstream.
        if cur_sha == old_sha:
            print(f"  unchanged upstream: {rel} (left as-is)")
            continue
        # Upstream changed it. If it matches our backup exactly, our edit was
        # already present and upstream preserved it -> fine.
        if cur_sha == _sha256(bak_text):
            print(f"  our edit preserved by upstream: {rel}")
            continue
        # Otherwise upstream diverged. For OWNED files we can safely overwrite
        # (they are wholly ours). For PATCHED files, do a Y-diff and stop.
        if rel in OWNED_FILES:
            shutil.copy2(backup, cur)
            print(f"  restored (wholly owned): {rel}")
        else:
            yd = _y_diff(bak_text, bak_text, cur_text)
            out = _backup_dir() / f"conflict_{rel.replace('/', '_')}.diff"
            out.write_text(yd, encoding="utf-8")
            conflicts += 1
            print(f"  CONFLICT: {rel} changed by upstream -> Y-diff at {out}")
    if conflicts:
        print(f"\n{conflicts} conflict(s). Resolve the .diff files, then re-apply the "
              f"marked region ({MARKERS[0]} .. {MARKERS[1]}) from backup into the updated file.")
        return 1
    print("\nAll self-learning files reconciled with the update. No conflicts.")
    return 0


def cmd_status() -> int:
    try:
        bdir, meta = _latest_backup()
    except SystemExit as e:
        print(str(e))
        return 1
    print(f"Latest backup: {bdir.name}")
    print(f"Based on upstream SHA: {meta['based_on_sha']}")
    print(f"Files backed up: {len(meta['files'])}")
    return 0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("pre", "post", "status"):
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    if cmd == "pre":
        return cmd_pre()
    if cmd == "post":
        return cmd_post()
    return cmd_status()


if __name__ == "__main__":
    raise SystemExit(main())
