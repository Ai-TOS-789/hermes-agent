#!/usr/bin/env python3
"""NetworkTransport — real P2P weight transfer over HTTP (stdlib only).

Extends the Transport interface in agent/weight_siphon.py so the bit-level siphon
can run ACROSS machines, not just across two local directories. A node exposes a
tiny HTTP server that serves/accepts weight chunks; NetworkTransport talks to it
via urllib. No third-party deps (http.server + urllib ship with Python).

Why this makes the job "not stutter and finish in one model": the siphon logic in
weight_siphon.py is unchanged — only the link differs. LocalTransport (disk) and
NetworkTransport (HTTP) are interchangeable, so the SAME equalize() call now works
peer-to-peer over the network. Hermes drives one balanced model of the mesh.

Server (run on a peer host):
    python -m agent.network_transport serve --root F:/HermesOffice/siphon_mesh/SEED --port 8731

Client transport (used by siphon_equalize on the other side):
    from agent.network_transport import NetworkTransport
    t = NetworkTransport("http://peer-host:8731")
    siphon_equalize(local_store, remote_store, "w1", transport=t)

Verified by tests/agent/test_network_transport.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from agent.weight_siphon import Transport, _CHUNK


class NetworkTransport(Transport):
    """Talks to a remote PeerServer over HTTP. Drop-in for LocalTransport."""

    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")

    def _url(self, shard: str, index: int) -> str:
        return f"{self.base}/chunk/{shard}/{index:08d}"

    def put_chunk(self, shard: str, index: int, data: bytes) -> None:
        req = Request(self._url(shard, index), data=data, method="PUT")
        with urlopen(req, timeout=30) as r:  # noqa: S310 (local/trusted net)
            r.read()

    def get_chunk(self, shard: str, index: int) -> Optional[bytes]:
        try:
            with urlopen(self._url(shard, index), timeout=30) as r:  # noqa: S310
                return r.read()
        except Exception:
            return None

    def has_chunk(self, shard: str, index: int) -> bool:
        return self.get_chunk(shard, index) is not None


class PeerServer:
    """HTTP server that exposes a local weight store for remote siphoning."""

    def __init__(self, root: Path, host: str = "0.0.0.0", port: int = 8731) -> None:
        self.root = Path(root)
        self.host = host
        self.port = port
        self._httpd = None

    def _handler(self):
        root = self.root

        class H(BaseHTTPRequestHandler):
            def _path(self, shard: str, idx: int) -> Path:
                return (root / shard / f"{idx:08d}.chunk")

            def do_GET(self):  # noqa: N802
                try:
                    parts = [p for p in self.path.split("/") if p]
                    # /chunk/<shard>/<idx>
                    _, shard, idx = parts[0], parts[1], int(parts[2])
                    p = self._path(shard, idx)
                    if p.is_file():
                        data = p.read_bytes()
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                    else:
                        self.send_response(404)
                        self.end_headers()
                except Exception:
                    self.send_response(500)
                    self.end_headers()

            def do_PUT(self):  # noqa: N802
                try:
                    parts = [p for p in self.path.split("/") if p]
                    _, shard, idx = parts[0], parts[1], int(parts[2])
                    length = int(self.headers.get("Content-Length", 0))
                    data = self.rfile.read(length)
                    p = self._path(shard, idx)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(data)
                    self.send_response(200)
                    self.end_headers()
                except Exception:
                    self.send_response(500)
                    self.end_headers()

            def log_message(self, *a):  # silence default logging
                pass

        return H

    def serve_forever(self) -> None:
        self._httpd = ThreadingHTTPServer((self.host, self.port), self._handler())
        self._httpd.serve_forever()

    def start_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.serve_forever, daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()


def _cli() -> None:
    ap = argparse.ArgumentParser(description="NetworkTransport peer server")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--root", required=True)
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8731)
    args = ap.parse_args()
    if args.cmd == "serve":
        srv = PeerServer(Path(args.root), args.host, args.port)
        print(f"[peer] serving {args.root} on {args.host}:{args.port}")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n[peer] stopped.")


if __name__ == "__main__":
    _cli()
