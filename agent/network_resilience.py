#!/usr/bin/env python3
"""Network resilience layer — retry, backoff, circuit breaker, fallback.

Hermes improves itself against 429/404/network errors:
  * Retry with exponential backoff for transient 429/502/503/timeout.
  * Circuit breaker per endpoint: opens after N failures in a window, skips calls,
    recovers after a cooldown.
  * 404 mapped to graceful fallback (content missing) instead of hard crash.
  * Offline-tolerant: every network path degrades gracefully, never crashes the caller.
  * Self-healing metrics: record failure/success per endpoint for monitoring + tuning.
"""

from __future__ import annotations

import http.client
import json
import socket
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

import os as _os

_OFFICE = Path(_os.environ.get("HERMES_OFFICE", "")) or (
    Path(r"F:/HermesOffice") if Path(r"F:/").exists()
    else Path(_os.environ.get("HERMES_HOME", r"C:\Users\w3ce\AppData\Local\hermes")) / "HermesOffice"
)

# ── Configurable retry/backoff parameters (user-tunable via _NETWORK_RESILIENCE_CONFIG) ──
_DEFAULT_CONFIG = {
    "max_retries": 3,
    "base_delay": 1.0,       # seconds, first backoff
    "max_delay": 60.0,       # cap backoff
    "backoff_factor": 2.0,   # exponential factor
    "retry_429": True,
    "retry_5xx": True,
    "retry_timeout": True,
    "retry_404": False,      # 404 is usually permanent, don't retry
    "retry_on_content_mismatch": False,
}

# ── Circuit breaker state (per endpoint) ──
class CircuitState:
    CLOSED = "closed"       # normal, calls go through
    OPEN = "open"           # failing, calls short-circuit
    HALF_OPEN = "half_open" # testing if recovered


