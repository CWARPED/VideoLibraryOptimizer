"""CRUD for the media_file scan cache."""

from __future__ import annotations

import json
from typing import Any

from ..core.enums import JobState, MediaKind
from ..core.models import (
    AudioTrack,
    Classification,
    MediaFile,
    ProbeResult,
    ScoreResult,
    SubTrack,
)
from .db import Database


class ScanRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get_by_path(self, path: str) -> MediaFile | None:
        with self._db.lock:
            row = self._db.conn.execute(
                "SELECT * FROM media_file WHERE path = ?", (path,)
            ).fetchone()
        return self._row_to_media(row) if row else None

    def get_by_id(self, file_id: int) -> MediaFile | None:
        with self._db.lock:
            row = self._db.conn.execute(
                "SELECT * FROM media_file WHERE id = ?", (file_id,)
            ).fetchone()
        return self._row_to_media(row) if row else None

    def cache_is_fresh(self, path: str, size_bytes: int, mtime: float) -> bool:
        """True if we already have a probe for this exact (size, mtime)."""
        existing = self.get_by_path(path)
        return (
            existing is not None
            and existing.size_bytes == size_bytes
            and abs(existing.mtime - mtime) < 1e-6
            and existing.probe is not None
        )

    def upsert(self, mf: MediaFile, now: float) -> int:
        """Insert or update a fully-populated MediaFile; returns its id."""
        p = mf.probe
        c = mf.classification
        s = mf.score
        with self._db.lock:
            self._db.conn.execute(
                """
                INSERT INTO media_file (
                    path, size_bytes, mtime, kind, series_slug, series_title, season, episode,
                    title, year, content_type, content_source, is_anime, reencoded_at,
                    duration_s, width,
                    height, fps, vcodec, pix_fmt, is_hdr,
                    is_dolby_vision, video_bitrate_bps, overall_bitrate_bps, n_audio, n_subs,
                    probe_json, probed_at, bpp_real, bpp_target, overhead_ratio, est_out_bytes,
                    est_gain_bytes, score, excluded_reason, updated_at
                ) VALUES (
                    :path, :size_bytes, :mtime, :kind, :series_slug, :series_title, :season,
                    :episode, :title, :year, :content_type, :content_source, :is_anime,
                    :reencoded_at, :duration_s, :width, :height, :fps, :vcodec,
                    :pix_fmt, :is_hdr, :is_dolby_vision, :video_bitrate_bps, :overall_bitrate_bps,
                    :n_audio, :n_subs, :probe_json, :probed_at, :bpp_real, :bpp_target,
                    :overhead_ratio, :est_out_bytes, :est_gain_bytes, :score, :excluded_reason,
                    :updated_at
                )
                ON CONFLICT(path) DO UPDATE SET
                    size_bytes=excluded.size_bytes, mtime=excluded.mtime, kind=excluded.kind,
                    series_slug=excluded.series_slug, series_title=excluded.series_title,
                    season=excluded.season, episode=excluded.episode, title=excluded.title,
                    year=excluded.year, content_type=excluded.content_type,
                    content_source=excluded.content_source, is_anime=excluded.is_anime,
                    reencoded_at=COALESCE(excluded.reencoded_at, media_file.reencoded_at),
                    duration_s=excluded.duration_s, width=excluded.width,
                    height=excluded.height, fps=excluded.fps, vcodec=excluded.vcodec,
                    pix_fmt=excluded.pix_fmt, is_hdr=excluded.is_hdr,
                    is_dolby_vision=excluded.is_dolby_vision,
                    video_bitrate_bps=excluded.video_bitrate_bps,
                    overall_bitrate_bps=excluded.overall_bitrate_bps, n_audio=excluded.n_audio,
                    n_subs=excluded.n_subs, probe_json=excluded.probe_json,
                    probed_at=excluded.probed_at, bpp_real=excluded.bpp_real,
                    bpp_target=excluded.bpp_target, overhead_ratio=excluded.overhead_ratio,
                    est_out_bytes=excluded.est_out_bytes, est_gain_bytes=excluded.est_gain_bytes,
                    score=excluded.score, excluded_reason=excluded.excluded_reason,
                    updated_at=excluded.updated_at
                """,
                {
                    "path": mf.path,
                    "size_bytes": mf.size_bytes,
                    "mtime": mf.mtime,
                    "kind": c.kind.value if c else MediaKind.UNKNOWN.value,
                    "series_slug": c.series_slug if c else None,
                    "series_title": c.series_title if c else None,
                    "season": c.season if c else None,
                    "episode": c.episode if c else None,
                    "title": c.title if c else None,
                    "year": c.year if c else None,
                    "content_type": c.content_type if c else "live_action",
                    "content_source": c.content_source if c else None,
                    "is_anime": int(c.is_anime) if c else 0,
                    "reencoded_at": mf.reencoded_at,
                    "duration_s": p.duration_s if p else None,
                    "width": p.width if p else None,
                    "height": p.height if p else None,
                    "fps": p.fps if p else None,
                    "vcodec": p.vcodec if p else None,
                    "pix_fmt": p.pix_fmt if p else None,
                    "is_hdr": int(p.is_hdr) if p else None,
                    "is_dolby_vision": int(p.is_dolby_vision) if p else None,
                    "video_bitrate_bps": p.video_bitrate_bps if p else None,
                    "overall_bitrate_bps": p.overall_bitrate_bps if p else None,
                    "n_audio": p.n_audio if p else None,
                    "n_subs": p.n_subs if p else None,
                    "probe_json": p.raw_json if p else None,
                    "probed_at": now if p else None,
                    "bpp_real": s.bpp_real if s else None,
                    "bpp_target": s.bpp_target if s else None,
                    "overhead_ratio": s.overhead_ratio if s else None,
                    "est_out_bytes": s.est_out_bytes if s else None,
                    "est_gain_bytes": s.est_gain_bytes if s else None,
                    "score": s.score if s else None,
                    "excluded_reason": s.excluded_reason if s else None,
                    "updated_at": now,
                },
            )
            self._db.conn.commit()
            row = self._db.conn.execute(
                "SELECT id FROM media_file WHERE path = ?", (mf.path,)
            ).fetchone()
        return row["id"]

    def update_path(self, file_id: int, new_path: str, size_bytes: int, mtime: float) -> None:
        """After a replacement (possibly with a new extension), point the row at the new file."""
        with self._db.lock:
            self._db.conn.execute(
                "UPDATE media_file SET path=?, size_bytes=?, mtime=? WHERE id=?",
                (new_path, size_bytes, mtime, file_id),
            )
            self._db.conn.commit()

    def set_content_type(
        self, file_id: int, content_type: str, *, is_anime: bool = False, source: str = "manual"
    ) -> None:
        """Override the content type of a file (manual source locks it from re-scan)."""
        with self._db.lock:
            self._db.conn.execute(
                "UPDATE media_file SET content_type=?, is_anime=?, content_source=? WHERE id=?",
                (content_type, int(is_anime), source, file_id),
            )
            self._db.conn.commit()

    def list_movies(
        self, *, only_candidates: bool = True, limit: int = 500, offset: int = 0
    ) -> list[MediaFile]:
        clause = "WHERE kind = ?"
        params: list[Any] = [MediaKind.MOVIE.value]
        if only_candidates:
            clause += " AND excluded_reason IS NULL AND reencoded_at IS NULL"
        with self._db.lock:
            rows = self._db.conn.execute(
                f"SELECT * FROM media_file {clause} "
                "ORDER BY score DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [self._row_to_media(r) for r in rows]

    def list_series_summary(self) -> list[dict[str, Any]]:
        """Aggregate per series: episode/candidate counts and cumulative estimated gain."""
        with self._db.lock:
            rows = self._db.conn.execute(
                """
                SELECT series_slug, MAX(series_title) AS series_title,
                       COUNT(*) AS n_episodes,
                       SUM(CASE WHEN excluded_reason IS NULL AND reencoded_at IS NULL
                                THEN 1 ELSE 0 END) AS n_candidates,
                       COALESCE(SUM(CASE WHEN excluded_reason IS NULL AND reencoded_at IS NULL
                                THEN est_gain_bytes ELSE 0 END), 0) AS est_gain_bytes,
                       MAX(score) AS top_score
                FROM media_file
                WHERE kind = ? AND series_slug IS NOT NULL
                GROUP BY series_slug
                ORDER BY est_gain_bytes DESC
                """,
                (MediaKind.EPISODE.value,),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_excluded(self) -> list[MediaFile]:
        """Cached files not proposed as candidates: skipped or already re-encoded."""
        with self._db.lock:
            rows = self._db.conn.execute(
                "SELECT * FROM media_file "
                "WHERE excluded_reason IS NOT NULL OR reencoded_at IS NOT NULL "
                "ORDER BY excluded_reason, path"
            ).fetchall()
        return [self._row_to_media(r) for r in rows]

    def list_all_episodes(self) -> list[MediaFile]:
        with self._db.lock:
            rows = self._db.conn.execute(
                "SELECT * FROM media_file WHERE kind = ? AND series_slug IS NOT NULL "
                "ORDER BY series_slug, season, episode",
                (MediaKind.EPISODE.value,),
            ).fetchall()
        return [self._row_to_media(r) for r in rows]

    def list_episodes(self, series_slug: str) -> list[MediaFile]:
        with self._db.lock:
            rows = self._db.conn.execute(
                "SELECT * FROM media_file WHERE series_slug = ? "
                "ORDER BY season, episode",
                (series_slug,),
            ).fetchall()
        return [self._row_to_media(r) for r in rows]

    def list_season_episodes(self, series_slug: str, season: int) -> list[MediaFile]:
        with self._db.lock:
            rows = self._db.conn.execute(
                "SELECT * FROM media_file WHERE series_slug = ? AND season = ? "
                "ORDER BY episode",
                (series_slug, season),
            ).fetchall()
        return [self._row_to_media(r) for r in rows]

    def delete_missing(self, present_paths: set[str]) -> int:
        """Remove cache rows for files no longer on disk. Returns count removed.

        Rows referenced by a *non-terminal* job (queued / encoding / awaiting
        confirmation / etc.) are kept — that job still needs them. For rows
        referenced only by terminal jobs, the job link is nulled first (history
        keeps its own ``source_path``) so the foreign key is not violated.
        """
        active_states = tuple(s.value for s in JobState if not s.is_terminal)
        placeholders = ", ".join("?" for _ in active_states)
        with self._db.lock:
            rows = self._db.conn.execute("SELECT id, path FROM media_file").fetchall()
            stale = [r["id"] for r in rows if r["path"] not in present_paths]

            # Ids still needed by a non-terminal job -> keep them.
            kept: set[int] = set()
            if stale and active_states:
                q = (
                    f"SELECT DISTINCT media_file_id FROM job "
                    f"WHERE media_file_id IS NOT NULL AND state IN ({placeholders})"
                )
                kept = {r["media_file_id"] for r in self._db.conn.execute(q, active_states)}

            removed = 0
            for fid in stale:
                if fid in kept:
                    continue
                # Detach terminal-job history from the row being deleted.
                self._db.conn.execute(
                    "UPDATE job SET media_file_id = NULL WHERE media_file_id = ?", (fid,)
                )
                self._db.conn.execute("DELETE FROM media_file WHERE id = ?", (fid,))
                removed += 1
            self._db.conn.commit()
        return removed

    # --- row mapping ----------------------------------------------------
    @staticmethod
    def _row_to_media(row: Any) -> MediaFile:
        probe: ProbeResult | None = None
        if row["probe_json"]:
            probe = ScanRepo._rebuild_probe(row)
        classification = Classification(
            kind=MediaKind(row["kind"]) if row["kind"] else MediaKind.UNKNOWN,
            title=row["title"],
            year=row["year"],
            series_slug=row["series_slug"],
            series_title=row["series_title"],
            season=row["season"],
            episode=row["episode"],
            content_type=(row["content_type"] or "live_action"),
            is_anime=bool(row["is_anime"]),
            content_source=row["content_source"],
        )
        score: ScoreResult | None = None
        if row["score"] is not None or row["excluded_reason"] is not None:
            score = ScoreResult(
                bpp_real=row["bpp_real"] or 0.0,
                bpp_target=row["bpp_target"] or 0.0,
                overhead_ratio=row["overhead_ratio"] or 0.0,
                est_out_bytes=row["est_out_bytes"] or 0,
                est_gain_bytes=row["est_gain_bytes"] or 0,
                score=row["score"] or 0.0,
                excluded_reason=row["excluded_reason"],
            )
        return MediaFile(
            id=row["id"],
            path=row["path"],
            size_bytes=row["size_bytes"],
            mtime=row["mtime"],
            probe=probe,
            classification=classification,
            score=score,
            reencoded_at=row["reencoded_at"],
        )

    @staticmethod
    def _rebuild_probe(row: Any) -> ProbeResult:
        raw = row["probe_json"]
        try:
            data = json.loads(raw)
            audio = [AudioTrack(**a) for a in data.get("_audio", [])]
            subs = [SubTrack(**s) for s in data.get("_subs", [])]
        except (ValueError, TypeError):
            audio, subs = [], []
        return ProbeResult(
            path=row["path"],
            size_bytes=row["size_bytes"],
            duration_s=row["duration_s"] or 0.0,
            width=row["width"] or 0,
            height=row["height"] or 0,
            fps=row["fps"] or 0.0,
            vcodec=row["vcodec"] or "",
            pix_fmt=row["pix_fmt"],
            is_hdr=bool(row["is_hdr"]),
            is_dolby_vision=bool(row["is_dolby_vision"]),
            video_bitrate_bps=row["video_bitrate_bps"] or 0,
            overall_bitrate_bps=row["overall_bitrate_bps"] or 0,
            audio=audio,
            subs=subs,
            n_chapters=0,
            raw_json=raw,
        )
