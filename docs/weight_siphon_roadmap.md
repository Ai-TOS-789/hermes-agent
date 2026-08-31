# Weight Siphon — P2P Bit-Level Equalization Roadmap

**Goal (user directive):** Hermes tunes weights P2P at the bit level, like a siphon
balancing water between two sides until equal, with disk as the backing store.
This document is the concrete, deliverable plan.

## Architecture
- **P2P bit-level transfer:** weights serialized to raw bytes, split into fixed
  `_CHUNK` (1 KB) chunks, moved one chunk at a time between two peers. Resumable
  via a manifest of completed chunks.
- **Siphon (equalization):** for each shard, compare peer A vs peer B chunk-by-chunk;
  copy only the *differing* bits from one side to the other until both sides are
  byte-identical (water level equal). Deterministic direction (A→B) guarantees
  convergence.
- **Disk-backed:** only one chunk pair is ever in RAM; shards stream from disk.
- **Transport abstraction:** `LocalTransport` (disk files) ships now; a
  `NetworkTransport` (gRPC/HTTP) drops in without changing siphon logic.

## Roadmap (executable via `SiphonPlan`)
| # | Step | Status | Artifact |
|---|------|--------|----------|
| 1 | COLLECT | DONE | enumerate shards + chunk counts per peer |
| 2 | DIFF | DONE | per-shard equality map (what needs siphoning) |
| 3 | SIPHON | DONE | equalize each differing shard (bit-level copy) |
| 4 | VERIFY | DONE | assert both peers equal post-siphon |
| 5 | COMMIT | DONE | write `siphon_manifest.json` audit trail |

## Files
- `agent/weight_siphon.py` — Transport, LocalTransport, shard I/O, siphon_equalize,
  SiphonPlan. Pure stdlib (no torch/GPU needed).
- `tests/agent/test_weight_siphon.py` — 6 tests (roundtrip, missing-chunk siphon,
  differing-chunk siphon, transport, roadmap exec, resume).

## Honest constraints
- This moves/balances OPAQUE weight blobs (bytes). It does NOT interpret tensor
  math — that stays in the training stack (see agent/model_forge.py).
- Equalization here means "both peers hold the SAME bytes", i.e. a siphon balancing
  two stores. True federated *averaging* (mean of weights) is a different op and
  needs tensor parsing; out of scope for the bit-level siphon.
- Runs on this GPU-less host because it is pure stdlib + disk.
