"""Database seeding/migration tests."""

from __future__ import annotations

from vlo.storage.db import Database


def test_fresh_db_seeds_all_default_profiles(tmp_path):
    db = Database(tmp_path / "vlo.db")
    try:
        names = {r["name"] for r in db.conn.execute("SELECT name FROM encode_profile")}
        assert names == {"Archive", "Light", "Balanced", "Compact", "Mini", "Extreme"}
    finally:
        db.close()


def test_fresh_db_uses_widened_av1_ladder(tmp_path):
    db = Database(tmp_path / "vlo.db")
    try:
        rows = {r["name"]: r["crf_av1"] for r in db.conn.execute(
            "SELECT name, crf_av1 FROM encode_profile")}
        assert rows["Balanced"] == 30
        assert rows["Compact"] == 36
        assert rows["Mini"] == 42
    finally:
        db.close()


def test_migration_widens_old_av1_ladder_but_keeps_user_edits(tmp_path):
    path = tmp_path / "vlo.db"
    db = Database(path)
    try:
        # Simulate a pre-v4 install: old AV1 defaults on Compact, a user-tuned Mini.
        db.conn.execute("UPDATE encode_profile SET crf_av1 = 34 WHERE name = 'Compact'")
        db.conn.execute("UPDATE encode_profile SET crf_av1 = 40 WHERE name = 'Mini'")
        db.conn.execute("UPDATE schema_version SET version = 3")
        db.conn.commit()
    finally:
        db.close()

    db2 = Database(path)  # re-open -> data migrations run (old_version 3 < 4 and < 5)
    try:
        rows = {r["name"]: r["crf_av1"] for r in db2.conn.execute(
            "SELECT name, crf_av1 FROM encode_profile")}
        assert rows["Compact"] == 36   # 34 -> 38 (v4) -> 36 (v5)
        assert rows["Mini"] == 40       # user edit (never an old default) preserved
    finally:
        db2.close()


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
