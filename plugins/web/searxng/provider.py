"""SearXNG search — plugin form.

Subclasses :class:`agent.web_search_provider.WebSearchProvider`. Same JSON
API call (``/search?format=json``), same result normalization. The legacy
in-tree module ``tools.web_providers.searxng`` was removed in the same
commit that moved this code under ``plugins/``; this file is now the
canonical implementation.

Search-only — SearXNG aggregates results from upstream engines but does not
fetch/extract arbitrary URLs. ``supports_extract()`` returns False.

Config keys this provider responds to::

    web:
      search_backend: "searxng"     # explicit per-capability
      backend: "searxng"            # shared fallback

Env var::

    SEARXNG_URL=http://localhost:8080
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)


def _searxng_url() -> str:
    """Return SEARXNG_URL from Hermes config-aware env, falling back to process env."""
    try:
        from hermes_cli.config import get_env_value

        val = get_env_value("SEARXNG_URL")
    except Exception:
        val = None
    if val is None:
        val = os.getenv("SEARXNG_URL", "")
    return (val or "").strip()


def _dead_engines(data: Dict[str, Any]) -> list[str]:
    """Return ``"engine (reason)"`` for every engine SearXNG could not reach.

    SearXNG answers HTTP 200 with ``results: []`` when its upstream engines are
    rate-limited or CAPTCHA-walled, which is byte-for-byte indistinguishable
    from a genuine zero-hit search at the tool's output level. The only signal
    is ``unresponsive_engines``: a list of ``[engine, reason]`` pairs (a third
    element appears on some versions).
    """
    entries = data.get("unresponsive_engines") or []
    dead: list[str] = []
    for entry in entries:
        if isinstance(entry, (list, tuple)) and entry:
            name = str(entry[0])
            reason = str(entry[1]) if len(entry) > 1 and entry[1] else ""
            dead.append(f"{name} ({reason})" if reason else name)
        elif entry:
            dead.append(str(entry))
    return dead


class SearXNGWebSearchProvider(WebSearchProvider):
    """Search via a user-hosted SearXNG instance."""

    @property
    def name(self) -> str:
        return "searxng"

    @property
    def display_name(self) -> str:
        return "SearXNG"

    def is_available(self) -> bool:
        """Return True when ``SEARXNG_URL`` is set."""
        return bool(_searxng_url())

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Execute a search against the configured SearXNG instance."""
        import httpx

        base_url = _searxng_url().rstrip("/")
        if not base_url:
            return {"success": False, "error": "SEARXNG_URL is not set"}

        params: Dict[str, Any] = {
            "q": query,
            "format": "json",
            "pageno": 1,
        }

        try:
            resp = httpx.get(
                f"{base_url}/search",
                params=params,
                timeout=15,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning("SearXNG HTTP error: %s", exc)
            return {
                "success": False,
                "error": f"SearXNG returned HTTP {exc.response.status_code}",
            }
        except httpx.RequestError as exc:
            logger.warning("SearXNG request error: %s", exc)
            return {
                "success": False,
                "error": f"Could not reach SearXNG at {base_url}: {exc}",
            }

        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("SearXNG response parse error: %s", exc)
            return {
                "success": False,
                "error": "Could not parse SearXNG response as JSON",
            }

        raw_results = data.get("results", [])
        dead_engines = _dead_engines(data)

        # SearXNG may return a score field; sort descending and cap to limit.
        sorted_results = sorted(
            raw_results,
            key=lambda r: float(r.get("score", 0)),
            reverse=True,
        )[:limit]

        web_results = [
            {
                "title": str(r.get("title", "")),
                "url": str(r.get("url", "")),
                "description": str(r.get("content", "")),
                "position": i + 1,
            }
            for i, r in enumerate(sorted_results)
        ]

        logger.info(
            "SearXNG search '%s': %d results (from %d raw, limit %d)%s",
            query,
            len(web_results),
            len(raw_results),
            limit,
            f", dead engines: {', '.join(dead_engines)}" if dead_engines else "",
        )

        # No results AND dead engines is a backend failure, not an empty
        # search: report it as one so the caller cannot read it as "the web
        # has nothing on this" (#TJS-211). Returning success=False also lets
        # the keyless rescue ring in tools/web_tools.py serve this call.
        if dead_engines and not raw_results:
            return {
                "success": False,
                "error": (
                    "SearXNG returned no results because every upstream engine "
                    f"failed: {', '.join(dead_engines)}. This is a backend "
                    "outage, not evidence that no results exist."
                ),
            }

        data_out: Dict[str, Any] = {"web": web_results}
        if dead_engines:
            # Partial coverage: keep the results but mark them incomplete,
            # reusing the annotation key the rescue path already sets.
            data_out["backend_error"] = (
                "Degraded search: these SearXNG engines failed and their "
                f"results are missing: {', '.join(dead_engines)}. Coverage is "
                "incomplete; absence of a result here is not evidence of absence."
            )

        return {"success": True, "data": data_out}

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "SearXNG",
            "badge": "free · self-hosted",
            "tag": "Free, privacy-respecting metasearch. Point SEARXNG_URL at your instance.",
            "env_vars": [
                {
                    "key": "SEARXNG_URL",
                    "prompt": "SearXNG instance URL (e.g. http://localhost:8080)",
                    "url": "https://searx.space/",
                },
            ],
        }
