"""CRUD for re-encode jobs."""

from __future__ import annotations

from typing import Any

from ..core.enums import Codec, JobState
from ..core.models import Job
from .db import Database


class JobsRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create(self, job: Job) -> int:
        with self._db.lock:
            cur = self._db.conn.execute(
                """
                INSERT INTO job (
                    media_file_id, source_path, codec, profile_name, crf, preset, state,
                    progress, batch_id, size_src_bytes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.media_file_id, job.source_path, job.codec.value, job.profile_name,
                    job.crf, job.preset, job.state.value, job.progress, job.batch_id,
                    job.size_src_bytes, job.created_at,
                ),
            )
            self._db.conn.commit()
            return int(cur.lastrowid)

    def get(self, job_id: int) -> Job | None:
        with self._db.lock:
            row = self._db.conn.execute(
                "SELECT * FROM job WHERE id = ?", (job_id,)
            ).fetchone()
        return self._row_to_job(row) if row else None

    def list(self, *, state: JobState | None = None, batch_id: str | None = None) -> list[Job]:
        clauses, params = [], []
        if state is not None:
            clauses.append("state = ?")
            params.append(state.value)
        if batch_id is not None:
            clauses.append("batch_id = ?")
            params.append(batch_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._db.lock:
            rows = self._db.conn.execute(
                f"SELECT * FROM job {where} ORDER BY id", params
            ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def next_queued(self) -> Job | None:
        with self._db.lock:
            row = self._db.conn.execute(
                "SELECT * FROM job WHERE state = ? ORDER BY id LIMIT 1",
                (JobState.QUEUED.value,),
            ).fetchone()
        return self._row_to_job(row) if row else None

    def update(self, job_id: int, **fields: Any) -> None:
        if not fields:
            return
        # Serialise enums.
        if "state" in fields and isinstance(fields["state"], JobState):
            fields["state"] = fields["state"].value
        if "codec" in fields and isinstance(fields["codec"], Codec):
            fields["codec"] = fields["codec"].value
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._db.lock:
            self._db.conn.execute(
                f"UPDATE job SET {cols} WHERE id = ?", (*fields.values(), job_id)
            )
            self._db.conn.commit()

    def delete(self, job_id: int) -> bool:
        """Delete a single terminal job. Returns False if it's still active."""
        with self._db.lock:
            row = self._db.conn.execute(
                "SELECT state FROM job WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return False
            if not JobState(row["state"]).is_terminal:
                return False
            self._db.conn.execute("DELETE FROM job WHERE id = ?", (job_id,))
            self._db.conn.commit()
            return True

    def delete_terminal(self) -> int:
        """Delete all terminal jobs (DONE/REJECTED/CANCELLED/FAILED). Returns count."""
        terminal = [s.value for s in JobState if s.is_terminal]
        placeholders = ", ".join("?" for _ in terminal)
        with self._db.lock:
            cur = self._db.conn.execute(
                f"DELETE FROM job WHERE state IN ({placeholders})", terminal
            )
            self._db.conn.commit()
            return cur.rowcount

    def reset_interrupted(self) -> int:
        """On startup, requeue jobs that were mid-flight when the app stopped."""
        active = [s.value for s in JobState if s.is_active_in_worker]
        placeholders = ", ".join("?" for _ in active)
        with self._db.lock:
            cur = self._db.conn.execute(
                f"UPDATE job SET state = ?, progress = 0, speed = NULL, eta_s = NULL "
                f"WHERE state IN ({placeholders})",
                (JobState.QUEUED.value, *active),
            )
            self._db.conn.commit()
            return cur.rowcount

    @staticmethod
    def _row_to_job(row: Any) -> Job:
        return Job(
            id=row["id"],
            media_file_id=row["media_file_id"],
            source_path=row["source_path"],
            codec=Codec(row["codec"]),
            profile_name=row["profile_name"],
            crf=row["crf"],
            preset=row["preset"],
            state=JobState(row["state"]),
            progress=row["progress"] or 0.0,
            speed=row["speed"],
            eta_s=row["eta_s"],
            batch_id=row["batch_id"],
            work_dir=row["work_dir"],
            out_path_local=row["out_path_local"],
            size_src_bytes=row["size_src_bytes"],
            size_out_bytes=row["size_out_bytes"],
            gain_bytes=row["gain_bytes"],
            validation_json=row["validation_json"],
            error_message=row["error_message"],
            created_at=row["created_at"] or 0.0,
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )
