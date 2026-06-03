"""Minimal TMDB client for content-type (Animation) detection.

Uses the stdlib (urllib) — no extra dependency. Network/credential failures
never propagate: ``lookup`` returns None so callers fall back to keywords.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger("vlo.metadata")

_BASE = "https://api.themoviedb.org/3"
_ANIMATION_GENRE_ID = 16


@dataclass(slots=True)
class GenreInfo:
    content_type: str  # "animation" | "live_action"
    is_anime: bool
    genres: list[int]
    tmdb_id: int | None


class TmdbClient:
    def __init__(self, api_key: str, *, timeout: float = 8.0) -> None:
        self._api_key = api_key
        self._timeout = timeout

    def lookup(self, title: str, year: int | None, kind: str) -> GenreInfo | None:
        """kind: 'movie' or 'tv'. Returns GenreInfo or None on miss/error."""
        if not self._api_key or not title:
            return None
        endpoint = "search/tv" if kind == "tv" else "search/movie"
        params = {"api_key": self._api_key, "query": title, "include_adult": "false"}
        if year and kind == "movie":
            params["year"] = str(year)
        url = f"{_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
        data = self._get_json(url)
        if data is None:
            return None
        results = data.get("results") or []
        if not results:
            return None
        first = results[0]
        genres = [int(g) for g in (first.get("genre_ids") or [])]
        is_animation = _ANIMATION_GENRE_ID in genres
        original_language = (first.get("original_language") or "").lower()
        origin = first.get("origin_country") or []
        is_anime = is_animation and (original_language == "ja" or "JP" in origin)
        return GenreInfo(
            content_type="animation" if is_animation else "live_action",
            is_anime=is_anime,
            genres=genres,
            tmdb_id=first.get("id"),
        )

    def _get_json(self, url: str) -> dict | None:
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            logger.warning("TMDB request failed: %s", exc)
            return None
