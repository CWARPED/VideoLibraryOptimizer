"""Classify a media file as a movie or a series episode (pure, path-based).

Combines two signals, in order of confidence:
1. A SxxExx / NxNN / "Season .. Episode" pattern in the *filename*.
2. A "Season N" / "Saison N" *parent folder* plus an episode-ish number.
Otherwise the file is treated as a movie.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePath

from ..core.enums import MediaKind
from ..core.models import Classification

# --- episode patterns (filename), highest confidence first ---------------
_RE_SXXEXX = re.compile(r"(?i)(?:^|[\s._\-\[])s(\d{1,2})[\s._\-]?e(\d{1,3})(?:[\s._\-]?e\d{1,3})*")
_RE_NXNN = re.compile(r"(?i)(?:^|[\s._\-\[])(\d{1,2})x(\d{2,3})(?![\dp])")
_RE_SEASON_EP = re.compile(
    r"(?i)season[\s._\-]?(\d{1,2}).*?(?:ep?|episode)[\s._\-]?(\d{1,3})"
)
_RE_EP_ONLY = re.compile(r"(?i)(?:^|[\s._\-\[])(?:ep|episode|e)[\s._\-]?(\d{1,3})\b")

# --- season folder ------------------------------------------------------
_RE_SEASON_DIR = re.compile(r"(?i)^(?:season|saison|s)[\s._\-]?(\d{1,2})$")
_RE_SPECIALS_DIR = re.compile(r"(?i)^(?:season\s*0+|saison\s*0+|specials?|extras?)$")

# --- year ---------------------------------------------------------------
_RE_YEAR = re.compile(r"(?:^|[^\d])((?:19|20)\d{2})(?:[^\d]|$)")

# --- release-noise tokens to strip from titles --------------------------
_NOISE_TOKENS = {
    "1080p", "1080i", "720p", "2160p", "4k", "uhd", "480p", "576p",
    "x264", "x265", "h264", "h265", "hevc", "avc", "av1", "10bit", "8bit",
    "bluray", "blu-ray", "brrip", "bdrip", "webrip", "web-dl", "webdl", "web",
    "hdtv", "dvdrip", "remux", "hdr", "hdr10", "dv", "dolby", "vision",
    "aac", "ac3", "eac3", "dts", "dtshd", "truehd", "atmos", "flac", "ddp",
    "ddp5", "dd5", "5", "1", "multi", "vff", "vostfr", "vo", "vf", "subfrench",
    "french", "english", "truefrench", "extended", "unrated", "proper", "repack",
    "imax", "hybrid", "amzn", "nf", "dsnp", "hmax", "atvp",
}


def _strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def slugify(title: str) -> str:
    """Lowercase, accent-free, alnum-only key used to merge series spellings."""
    s = _strip_accents(title).lower()
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return re.sub(r"\s+", "-", s)


def _clean_title(raw: str) -> str:
    """Turn 'Show.Name.2019.1080p.x265' into 'Show Name'."""
    # Cut at a 4-digit year if present (keep what's before it).
    text = raw.replace("_", " ").replace(".", " ")
    text = re.sub(r"[\[\](){}]", " ", text)
    words = [w for w in re.split(r"[\s\-]+", text) if w]
    cleaned: list[str] = []
    for w in words:
        lw = w.lower()
        if lw in _NOISE_TOKENS:
            break  # noise usually marks the end of the real title
        if re.fullmatch(r"(?:19|20)\d{2}", w):
            break
        cleaned.append(w)
    result = " ".join(cleaned).strip(" -")
    return result or " ".join(words).strip()


def _extract_year(raw: str) -> int | None:
    m = _RE_YEAR.search(raw)
    return int(m.group(1)) if m else None


def _season_from_folder(parent_name: str) -> int | None:
    m = _RE_SEASON_DIR.match(parent_name.strip())
    if m:
        return int(m.group(1))
    if _RE_SPECIALS_DIR.match(parent_name.strip()):
        return 0
    return None


def classify(path: str) -> Classification:
    """Classify a single file path. Pure: only inspects the path string."""
    pp = PurePath(path)
    stem = pp.stem
    parent = pp.parent.name
    grandparent = pp.parent.parent.name

    # 1) Filename episode patterns -------------------------------------
    m = _RE_SXXEXX.search(stem)
    if m:
        return _episode_from_filename(stem, parent, int(m.group(1)), int(m.group(2)), m.start())

    m = _RE_SEASON_EP.search(stem)
    if m:
        return _episode_from_filename(stem, parent, int(m.group(1)), int(m.group(2)), m.start())

    m = _RE_NXNN.search(stem)
    if m:
        return _episode_from_filename(stem, parent, int(m.group(1)), int(m.group(2)), m.start())

    # 2) Season folder + episode-ish number ----------------------------
    season = _season_from_folder(parent)
    if season is not None:
        ep_match = _RE_EP_ONLY.search(stem)
        episode = int(ep_match.group(1)) if ep_match else None
        series_title = _clean_title(grandparent) if grandparent else _clean_title(stem)
        return Classification(
            kind=MediaKind.EPISODE,
            series_title=series_title,
            series_slug=slugify(series_title),
            season=season,
            episode=episode,
            title=stem,
        )

    # 3) Movie ---------------------------------------------------------
    title = _clean_title(stem)
    return Classification(
        kind=MediaKind.MOVIE,
        title=title or stem,
        year=_extract_year(stem),
    )


def _episode_from_filename(
    stem: str, parent: str, season: int, episode: int, match_start: int
) -> Classification:
    prefix = stem[:match_start]
    series_title = _clean_title(prefix)
    if not series_title:
        # Pattern at the very start of the name; fall back to the parent folder,
        # unless it's a season folder (then nothing useful is there).
        if _season_from_folder(parent) is None and parent:
            series_title = _clean_title(parent)
    series_title = series_title or "Unknown"
    return Classification(
        kind=MediaKind.EPISODE,
        series_title=series_title,
        series_slug=slugify(series_title),
        season=season,
        episode=episode,
        title=stem,
    )
