"""Single and batch backups, validation, deletion and retention."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from dbnero.adapters.base import (
    AdapterError,
    JobContext,
    OperationCancelled,
    create_adapter,
)
from dbnero.compare.snapshots import SchemaSnapshot
from dbnero.core.connection_service import ConnectionService
from dbnero.core.logging_setup import mask_secrets
from dbnero.core.models import (
    BackupBatch,
    BackupMode,
    BackupRecord,
    BackupStatus,
    ConnectionProfile,
    ValidationResult,
    utc_now,
)
from dbnero.core.settings import Settings
from dbnero.storage.archive import backup_paths, finalize, partial_file, sha256_file
from dbnero.storage.catalog import BackupFilter, Catalog
from dbnero.storage.manifests import write_manifest

log = logging.getLogger(__name__)

LogFn = Callable[[str, str], None]  # (level, message)


def _noop_log(level: str, message: str) -> None:
    log.log(logging.getLevelName(level), message)


class BackupError(Exception):
    pass


class InsufficientDiskSpace(BackupError):
    pass


@dataclass(slots=True)
class BackupRequest:
    connection_id: str
    database: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    reference_date: datetime | None = None
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BatchItemResult:
    database: str
    record: BackupRecord | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.record is not None and self.record.status in {
            BackupStatus.SUCCEEDED,
            BackupStatus.UNVERIFIED,
        }


@dataclass(slots=True)
class BatchResult:
    batch: BackupBatch
    items: list[BatchItemResult] = field(default_factory=list)

    @property
    def succeeded(self) -> list[BatchItemResult]:
        return [i for i in self.items if i.ok]

    @property
    def failed(self) -> list[BatchItemResult]:
        return [i for i in self.items if not i.ok]


class BackupService:
    def __init__(
        self, catalog: Catalog, connections: ConnectionService, settings: Settings
    ) -> None:
        self.catalog = catalog
        self.connections = connections
        self.settings = settings

    @property
    def archive_root(self) -> Path:
        return self.settings.resolved_archive_root()

    # -- single backup ----------------------------------------------------

    def backup_database(
        self,
        request: BackupRequest,
        ctx: JobContext | None = None,
        log_fn: LogFn | None = None,
        batch_id: str | None = None,
    ) -> BackupRecord:
        """Back up one database to its own file. Always leaves a catalog record
        (succeeded / unverified / failed / cancelled) and never a partial file."""
        emit = log_fn or _noop_log
        profile = self.connections.get(request.connection_id)
        adapter = self.connections.adapter_for(profile)
        options = {
            **self.settings.backup_defaults.get(profile.engine.value, {}),
            **request.options,
        }
        ext, fmt = adapter.backup_format(options)

        record = BackupRecord(
            connection_id=profile.id,
            engine=profile.engine,
            database_name=request.database,
            server_label=profile.server_label,
            backup_format=fmt,
            file_path=Path(),
            backup_mode=BackupMode.BATCH if batch_id else BackupMode.SINGLE,
            batch_id=batch_id,
            description=request.description,
            tags=list(dict.fromkeys(t.strip() for t in request.tags if t.strip())),
            reference_date=request.reference_date,
            status=BackupStatus.RUNNING,
        )
        paths = backup_paths(
            self.archive_root,
            profile.engine,
            profile.server_label,
            request.database,
            record.created_at_utc,
            record.short_id,
            ext,
        )
        record.file_path = paths.backup
        self.catalog.add_backup(record)
        emit("INFO", f"{request.database}: backup started ({profile.name})")

        try:
            if ctx:
                ctx.raise_if_cancelled()
            self._check_free_space(adapter.database_size_bytes(request.database), emit)

            snapshot_path: Path | None = None
            try:
                if ctx:
                    ctx.progress(None, f"{request.database}: capturing schema snapshot")
                snap = adapter.inspect_schema(request.database)
                paths.schema.parent.mkdir(parents=True, exist_ok=True)
                snap.save(paths.schema)
                snapshot_path = paths.schema
                emit(
                    "INFO",
                    f"{request.database}: schema snapshot captured ({len(snap.tables)} tables)",
                )
            except AdapterError as exc:
                emit("WARNING", f"{request.database}: could not capture schema snapshot: {exc}")

            with partial_file(paths.backup) as partial:
                result = adapter.backup(request.database, partial, options, ctx)
                if ctx:
                    ctx.progress(None, f"{request.database}: computing SHA-256")
                checksum = sha256_file(partial)
                size = partial.stat().st_size
                finalize(partial, paths.backup)
            for warning in result.warnings:
                emit("WARNING", f"{request.database}: {warning}")

            try:
                validation = adapter.validate_backup(paths.backup, ctx)
            except OperationCancelled:
                validation = ValidationResult(False, [], ["Verification was cancelled"])
            status = BackupStatus.SUCCEEDED if validation.ok else BackupStatus.UNVERIFIED
            if not validation.ok:
                emit(
                    "WARNING",
                    f"{request.database}: validation failed: {'; '.join(validation.errors)}",
                )

            record.file_size = size
            record.checksum_sha256 = checksum
            record.engine_version = result.engine_version
            record.tool_versions = result.tool_versions
            record.backup_format = result.backup_format
            record.schema_snapshot_path = snapshot_path
            record.extra = result.extra
            record.status = status
            record.error_message = None if validation.ok else "; ".join(validation.errors)
            write_manifest(record, paths.manifest)
            self.catalog.update_backup(
                record.backup_id,
                file_size=size,
                checksum_sha256=checksum,
                engine_version=record.engine_version,
                tool_versions=record.tool_versions,
                backup_format=record.backup_format,
                schema_snapshot_path=snapshot_path,
                extra=record.extra,
                status=status,
                error_message=record.error_message,
            )
            emit("INFO", f"{request.database}: done → {paths.backup.name} ({size} bytes)")
            return record
        except OperationCancelled:
            self._mark(record, BackupStatus.CANCELLED, "Cancelled", paths.schema)
            emit("WARNING", f"{request.database}: cancelled")
            raise
        except Exception as exc:
            message = mask_secrets(str(exc)) or type(exc).__name__
            self._mark(record, BackupStatus.FAILED, message, paths.schema)
            emit("ERROR", f"{request.database}: failed: {message}")
            raise

    def _mark(
        self, record: BackupRecord, status: BackupStatus, message: str, schema_path: Path
    ) -> None:
        record.status = status
        record.error_message = message
        schema_path.unlink(missing_ok=True)
        self.catalog.update_backup(
            record.backup_id, status=status, error_message=message, schema_snapshot_path=None
        )

    def _check_free_space(self, estimated: int | None, emit: LogFn) -> None:
        root = self.archive_root
        root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(root).free
        reserve = self.settings.min_free_space_mb * 1024 * 1024
        needed = (estimated or 0) + reserve
        if free < needed:
            raise InsufficientDiskSpace(
                f"Not enough free space on the archive disk: {free // 2**20} MB free, "
                f"at least {needed // 2**20} MB required"
            )
        if estimated:
            emit(
                "INFO",
                f"Estimated database size {estimated // 2**20} MB, free space {free // 2**20} MB",
            )

    # -- batch backup -----------------------------------------------------

    def backup_batch(
        self,
        connection_id: str,
        databases: Sequence[str],
        description: str = "",
        tags: Sequence[str] = (),
        reference_date: datetime | None = None,
        options: dict[str, Any] | None = None,
        ctx: JobContext | None = None,
        log_fn: LogFn | None = None,
        max_parallel: int | None = None,
        schedule_id: str | None = None,
    ) -> BatchResult:
        """Back up several databases into separate files and catalog records.
        A failing database never cancels or hides the others."""
        if not databases:
            raise BackupError("Select at least one database")
        emit = log_fn or _noop_log
        batch = BackupBatch(
            connection_id=connection_id,
            database_names=list(databases),
            description=description,
            schedule_id=schedule_id,
        )
        self.catalog.add_batch(batch)
        result = BatchResult(batch)
        parallel = max(1, min(max_parallel or self.settings.max_parallel_jobs, len(databases)))
        done = 0
        total = len(databases)

        def run_one(db: str) -> BatchItemResult:
            nonlocal done
            sub_ctx = JobContext(cancel_event=ctx.cancel_event) if ctx else None
            if ctx and sub_ctx:
                sub_ctx.on_progress = lambda f, m: ctx.progress(done / total, m)
            try:
                if ctx and ctx.cancelled:
                    raise OperationCancelled
                rec = self.backup_database(
                    BackupRequest(
                        connection_id, db, description, list(tags), reference_date, options or {}
                    ),
                    sub_ctx,
                    emit,
                    batch_id=batch.batch_id,
                )
                return BatchItemResult(db, rec)
            except OperationCancelled:
                return BatchItemResult(db, None, "Cancelled")
            except Exception as exc:
                return BatchItemResult(db, None, mask_secrets(str(exc)) or type(exc).__name__)
            finally:
                done += 1
                if ctx:
                    ctx.progress(done / total, f"{done}/{total} databases processed")

        if parallel == 1:
            result.items = [run_one(db) for db in databases]
        else:
            with ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="dbnero-batch") as ex:
                result.items = list(ex.map(run_one, databases))

        emit(
            "INFO" if not result.failed else "WARNING",
            f"Batch finished: {len(result.succeeded)} succeeded, {len(result.failed)} failed",
        )
        if ctx and ctx.cancelled:
            raise OperationCancelled
        return result

    # -- validation -------------------------------------------------------

    def validate(
        self, backup_id: str, update_status: bool = True, ctx: JobContext | None = None
    ) -> ValidationResult:
        record = self.catalog.get_backup(backup_id)
        checks: list[str] = []
        errors: list[str] = []
        path = record.file_path
        if not path.is_file():
            errors.append(f"Backup file not found: {path}")
        else:
            size = path.stat().st_size
            if record.file_size is not None and size != record.file_size:
                errors.append(f"File size changed: recorded {record.file_size}, now {size}")
            else:
                checks.append(f"File size matches ({size} bytes)")
            if record.checksum_sha256:
                actual = sha256_file(path)
                if actual != record.checksum_sha256:
                    errors.append("SHA-256 mismatch: the file is corrupted or was modified")
                else:
                    checks.append("SHA-256 matches")
            else:
                errors.append("No SHA-256 recorded for this backup")
            if not errors:
                adapter = self._offline_adapter(record)
                deep = adapter.validate_backup(path, ctx)
                checks.extend(deep.checks)
                errors.extend(deep.errors)

        result = ValidationResult(not errors, checks, errors)
        if update_status and record.status in {
            BackupStatus.SUCCEEDED,
            BackupStatus.UNVERIFIED,
        }:
            new_status = BackupStatus.SUCCEEDED if result.ok else BackupStatus.UNVERIFIED
            self.catalog.update_backup(
                backup_id,
                status=new_status,
                error_message=None if result.ok else "; ".join(errors),
            )
        return result

    def _offline_adapter(self, record: BackupRecord) -> Any:
        """Adapter for local file checks; works even if the connection was deleted."""
        try:
            profile = self.connections.get(record.connection_id)
        except LookupError:
            profile = ConnectionProfile(
                name="offline", engine=record.engine, host="-", port=record.engine.default_port
            )
        return create_adapter(profile, None, self.settings.tool_paths)

    # -- metadata ------------------------------------------------------------

    def update_metadata(
        self,
        backup_id: str,
        description: str,
        tags: Sequence[str],
        reference_date: datetime | None = None,
    ) -> BackupRecord:
        """Change user metadata (never the creation time) in catalog and manifest."""
        self.catalog.update_backup(
            backup_id,
            description=description.strip(),
            tags=list(dict.fromkeys(t.strip() for t in tags if t.strip())),
            reference_date=reference_date,
        )
        record = self.catalog.get_backup(backup_id)
        manifest = manifest_path(record)
        if manifest.exists():
            write_manifest(record, manifest)
        return record

    # -- deletion & retention -----------------------------------------------

    def delete_backup(self, backup_id: str, delete_files: bool = True) -> None:
        record = self.catalog.get_backup(backup_id)
        if delete_files:
            for p in backup_files(record):
                p.unlink(missing_ok=True)
            _prune_empty_dirs(record.file_path.parent, self.archive_root)
        self.catalog.delete_backup(backup_id)

    def retention_plan(self, now: datetime | None = None) -> list[BackupRecord]:
        """Backups the retention policy would delete.

        * "keep last N" counts only usable backups (succeeded or unverified);
          failed and cancelled entries never take a slot.
        * the newest usable backup of every database is always kept, preferring
          a succeeded one, so a database never loses its last restorable copy.
        * failed / cancelled entries are removed only by the age rule.
        """
        keep_last = self.settings.retention_keep_last
        max_age = self.settings.retention_max_age_days
        if keep_last <= 0 and max_age <= 0:
            return []
        now = now or utc_now()
        cutoff = now - timedelta(days=max_age) if max_age > 0 else None
        usable_states = {BackupStatus.SUCCEEDED, BackupStatus.UNVERIFIED}
        groups: dict[tuple[str, str], list[BackupRecord]] = {}
        for rec in self.catalog.list_backups(BackupFilter()):
            groups.setdefault((rec.server_label, rec.database_name), []).append(rec)
        doomed: list[BackupRecord] = []
        for recs in groups.values():
            recs.sort(key=lambda r: r.created_at_utc, reverse=True)
            usable = [r for r in recs if r.status in usable_states]
            succeeded = [r for r in usable if r.status is BackupStatus.SUCCEEDED]
            keeper = succeeded[0] if succeeded else (usable[0] if usable else None)
            for idx, rec in enumerate(usable):
                if keeper is not None and rec.backup_id == keeper.backup_id:
                    continue
                too_many = keep_last > 0 and idx >= keep_last
                too_old = cutoff is not None and rec.created_at_utc < cutoff
                if too_many or too_old:
                    doomed.append(rec)
            for rec in recs:
                if rec.status in usable_states or rec.status in {
                    BackupStatus.RUNNING,
                    BackupStatus.PENDING,
                }:
                    continue
                if cutoff is not None and rec.created_at_utc < cutoff:
                    doomed.append(rec)
        doomed.sort(key=lambda r: r.created_at_utc)
        return doomed

    def apply_retention(self, backup_ids: Sequence[str]) -> int:
        for backup_id in backup_ids:
            self.delete_backup(backup_id)
        return len(backup_ids)

    def load_snapshot(self, record: BackupRecord) -> SchemaSnapshot | None:
        if record.schema_snapshot_path and record.schema_snapshot_path.is_file():
            return SchemaSnapshot.load(record.schema_snapshot_path)
        return None


def manifest_path(record: BackupRecord) -> Path:
    from dbnero.storage.archive import MANIFEST_SUFFIX

    base = record.file_path
    marker = f"_{record.short_id}"
    stem = base.name[: base.name.rfind(marker) + len(marker)] if marker in base.name else base.stem
    return base.parent / f"{stem}{MANIFEST_SUFFIX}"


def backup_files(record: BackupRecord) -> list[Path]:
    base = record.file_path
    files = [base, base.with_name(base.name + ".partial"), manifest_path(record)]
    if record.schema_snapshot_path:
        files.append(record.schema_snapshot_path)
    if record.comparison_snapshot_path:
        files.append(record.comparison_snapshot_path)
    return files


def _prune_empty_dirs(directory: Path, root: Path) -> None:
    try:
        directory.resolve().relative_to(root.resolve())
    except ValueError:
        return
    current = directory
    while current != root and current.is_dir() and not any(current.iterdir()):
        current.rmdir()
        current = current.parent


__all__ = [
    "BackupError",
    "BackupRequest",
    "BackupService",
    "BatchItemResult",
    "BatchResult",
    "InsufficientDiskSpace",
    "backup_files",
    "manifest_path",
]
