"""Database seeding/migration tests."""

from __future__ import annotations

from vlo.storage.db import Database


def test_fresh_db_seeds_all_default_profiles(tmp_path):
    db = Database(tmp_path / "vlo.db")
    try:
        names = {r["name"] for r in db.conn.execute("SELECT name FROM encode_profile")}
        assert names == {"Archive", "Light", "Balanced", "Compact", "Mini"}
    finally:
        db.close()


def test_existing_db_gains_new_profiles_without_overwriting(tmp_path):
    path = tmp_path / "vlo.db"
    db = Database(path)
    try:
        # Simulate a pre-existing install: only the original three profiles, and a
        # user-edited Archive CRF.
        db.conn.execute("DELETE FROM encode_profile WHERE name IN ('Compact', 'Mini')")
        db.conn.execute("UPDATE encode_profile SET crf_x265 = 15 WHERE name = 'Archive'")
        db.conn.commit()
    finally:
        db.close()

    db2 = Database(path)  # re-open -> _seed runs again
    try:
        rows = {r["name"]: r["crf_x265"] for r in db2.conn.execute(
            "SELECT name, crf_x265 FROM encode_profile")}
        assert {"Compact", "Mini"} <= set(rows)        # new tiers added
        assert rows["Archive"] == 15                   # user edit preserved (INSERT OR IGNORE)
    finally:
        db2.close()
