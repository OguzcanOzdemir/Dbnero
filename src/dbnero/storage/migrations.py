"""Versioned schema migrations for the SQLite catalog.

The schema version is tracked in ``PRAGMA user_version``. Each entry in
``MIGRATIONS`` upgrades the catalog by exactly one version. Never edit a
migration that has been released; append a new one instead.
"""

from __future__ import annotations

import logging

from sqlalchemy import Connection, text

log = logging.getLogger(__name__)

_V1 = [
    """
    CREATE TABLE connections (
        id                TEXT PRIMARY KEY,
        name              TEXT NOT NULL UNIQUE,
        engine            TEXT NOT NULL,
        host              TEXT NOT NULL,
        port              INTEGER NOT NULL,
        username          TEXT NOT NULL DEFAULT '',
        auth_type         TEXT NOT NULL,
        ssl_mode          TEXT NOT NULL,
        default_database  TEXT NOT NULL DEFAULT '',
        options           TEXT NOT NULL DEFAULT '{}',
        created_at_utc    TEXT NOT NULL,
        updated_at_utc    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE batches (
        batch_id        TEXT PRIMARY KEY,
        connection_id   TEXT NOT NULL,
        description     TEXT NOT NULL DEFAULT '',
        database_names  TEXT NOT NULL DEFAULT '[]',
        created_at_utc  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE backups (
        backup_id                 TEXT PRIMARY KEY,
        batch_id                  TEXT REFERENCES batches(batch_id),
        connection_id             TEXT NOT NULL,
        engine                    TEXT NOT NULL,
        engine_version            TEXT NOT NULL DEFAULT '',
        server_label              TEXT NOT NULL,
        database_name             TEXT NOT NULL,
        created_at_utc            TEXT NOT NULL,
        reference_date            TEXT,
        description               TEXT NOT NULL DEFAULT '',
        tags                      TEXT NOT NULL DEFAULT '[]',
        backup_format             TEXT NOT NULL,
        backup_mode               TEXT NOT NULL,
        file_path                 TEXT NOT NULL,
        file_size                 INTEGER,
        checksum_sha256           TEXT,
        status                    TEXT NOT NULL,
        error_message             TEXT,
        tool_versions             TEXT NOT NULL DEFAULT '{}',
        schema_snapshot_path      TEXT,
        comparison_snapshot_path  TEXT
    )
    """,
    "CREATE INDEX ix_backups_created ON backups(created_at_utc)",
    "CREATE INDEX ix_backups_db ON backups(connection_id, database_name)",
    "CREATE INDEX ix_backups_batch ON backups(batch_id)",
    # The real creation time is assigned by the system and must never change.
    """
    CREATE TRIGGER trg_backups_created_immutable
    BEFORE UPDATE OF created_at_utc ON backups
    WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
    BEGIN
        SELECT RAISE(ABORT, 'created_at_utc is immutable');
    END
    """,
    """
    CREATE TABLE jobs (
        job_id           TEXT PRIMARY KEY,
        kind             TEXT NOT NULL,
        status           TEXT NOT NULL,
        title            TEXT NOT NULL,
        connection_id    TEXT,
        backup_id        TEXT,
        batch_id         TEXT,
        started_at_utc   TEXT NOT NULL,
        finished_at_utc  TEXT,
        error_message    TEXT
    )
    """,
    """
    CREATE TABLE job_logs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id      TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        logged_at_utc TEXT NOT NULL,
        level       TEXT NOT NULL,
        message     TEXT NOT NULL
    )
    """,
    "CREATE INDEX ix_job_logs_job ON job_logs(job_id)",
]

# v2: engine-specific extra metadata (e.g. MSSQL server-side .bak path).
_V2 = [
    "ALTER TABLE backups ADD COLUMN extra TEXT NOT NULL DEFAULT '{}'",
]

MIGRATIONS: list[list[str]] = [_V1, _V2]

LATEST_VERSION = len(MIGRATIONS)


class CatalogTooNew(RuntimeError):
    pass


def current_version(conn: Connection) -> int:
    return int(conn.exec_driver_sql("PRAGMA user_version").scalar_one())


def migrate(conn: Connection) -> int:
    """Upgrade the catalog to ``LATEST_VERSION``. Returns the resulting version."""
    version = current_version(conn)
    if version > LATEST_VERSION:
        raise CatalogTooNew(
            f"Catalog schema version {version} is newer than this application "
            f"supports ({LATEST_VERSION}). Please update dbnero."
        )
    for target in range(version + 1, LATEST_VERSION + 1):
        log.info("Migrating catalog to schema version %d", target)
        for statement in MIGRATIONS[target - 1]:
            conn.execute(text(statement))
        conn.exec_driver_sql(f"PRAGMA user_version = {int(target)}")
    return LATEST_VERSION
