#!/usr/bin/env python3
"""Web + image search for the research loop.

Resolution order (best-effort, never raises):
1. If the runtime exposes ``hermes_tools.web_search``, use it.
2. Else fall back to a lightweight urllib query against DuckDuckGo Lite (no API key,
   no extra deps) so research_loop actually retrieves references on this machine.

Only URLs/notes are returned — never executed as code (security + YAGNI).
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from html import unescape


def _ddg_lite(query: str, limit: int = 5) -> list:
    try:
        q = urllib.parse.quote(query)
        url = f"https://lite.duckduckgo.com/lite/?q={q}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (research)"})
        html = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "ignore")
        # DDG Lite results: <a class="result-link" href="...">title</a>
        rows = re.findall(r'<a[^>]*class="result-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S)
        hits = []
        for href, title in rows[:limit]:
            hits.append({
                "url": unescape(href),
                "title": re.sub(r"<[^>]+>", "", unescape(title)).strip(),
                "description": "",
            })
        # Fallback to any href containing http if the class selector missed.
        if not hits:
            for href in re.findall(r'href="(https?://[^"]+)"', html)[:limit]:
                if "duckduckgo" not in href:
                    hits.append({"url": href, "title": "", "description": ""})
        return hits
    except Exception:
        return []


def search(query: str, limit: int = 5) -> list:
    """Return a list of {url, title, description} hits (may be empty)."""
    try:
        from hermes_tools import web_search
        data = web_search(query, limit=limit)
        hits = data.get("data", {}).get("web", []) or []
        if hits:
            return hits
    except Exception:
        pass
    return _ddg_lite(query, limit)


def search_images(query: str, limit: int = 5) -> list:
    """Return image reference URLs only (never fetched/executed as code)."""
    try:
        from hermes_tools import web_search
        data = web_search(query, limit=limit)
        imgs = data.get("data", {}).get("images", []) or []
        if imgs:
            return imgs
    except Exception:
        pass
    # Images via DDG Lite with image-ish query; reuse same endpoint for refs.
    return _ddg_lite(f"{query} image reference", limit)
