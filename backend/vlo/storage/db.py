"""SQLite connection management, schema and seed data.

A single shared connection (``check_same_thread=False``) is used behind a
re-entrant lock. SQLite in WAL mode handles the modest concurrency of a
single-user local app; all writes go through the repos in this package.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS media_file (
    id                  INTEGER PRIMARY KEY,
    path                TEXT UNIQUE NOT NULL,
    size_bytes          INTEGER NOT NULL,
    mtime               REAL NOT NULL,
    kind                TEXT,
    series_slug         TEXT,
    series_title        TEXT,
    season              INTEGER,
    episode             INTEGER,
    title               TEXT,
    year                INTEGER,
    content_type        TEXT DEFAULT 'live_action',
    content_source      TEXT,
    is_anime            INTEGER DEFAULT 0,
    reencoded_at        REAL,
    duration_s          REAL,
    width               INTEGER,
    height              INTEGER,
    fps                 REAL,
    vcodec              TEXT,
    pix_fmt             TEXT,
    is_hdr              INTEGER,
    is_dolby_vision     INTEGER,
    video_bitrate_bps   INTEGER,
    overall_bitrate_bps INTEGER,
    n_audio             INTEGER,
    n_subs              INTEGER,
    probe_json          TEXT,
    probed_at           REAL,
    bpp_real            REAL,
    bpp_target          REAL,
    overhead_ratio      REAL,
    est_out_bytes       INTEGER,
    est_gain_bytes      INTEGER,
    score               REAL,
    excluded_reason     TEXT,
    updated_at          REAL
);
CREATE INDEX IF NOT EXISTS idx_media_score ON media_file(score DESC);
CREATE INDEX IF NOT EXISTS idx_media_series ON media_file(series_slug, season, episode);

CREATE TABLE IF NOT EXISTS metadata_cache (
    key          TEXT PRIMARY KEY,
    content_type TEXT NOT NULL,
    is_anime     INTEGER NOT NULL DEFAULT 0,
    genres       TEXT,
    tmdb_id      INTEGER,
    fetched_at   REAL
);

CREATE TABLE IF NOT EXISTS job (
    id              INTEGER PRIMARY KEY,
    media_file_id   INTEGER REFERENCES media_file(id),
    source_path     TEXT NOT NULL,
    codec           TEXT NOT NULL,
    profile_name    TEXT NOT NULL,
    crf             INTEGER NOT NULL,
    preset          TEXT NOT NULL,
    state           TEXT NOT NULL,
    progress        REAL DEFAULT 0,
    speed           TEXT,
    eta_s           REAL,
    batch_id        TEXT,
    work_dir        TEXT,
    out_path_local  TEXT,
    size_src_bytes  INTEGER,
    size_out_bytes  INTEGER,
    gain_bytes      INTEGER,
    validation_json TEXT,
    error_message   TEXT,
    created_at      REAL,
    started_at      REAL,
    finished_at     REAL
);
CREATE INDEX IF NOT EXISTS idx_job_state ON job(state);
CREATE INDEX IF NOT EXISTS idx_job_batch ON job(batch_id);

CREATE TABLE IF NOT EXISTS reference_bpp (
    id           INTEGER PRIMARY KEY,
    height_min   INTEGER NOT NULL,
    height_max   INTEGER NOT NULL,
    bpp_target   REAL NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'live_action'
);

CREATE TABLE IF NOT EXISTS encode_profile (
    name          TEXT PRIMARY KEY,
    crf_x265      INTEGER NOT NULL,
    crf_av1       INTEGER NOT NULL,
    preset_x265   TEXT NOT NULL,
    preset_av1    INTEGER NOT NULL,
    floor_x265    REAL NOT NULL,
    floor_av1     REAL NOT NULL,
    x265_params   TEXT,
    svtav1_params TEXT
);

CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Default reference bits-per-pixel targets, by source height band (live action).
_DEFAULT_REFERENCE_BPP: list[tuple[int, int, float]] = [
    (0, 576, 0.060),
    (577, 800, 0.050),
    (801, 1100, 0.045),
    (1101, 1600, 0.040),
    (1601, 2200, 0.035),
    (2201, 100000, 0.030),
]

# Animation/anime compress much better -> lower targets at equal quality.
_DEFAULT_REFERENCE_BPP_ANIM: list[tuple[int, int, float]] = [
    (0, 576, 0.035),
    (577, 800, 0.028),
    (801, 1100, 0.025),
    (1101, 1600, 0.022),
    (1601, 2200, 0.020),
    (2201, 100000, 0.017),
]

_DEFAULT_X265_PARAMS = "profile=main10:aq-mode=3:psy-rd=2.0:psy-rdoq=1.0:rc-lookahead=60:bframes=6"
_DEFAULT_SVTAV1_PARAMS = "tune=0:scd=1:enable-overlays=1"

# (name, crf_x265, crf_av1, preset_x265, preset_av1, floor_x265, floor_av1)
# Ordered quality -> most compressed (lower CRF = higher quality). The floor_* columns
# are no longer used by the gain estimate (CRF drives it) and are kept at 0.0.
_DEFAULT_PROFILES: list[tuple[str, int, int, str, int, float, float]] = [
    ("Archive", 18, 24, "slow", 4, 0.0, 0.0),
    ("Light", 20, 28, "slow", 6, 0.0, 0.0),
    ("Balanced", 22, 30, "slow", 6, 0.0, 0.0),
    ("Compact", 26, 34, "slow", 6, 0.0, 0.0),
    ("Mini", 28, 36, "slow", 6, 0.0, 0.0),
]


class Database:
    """Thread-safe wrapper around a single SQLite connection."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._migrate()
        self._seed()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    # Columns added after v1, applied idempotently to pre-existing databases.
    _ADDED_COLUMNS: list[tuple[str, str, str]] = [
        ("media_file", "content_type", "TEXT DEFAULT 'live_action'"),
        ("media_file", "content_source", "TEXT"),
        ("media_file", "is_anime", "INTEGER DEFAULT 0"),
        ("media_file", "reencoded_at", "REAL"),
        ("reference_bpp", "content_type", "TEXT NOT NULL DEFAULT 'live_action'"),
    ]

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            for table, column, decl in self._ADDED_COLUMNS:
                if not self._has_column(table, column):
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            row = self._conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
                )
            else:
                self._conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
            self._conn.commit()

    def _has_column(self, table: str, column: str) -> bool:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(r["name"] == column for r in rows)

    def _seed(self) -> None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) AS c FROM reference_bpp WHERE content_type = 'live_action'"
            )
            if cur.fetchone()["c"] == 0:
                self._conn.executemany(
                    "INSERT INTO reference_bpp (height_min, height_max, bpp_target, content_type) "
                    "VALUES (?, ?, ?, 'live_action')",
                    _DEFAULT_REFERENCE_BPP,
                )
            cur = self._conn.execute(
                "SELECT COUNT(*) AS c FROM reference_bpp WHERE content_type = 'animation'"
            )
            if cur.fetchone()["c"] == 0:
                self._conn.executemany(
                    "INSERT INTO reference_bpp (height_min, height_max, bpp_target, content_type) "
                    "VALUES (?, ?, ?, 'animation')",
                    _DEFAULT_REFERENCE_BPP_ANIM,
                )
            # Insert any missing default profiles (adds new tiers like Compact/Mini to
            # existing databases without overwriting user-edited profiles; name is PK).
            self._conn.executemany(
                "INSERT OR IGNORE INTO encode_profile "
                "(name, crf_x265, crf_av1, preset_x265, preset_av1, floor_x265, floor_av1, "
                " x265_params, svtav1_params) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (*p, _DEFAULT_X265_PARAMS, _DEFAULT_SVTAV1_PARAMS)
                    for p in _DEFAULT_PROFILES
                ],
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
