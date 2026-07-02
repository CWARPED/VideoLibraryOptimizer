"""Tests for the ffprobe JSON parser (pure, no ffprobe needed)."""

from __future__ import annotations

from vlo.probe.parser import parse_fraction, parse_probe


def test_parse_fraction():
    assert abs(parse_fraction("24000/1001") - 23.976) < 0.01
    assert parse_fraction("25/1") == 25.0
    assert parse_fraction("30") == 30.0
    assert parse_fraction("0/0") == 0.0
    assert parse_fraction(None) == 0.0
    assert parse_fraction("") == 0.0


def _mkv_two_audio():
    return {
        "format": {"duration": "3600.0", "bit_rate": "10000000", "size": "4500000000"},
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "r_frame_rate": "24000/1001",
                "pix_fmt": "yuv420p",
            },
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "dts",
                "profile": "DTS-HD MA",
                "channels": 6,
                "tags": {"language": "fre", "BPS-eng": "1536000"},
            },
            {
                "index": 2,
                "codec_type": "audio",
                "codec_name": "ac3",
                "channels": 6,
                "bit_rate": "640000",
                "tags": {"language": "eng"},
            },
            {
                "index": 3,
                "codec_type": "subtitle",
                "codec_name": "hdmv_pgs_subtitle",
                "tags": {"language": "fre"},
                "disposition": {"default": 1},
            },
        ],
    }


def test_parse_basic_mkv():
    p = parse_probe(_mkv_two_audio(), "X.mkv", 4_500_000_000)
    assert p.width == 1920 and p.height == 1080
    assert abs(p.fps - 23.976) < 0.01
    assert p.vcodec == "h264"
    assert p.n_audio == 2
    assert p.n_subs == 1
    assert p.audio[0].language == "fre"
    assert p.audio[0].profile == "DTS-HD MA"  # captured for lossless detection
    assert p.audio[0].bitrate_bps == 1_536_000  # from BPS-eng tag
    assert p.audio[1].bitrate_bps == 640_000
    assert p.subs[0].codec == "hdmv_pgs_subtitle"
    assert p.subs[0].default is True
    # video bitrate = overall - audio = 10_000_000 - (1_536_000 + 640_000)
    assert p.video_bitrate_bps == 10_000_000 - 2_176_000


def test_video_bitrate_falls_back_to_overall_when_no_audio_bitrate():
    data = _mkv_two_audio()
    # Strip audio bitrate info -> can't subtract, attribute ~97% to video.
    for st in data["streams"]:
        st.pop("bit_rate", None)
        st.pop("tags", None)
    p = parse_probe(data, "X.mkv", 4_500_000_000)
    assert p.video_bitrate_bps == int(10_000_000 * 0.97)


def test_overall_bitrate_computed_from_size_when_absent():
    data = _mkv_two_audio()
    data["format"].pop("bit_rate")
    p = parse_probe(data, "X.mkv", 9_000_000_000)  # 9 GB / 3600s
    assert p.overall_bitrate_bps == int(9_000_000_000 * 8 / 3600)


def test_hdr_detection():
    data = _mkv_two_audio()
    data["streams"][0]["color_transfer"] = "smpte2084"
    data["streams"][0]["color_primaries"] = "bt2020"
    p = parse_probe(data, "X.mkv", 4_500_000_000)
    assert p.is_hdr is True
    assert p.is_dolby_vision is False


def test_dolby_vision_detection_via_tag():
    data = _mkv_two_audio()
    data["streams"][0]["codec_tag_string"] = "dvhe"
    p = parse_probe(data, "X.mkv", 4_500_000_000)
    assert p.is_dolby_vision is True


def test_dolby_vision_detection_via_side_data():
    data = _mkv_two_audio()
    data["streams"][0]["side_data_list"] = [
        {"side_data_type": "DOVI configuration record", "dv_profile": 8}
    ]
    p = parse_probe(data, "X.mkv", 4_500_000_000)
    assert p.is_dolby_vision is True


def test_cover_art_video_stream_skipped():
    data = _mkv_two_audio()
    # Insert a cover-art "video" stream before the real one.
    data["streams"].insert(
        0,
        {
            "index": 9,
            "codec_type": "video",
            "codec_name": "mjpeg",
            "width": 600,
            "height": 900,
            "disposition": {"attached_pic": 1},
        },
    )
    p = parse_probe(data, "X.mkv", 4_500_000_000)
    assert p.vcodec == "h264"  # real stream, not the mjpeg cover
    assert p.width == 1920


def test_mp4_mov_text_subtitle():
    data = {
        "format": {"duration": "1200.0", "bit_rate": "5000000"},
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264",
             "width": 1280, "height": 720, "r_frame_rate": "25/1"},
            {"index": 1, "codec_type": "audio", "codec_name": "aac", "bit_rate": "128000"},
            {"index": 2, "codec_type": "subtitle", "codec_name": "mov_text",
             "tags": {"language": "eng"}},
        ],
    }
    p = parse_probe(data, "X.mp4", 750_000_000)
    assert p.subs[0].codec == "mov_text"
    assert p.n_subs == 1
