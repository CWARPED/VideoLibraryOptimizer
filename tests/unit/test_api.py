"""API smoke tests with a temporary DB (no ffmpeg needed)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vlo.config import Settings
from vlo.core.enums import MediaKind
from vlo.core.models import Classification, MediaFile, ProbeResult, ScoreResult
from vlo.deps import build_app_state
from vlo.main import create_app


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "api.db", work_dir=tmp_path / "work")
    state = build_app_state(settings)
    app = create_app(state)
    with TestClient(app) as c:
        yield c, state
    state.db.close()


def _add_movie(state, path="/lib/Big.Movie.1080p.mkv", excluded=None, reencoded=False) -> int:
    probe = ProbeResult(
        path=path, size_bytes=8_000_000_000, duration_s=3600.0, width=1920, height=1080,
        fps=24.0, vcodec="h264", pix_fmt="yuv420p", is_hdr=False, is_dolby_vision=False,
        video_bitrate_bps=15_000_000, overall_bitrate_bps=15_000_000, raw_json="{}",
    )
    mf = MediaFile(
        id=None, path=path, size_bytes=8_000_000_000, mtime=1.0, probe=probe,
        classification=Classification(kind=MediaKind.MOVIE, title="Big Movie"),
        score=ScoreResult(bpp_real=0.3, bpp_target=0.045, overhead_ratio=6.0,
                          est_out_bytes=4_000_000_000, est_gain_bytes=4_000_000_000,
                          score=85.0, excluded_reason=excluded),
        reencoded_at=123.0 if reencoded else None,
    )
    return state.scan_repo.upsert(mf, 1.0)


def test_health(client):
    c, _ = client
    assert c.get("/api/health").json() == {"status": "ok"}


def test_default_profiles_present(client):
    c, _ = client
    names = {p["name"] for p in c.get("/api/profiles").json()["profiles"]}
    assert names == {"Archive", "Light", "Balanced", "Compact", "Mini"}


def test_settings_roundtrip(client):
    c, _ = client
    assert c.get("/api/settings").json()["scoring"]["weight_gain"] == 0.5
    r = c.put("/api/settings", json={"weight_gain": 0.7})
    assert r.status_code == 200
    assert c.get("/api/settings").json()["scoring"]["weight_gain"] == 0.7


def test_movies_listing_and_candidate_filter(client):
    c, state = client
    _add_movie(state, "/lib/Good.mkv")
    _add_movie(state, "/lib/Eff.mkv", excluded="already efficient")
    candidates = c.get("/api/movies").json()["movies"]
    assert len(candidates) == 1
    all_movies = c.get("/api/movies?only_candidates=false").json()["movies"]
    assert len(all_movies) == 2


def test_scan_invalid_dir(client):
    c, _ = client
    r = c.post("/api/scan", json={"root_path": "/does/not/exist", "force": False})
    assert r.status_code == 400


def test_batch_requires_eligible_files(client):
    c, _ = client
    r = c.post("/api/jobs/batch", json={"codec": "X265", "profile_name": "Light",
                                        "file_ids": [999]})
    assert r.status_code == 400


def test_batch_creates_jobs(client):
    c, state = client
    fid = _add_movie(state, "/lib/Big.Movie.1080p.mkv")
    r = c.post("/api/jobs/batch", json={"codec": "X265", "profile_name": "Light",
                                        "file_ids": [fid]})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    jobs = c.get("/api/jobs").json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["codec"] == "X265"
    assert jobs[0]["crf"] == 20  # Light x265


def test_unknown_profile_rejected(client):
    c, state = client
    fid = _add_movie(state)
    r = c.post("/api/jobs/batch", json={"codec": "X265", "profile_name": "Nope",
                                        "file_ids": [fid]})
    assert r.status_code == 400


def test_estimate_depends_on_codec_and_profile(client):
    c, state = client
    _add_movie(state, "/lib/Big.Movie.1080p.mkv")
    # Same codec, two profiles: Mini (CRF 28, compressed) must estimate a smaller
    # output -> a larger gain than Archive (CRF 18, high quality).
    archive = c.get("/api/movies?codec=X265&profile=Archive").json()["movies"][0]
    mini = c.get("/api/movies?codec=X265&profile=Mini").json()["movies"][0]
    assert mini["est_gain_bytes"] > archive["est_gain_bytes"]
    assert mini["est_out_bytes"] < archive["est_out_bytes"]


def test_work_dir_setting_roundtrip(client, tmp_path):
    c, state = client
    new_wd = tmp_path / "custom_work"
    r = c.put("/api/settings", json={"work_dir": str(new_wd)})
    assert r.status_code == 200
    assert new_wd.exists()  # directory was created
    assert c.get("/api/settings").json()["work_dir"] == str(new_wd)
    # The manager picks it up dynamically.
    assert state.job_manager._work_dir() == new_wd


def test_exclusion_category():
    from vlo.api.schemas import exclusion_category
    assert exclusion_category("probe failed: boom") == "unreadable"
    assert exclusion_category("scan error: X") == "unreadable"
    assert exclusion_category("unreadable video stream") == "unreadable"
    assert exclusion_category("unknown bitrate/resolution") == "unreadable"
    assert exclusion_category("Dolby Vision (excluded by default)") == "dolby_vision"
    assert exclusion_category("already efficient") == "efficient"
    assert exclusion_category("no estimated gain") == "efficient"
    assert exclusion_category(None) == "other"


def test_excluded_endpoint_lists_skipped_with_categories(client):
    c, state = client
    _add_movie(state, "/lib/Good.mkv")  # candidate, not excluded
    _add_movie(state, "/lib/Corrupt.mkv", excluded="probe failed: EBML")
    _add_movie(state, "/lib/DV.mkv", excluded="Dolby Vision (excluded by default)")
    _add_movie(state, "/lib/Eff.mkv", excluded="already efficient")

    excluded = c.get("/api/excluded").json()["excluded"]
    by_name = {e["filename"]: e for e in excluded}
    assert "Good.mkv" not in by_name  # candidates are not in the excluded list
    assert by_name["Corrupt.mkv"]["category"] == "unreadable"
    assert by_name["DV.mkv"]["category"] == "dolby_vision"
    assert by_name["Eff.mkv"]["category"] == "efficient"
    assert by_name["Corrupt.mkv"]["reason"] == "probe failed: EBML"


def test_content_type_override_rescore(client):
    c, state = client
    fid = _add_movie(state, "/lib/Mystery.1080p.mkv")
    before = c.get("/api/movies?only_candidates=false").json()["movies"][0]
    assert before["content_type"] == "live_action"

    r = c.post(f"/api/media/{fid}/content_type",
               json={"content_type": "animation", "is_anime": True})
    assert r.status_code == 200
    body = r.json()
    assert body["content_type"] == "animation"
    assert body["is_anime"] is True
    # Lower animation target -> overhead recomputed higher than before.
    assert body["overhead_ratio"] > before["overhead_ratio"]


def test_content_type_manual_override_persists_after_rescore(client):
    c, state = client
    fid = _add_movie(state)
    c.post(f"/api/media/{fid}/content_type", json={"content_type": "animation"})
    mf = state.scan_repo.get_by_id(fid)
    assert mf.classification.content_source == "manual"
    assert mf.classification.content_type == "animation"


def test_settings_exposes_animation_bands_and_tmdb(client):
    c, _ = client
    s = c.get("/api/settings").json()
    assert len(s["animation_bands"]) == 6
    assert len(s["reference_bands"]) == 6
    assert s["animation_bands"][2]["bpp_target"] < s["reference_bands"][2]["bpp_target"]
    assert "tmdb_api_key" in s["content_detection"]


def test_settings_update_tmdb_and_bands(client):
    c, _ = client
    r = c.put("/api/settings", json={
        "tmdb_api_key": "abc123", "tmdb_enabled": True,
        "animation_bands": [[0, 1080, 0.02], [1081, 100000, 0.015]],
    })
    assert r.status_code == 200
    s = c.get("/api/settings").json()
    assert s["content_detection"]["tmdb_api_key"] == "abc123"
    assert len(s["animation_bands"]) == 2


def test_queue_clear_and_delete(client):
    c, state = client
    from vlo.core.enums import Codec, JobState
    from vlo.core.models import Job
    jid_done = state.jobs_repo.create(Job(
        id=None, media_file_id=None, source_path="/x/a.mkv", codec=Codec.X265,
        profile_name="Light", crf=20, preset="slow", state=JobState.QUEUED,
        created_at=1.0))
    state.jobs_repo.update(jid_done, state=JobState.DONE)
    jid_active = state.jobs_repo.create(Job(
        id=None, media_file_id=None, source_path="/x/b.mkv", codec=Codec.X265,
        profile_name="Light", crf=20, preset="slow", state=JobState.QUEUED,
        created_at=2.0))

    # Cannot delete an active (queued) job.
    assert c.request("DELETE", f"/api/jobs/{jid_active}").status_code == 409
    # Can delete a terminal job.
    assert c.request("DELETE", f"/api/jobs/{jid_done}").status_code == 200
    assert c.get(f"/api/jobs/{jid_done}").status_code == 404

    # Clear removes terminal jobs only.
    state.jobs_repo.update(jid_active, state=JobState.FAILED)
    cleared = c.post("/api/jobs/clear").json()
    assert cleared["removed"] == 1
    assert len(c.get("/api/jobs").json()["jobs"]) == 0


def test_content_resolver_respects_manual_and_keyword(client):
    c, state = client
    from vlo.core.models import Classification
    # Manual override on an existing row must be preserved (no TMDB key set).
    fid = _add_movie(state, "/lib/Forced.mkv")
    state.scan_repo.set_content_type(fid, "animation", is_anime=True, source="manual")
    resolve = state.make_content_resolver()
    ct, anime, src = resolve("/lib/Forced.mkv", Classification(kind=MediaKind.MOVIE, title="Forced"))
    assert (ct, anime, src) == ("animation", True, "manual")

    # Keyword fallback when path looks like animation and no TMDB.
    ct2, _, src2 = resolve(
        "/lib/Anime/New Show.mkv", Classification(kind=MediaKind.MOVIE, title="New Show")
    )
    assert ct2 == "animation" and src2 == "keyword"

    # Default otherwise.
    ct3, _, src3 = resolve(
        "/lib/Films/Plain.mkv", Classification(kind=MediaKind.MOVIE, title="Plain")
    )
    assert ct3 == "live_action" and src3 == "default"


def test_reencoded_not_proposed_but_indicated(client):
    c, state = client
    _add_movie(state, "/lib/Fresh.mkv")  # candidate
    fid = _add_movie(state, "/lib/Done.mkv", reencoded=True)  # already re-encoded

    # Not proposed among candidates.
    cands = c.get("/api/movies").json()["movies"]
    assert {m["filename"] for m in cands} == {"Fresh.mkv"}

    # Visible (with the flag) when listing everything.
    allm = {m["filename"]: m for m in c.get("/api/movies?only_candidates=false").json()["movies"]}
    assert allm["Done.mkv"]["reencoded"] is True
    assert allm["Fresh.mkv"]["reencoded"] is False

    # Surfaced in the excluded list under the 'reencoded' category.
    ex = c.get("/api/excluded").json()["excluded"]
    done = next(e for e in ex if e["filename"] == "Done.mkv")
    assert done["category"] == "reencoded"

    # Cannot be enqueued again.
    r = c.post("/api/jobs/batch", json={"codec": "X265", "profile_name": "Light",
                                        "file_ids": [fid]})
    assert r.status_code == 400


def test_stats_endpoint(client):
    c, state = client
    assert c.get("/api/stats").json() == {"total_gain_bytes": 0, "total_encodes_done": 0}
    # Simulate two completed encodes accumulating persistent gain.
    state.settings_repo.set("total_gain_bytes", 5_000_000_000)
    state.settings_repo.set("total_encodes_done", 2)
    s = c.get("/api/stats").json()
    assert s["total_gain_bytes"] == 5_000_000_000
    assert s["total_encodes_done"] == 2


def test_logs_endpoint(client):
    c, _ = client
    import logging
    logging.getLogger("vlo.test").warning("hello-from-test")
    logs = c.get("/api/logs?level=WARNING").json()["logs"]
    assert any("hello-from-test" in r["message"] for r in logs)


def test_ffmpeg_info(client, monkeypatch):
    c, _ = client
    from vlo import ffmpeg_dist
    monkeypatch.setattr(ffmpeg_dist, "current_info", lambda p: {"version": "n7.1", "build_date": "20260101"})
    monkeypatch.setattr(ffmpeg_dist, "latest_release", lambda: {"published_at": "2026-06-04T00:00:00Z", "tag": "latest"})
    monkeypatch.setattr(ffmpeg_dist, "update_available", lambda p: True)
    d = c.get("/api/ffmpeg").json()
    assert d["version"] == "n7.1"
    assert d["update_available"] is True
    assert d["latest_published_at"].startswith("2026-06-04")


def test_ffmpeg_update_refused_while_encoding(client, monkeypatch):
    c, state = client
    monkeypatch.setattr(state.job_manager, "has_active", lambda: True)
    r = c.post("/api/ffmpeg/update")
    assert r.status_code == 409


def test_pause_resume_invalid_state_returns_409(client):
    c, _ = client
    # Unknown / non-encoding job cannot be paused or resumed.
    assert c.post("/api/jobs/999999/pause").status_code == 409
    assert c.post("/api/jobs/999999/resume").status_code == 409


def test_global_job_controls_smoke(client):
    c, _ = client
    assert c.post("/api/jobs/pause-all").json() == {"ok": True, "paused": 0}
    assert c.post("/api/jobs/resume-all").json() == {"ok": True, "resumed": 0}
    assert c.post("/api/jobs/stop-all").json() == {"ok": True, "stopped": 0}
