"""Cache of resolved content-type per title (avoids re-querying TMDB)."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .db import Database


@dataclass(slots=True)
class CachedGenre:
    content_type: str
    is_anime: bool
    genres: list[int]
    tmdb_id: int | None


class MetadataRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self, key: str) -> CachedGenre | None:
        with self._db.lock:
            row = self._db.conn.execute(
                "SELECT content_type, is_anime, genres, tmdb_id FROM metadata_cache "
                "WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        try:
            genres = json.loads(row["genres"]) if row["genres"] else []
        except ValueError:
            genres = []
        return CachedGenre(
            content_type=row["content_type"],
            is_anime=bool(row["is_anime"]),
            genres=genres,
            tmdb_id=row["tmdb_id"],
        )

    def set(self, key: str, value: CachedGenre, now: float) -> None:
        with self._db.lock:
            self._db.conn.execute(
                "INSERT INTO metadata_cache (key, content_type, is_anime, genres, tmdb_id, "
                "fetched_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET content_type=excluded.content_type, "
                "is_anime=excluded.is_anime, genres=excluded.genres, tmdb_id=excluded.tmdb_id, "
                "fetched_at=excluded.fetched_at",
                (key, value.content_type, int(value.is_anime),
                 json.dumps(value.genres), value.tmdb_id, now),
            )
            self._db.conn.commit()
