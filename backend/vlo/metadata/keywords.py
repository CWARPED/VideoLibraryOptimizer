"""Keyword-based animation detection fallback (offline / no TMDB key)."""

from __future__ import annotations

from ..scan.classifier import _strip_accents

# Default keywords matched (accent-insensitive, lowercased) against the path.
DEFAULT_ANIMATION_KEYWORDS: list[str] = [
    "anime", "animation", "animated", "dessin anime", "dessins animes",
    "cartoon", "manga", "japanime",
]


def looks_like_animation(path: str, keywords: list[str] | None = None) -> bool:
    """True if any keyword appears in the (accent-stripped, lowercased) path."""
    kws = keywords if keywords is not None else DEFAULT_ANIMATION_KEYWORDS
    haystack = _strip_accents(path).lower()
    return any(_strip_accents(kw).lower() in haystack for kw in kws)
