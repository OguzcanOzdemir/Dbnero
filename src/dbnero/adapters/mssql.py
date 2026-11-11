"""Microsoft SQL Server adapter (pyodbc + T-SQL BACKUP/RESTORE).

``BACKUP DATABASE`` writes the ``.bak`` on the *server's* file system. Getting
it into the local archive is a separate transfer step, chosen per connection:

* ``share``  - the server backup directory is reachable from this machine
  (SMB share, NFS or bind mount). Set ``server_backup_dir`` (path as the
  server sees it) and ``local_backup_dir`` (same directory as seen locally).
  Recommended; also required for restoring from the local archive.
* ``sql``    - stream the file through T-SQL ``OPENROWSET(BULK ...)``. Needs
  ``ADMINISTER BULK OPERATIONS``; reads the file in large chunks, so it is
  slower for very large databases. Restore then uses the server-side copy.

Profile ``options`` keys: ``odbc_driver``, ``trust_server_certificate``
("true"/"false"), ``server_backup_dir``, ``local_backup_dir``, ``transfer``
("auto" | "share" | "sql"), ``connect_timeout``.
"""

from __future__ import annotations

import re
import shutil
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from dbnero.adapters.base import (
    AdapterError,
    ConnectionFailed,
    JobContext,
    OperationCancelled,
    register_adapter,
)
from dbnero.adapters.identifiers import quote_identifier
from dbnero.adapters.tools import list_odbc_drivers
from dbnero.compare.snapshots import (
    Column,
    ForeignKey,
    Index,
    Routine,
    SchemaSnapshot,
    Table,
)
from dbnero.core.models import (
    AuthType,
    BackupResult,
    ConnectionProfile,
    Engine,
    RestoreResult,
    SslMode,
    ValidationResult,
    utc_now,
)

SYSTEM_DATABASES = frozenset({"master", "model", "msdb", "tempdb"})
FORMAT_LABEL = "mssql_bak"
SQL_TRANSFER_CHUNK = 32 * 1024 * 1024
_PERCENT_RE = re.compile(r"(\d+)\s+percent processed")
_MTF_MAGIC = b"TAPE"


def _quote(name: str) -> str:
    return quote_identifier(name, Engine.MSSQL)


def _literal(value: str) -> str:
    """N'...' string literal for statements that do not accept parameters."""
    if "\x00" in value:
        raise AdapterError("Invalid character in path")
    return "N'" + value.replace("'", "''") + "'"


def _odbc_value(value: str) -> str:
    return "{" + value.replace("}", "}}") + "}"


