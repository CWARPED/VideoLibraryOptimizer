"""Output filename / title helpers (pure).

Two independent, opt-in transforms on the output name:
- ``filename_tag``: a user-defined suffix appended to the stem (e.g. " x265").
- ``rewrite_codec_tags``: replace codec tokens in the name (x264/AVC/HEVC…) with
  the target codec, so a re-encoded file's name reflects the new codec.
Both default off; the library is mostly Radarr-style ``Title (Year) Source-Res``
which has no codec token, so rewriting is a no-op there and the tag is the main
mechanism.
"""

from __future__ import annotations

import re

from .core.enums import Codec

# Whole-token codec markers (not matching inside longer words). Covers the
# codecs actually seen across the library: x264/x265, h264/h265, AVC/HEVC,
# AV1, VC-1, XviD/DivX, VP9, MPEG-2.
_CODEC_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(x264|x265|h\.?264|h\.?265|avc|hevc|av1|vc-?1|xvid|divx|vp9|mpeg-?2)"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def codec_token(codec: Codec) -> str:
    """The filename token for a target codec."""
    return "x265" if codec is Codec.X265 else "AV1"


def rewrite_codec_tokens(text: str, codec: Codec) -> str:
    """Replace any codec marker in ``text`` with the target codec token."""
    return _CODEC_RE.sub(codec_token(codec), text)


def output_stem(original_stem: str, codec: Codec, *, tag: str = "", rewrite: bool = False) -> str:
    """Build the output filename stem from the source stem + settings."""
    stem = rewrite_codec_tokens(original_stem, codec) if rewrite else original_stem
    if tag:
        stem = f"{stem}{tag}"
    return stem
