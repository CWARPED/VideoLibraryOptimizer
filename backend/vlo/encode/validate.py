"""Post-encode validation: is the output safe to swap in for the source?"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..core.enums import Codec
from ..core.models import ProbeResult

_EXPECTED_VCODEC = {Codec.X265: "hevc", Codec.SVTAV1: "av1"}
_TENBIT_PIX_FMTS = {"yuv420p10le", "yuv422p10le", "yuv444p10le", "p010le"}


@dataclass(slots=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass(slots=True)
class ValidationReport:
    ok: bool
    checks: list[Check] = field(default_factory=list)
    size_src_bytes: int = 0
    size_out_bytes: int = 0
    gain_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [asdict(c) for c in self.checks],
            "size_src_bytes": self.size_src_bytes,
            "size_out_bytes": self.size_out_bytes,
            "gain_bytes": self.gain_bytes,
        }


def validate_output(
    src: ProbeResult,
    out: ProbeResult,
    *,
    codec: Codec,
    duration_tolerance_pct: float = 0.5,
    is_vfr: bool = False,
    decoded_ok: bool = True,
) -> ValidationReport:
    """Compare a source probe to its re-encoded output probe.

    ``decoded_ok`` is the result of a full-decode readability check performed
    by the caller (``ffmpeg -f null``); pass True if that check passed.
    """
    checks: list[Check] = []

    # 1) Readable / decodes without error.
    checks.append(
        Check("readable", decoded_ok, "decoded without errors" if decoded_ok
              else "decode reported errors")
    )

    # 2) Duration within tolerance (relaxed for VFR sources).
    tol = duration_tolerance_pct * (3.0 if is_vfr else 1.0)
    if src.duration_s > 0:
        diff_pct = abs(out.duration_s - src.duration_s) / src.duration_s * 100.0
    else:
        diff_pct = 100.0
    dur_ok = diff_pct <= tol
    checks.append(
        Check("duration", dur_ok,
              f"src={src.duration_s:.1f}s out={out.duration_s:.1f}s "
              f"diff={diff_pct:.3f}% (tol {tol:.3f}%)")
    )

    # 3) Audio track count preserved.
    audio_ok = out.n_audio == src.n_audio
    checks.append(Check("audio_tracks", audio_ok, f"src={src.n_audio} out={out.n_audio}"))

    # 4) Subtitle track count preserved.
    subs_ok = out.n_subs == src.n_subs
    checks.append(Check("subtitle_tracks", subs_ok, f"src={src.n_subs} out={out.n_subs}"))

    # 5) Real size gain.
    gain = src.size_bytes - out.size_bytes
    gain_ok = gain > 0
    checks.append(
        Check("size_gain", gain_ok,
              f"src={src.size_bytes} out={out.size_bytes} gain={gain}")
    )

    # 6) Output codec / 10-bit as expected.
    expected = _EXPECTED_VCODEC[codec]
    codec_ok = out.vcodec == expected
    checks.append(Check("video_codec", codec_ok, f"expected={expected} got={out.vcodec}"))
    pix_ok = (out.pix_fmt or "") in _TENBIT_PIX_FMTS
    checks.append(Check("pixel_format", pix_ok, f"got={out.pix_fmt}"))

    ok = all(c.passed for c in checks)
    return ValidationReport(
        ok=ok,
        checks=checks,
        size_src_bytes=src.size_bytes,
        size_out_bytes=out.size_bytes,
        gain_bytes=gain,
    )