def _bool_opt(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class MSSQLAdapter:
    engine = Engine.MSSQL

    def __init__(
        self,
        profile: ConnectionProfile,
        password: str | None,
        tool_paths: dict[str, str] | None = None,
    ) -> None:
        self.profile = profile
        self._password = password
        self._platform: str | None = None

    # -- connection helpers ---------------------------------------------

    def _driver(self) -> str:
        configured = self.profile.options.get("odbc_driver")
        if configured:
            return configured
        drivers = list_odbc_drivers()
        for preferred in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"):
            if preferred in drivers:
                return preferred
        if drivers:
            return drivers[-1]
        raise ConnectionFailed(
            "No Microsoft ODBC Driver for SQL Server found. Install ODBC Driver 18."
        )

    def connection_string(self, database: str | None = None) -> str:
        p = self.profile
        parts = [
            f"DRIVER={_odbc_value(self._driver())}",
            f"SERVER={_odbc_value(f'{p.host},{p.port}')}",
            "APP=dbnero",
        ]
        db = database or p.default_database
        if db:
            parts.append(f"DATABASE={_odbc_value(db)}")
        if p.auth_type is AuthType.WINDOWS:
            parts.append("Trusted_Connection=yes")
        else:
            parts.append(f"UID={_odbc_value(p.username)}")
            parts.append(f"PWD={_odbc_value(self._password or '')}")
        encrypt = "no" if p.ssl_mode is SslMode.DISABLE else "yes"
        parts.append(f"Encrypt={encrypt}")
        trust = _bool_opt(
            p.options.get("trust_server_certificate"), p.ssl_mode is not SslMode.VERIFY_FULL
        )
        parts.append(f"TrustServerCertificate={'yes' if trust else 'no'}")
        return ";".join(parts)

    @contextmanager
    def _connect(self, database: str | None = None) -> Iterator[Any]:
        import pyodbc  # type: ignore[import-not-found, unused-ignore]

        try:
            conn = pyodbc.connect(
                self.connection_string(database),
                autocommit=True,
                timeout=int(self.profile.options.get("connect_timeout", "10")),
            )
        except pyodbc.Error as exc:
            raise ConnectionFailed(_odbc_error(exc)) from None
        try:
            yield conn
        finally:
            conn.close()

    def _scalar(self, sql: str, params: Sequence[Any] = (), database: str | None = None) -> Any:
        with self._connect(database) as conn:
            row = conn.cursor().execute(sql, *params).fetchone()
        return row[0] if row else None

    def _run_with_messages(
        self, sql: str, ctx: JobContext | None, label: str, database: str | None = None
    ) -> list[str]:
        """Execute a long-running statement, forwarding STATS progress messages
        and supporting cancellation via ``cursor.cancel()``."""
        import pyodbc

        messages: list[str] = []
        with self._connect(database) as conn:
            cur = conn.cursor()
            stop = threading.Event()

            def watch() -> None:
                while not stop.wait(0.3):
                    if ctx is not None and ctx.cancelled:
                        cur.cancel()
                        return

            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()
            try:
                cur.execute(sql)
                while True:
                    for _, text in getattr(cur, "messages", None) or []:
                        msg = str(text).split("]")[-1].strip()
                        messages.append(msg)
                        if ctx is not None and (m := _PERCENT_RE.search(msg)):
                            ctx.progress(int(m.group(1)) / 100, f"{label} {m.group(1)}%")
                    if not cur.nextset():
                        break
            except pyodbc.Error as exc:
                if ctx is not None and ctx.cancelled:
                    raise OperationCancelled from None
                raise AdapterError(_odbc_error(exc)) from None
            finally:
                stop.set()
                watcher.join(timeout=2)
        return messages

    def _server_platform(self) -> str:
        if self._platform is None:
            try:
                value = self._scalar("SELECT host_platform FROM sys.dm_os_host_info")
            except AdapterError:
                value = None
            self._platform = str(value or "Windows")
        return self._platform

    def _server_join(self, directory: str, name: str) -> str:
        if self._server_platform().lower() == "linux":
            return str(PurePosixPath(directory) / name)
        return str(PureWindowsPath(directory) / name)

    def server_backup_dir(self) -> str:
        configured = self.profile.options.get("server_backup_dir")
        if configured:
            return configured
        value = self._scalar(
            "SELECT CAST(SERVERPROPERTY('InstanceDefaultBackupPath') AS nvarchar(4000))"
        )
        if not value:
            raise AdapterError(
                "Server backup directory unknown; set 'server_backup_dir' on the connection"
            )
        return str(value)

    def _local_backup_dir(self) -> Path | None:
        value = self.profile.options.get("local_backup_dir")
        return Path(value) if value else None

    def _transfer_mode(self) -> str:
        mode = self.profile.options.get("transfer", "auto")
        if mode == "auto":
            return "share" if self._local_backup_dir() else "sql"
        if mode not in {"share", "sql"}:
            raise AdapterError(f"Unknown transfer mode {mode!r}")
        return mode

    # -- protocol -------------------------------------------------------

    def backup_format(self, options: dict[str, Any]) -> tuple[str, str]:
        return "bak", FORMAT_LABEL

    def test_connection(self) -> str:
        version = self._scalar(
            "SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(128)) + ' ' + "
            "CAST(SERVERPROPERTY('Edition') AS nvarchar(128))"
        )
        return str(version or "")

    def list_databases(self, include_system: bool = False) -> list[str]:
        with self._connect() as conn:
            rows = (
                conn.cursor()
                .execute("SELECT name FROM sys.databases WHERE state_desc = 'ONLINE' ORDER BY name")
                .fetchall()
            )
        names = [str(r[0]) for r in rows]
        if include_system:
            return names
        return [n for n in names if n not in SYSTEM_DATABASES]

    def _all_database_names(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.cursor().execute("SELECT name FROM sys.databases").fetchall()
        return [str(r[0]) for r in rows]

    def create_database(self, name: str) -> None:
        with self._connect() as conn:
            conn.cursor().execute(f"CREATE DATABASE {_quote(name)}")

    def database_size_bytes(self, database: str) -> int | None:
        value = self._scalar(
            "SELECT SUM(CAST(size AS bigint)) * 8192 FROM sys.master_files "
            "WHERE database_id = DB_ID(?) AND type = 0",
            (database,),
        )
        return int(value) if value is not None else None

    def drop_database(self, name: str) -> None:
        if name in SYSTEM_DATABASES:
            raise AdapterError(f"Refusing to drop system database {name!r}")
        if name not in self._all_database_names():
            return
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(f"ALTER DATABASE {_quote(name)} SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
            cur.execute(f"DROP DATABASE {_quote(name)}")

    def backup(
        self,
        database: str,
        destination: Path,
        options: dict[str, Any],
        ctx: JobContext | None = None,
    ) -> BackupResult:
        if database == "tempdb":
            raise AdapterError("tempdb cannot be backed up")
        version = self.test_connection()
        mode = self._transfer_mode()
        file_name = destination.name.removesuffix(".partial")
        server_path = self._server_join(self.server_backup_dir(), file_name)

        sql = (
            f"BACKUP DATABASE {_quote(database)} TO DISK = {_literal(server_path)} "
            "WITH COPY_ONLY, CHECKSUM, INIT, FORMAT, STATS = 5"
        )
        if options.get("compression"):
            sql += ", COMPRESSION"
        if ctx:
            ctx.progress(0.0, f"BACKUP DATABASE {database}")
        self._run_with_messages(sql, ctx, "Backup")

        if ctx:
            ctx.progress(None, "RESTORE VERIFYONLY")
        self._run_with_messages(
            f"RESTORE VERIFYONLY FROM DISK = {_literal(server_path)} WITH CHECKSUM",
            ctx,
            "Verify",
        )

        warnings: list[str] = []
        extra = {"server_file_path": server_path, "transfer": mode}
        if mode == "share":
            local_dir = self._local_backup_dir()
            assert local_dir is not None  # noqa: S101
            local_copy = local_dir / file_name
            if not local_copy.is_file():
                raise AdapterError(
                    f"Backup written to {server_path} but not visible at {local_copy}; "
                    "check that local_backup_dir points to the same directory"
                )
            if ctx:
                ctx.progress(None, "Copying backup from share")
            try:
                shutil.copyfile(local_copy, destination)
            except PermissionError:
                # The SQL Server service account may create the file with modes
                # this user cannot read; fall back to streaming it over SQL.
                warnings.append(
                    f"No read permission on {local_copy}; transferred through SQL instead"
                )
                extra["transfer"] = "sql"
                self._download_via_sql(server_path, destination, ctx)
            if not _bool_opt(self.profile.options.get("keep_server_copy"), False):
                try:
                    local_copy.unlink()
                    extra.pop("server_file_path")
                except FileNotFoundError:
                    extra.pop("server_file_path")
                except PermissionError:
                    warnings.append(f"Server-side copy could not be removed: {server_path}")
        else:
            self._download_via_sql(server_path, destination, ctx)
            warnings.append(f"Server-side copy kept at {server_path}")

        return BackupResult(
            file_path=destination,
            backup_format=FORMAT_LABEL,
            engine_version=version,
            tool_versions={"odbc_driver": self._driver()},
            warnings=warnings,
            extra=extra,
        )

    def _download_via_sql(
        self, server_path: str, destination: Path, ctx: JobContext | None
    ) -> None:
        source = f"OPENROWSET(BULK {_literal(server_path)}, SINGLE_BLOB) AS f"
        with self._connect() as conn:
            cur = conn.cursor()
            try:
                total = int(
                    cur.execute(f"SELECT DATALENGTH(BulkColumn) FROM {source}").fetchone()[0]
                )
            except Exception as exc:
                raise AdapterError(
                    "Could not read the backup through OPENROWSET (needs ADMINISTER BULK "
                    f"OPERATIONS). Configure a shared backup directory instead. {_odbc_error(exc)}"
                ) from None
            offset = 1
            with destination.open("wb") as fh:
                while offset <= total:
                    if ctx:
                        ctx.raise_if_cancelled()
                        ctx.progress((offset - 1) / total if total else None, "Downloading backup")
                    row = cur.execute(
                        f"SELECT SUBSTRING(BulkColumn, ?, ?) FROM {source}",
                        offset,
                        SQL_TRANSFER_CHUNK,
                    ).fetchone()
                    chunk = bytes(row[0])
                    if not chunk:
                        break
                    fh.write(chunk)
                    offset += len(chunk)
        if destination.stat().st_size != total:
            raise AdapterError("Downloaded backup size does not match the server file")

    def server_file_sha256(self, server_path: str) -> str | None:
        """SHA-256 of a server-side file (SQL Server 2016+), or None if unreadable."""
        try:
            value = self._scalar(
                "SELECT HASHBYTES('SHA2_256', BulkColumn) FROM "
                f"OPENROWSET(BULK {_literal(server_path)}, SINGLE_BLOB) AS f"
            )
        except AdapterError:
            return None
        return bytes(value).hex() if value else None

    def _stage_for_restore(
        self, source: Path, options: dict[str, Any], ctx: JobContext | None
    ) -> tuple[str, Path | None]:
        """Make the backup available on the server. Returns (server path, temp local copy)."""
        local_dir = self._local_backup_dir()
        if local_dir is not None:
            staged = local_dir / f"dbnero_restore_{source.name}"
            if ctx:
                ctx.progress(None, "Copying backup to server share")
            shutil.copyfile(source, staged)
            return self._server_join(self.server_backup_dir(), staged.name), staged

        server_path = options.get("server_file_path")
        if not server_path:
            raise AdapterError(
                "SQL Server can only restore files it can read. Configure a shared backup "
                "directory (server_backup_dir + local_backup_dir) on the target connection."
            )
        expected = options.get("checksum_sha256")
        if expected:
            actual = self.server_file_sha256(server_path)
            if actual is None:
                raise AdapterError(f"Server copy {server_path} is not readable")
            if actual != expected:
                raise AdapterError(f"Server copy {server_path} does not match the archived backup")
        return server_path, None

    def restore(
        self,
        source: Path,
        target_database: str,
        options: dict[str, Any],
        ctx: JobContext | None = None,
    ) -> RestoreResult:
        _quote(target_database)
        if target_database in SYSTEM_DATABASES:
            raise AdapterError(f"Cannot restore into system database {target_database!r}")
        exists = target_database in self._all_database_names()
        if exists and not options.get("overwrite"):
            raise AdapterError(f"Target database {target_database!r} already exists")

        server_path, staged = self._stage_for_restore(source, options, ctx)
        try:
            with self._connect() as conn:
                cur = conn.cursor()
                files = cur.execute(
                    f"RESTORE FILELISTONLY FROM DISK = {_literal(server_path)}"
                ).fetchall()
                data_dir = cur.execute(
                    "SELECT CAST(SERVERPROPERTY('InstanceDefaultDataPath') AS nvarchar(4000))"
                ).fetchone()[0]
                log_dir = cur.execute(
                    "SELECT CAST(SERVERPROPERTY('InstanceDefaultLogPath') AS nvarchar(4000))"
                ).fetchone()[0]
            moves = []
            safe_target = re.sub(r"[^A-Za-z0-9_]+", "_", target_database)[:60] or "db"
            for f in files:
                logical, physical, ftype = str(f[0]), str(f[1]), str(f[2])
                suffix = Path(physical.replace("\\", "/")).suffix or (
                    ".ldf" if ftype == "L" else ".mdf"
                )
                directory = log_dir if ftype == "L" else data_dir
                new_name = f"{safe_target}_{re.sub(r'[^A-Za-z0-9_]+', '_', logical)}{suffix}"
                new_path = self._server_join(directory, new_name)
                moves.append(f"MOVE {_literal(logical)} TO {_literal(new_path)}")

            if exists:
                if ctx:
                    ctx.progress(None, f"Dropping existing database {target_database}")
                self.drop_database(target_database)
            sql = (
                f"RESTORE DATABASE {_quote(target_database)} FROM DISK = {_literal(server_path)} "
                f"WITH {', '.join(moves)}, CHECKSUM, RECOVERY, STATS = 5"
            )
            self._run_with_messages(sql, ctx, "Restore")
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)
        return RestoreResult(target_database=target_database)

    def validate_backup(self, source: Path) -> ValidationResult:
        if not source.is_file():
            return ValidationResult(False, [], [f"File not found: {source}"])
        with source.open("rb") as fh:
            magic = fh.read(4)
        if magic != _MTF_MAGIC:
            return ValidationResult(False, [], ["Not a SQL Server backup (missing MTF header)"])
        checks = [f"MTF header OK, size {source.stat().st_size} bytes"]
        return ValidationResult(True, checks, [])

    def verify_on_server(self, server_path: str) -> ValidationResult:
        try:
            self._run_with_messages(
                f"RESTORE VERIFYONLY FROM DISK = {_literal(server_path)} WITH CHECKSUM",
                None,
                "Verify",
            )
        except AdapterError as exc:
            return ValidationResult(False, [], [str(exc)])
        return ValidationResult(True, ["RESTORE VERIFYONLY succeeded"], [])

    # -- schema inspection ---------------------------------------------

    def inspect_schema(self, database: str) -> SchemaSnapshot:
        snap = SchemaSnapshot(
            engine=self.engine.value,
            database=database,
            engine_version=self.test_connection(),
            captured_at_utc=utc_now(),
        )
        with self._connect(database) as conn:
            cur = conn.cursor()

            tables: dict[int, Table] = {}
            for obj_id, schema, name, rows in cur.execute(
                """
                SELECT t.object_id, s.name, t.name,
                       (SELECT SUM(p.rows) FROM sys.partitions p
                         WHERE p.object_id = t.object_id AND p.index_id IN (0, 1))
                FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id
                WHERE t.is_ms_shipped = 0
                ORDER BY s.name, t.name
                """
            ).fetchall():
                tables[int(obj_id)] = Table(schema=schema, name=name, row_count=rows)

            for (
                obj_id,
                col,
                tname,
                max_len,
                prec,
                scale,
                nullable,
                identity,
                default,
                col_id,
            ) in cur.execute(
                """
                    SELECT c.object_id, c.name, ty.name, c.max_length, c.precision, c.scale,
                           c.is_nullable, c.is_identity, dc.definition, c.column_id
                    FROM sys.columns c
                    JOIN sys.types ty ON ty.user_type_id = c.user_type_id
                    LEFT JOIN sys.default_constraints dc
                      ON dc.parent_object_id = c.object_id AND dc.parent_column_id = c.column_id
                    JOIN sys.tables t ON t.object_id = c.object_id
                    WHERE t.is_ms_shipped = 0
                    ORDER BY c.object_id, c.column_id
                    """
            ).fetchall():
                if (t := tables.get(int(obj_id))) is not None:
                    dtype = _format_type(tname, max_len, prec, scale)
                    if identity:
                        dtype += " identity"
                    t.columns.append(Column(col, dtype, bool(nullable), default, int(col_id)))

            index_map: dict[tuple[int, int], Index] = {}
            for obj_id, idx_id, idx_name, is_pk, is_unique, col in cur.execute(
                """
                SELECT i.object_id, i.index_id, i.name, i.is_primary_key, i.is_unique, c.name
                FROM sys.indexes i
                JOIN sys.index_columns ic
                  ON ic.object_id = i.object_id AND ic.index_id = i.index_id
                 AND ic.key_ordinal > 0
                JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
                JOIN sys.tables t ON t.object_id = i.object_id
                WHERE t.is_ms_shipped = 0 AND i.type > 0
                ORDER BY i.object_id, i.index_id, ic.key_ordinal
                """
            ).fetchall():
                if (t := tables.get(int(obj_id))) is None:
                    continue
                if is_pk:
                    t.primary_key.append(col)
                    continue
                key = (int(obj_id), int(idx_id))
                if key not in index_map:
                    index_map[key] = Index(idx_name, [], bool(is_unique))
                    t.indexes.append(index_map[key])
                index_map[key].columns.append(col)

            fk_map: dict[int, ForeignKey] = {}
            for (
                fk_id,
                obj_id,
                fk_name,
                col,
                ref_schema,
                ref_table,
                ref_col,
                dele,
                upd,
            ) in cur.execute(
                """
                    SELECT fk.object_id, fk.parent_object_id, fk.name, pc.name,
                           rs.name, rt.name, rc.name,
                           fk.delete_referential_action_desc, fk.update_referential_action_desc
                    FROM sys.foreign_keys fk
                    JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
                    JOIN sys.columns pc
                      ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id
                    JOIN sys.tables rt ON rt.object_id = fkc.referenced_object_id
                    JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
                    JOIN sys.columns rc
                      ON rc.object_id = fkc.referenced_object_id
                     AND rc.column_id = fkc.referenced_column_id
                    ORDER BY fk.object_id, fkc.constraint_column_id
                    """
            ).fetchall():
                if (t := tables.get(int(obj_id))) is None:
                    continue
                if int(fk_id) not in fk_map:
                    fk_map[int(fk_id)] = ForeignKey(
                        fk_name,
                        [],
                        f"{ref_schema}.{ref_table}",
                        [],
                        on_delete=str(dele).replace("_", " "),
                        on_update=str(upd).replace("_", " "),
                    )
                    t.foreign_keys.append(fk_map[int(fk_id)])
                fk_map[int(fk_id)].columns.append(col)
                fk_map[int(fk_id)].ref_columns.append(ref_col)
            snap.tables = list(tables.values())

            kinds = {
                "V": "view",
                "P": "procedure",
                "FN": "function",
                "IF": "function",
                "TF": "function",
                "TR": "trigger",
            }
            routines: list[Routine] = []
            for schema, name, otype, parent, definition in cur.execute(
                """
                SELECT s.name, o.name, RTRIM(o.type), OBJECT_NAME(o.parent_object_id), m.definition
                FROM sys.objects o
                JOIN sys.schemas s ON s.schema_id = o.schema_id
                LEFT JOIN sys.sql_modules m ON m.object_id = o.object_id
                WHERE o.is_ms_shipped = 0 AND o.type IN ('V', 'P', 'FN', 'IF', 'TF', 'TR')
                ORDER BY s.name, o.name
                """
            ).fetchall():
                kind = kinds[str(otype)]
                full = f"{parent}.{name}" if kind == "trigger" and parent else name
                routines.append(Routine(schema, full, kind, definition))
            snap.routines = routines
        return snap

    # -- data access ----------------------------------------------------

    def count_rows(self, database: str, schema: str, table: str) -> int:
        value = self._scalar(
            f"SELECT COUNT_BIG(*) FROM {_quote(schema)}.{_quote(table)}", database=database
        )
        return int(value or 0)

    def iter_rows(
        self,
        database: str,
        schema: str,
        table: str,
        columns: Sequence[str],
        order_by: Sequence[str],
        batch_size: int = 5000,
    ) -> Iterator[tuple[Any, ...]]:
        cols = ", ".join(_quote(c) for c in columns)
        sql = f"SELECT {cols} FROM {_quote(schema)}.{_quote(table)}"
        if order_by:
            sql += " ORDER BY " + ", ".join(_quote(c) for c in order_by)
        with self._connect(database) as conn:
            cur = conn.cursor()
            cur.execute(sql)
            while rows := cur.fetchmany(batch_size):
                yield from (tuple(r) for r in rows)


def _format_type(name: str, max_len: int, precision: int, scale: int) -> str:
    if name in {"varchar", "char", "varbinary", "binary"}:
        return f"{name}({'max' if max_len == -1 else max_len})"
    if name in {"nvarchar", "nchar"}:
        return f"{name}({'max' if max_len == -1 else max_len // 2})"
    if name in {"decimal", "numeric"}:
        return f"{name}({precision},{scale})"
    if name in {"datetime2", "time", "datetimeoffset"}:
        return f"{name}({scale})"
    return name


def _odbc_error(exc: Exception) -> str:
    args: tuple[Any, ...] = tuple(getattr(exc, "args", ()))
    text = str(args[1]) if len(args) > 1 else str(exc)
    # Drop the "[Microsoft][ODBC Driver 18 ...][SQL Server]" prefixes and API names.
    text = re.sub(r"(\[[^\]]*\])+", "", text)
    return re.sub(r"\s*\(SQL[A-Za-z]+\)", "", text).strip()


register_adapter(Engine.MSSQL, MSSQLAdapter)
