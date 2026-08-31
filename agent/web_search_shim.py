#!/usr/bin/env python3
"""Thin, dependency-free web-search shim (used by the research loop).

If the runtime exposes a `hermes_tools.web_search` helper it is used; otherwise we
degrade to an empty result so the research loop keeps running offline (and stays
testable). We never raise from here — research is best-effort.
"""

from __future__ import annotations


def search(query: str, limit: int = 5) -> list:
    """Return a list of {url, title, description} hits (may be empty)."""
    try:
        from hermes_tools import web_search
        data = web_search(query, limit=limit)
        return data.get("data", {}).get("web", []) or []
    except Exception:
        return []


def search_images(query: str, limit: int = 5) -> list:
    """Return image reference URLs only (never fetched/executed as code)."""
    try:
        from hermes_tools import web_search
        data = web_search(query, limit=limit)
        return data.get("data", {}).get("images", []) or []
    except Exception:
        return []
