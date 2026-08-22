"""Regression coverage for the temporary Chroma dimension-reset tool."""

import asyncio
import json
import tarfile

from scripts import reset_chroma_dimension as tool


def _insert_source_with_embedding(db, tmp_path, *, status, dimension, error=""):
    source_path = tmp_path / f"source-{dimension}-{status}.txt"
    source_path.write_text("dimension migration source", encoding="utf-8")
    with db.connect() as connection:
        user_id = connection.execute("SELECT id FROM users WHERE username = 'user'").fetchone()["id"]
        source_id = connection.execute(
            """
            INSERT INTO sources (user_id, filename, stored_path, content_type, status, error)
            VALUES (?, ?, ?, 'text/plain', ?, ?)
            """,
            (user_id, source_path.name, str(source_path), status, error),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO chunks (user_id, source_id, chunk_index, location, text, embedding_json)
            VALUES (?, ?, 0, 'document', 'dimension migration source', ?)
            """,
            (user_id, source_id, json.dumps([0.0] * dimension)),
        )
    return source_id


def test_inspect_sources_classifies_safe_reuse_and_reindex(fresh_modules, tmp_path):
    db = fresh_modules.db
    old_source = _insert_source_with_embedding(db, tmp_path, status="indexed", dimension=384)
    current_source = _insert_source_with_embedding(db, tmp_path, status="indexed", dimension=1536)
    recoverable_source = _insert_source_with_embedding(
        db,
        tmp_path,
        status="failed",
        dimension=1536,
        error="Collection expecting embedding with dimension of 384, got 1536",
    )
    unrelated_failure = _insert_source_with_embedding(
        db,
        tmp_path,
        status="failed",
        dimension=1536,
        error="All connection attempts failed",
    )

    plan = tool.inspect_sources(db.DB_PATH, 1536)

    assert plan.reusable_source_ids == (current_source, recoverable_source)
    assert plan.recoverable_failure_ids == (recoverable_source,)
    assert plan.mark_for_reindex_ids == (old_source,)
    assert unrelated_failure in plan.unchanged_source_ids


def test_apply_migration_rebuilds_target_dimension_and_backs_up(fresh_modules, local_embed, tmp_path):
    db, ingest, vs = fresh_modules.db, fresh_modules.ingest, fresh_modules.vector_store
    source_path = tmp_path / "old.txt"
    source_path.write_text("old dimension source", encoding="utf-8")
    with db.connect() as connection:
        user_id = connection.execute("SELECT id FROM users WHERE username = 'user'").fetchone()["id"]
        old_source = connection.execute(
            """
            INSERT INTO sources (user_id, filename, stored_path, content_type, status)
            VALUES (?, 'old.txt', ?, 'text/plain', 'uploaded')
            """,
            (user_id, str(source_path)),
        ).lastrowid
    asyncio.run(ingest.process_source(old_source))
    assert vs.current_dimension() == 384

    recoverable_source = _insert_source_with_embedding(
        db,
        tmp_path,
        status="failed",
        dimension=1536,
        error="Collection expecting embedding with dimension of 384, got 1536",
    )
    plan = tool.inspect_sources(db.DB_PATH, 1536)
    backup_dir = tmp_path / "backups"

    backup_path, restored, dimension = tool.apply_migration(
        db.DATA_DIR,
        tool.DEFAULT_COLLECTION,
        plan,
        backup_dir,
        batch_size=100,
    )

    assert backup_path.is_file()
    with tarfile.open(backup_path, "r:gz") as archive:
        names = archive.getnames()
    assert "app.sqlite3" in names
    assert any(name == "chroma" or name.startswith("chroma/") for name in names)
    assert restored == 1
    assert dimension == 1536

    vs.reset_client()
    assert vs.current_dimension() == 1536
    assert vs.collection().count() == 1
    # Startup diff-sync must not replay the old 384-dimension source and lock
    # the new collection back to the previous schema.
    assert vs.sync_from_sqlite(mode="diff") == {"upserted": 0, "deleted": 0, "skipped_dimension": 0}
    assert vs.current_dimension() == 1536
    with db.connect() as connection:
        statuses = {
            row["id"]: row["status"]
            for row in connection.execute(
                "SELECT id, status FROM sources WHERE id IN (?, ?)",
                (old_source, recoverable_source),
            ).fetchall()
        }
    assert statuses == {old_source: "failed", recoverable_source: "indexed"}


def test_dry_run_does_not_change_collection_or_source_status(fresh_modules, local_embed, tmp_path):
    db, ingest, vs = fresh_modules.db, fresh_modules.ingest, fresh_modules.vector_store
    source_path = tmp_path / "dry-run.txt"
    source_path.write_text("dry run source", encoding="utf-8")
    with db.connect() as connection:
        user_id = connection.execute("SELECT id FROM users WHERE username = 'user'").fetchone()["id"]
        source_id = connection.execute(
            """
            INSERT INTO sources (user_id, filename, stored_path, content_type, status)
            VALUES (?, 'dry-run.txt', ?, 'text/plain', 'uploaded')
            """,
            (user_id, str(source_path)),
        ).lastrowid
    asyncio.run(ingest.process_source(source_id))

    result = tool.main(
        [
            "--data-dir",
            str(db.DATA_DIR),
            "--target-dimension",
            "1536",
        ]
    )

    assert result == 0
    vs.reset_client()
    assert vs.current_dimension() == 384
    with db.connect() as connection:
        assert connection.execute("SELECT status FROM sources WHERE id = ?", (source_id,)).fetchone()["status"] == "indexed"


def test_cli_requires_explicit_services_stopped_acknowledgement(fresh_modules):
    try:
        tool.main(
            [
                "--data-dir",
                str(fresh_modules.db.DATA_DIR),
                "--target-dimension",
                "1536",
                "--apply",
            ]
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("--apply unexpectedly ran without --services-stopped")


def test_apply_rejects_populated_collection_already_at_target_dimension(
    fresh_modules,
    local_embed,
    tmp_path,
):
    db, ingest, vs = fresh_modules.db, fresh_modules.ingest, fresh_modules.vector_store
    source_path = tmp_path / "same-dimension.txt"
    source_path.write_text("same dimension source", encoding="utf-8")
    with db.connect() as connection:
        user_id = connection.execute("SELECT id FROM users WHERE username = 'user'").fetchone()["id"]
        source_id = connection.execute(
            """
            INSERT INTO sources (user_id, filename, stored_path, content_type, status)
            VALUES (?, 'same-dimension.txt', ?, 'text/plain', 'uploaded')
            """,
            (user_id, str(source_path)),
        ).lastrowid
    asyncio.run(ingest.process_source(source_id))

    try:
        tool.main(
            [
                "--data-dir",
                str(db.DATA_DIR),
                "--target-dimension",
                "384",
                "--apply",
                "--services-stopped",
            ]
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("same-dimension apply unexpectedly reset the collection")

    assert not (db.DATA_DIR / "backups").exists()
    vs.reset_client()
    assert vs.current_dimension() == 384
