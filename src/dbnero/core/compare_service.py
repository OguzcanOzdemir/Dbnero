"""Schema and data comparisons between backups and live databases.

* Schema: uses snapshots captured at backup time, or inspects live databases.
* Data: live databases are compared directly. A backup is first restored
  into an isolated temporary database (``dbnero_tmp_*``) on a chosen
  "scratch" connection of the same engine, compared, then dropped.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from dbnero.adapters.base import DatabaseAdapter, JobContext
from dbnero.compare.data_diff import DataDiff, DataDiffOptions, compare_data
from dbnero.compare.schema_diff import DiffOptions, SchemaDiff, compare_schemas
from dbnero.compare.snapshots import SchemaSnapshot
from dbnero.core.backup_service import BackupService, LogFn, _noop_log
from dbnero.core.connection_service import ConnectionService
from dbnero.core.models import BackupRecord
from dbnero.storage.catalog import Catalog

TEMP_DB_PREFIX = "dbnero_tmp_"


class SnapshotMissing(Exception):
    def __init__(self, record: BackupRecord) -> None:
        super().__init__(
            f"Backup {record.database_name} ({record.short_id}) has no schema snapshot, "
            "so its schema cannot be compared directly"
        )


class EngineMismatch(Exception):
    pass


@dataclass(slots=True)
class LiveSide:
    connection_id: str
    database: str


def backup_label(record: BackupRecord) -> str:
    return f"backup {record.database_name} @ {record.created_at_utc:%Y-%m-%d %H:%M} UTC"


class CompareService:
    def __init__(
        self, catalog: Catalog, connections: ConnectionService, backups: BackupService
    ) -> None:
        self.catalog = catalog
        self.connections = connections
        self.backups = backups

    # -- schema -------------------------------------------------------------

    def _snapshot(self, record: BackupRecord) -> SchemaSnapshot:
        snap = self.backups.load_snapshot(record)
        if snap is None:
            raise SnapshotMissing(record)
        return snap

    def _live_label(self, side: LiveSide) -> str:
        return f"{self.connections.get(side.connection_id).name}/{side.database}"

    def _check_engines(self, *engines: object) -> None:
        if len(set(engines)) > 1:
            raise EngineMismatch("Only sources of the same database engine can be compared")

    def schema_backup_vs_live(
        self, backup_id: str, live: LiveSide, options: DiffOptions | None = None
    ) -> SchemaDiff:
        record = self.catalog.get_backup(backup_id)
        profile = self.connections.get(live.connection_id)
        self._check_engines(record.engine, profile.engine)
        snap = self._snapshot(record)
        live_snap = self.connections.adapter_for(profile).inspect_schema(live.database)
        return compare_schemas(
            snap, live_snap, options, backup_label(record), self._live_label(live)
        )

    def schema_backup_vs_backup(
        self, left_id: str, right_id: str, options: DiffOptions | None = None
    ) -> SchemaDiff:
        left, right = self.catalog.get_backup(left_id), self.catalog.get_backup(right_id)
        self._check_engines(left.engine, right.engine)
        return compare_schemas(
            self._snapshot(left),
            self._snapshot(right),
            options,
            backup_label(left),
            backup_label(right),
        )

    def schema_live_vs_live(
        self, left: LiveSide, right: LiveSide, options: DiffOptions | None = None
    ) -> SchemaDiff:
        lp, rp = self.connections.get(left.connection_id), self.connections.get(right.connection_id)
        self._check_engines(lp.engine, rp.engine)
        return compare_schemas(
            self.connections.adapter_for(lp).inspect_schema(left.database),
            self.connections.adapter_for(rp).inspect_schema(right.database),
            options,
            self._live_label(left),
            self._live_label(right),
        )

    # -- data ---------------------------------------------------------------

    @contextmanager
    def isolated_restore(
        self,
        record: BackupRecord,
        scratch_connection_id: str,
        ctx: JobContext | None = None,
        log_fn: LogFn | None = None,
    ) -> Iterator[tuple[DatabaseAdapter, str]]:
        """Restore ``record`` into a throwaway database and drop it afterwards."""
        emit = log_fn or _noop_log
        profile = self.connections.get(scratch_connection_id)
        self._check_engines(record.engine, profile.engine)
        adapter = self.connections.adapter_for(profile)
        temp_name = f"{TEMP_DB_PREFIX}{record.short_id}_{uuid.uuid4().hex[:6]}"
        emit("INFO", f"Restoring backup into temporary database {temp_name} ({profile.name})")
        try:
            adapter.restore(
                record.file_path,
                temp_name,
                {
                    "overwrite": False,
                    "server_file_path": record.extra.get("server_file_path"),
                    "checksum_sha256": record.checksum_sha256,
                },
                ctx,
            )
            yield adapter, temp_name
        finally:
            try:
                adapter.drop_database(temp_name)
                emit("INFO", f"Temporary database dropped: {temp_name}")
            except Exception as exc:  # report, but don't mask the real error
                emit("WARNING", f"Could not drop temporary database {temp_name}: {exc}")

    def data_live_vs_live(
        self,
        left: LiveSide,
        right: LiveSide,
        options: DataDiffOptions | None = None,
        ctx: JobContext | None = None,
    ) -> DataDiff:
        la = self.connections.adapter_for(left.connection_id)
        ra = self.connections.adapter_for(right.connection_id)
        self._check_engines(la.profile.engine, ra.profile.engine)
        return compare_data(
            la,
            left.database,
            la.inspect_schema(left.database),
            ra,
            right.database,
            ra.inspect_schema(right.database),
            options,
            ctx,
            self._live_label(left),
            self._live_label(right),
        )

    def data_backup_vs_live(
        self,
        backup_id: str,
        live: LiveSide,
        scratch_connection_id: str | None = None,
        options: DataDiffOptions | None = None,
        ctx: JobContext | None = None,
        log_fn: LogFn | None = None,
    ) -> DataDiff:
        record = self.catalog.get_backup(backup_id)
        live_adapter = self.connections.adapter_for(live.connection_id)
        with self.isolated_restore(
            record, scratch_connection_id or live.connection_id, ctx, log_fn
        ) as (tmp_adapter, tmp_db):
            return compare_data(
                tmp_adapter,
                tmp_db,
                tmp_adapter.inspect_schema(tmp_db),
                live_adapter,
                live.database,
                live_adapter.inspect_schema(live.database),
                options,
                ctx,
                backup_label(record),
                self._live_label(live),
            )

    def data_backup_vs_backup(
        self,
        left_id: str,
        right_id: str,
        scratch_connection_id: str,
        options: DataDiffOptions | None = None,
        ctx: JobContext | None = None,
        log_fn: LogFn | None = None,
    ) -> DataDiff:
        left, right = self.catalog.get_backup(left_id), self.catalog.get_backup(right_id)
        self._check_engines(left.engine, right.engine)
        with (
            self.isolated_restore(left, scratch_connection_id, ctx, log_fn) as (la, ldb),
            self.isolated_restore(right, scratch_connection_id, ctx, log_fn) as (ra, rdb),
        ):
            return compare_data(
                la,
                ldb,
                la.inspect_schema(ldb),
                ra,
                rdb,
                ra.inspect_schema(rdb),
                options,
                ctx,
                backup_label(left),
                backup_label(right),
            )
