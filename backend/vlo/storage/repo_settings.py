"""Access to encode profiles, the reference bpp table and key/value settings."""

from __future__ import annotations

import json
from typing import Any

from ..core.models import EncodeProfile
from .db import Database


class SettingsRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    # --- encode profiles ------------------------------------------------
    def list_profiles(self) -> list[EncodeProfile]:
        with self._db.lock:
            rows = self._db.conn.execute(
                # Ordered quality -> most compressed (lower CRF = higher quality).
                "SELECT * FROM encode_profile ORDER BY crf_x265"
            ).fetchall()
        return [self._row_to_profile(r) for r in rows]

    def get_profile(self, name: str) -> EncodeProfile | None:
        with self._db.lock:
            row = self._db.conn.execute(
                "SELECT * FROM encode_profile WHERE name = ?", (name,)
            ).fetchone()
        return self._row_to_profile(row) if row else None

    def upsert_profile(self, p: EncodeProfile) -> None:
        with self._db.lock:
            self._db.conn.execute(
                "INSERT INTO encode_profile "
                "(name, crf_x265, crf_av1, preset_x265, preset_av1, floor_x265, floor_av1, "
                " x265_params, svtav1_params) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "crf_x265=excluded.crf_x265, crf_av1=excluded.crf_av1, "
                "preset_x265=excluded.preset_x265, preset_av1=excluded.preset_av1, "
                "floor_x265=excluded.floor_x265, floor_av1=excluded.floor_av1, "
                "x265_params=excluded.x265_params, svtav1_params=excluded.svtav1_params",
                (
                    p.name, p.crf_x265, p.crf_av1, p.preset_x265, p.preset_av1,
                    p.floor_x265, p.floor_av1, p.x265_params, p.svtav1_params,
                ),
            )
            self._db.conn.commit()

    @staticmethod
    def _row_to_profile(row: Any) -> EncodeProfile:
        return EncodeProfile(
            name=row["name"],
            crf_x265=row["crf_x265"],
            crf_av1=row["crf_av1"],
            preset_x265=row["preset_x265"],
            preset_av1=row["preset_av1"],
            floor_x265=row["floor_x265"],
            floor_av1=row["floor_av1"],
            x265_params=row["x265_params"] or "",
            svtav1_params=row["svtav1_params"] or "",
        )

    # --- reference bpp --------------------------------------------------
    def reference_bands(
        self, content_type: str = "live_action"
    ) -> list[tuple[int, int, float]]:
        """Return [(height_min, height_max, bpp_target), ...] for a content type."""
        with self._db.lock:
            rows = self._db.conn.execute(
                "SELECT height_min, height_max, bpp_target FROM reference_bpp "
                "WHERE content_type = ? ORDER BY height_min",
                (content_type,),
            ).fetchall()
        return [(r["height_min"], r["height_max"], r["bpp_target"]) for r in rows]

    def replace_reference_bands(
        self, bands: list[tuple[int, int, float]], content_type: str = "live_action"
    ) -> None:
        with self._db.lock:
            self._db.conn.execute(
                "DELETE FROM reference_bpp WHERE content_type = ?", (content_type,)
            )
            self._db.conn.executemany(
                "INSERT INTO reference_bpp (height_min, height_max, bpp_target, content_type) "
                "VALUES (?, ?, ?, ?)",
                [(a, b, c, content_type) for a, b, c in bands],
            )
            self._db.conn.commit()

    # --- generic key/value settings ------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        with self._db.lock:
            row = self._db.conn.execute(
                "SELECT value FROM setting WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return default
        return json.loads(row["value"])

    def set(self, key: str, value: Any) -> None:
        with self._db.lock:
            self._db.conn.execute(
                "INSERT INTO setting (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
            self._db.conn.commit()

    def all_settings(self) -> dict[str, Any]:
        with self._db.lock:
            rows = self._db.conn.execute("SELECT key, value FROM setting").fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}
