"""Internal domain models (plain dataclasses, no I/O).

API request/response models live in ``vlo.api`` as pydantic models; these
dataclasses are the in-process representation used by scan/probe/scoring/jobs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .enums import Codec, JobState, MediaKind


@dataclass(slots=True)
class AudioTrack:
    index: int
    codec: str
    channels: int | None = None
    channel_layout: str | None = None
    language: str | None = None
    title: str | None = None
    bitrate_bps: int | None = None


@dataclass(slots=True)
class SubTrack:
    index: int
    codec: str  # ffprobe codec_name: pgs/dvd_subtitle/ass/subrip/mov_text...
    language: str | None = None
    title: str | None = None
    forced: bool = False
    default: bool = False


@dataclass(slots=True)
class ProbeResult:
    """Normalised output of ffprobe for a single media file."""

    path: str
    size_bytes: int
    duration_s: float
    width: int
    height: int
    fps: float
    vcodec: str
    pix_fmt: str | None
    is_hdr: bool
    is_dolby_vision: bool
    video_bitrate_bps: int
    overall_bitrate_bps: int
    audio: list[AudioTrack] = field(default_factory=list)
    subs: list[SubTrack] = field(default_factory=list)
    n_chapters: int = 0
    video_language: str | None = None
    # Colour metadata, preserved on re-encode (especially for HDR10).
    color_primaries: str | None = None
    color_transfer: str | None = None
    color_space: str | None = None
    raw_json: str = ""  # serialised audio/sub streams, for replaying the mapping

    @property
    def n_audio(self) -> int:
        return len(self.audio)

    @property
    def n_subs(self) -> int:
        return len(self.subs)


@dataclass(slots=True)
class Classification:
    """How a file was classified (movie vs episode of a series)."""

    kind: MediaKind
    title: str | None = None
    year: int | None = None
    series_slug: str | None = None
    series_title: str | None = None
    season: int | None = None
    episode: int | None = None
    # Content type for scoring (live_action vs animation) and its provenance.
    content_type: str = "live_action"
    is_anime: bool = False
    content_source: str | None = None  # tmdb | keyword | manual | default


@dataclass(slots=True)
class ScoreResult:
    """Output of the scoring engine for a single candidate."""

    bpp_real: float
    bpp_target: float
    overhead_ratio: float
    est_out_bytes: int
    est_gain_bytes: int
    score: float
    excluded_reason: str | None = None

    @property
    def is_candidate(self) -> bool:
        return self.excluded_reason is None


@dataclass(slots=True)
class MediaFile:
    """A scanned, probed, classified and scored library entry."""

    id: int | None
    path: str
    size_bytes: int
    mtime: float
    probe: ProbeResult | None = None
    classification: Classification | None = None
    score: ScoreResult | None = None
    reencoded_at: float | None = None  # set once this app has re-encoded the file


@dataclass(slots=True)
class EncodeProfile:
    """A named quality preset, with per-codec CRF/preset/floor settings."""

    name: str
    crf_x265: int
    crf_av1: int
    preset_x265: str
    preset_av1: int
    floor_x265: float
    floor_av1: float
    x265_params: str = ""
    svtav1_params: str = ""

    def crf_for(self, codec: Codec) -> int:
        return self.crf_x265 if codec is Codec.X265 else self.crf_av1

    def floor_for(self, codec: Codec) -> float:
        return self.floor_x265 if codec is Codec.X265 else self.floor_av1


@dataclass(slots=True)
class Job:
    """A re-encode job (one per media file)."""

    id: int | None
    media_file_id: int | None
    source_path: str
    codec: Codec
    profile_name: str
    crf: int
    preset: str
    state: JobState = JobState.QUEUED
    progress: float = 0.0
    speed: str | None = None
    eta_s: float | None = None
    batch_id: str | None = None
    work_dir: str | None = None
    out_path_local: str | None = None
    size_src_bytes: int | None = None
    size_out_bytes: int | None = None
    gain_bytes: int | None = None
    validation_json: str | None = None
    error_message: str | None = None
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
