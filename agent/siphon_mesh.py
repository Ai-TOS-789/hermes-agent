#!/usr/bin/env python3
"""Siphon Mesh — equalize N P2P peers to ZERO delta (all-identical) using the
bit-level Weight Siphon.

User directive: make Hermes run to completion on Hy3:free; everything must end
up EQUAL (zero delta) and stay that way. The four named peers are SEED, REED,
DEEP, BEEM — they form a P2P mesh; every pair is siphoned until both sides hold
byte-identical weight shards (water level equal, delta = 0).

Design:
  * Each peer is a disk-backed weight store (directory of chunked shards).
  * A "reference" peer (SEED) is the canonical source; every other peer is
    siphoned TO match SEED. After one full pass all four are identical => the
    mesh is in equilibrium (0 delta everywhere).
  * Idempotent + resumable: re-running only copies what still differs, so the
    mesh self-heals toward zero delta continuously (fits always-on supervisor).
  * Pure stdlib; disk-backed; no torch/GPU. Runs on this host.

Verified by tests/agent/test_siphon_mesh.py (4-peer equalization to zero delta,
partial-drift self-heal, idempotency).
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from agent.weight_siphon import (
    LocalTransport,
    shard_chunk_count,
    shard_from_bytes,
    shard_to_bytes,
    shards_equal,
    siphon_equalize,
)

PEERS = ("SEED", "REED", "DEEP", "BEEM")


class SiphonMesh:
    """Equalize a set of peers so every pair has zero weight delta."""

    def __init__(self, root: Path, shards: List[str], peers: List[str] = list(PEERS)):
        self.root = Path(root)
        self.shards = shards
        self.peers = peers
        for p in peers:
            (self.root / p).mkdir(parents=True, exist_ok=True)

    # ── seed a peer with a weight blob for one or more shards ──────────────
    def plant(self, peer: str, blobs: Dict[str, bytes]) -> None:
        store = self.root / peer
        for name, blob in blobs.items():
            shard_from_bytes(blob, store, name)

    # ── delta measurement (0 = perfect equilibrium) ───────────────────────
    def total_delta(self) -> int:
        """Count differing chunk-pairs across all peer pairs (0 = all equal)."""
        delta = 0
        ref = self.root / self.peers[0]
        for peer in self.peers[1:]:
            pstore = self.root / peer
            for s in self.shards:
                n = max(shard_chunk_count(ref, s), shard_chunk_count(pstore, s))
                ta = LocalTransport(ref)
                tb = LocalTransport(pstore)
                for i in range(n):
                    if ta.get_chunk(s, i) != tb.get_chunk(s, i):
                        delta += 1
        return delta

    def equilibrium(self) -> bool:
        return self.total_delta() == 0

    # ── run the siphon until all peers match SEED (zero delta) ────────────
    def equalize(self) -> Dict[str, object]:
        report: Dict[str, object] = {"copied_chunks": 0, "bytes_copied": 0, "passes": 0}
        # Reference is SEED; siphon every other peer toward it.
        ref = self.root / self.peers[0]
        for peer in self.peers[1:]:
            pstore = self.root / peer
            for s in self.shards:
                st = siphon_equalize(ref, pstore, s)
                report["copied_chunks"] += st["chunks_copied"]
                report["bytes_copied"] += st["bytes_copied"]
            report["passes"] += 1
        report["delta_after"] = self.total_delta()
        report["equilibrium"] = self.equilibrium()
        return report

    def snapshot_bytes(self, peer: str, shard: str) -> bytes:
        store = self.root / peer
        n = shard_chunk_count(store, shard)
        return shard_to_bytes(store, shard, n)

    def all_equal(self) -> bool:
        ref = self.snapshot_bytes(self.peers[0], self.shards[0]) if self.shards else b""
        for peer in self.peers[1:]:
            for s in self.shards:
                if self.snapshot_bytes(peer, s) != self.snapshot_bytes(self.peers[0], s):
                    return False
        return True
