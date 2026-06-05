"""Tests for output filename / title helpers (pure)."""

from __future__ import annotations

from vlo.core.enums import Codec
from vlo.naming import output_stem, rewrite_codec_tokens


def test_rewrite_codec_tokens_x265():
    assert rewrite_codec_tokens("Movie 1080p BluRay x264-GRP", Codec.X265) \
        == "Movie 1080p BluRay x265-GRP"
    assert rewrite_codec_tokens("Show.AVC.DTS", Codec.X265) == "Show.x265.DTS"
    assert rewrite_codec_tokens("Film h.264", Codec.X265) == "Film x265"


def test_rewrite_codec_tokens_av1():
    assert rewrite_codec_tokens("Movie x265-VLO", Codec.SVTAV1) == "Movie AV1-VLO"
    assert rewrite_codec_tokens("Movie HEVC", Codec.SVTAV1) == "Movie AV1"


def test_rewrite_legacy_codecs():
    # Real legacy tokens seen on the NAS: VC-1 remux, XviD, DivX.
    assert rewrite_codec_tokens("HP.2001.BluRay.REMUX.VC-1.DTS", Codec.X265) \
        == "HP.2001.BluRay.REMUX.x265.DTS"
    assert rewrite_codec_tokens("Frank.2014.BRRiP.XviD.AC3", Codec.X265) \
        == "Frank.2014.BRRiP.x265.AC3"


def test_rewrite_no_codec_token_unchanged():
    # Standard Radarr name has no codec token -> rewriting is a no-op.
    assert rewrite_codec_tokens("Alien (1979) WEBDL-1080p", Codec.X265) \
        == "Alien (1979) WEBDL-1080p"


def test_rewrite_does_not_touch_substrings():
    # "avcodec" must not be partially rewritten via the "avc" token.
    assert rewrite_codec_tokens("the avcodec library", Codec.X265) == "the avcodec library"


def test_output_stem_tag_only():
    assert output_stem("Alien (1979) WEBDL-1080p", Codec.X265, tag=" x265-VLO") \
        == "Alien (1979) WEBDL-1080p x265-VLO"


def test_output_stem_rewrite_and_tag():
    assert output_stem("Movie x264-GRP", Codec.X265, tag=" [VLO]", rewrite=True) \
        == "Movie x265-GRP [VLO]"


def test_output_stem_default_noop():
    assert output_stem("Movie (2020) Bluray-1080p", Codec.X265) == "Movie (2020) Bluray-1080p"
