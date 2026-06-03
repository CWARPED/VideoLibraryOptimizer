"""Normalise raw ffprobe JSON into a :class:`ProbeResult` (pure, no I/O)."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from ..core.models import AudioTrack, ProbeResult, SubTrack

_HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
_HDR_PRIMARIES = {"bt2020"}
_HDR_SPACES = {"bt2020nc", "bt2020c"}
_DV_TAGS = {"dvhe", "dvh1", "dav1", "dvav"}


def parse_fraction(value: str | None) -> float:
    """Parse an ffprobe rational like '24000/1001' into a float (0.0 on failure)."""
    if not value:
        return 0.0
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        return float(value)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _stream_bitrate(stream: dict[str, Any]) -> int | None:
    """Bitrate of a stream from `bit_rate` or the MKV `BPS`/`BPS-eng` tags."""
    br = _to_int(stream.get("bit_rate"))
    if br:
        return br
    tags = stream.get("tags") or {}
    for key, val in tags.items():
        if key.upper().startswith("BPS"):
            br = _to_int(val)
            if br:
                return br
    return None


def _lang(stream: dict[str, Any]) -> str | None:
    tags = stream.get("tags") or {}
    return tags.get("language") or tags.get("LANGUAGE")


def _title(stream: dict[str, Any]) -> str | None:
    tags = stream.get("tags") or {}
    return tags.get("title") or tags.get("TITLE")


def _is_dolby_vision(stream: dict[str, Any]) -> bool:
    if (stream.get("codec_tag_string") or "").lower() in _DV_TAGS:
        return True
    for sd in stream.get("side_data_list") or []:
        text = json.dumps(sd).lower()
        if "dolby vision" in text or "dovi" in text:
            return True
    return False


def _is_hdr(stream: dict[str, Any]) -> bool:
    transfer = (stream.get("color_transfer") or "").lower()
    primaries = (stream.get("color_primaries") or "").lower()
    space = (stream.get("color_space") or "").lower()
    return (
        transfer in _HDR_TRANSFERS
        or primaries in _HDR_PRIMARIES
        or space in _HDR_SPACES
    )


def parse_probe(data: dict[str, Any], path: str, size_bytes: int) -> ProbeResult:
    """Turn raw ffprobe JSON into a normalised :class:`ProbeResult`."""
    fmt = data.get("format") or {}
    streams = data.get("streams") or []

    duration_s = parse_fraction(fmt.get("duration"))
    overall_bitrate = _to_int(fmt.get("bit_rate"))
    if not overall_bitrate and duration_s > 0:
        overall_bitrate = int(size_bytes * 8 / duration_s)
    overall_bitrate = overall_bitrate or 0

    video_stream: dict[str, Any] | None = None
    audio: list[AudioTrack] = []
    subs: list[SubTrack] = []

    for st in streams:
        kind = st.get("codec_type")
        if kind == "video":
            # Skip embedded cover art / thumbnails.
            disp = st.get("disposition") or {}
            if disp.get("attached_pic"):
                continue
            if video_stream is None:
                video_stream = st
        elif kind == "audio":
            disp = st.get("disposition") or {}
            audio.append(
                AudioTrack(
                    index=st.get("index", len(audio)),
                    codec=st.get("codec_name", "?"),
                    channels=_to_int(st.get("channels")),
                    channel_layout=st.get("channel_layout"),
                    language=_lang(st),
                    title=_title(st),
                    bitrate_bps=_stream_bitrate(st),
                )
            )
        elif kind == "subtitle":
            disp = st.get("disposition") or {}
            subs.append(
                SubTrack(
                    index=st.get("index", len(subs)),
                    codec=st.get("codec_name", "?"),
                    language=_lang(st),
                    title=_title(st),
                    forced=bool(disp.get("forced")),
                    default=bool(disp.get("default")),
                )
            )

    if video_stream is None:
        # No real video stream: caller (scanner) will exclude it.
        width = height = 0
        fps = 0.0
        vcodec = ""
        pix_fmt = None
        is_hdr = is_dv = False
        video_bitrate = 0
        color_primaries = color_transfer = color_space = None
    else:
        width = _to_int(video_stream.get("width")) or 0
        height = _to_int(video_stream.get("height")) or 0
        fps = parse_fraction(
            video_stream.get("r_frame_rate") or video_stream.get("avg_frame_rate")
        )
        vcodec = video_stream.get("codec_name", "")
        pix_fmt = video_stream.get("pix_fmt")
        is_hdr = _is_hdr(video_stream)
        is_dv = _is_dolby_vision(video_stream)
        video_bitrate = _compute_video_bitrate(video_stream, overall_bitrate, audio)
        color_primaries = video_stream.get("color_primaries")
        color_transfer = video_stream.get("color_transfer")
        color_space = video_stream.get("color_space")

    n_chapters = len(data.get("chapters") or [])

    raw_blob = json.dumps(
        {"_audio": [asdict(a) for a in audio], "_subs": [asdict(s) for s in subs]}
    )

    return ProbeResult(
        path=path,
        size_bytes=size_bytes,
        duration_s=duration_s,
        width=width,
        height=height,
        fps=fps,
        vcodec=vcodec,
        pix_fmt=pix_fmt,
        is_hdr=is_hdr,
        is_dolby_vision=is_dv,
        video_bitrate_bps=video_bitrate,
        overall_bitrate_bps=overall_bitrate,
        audio=audio,
        subs=subs,
        n_chapters=n_chapters,
        color_primaries=color_primaries,
        color_transfer=color_transfer,
        color_space=color_space,
        raw_json=raw_blob,
    )


def _compute_video_bitrate(
    video_stream: dict[str, Any], overall_bitrate: int, audio: list[AudioTrack]
) -> int:
    """Best-effort video bitrate (MKV rarely exposes a per-stream value).

    1. Use the video stream's own bit_rate/BPS tag if present.
    2. Else overall − Σ(audio bitrates), if that yields a plausible value.
    3. Else 0 (scoring will mark the file as not analysable).
    """
    direct = _stream_bitrate(video_stream)
    if direct:
        return direct
    if overall_bitrate:
        audio_total = sum(a.bitrate_bps or 0 for a in audio)
        if audio_total > 0:
            remainder = overall_bitrate - audio_total
            # Require the remainder to be a sane fraction of the overall bitrate.
            if remainder >= overall_bitrate * 0.3:
                return remainder
        else:
            # Audio bitrates unknown: attribute most of the overall to video.
            return int(overall_bitrate * 0.97)
    return 0
