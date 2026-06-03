"""Helpers to turn a named profile + codec into concrete encode parameters."""

from __future__ import annotations

from ..core.enums import Codec
from ..core.models import EncodeProfile


def resolve_encode_params(profile: EncodeProfile, codec: Codec) -> tuple[int, str]:
    """Return (crf, preset) for the given codec from a profile.

    Preset is returned as a string for both codecs (x265 uses words like
    ``slow``; SVT-AV1 uses an integer rendered as text).
    """
    if codec is Codec.X265:
        return profile.crf_x265, profile.preset_x265
    return profile.crf_av1, str(profile.preset_av1)


def params_for(profile: EncodeProfile, codec: Codec) -> str:
    """Return the encoder-specific params string for the given codec."""
    return profile.x265_params if codec is Codec.X265 else profile.svtav1_params