class CircuitBreaker:
    """Per-endpoint circuit breaker.

    Opens after ``fail_threshold`` failures within ``window`` seconds.
    Half-open after ``recover_time``; on success in half-open, closes.
    """

    def __init__(
        self,
        endpoint: str,
        fail_threshold: int = 5,
        window: float = 60.0,
        recover_time: float = 30.0,
        record_dir: Optional[Path] = None,
    ) -> None:
        self.endpoint = endpoint
        self.fail_threshold = fail_threshold
        self.window = window
        self.recover_time = recover_time
        self.state = CircuitState.CLOSED
        self._fails: list[float] = []       # timestamps of recent failures
        self._last_fail: Optional[float] = None
        self._record_dir = record_dir or (_OFFICE / "network_metrics")
        self._record_file = self._record_dir / f"circuit_{endpoint}.json"

    def _save(self) -> None:
        try:
            self._record_dir.mkdir(parents=True, exist_ok=True)
            self._record_file.write_text(
                json.dumps({"state": self.state, "endpoint": self.endpoint,
                            "last_fail": self._last_fail, "fails": self._fails},
                           default=str), encoding="utf-8")
        except Exception:
            pass

    def is_allowed(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if self._last_fail is None:
                return True
            if (time.time() - self._last_fail) >= self.recover_time:
                self.state = CircuitState.HALF_OPEN
                self._save()
                return True
            return False
        if self.state == CircuitState.HALF_OPEN:
            return True
        return True

    def record_success(self) -> None:
        self.state = CircuitState.CLOSED
        self._fails.clear()
        self._last_fail = None
        self._save()

    def record_failure(self) -> None:
        now = time.time()
        self._fails.append(now)
        self._last_fail = now
        # Prune old failures outside the window
        cutoff = now - self.window
        self._fails = [t for t in self._fails if t > cutoff]
        if len(self._fails) >= self.fail_threshold:
            self.state = CircuitState.OPEN
        self._save()

    def snapshot(self) -> dict:
        return {"endpoint": self.endpoint, "state": self.state,
                "recent_fails": len(self._fails),
                "last_fail": self._last_fail,
                "fail_threshold": self.fail_threshold,
                "window": self.window}


# ── Global registry of circuits (in-memory; survive only this process) ──
_circuits: dict[str, CircuitBreaker] = {}
_circuit_lock: Optional[Any] = None  # placeholder for future threading.Lock if needed


def get_circuit(endpoint: str) -> CircuitBreaker:
    if endpoint not in _circuits:
        _circuits[endpoint] = CircuitBreaker(endpoint)
    return _circuits[endpoint]


# ── Exponential backoff retry ──
def _should_retry(status: Optional[int], err: Optional[Exception],
                   config: dict) -> bool:
    if config.get("retry_on_content_mismatch") and err is None:
        return False
    if status is None and err is None:
        return False
    if status is not None:
        if status == 429 and config.get("retry_429"):
            return True
        if 500 <= status < 600 and config.get("retry_5xx"):
            return True
        if status == 404 and config.get("retry_404"):
            return True
    if err is not None:
        if isinstance(err, (socket.timeout, TimeoutError)):
            return config.get("retry_timeout", True)
        if isinstance(err, (URLError, ConnectionError, OSError)):
            return True
    return False


def _backoff_delay(attempt: int, config: dict) -> float:
    base = config.get("base_delay", 1.0)
    factor = config.get("backoff_factor", 2.0)
    cap = config.get("max_delay", 60.0)
    delay = base * (factor ** attempt)
    return min(delay, cap)


def _is_http_status(err: Exception) -> Optional[int]:
    """Extract HTTP status from URLError/HTTPError if possible."""
    # urllib.error.HTTPError has a 'code' attribute
    try:
        code = getattr(err, "code", None)
        if code is not None:
            return int(code)
    except Exception:
        pass
    # socket.timeout / TimeoutError → treat as timeout
    return None


def resilient_fetch(
    url: str,
    config: Optional[dict] = None,
    circuit_endpoint: Optional[str] = None,
    on_404: Optional[Callable[[str], Any]] = None,
    on_error: Optional[Callable[[str, Exception], Any]] = None,
    timeout: float = 30.0,
    headers: Optional[dict] = None,
) -> Any:
    """Fetch ``url`` with retry + backoff + circuit breaker + graceful fallback.

    Returns the decoded response body (str) on success, or the result of the
    appropriate fallback handler (on_404/on_error) when those fire. Never raises
    for network errors — always degrades gracefully.
    """
    cfg = dict(_DEFAULT_CONFIG)
    if config:
        cfg.update(config)

    endpoint = circuit_endpoint or url
    circuit = get_circuit(endpoint)

    if not circuit.is_allowed():
        err = RuntimeError(f"circuit-open: {endpoint}")
        if on_error:
            return on_error(url, err)
        return {"error": "circuit-open", "url": url, "endpoint": endpoint}

    last_err: Optional[Exception] = None
    last_status: Optional[int] = None

    for attempt in range(cfg.get("max_retries", 3) + 1):
        try:
            req = Request(url, headers=headers or {})
            req.add_header("User-Agent", "Hermes/1.0 (network-resilience)")
            with urlopen(req, timeout=timeout) as resp:
                status = resp.status
                body = resp.read().decode("utf-8", "ignore")
                if 200 <= status < 300:
                    circuit.record_success()
                    return body
                # Non-2xx but not error-triggering (e.g. 304)? treat as content.
                last_status = status
                last_err = None
                # For non-success but non-retryable: fall through to error handling
                if not _should_retry(status, None, cfg):
                    break
        except Exception as e:
            last_err = e
            last_status = _is_http_status(e)
            if not _should_retry(last_status, e, cfg):
                # If it's a 404 without retry, route to on_404
                if last_status == 404 and on_404:
                    circuit.record_failure()
                    return on_404(url)
                break
            # Record failure before retry
            circuit.record_failure()
        # Backoff before retry (skip delay on first attempt = 0)
        if attempt < cfg.get("max_retries", 3):
            time.sleep(_backoff_delay(attempt, cfg))

    # Exhausted retries (or non-retryable) → fallback
    if last_status == 404 and on_404:
        return on_404(url)
    if last_err is not None and on_error:
        return on_error(url, last_err)
    # Default: return error info so caller can decide
    return {"error": True, "url": url,
            "status": last_status,
            "message": str(last_err) if last_err else (f"HTTP {last_status}" if last_status else "unknown")}


# ── Higher-level convenience wrappers ──


def fetch_json(url: str, **kwargs) -> Any:
    """Fetch and decode JSON with full resilience. Returns parsed JSON or error dict."""
    result = resilient_fetch(url, **kwargs)
    if isinstance(result, dict) and result.get("error"):
        return result
    import json as _json
    try:
        return _json.loads(result)
    except Exception as e:
        return {"error": "json-parse", "message": str(e), "raw_preview": (str(result)[:200])}


def fetch_text(url: str, **kwargs) -> str:
    """Fetch text with resilience; returns the body or an error string."""
    result = resilient_fetch(url, **kwargs)
    if isinstance(result, dict) and result.get("error"):
        return f"ERROR: {result.get('message', result)}"
    return result  # already a string


def set_circuit_state(endpoint: str, state: str) -> dict:
    """Manually force a circuit state (for diagnostics / manual recovery)."""
    if state not in (CircuitState.CLOSED, CircuitState.OPEN, CircuitState.HALF_OPEN):
        return {"error": "invalid-state", "allowed": "closed/open/half_open"}
    c = get_circuit(endpoint)
    c.state = state
    c._save()
    return c.snapshot()


def list_circuits() -> dict:
    """Return snapshot of all known circuits."""
    return {ep: c.snapshot() for ep, c in _circuits.items()}


def reset_circuit(endpoint: str) -> dict:
    """Reset a circuit to closed (clear failures)."""
    c = get_circuit(endpoint)
    c.state = CircuitState.CLOSED
    c._fails.clear()
    c._last_fail = None
    c._save()
    return c.snapshot()


# ── Self-improvement: periodic health review ──
def network_health_report() -> dict:
    """Return a concise network health report across all known circuits."""
    circuits = list_circuits()
    closed = sum(1 for c in circuits.values() if c["state"] == "closed")
    open_ = sum(1 for c in circuits.values() if c["state"] == "open")
    half = sum(1 for c in circuits.values() if c["state"] == "half_open")
    return {
        "circuits": circuits,
        "summary": {
            "total": len(circuits),
            "closed": closed,
            "open": open_,
            "half_open": half,
            "healthy_ratio": (closed / max(len(circuits), 1)),
        },
    }


# ── Record error classification for self-learning ──
def record_error_classification(endpoint: str, status: Optional[int],
                                error_type: str, detail: str) -> None:
    """Append an error-classification record so Hermes can learn from failures.

    Used by: resilient_fetch callers that want to feed network learning.
    Stored as JSONL in the office.
    """
    rec = {
        "endpoint": endpoint,
        "status": status,
        "error_type": error_type,  # "429","404","timeout","connection","unknown"
        "detail": detail[:500],
        "ts": int(time.time()),
    }
    try:
        (_OFFICE / "network_metrics" / "error_classifications.jsonl").parent.mkdir(
            parents=True, exist_ok=True)
        (_OFFICE / "network_metrics" / "error_classifications.jsonl").open(
            "a", encoding="utf-8").write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


def classify_error(err: Exception, status: Optional[int] = None) -> str:
    """Return a simple error-type label for logging / learning."""
    if status is not None:
        if status == 429:
            return "429"
        if status == 404:
            return "404"
        if 500 <= status < 600:
            return "5xx"
    if isinstance(err, socket.timeout) or isinstance(err, TimeoutError):
        return "timeout"
    if isinstance(err, (URLError, ConnectionError, OSError)):
        return "connection"
    return "unknown"


# ── Self-test (when run directly) ──
if __name__ == "__main__":
    print("[network_resilience] module loaded. Run from Hermes to use.")
    print("Available functions: resilient_fetch, fetch_json, fetch_text,"
          " get_circuit, set_circuit_state, list_circuits, reset_circuit,"
          " network_health_report, record_error_classification, classify_error")
