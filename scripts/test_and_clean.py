#!/usr/bin/env python3
"""Test-harvest + cleanup: run the project test suite, then REMOVE every test
artifact so a production checkout stays clean (per user directive: real usage
must not leave test* files behind).

Why this exists:
  The features we built (self-learning, sensory, always-on, multi-model) were
  developed WITH tests for verification. But shipping those test_*.py files into
  a production Hermes install violates the "clean real deployment" rule — they
  bloat the tree, can shadow real modules, and linger across `hermes update`.

Behaviour:
  1. Run the requested test files via scripts/run_tests.sh (verification step).
  2. On success, delete every test_*.py / test_*.pyc we can find under the repo
     (configurable allow-list below). __pycache__ dirs are also swept.
  3. Never deletes files outside the repo root; never touches source modules.

Usage:
    python scripts/test_and_clean.py tests/agent/test_sensory_system.py ...
    python scripts/test_and_clean.py --all        # run a broad smoke set, then clean
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent

# Files we are allowed to delete (our own test artifacts only). Anything not
# listed here is never touched — safety boundary.
_OUR_TEST_FILES = [
    "tests/agent/test_sensory_system.py",
    "tests/agent/test_self_learning.py",
    "tests/agent/test_model_coordinator.py",
    "tests/run_agent/test_multi_core_function.py",
    "tests/tools/test_approval_timeout_overflow.py",
]


def _run_tests(targets: list[str]) -> int:
    cmd = [str(_REPO / "scripts" / "run_tests.sh"), *targets]
    # On Windows the .sh needs a POSIX shell; try common interpreters.
    import shutil as _shutil

    shell = None
    for cand in ("bash", "git-bash", "sh"):
        p = _shutil.which(cand)
        if p:
            shell = p
            break
    if shell:
        full = [shell, *cmd]
    else:
        full = cmd  # hope it resolves; non-Windows
    print(f"[test_and_clean] running: {' '.join(full)}")
    return subprocess.run(full, cwd=str(_REPO)).returncode


def _clean() -> int:
    removed = 0
    # Remove our specific test files (tracked or untracked) if present.
    for rel in _OUR_TEST_FILES:
        p = _REPO / rel
        if p.is_file():
            p.unlink()
            removed += 1
            print(f"[test_and_clean] removed {rel}")
    # Sweep any leftover __pycache__ containing our test modules.
    for pyc in _REPO.rglob("__pycache__"):
        if pyc.is_dir():
            # Only remove caches that hold test_*.pyc to be conservative.
            has_test = any(f.name.startswith("test_") for f in pyc.iterdir())
            if has_test:
                shutil.rmtree(pyc, ignore_errors=True)
                removed += 1
    print(f"[test_and_clean] removed {removed} test artifact item(s).")
    return removed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("targets", nargs="*", help="test files/dirs to run")
    ap.add_argument("--all", action="store_true",
                    help="run a broad smoke set then clean")
    ap.add_argument("--no-clean", action="store_true",
                    help="run tests but skip the cleanup step (debug)")
    args = ap.parse_args()

    if args.all:
        targets = [
            "tests/agent/test_sensory_system.py",
            "tests/agent/test_self_learning.py",
            "tests/agent/test_model_coordinator.py",
        ]
    else:
        targets = args.targets or _OUR_TEST_FILES

    rc = _run_tests(targets)
    if rc != 0:
        print(f"[test_and_clean] tests failed (rc={rc}); NOT cleaning so you can inspect.")
        return rc
    if not args.no_clean:
        _clean()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
