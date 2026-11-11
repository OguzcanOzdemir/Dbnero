"""MySQL adapter: mysql-connector for metadata, mysqldump/mysql for backups.

Credentials reach the CLI tools through a private ``--defaults-extra-file``;
the password never appears in argv.
"""

from __future__ import annotations

import gzip
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from dbnero.adapters.base import (
    AdapterError,
    ConnectionFailed,
    JobContext,
    ToolNotFound,
    register_adapter,
)
from dbnero.adapters.identifiers import quote_identifier
from dbnero.adapters.process import run_tool, secret_file
from dbnero.adapters.tools import find_tool
from dbnero.compare.snapshots import (
    Column,
    ForeignKey,
    Index,
    Routine,
    SchemaSnapshot,
    Table,
)
from dbnero.core.models import (
    BackupResult,
    ConnectionProfile,
    Engine,
    RestoreResult,
    SslMode,
    ValidationResult,
    utc_now,
)

SYSTEM_DATABASES = frozenset({"mysql", "sys", "information_schema", "performance_schema"})
DUMP_COMPLETED_MARKER = b"-- Dump completed"
_CHUNK = 1024 * 1024
_TAIL_BYTES = 4096

_SSL_MODES = {
    SslMode.DISABLE: "DISABLED",
    SslMode.PREFER: "PREFERRED",
    SslMode.REQUIRE: "REQUIRED",
    SslMode.VERIFY_FULL: "VERIFY_IDENTITY",
}


def _quote(name: str) -> str:
    return quote_identifier(name, Engine.MYSQL)


def _option_value(value: str) -> str:
    """Quote a value for a MySQL option file."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _check_db_name(name: str) -> str:
    quote_identifier(name, Engine.MYSQL)  # validates
    if name.startswith("-"):
        raise AdapterError(f"Unsupported database name {name!r}")
    return name


class MySQLAdapter:
    engine = Engine.MYSQL

    def __init__(
        self,
        profile: ConnectionProfile,
        password: str | None,
        tool_paths: dict[str, str] | None = None,
    ) -> None:
        self.profile = profile
        self._password = password
        self._tool_paths = tool_paths or {}
        self._tool_info: dict[str, str] = {}

    # -- connection helpers ---------------------------------------------

    @contextmanager
    def _connect(self, database: str | None = None, buffered: bool = True) -> Iterator[Any]:
        import mysql.connector

        kwargs: dict[str, Any] = {
            "host": self.profile.host,
            "port": self.profile.port,
            "user": self.profile.username or None,
            "password": self._password or "",
            "connection_timeout": int(self.profile.options.get("connect_timeout", "10")),
            "charset": "utf8mb4",
            "autocommit": True,
        }
        if database or self.profile.default_database:
            kwargs["database"] = database or self.profile.default_database
        if self.profile.ssl_mode is SslMode.DISABLE:
            kwargs["ssl_disabled"] = True
        elif self.profile.ssl_mode is SslMode.VERIFY_FULL:
            kwargs["ssl_verify_cert"] = True
            kwargs["ssl_verify_identity"] = True
        try:
            conn = mysql.connector.connect(**kwargs)
        except mysql.connector.Error as exc:
            raise ConnectionFailed(str(getattr(exc, "msg", exc))) from None
        try:
            yield conn
        finally:
            conn.close()

    def _query(
        self, sql: str, params: Sequence[Any] = (), database: str | None = None
    ) -> list[Any]:
        with self._connect(database) as conn:
            cur = conn.cursor()
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
            cur.close()
        return list(rows)

    def _tool(self, name: str) -> Path:
        path = find_tool(name, self._tool_paths.get(name))
        if path is None:
            raise ToolNotFound(name)
        return path

    def _tool_version_line(self, path: Path) -> str:
        key = str(path)
        if key not in self._tool_info:
            out: list[bytes] = []
            try:
                run_tool([path, "--version"], stdout_sink=out.append, timeout=30)
            except AdapterError:
                out = []
            self._tool_info[key] = b"".join(out).decode("utf-8", "replace").strip()
        return self._tool_info[key]

    def _options_file(self, tool: Path) -> str:
        lines = [
            "[client]",
            f"host={_option_value(self.profile.host)}",
            f"port={int(self.profile.port)}",
        ]
        if self.profile.username:
            lines.append(f"user={_option_value(self.profile.username)}")
        if self._password:
            lines.append(f"password={_option_value(self._password)}")
        if "MariaDB" not in self._tool_version_line(tool):
            lines.append(f"ssl-mode={_SSL_MODES[self.profile.ssl_mode]}")
        return "\n".join(lines) + "\n"

    # -- protocol -------------------------------------------------------

    def backup_format(self, options: dict[str, Any]) -> tuple[str, str]:
        if options.get("compress", True):
            return "sql.gz", "mysqldump_sql_gz"
        return "sql", "mysqldump_sql"

    def test_connection(self) -> str:
        rows = self._query("SELECT VERSION()")
        return str(rows[0][0]) if rows else ""

    def list_databases(self, include_system: bool = False) -> list[str]:
        names = [str(r[0]) for r in self._query("SHOW DATABASES")]
        if not include_system:
            names = [n for n in names if n not in SYSTEM_DATABASES]
        return sorted(names)

    def create_database(self, name: str) -> None:
        _check_db_name(name)
        self._query(
            f"CREATE DATABASE {_quote(name)} CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            if self._supports_0900()
            else f"CREATE DATABASE {_quote(name)} CHARACTER SET utf8mb4"
        )

    def _supports_0900(self) -> bool:
        version = self.test_connection()
        m = re.match(r"(\d+)", version)
        return bool(m and int(m.group(1)) >= 8 and "MariaDB" not in version)

    def drop_database(self, name: str) -> None:
        if name in SYSTEM_DATABASES:
            raise AdapterError(f"Refusing to drop system database {name!r}")
        self._query(f"DROP DATABASE IF EXISTS {_quote(name)}")

    def backup(
        self,
        database: str,
        destination: Path,
        options: dict[str, Any],
        ctx: JobContext | None = None,
    ) -> BackupResult:
        _check_db_name(database)
        mysqldump = self._tool("mysqldump")
        version_line = self._tool_version_line(mysqldump)
        is_mariadb_tool = "MariaDB" in version_line
        server_version = self.test_connection()
        warnings = self._non_transactional_warnings(database)

        args: list[str | Path] = [
            mysqldump,
            # --defaults-extra-file must be the first option.
            "",  # placeholder, replaced below
            "--single-transaction",
            "--quick",
            "--routines",
            "--triggers",
            "--events",
            "--hex-blob",
            "--no-tablespaces",
            "--default-character-set=utf8mb4",
        ]
        if not is_mariadb_tool:
            args += ["--set-gtid-purged=OFF", "--column-statistics=0"]
        args.append(database)

        compress = bool(options.get("compress", True))
        if ctx:
            ctx.progress(None, f"mysqldump {database}")
        with secret_file(self._options_file(mysqldump), suffix=".cnf") as cnf:
            args[1] = f"--defaults-extra-file={cnf}"
            if compress:
                with gzip.open(destination, "wb", compresslevel=6) as gz:
                    run_tool(args, stdout_sink=lambda b: _write(gz, b), ctx=ctx)
            else:
                with destination.open("wb") as fh:
                    run_tool(args, stdout=fh, ctx=ctx)

        label = self.backup_format(options)[1]
        return BackupResult(
            file_path=destination,
            backup_format=label,
            engine_version=server_version,
            tool_versions={"mysqldump": _short_version(version_line)},
            warnings=warnings,
        )

    def _non_transactional_warnings(self, database: str) -> list[str]:
        rows = self._query(
            "SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE' "
            "AND ENGINE IS NOT NULL AND ENGINE <> 'InnoDB'",
            (database,),
        )
        if not rows:
            return []
        names = ", ".join(f"{r[0]} ({r[1]})" for r in rows[:10])
        return [
            "Non-InnoDB tables are not covered by --single-transaction and may be "
            f"inconsistent: {names}" + (" ..." if len(rows) > 10 else "")
        ]

    def restore(
        self,
        source: Path,
        target_database: str,
        options: dict[str, Any],
        ctx: JobContext | None = None,
    ) -> RestoreResult:
        _check_db_name(target_database)
        if target_database in SYSTEM_DATABASES:
            raise AdapterError(f"Cannot restore into system database {target_database!r}")
        mysql_cli = self._tool("mysql")
        exists = target_database in self.list_databases(include_system=True)
        if exists and not options.get("overwrite"):
            raise AdapterError(f"Target database {target_database!r} already exists")
        if exists:
            if ctx:
                ctx.progress(None, f"Dropping existing database {target_database}")
            self.drop_database(target_database)
        self.create_database(target_database)

        total = source.stat().st_size
        read = 0

        def chunks() -> Iterator[bytes]:
            nonlocal read
            with source.open("rb") as raw:
                stream: Any = gzip.GzipFile(fileobj=raw) if _is_gzip(source) else raw
                while chunk := stream.read(_CHUNK):
                    read = raw.tell()
                    if ctx:
                        ctx.raise_if_cancelled()
                        ctx.progress(read / total if total else None, "mysql import")
                    yield chunk

        with secret_file(self._options_file(mysql_cli), suffix=".cnf") as cnf:
            run_tool(
                [
                    mysql_cli,
                    f"--defaults-extra-file={cnf}",
                    "--default-character-set=utf8mb4",
                    "--binary-mode",
                    f"--database={target_database}",
                ],
                stdin_source=chunks(),
                ctx=ctx,
            )
        return RestoreResult(target_database=target_database)

    def validate_backup(self, source: Path) -> ValidationResult:
        checks: list[str] = []
        if not source.is_file():
            return ValidationResult(False, [], [f"File not found: {source}"])
        tail = b""
        try:
            if _is_gzip(source):
                with gzip.open(source, "rb") as gz:
                    while chunk := gz.read(_CHUNK):
                        tail = (tail + chunk)[-_TAIL_BYTES:]
                checks.append("gzip stream intact")
            else:
                with source.open("rb") as fh:
                    size = fh.seek(0, 2)
                    fh.seek(max(size - _TAIL_BYTES, 0))
                    tail = fh.read()
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            return ValidationResult(False, checks, [f"Corrupt compressed file: {exc}"])
        if DUMP_COMPLETED_MARKER not in tail:
            return ValidationResult(
                False, checks, ["Dump is incomplete (missing '-- Dump completed' trailer)"]
            )
        checks.append("Dump trailer present")
        return ValidationResult(True, checks, [])

    # -- schema inspection ---------------------------------------------

    def inspect_schema(self, database: str) -> SchemaSnapshot:
        snap = SchemaSnapshot(
            engine=self.engine.value,
            database=database,
            engine_version=self.test_connection(),
            captured_at_utc=utc_now(),
        )
        strip_db = re.compile(r"`" + re.escape(database.replace("`", "``")) + r"`\.")

        def clean(definition: str | None) -> str | None:
            return strip_db.sub("", definition) if definition else definition

        with self._connect() as conn:
            cur = conn.cursor()

            def q(sql: str) -> list[Any]:
                cur.execute(sql, (database,))
                return list(cur.fetchall())

            tables: dict[str, Table] = {}
            for name, rows in q(
                "SELECT TABLE_NAME, TABLE_ROWS FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME"
            ):
                tables[name] = Table(schema="", name=name, row_count=rows)
            for table, col, ctype, nullable, default, ordinal, extra in q(
                "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, "
                "ORDINAL_POSITION, EXTRA FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME, ORDINAL_POSITION"
            ):
                if (t := tables.get(table)) is not None:
                    dtype = str(ctype) + (" auto_increment" if "auto_increment" in extra else "")
                    t.columns.append(
                        Column(col, dtype, nullable == "YES", _str(default), int(ordinal))
                    )
            indexes: dict[tuple[str, str], Index] = {}
            for table, idx, non_unique, col, expr in q(
                "SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, COLUMN_NAME, NULL "
                "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = %s "
                "ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX"
            ):
                if (t := tables.get(table)) is None:
                    continue
                column = col if col is not None else str(expr)
                if idx == "PRIMARY":
                    t.primary_key.append(column)
                    continue
                key = (table, idx)
                if key not in indexes:
                    indexes[key] = Index(idx, [], not int(non_unique))
                    t.indexes.append(indexes[key])
                indexes[key].columns.append(column)
            fks: dict[tuple[str, str], ForeignKey] = {}
            for table, name, col, ref_schema, ref_table, ref_col, upd, dele in q(
                "SELECT k.TABLE_NAME, k.CONSTRAINT_NAME, k.COLUMN_NAME, "
                "k.REFERENCED_TABLE_SCHEMA, k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME, "
                "r.UPDATE_RULE, r.DELETE_RULE "
                "FROM information_schema.KEY_COLUMN_USAGE k "
                "JOIN information_schema.REFERENTIAL_CONSTRAINTS r "
                "  ON r.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA "
                " AND r.CONSTRAINT_NAME = k.CONSTRAINT_NAME AND r.TABLE_NAME = k.TABLE_NAME "
                "WHERE k.TABLE_SCHEMA = %s AND k.REFERENCED_TABLE_NAME IS NOT NULL "
                "ORDER BY k.TABLE_NAME, k.CONSTRAINT_NAME, k.ORDINAL_POSITION"
            ):
                if (t := tables.get(table)) is None:
                    continue
                key = (table, name)
                if key not in fks:
                    ref = ref_table if ref_schema == database else f"{ref_schema}.{ref_table}"
                    fks[key] = ForeignKey(name, [], ref, [], on_delete=dele, on_update=upd)
                    t.foreign_keys.append(fks[key])
                fks[key].columns.append(col)
                fks[key].ref_columns.append(ref_col)
            snap.tables = list(tables.values())

            routines: list[Routine] = []
            for name, definition in q(
                "SELECT TABLE_NAME, VIEW_DEFINITION FROM information_schema.VIEWS "
                "WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME"
            ):
                routines.append(Routine("", name, "view", clean(definition)))
            for name, rtype, definition in q(
                "SELECT ROUTINE_NAME, ROUTINE_TYPE, ROUTINE_DEFINITION "
                "FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = %s "
                "ORDER BY ROUTINE_NAME"
            ):
                routines.append(Routine("", name, str(rtype).lower(), clean(definition)))
            for name, table, timing, event, definition in q(
                "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION, "
                "ACTION_STATEMENT FROM information_schema.TRIGGERS "
                "WHERE TRIGGER_SCHEMA = %s ORDER BY TRIGGER_NAME"
            ):
                routines.append(
                    Routine(
                        "",
                        f"{table}.{name}",
                        "trigger",
                        f"{timing} {event}: {clean(definition)}",
                    )
                )
            cur.close()
            snap.routines = routines
        return snap

    # -- data access ----------------------------------------------------

    def count_rows(self, database: str, schema: str, table: str) -> int:
        rows = self._query(f"SELECT COUNT(*) FROM {_quote(table)}", database=database)
        return int(rows[0][0]) if rows else 0

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
        sql = f"SELECT {cols} FROM {_quote(table)}"
        if order_by:
            sql += " ORDER BY " + ", ".join(_quote(c) for c in order_by)
        with self._connect(database) as conn:
            cur = conn.cursor(buffered=False)
            cur.execute(sql)
            try:
                while rows := cur.fetchmany(batch_size):
                    yield from (tuple(r) for r in rows)
            finally:
                cur.close()


def _write(fh: Any, data: bytes) -> None:
    fh.write(data)


def _is_gzip(path: Path) -> bool:
    with path.open("rb") as fh:
        return fh.read(2) == b"\x1f\x8b"


def _short_version(line: str) -> str:
    m = re.search(r"(\d+\.\d+\.\d+)", line)
    return m.group(1) if m else line


def _str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes | bytearray):
        return value.decode("utf-8", "replace")
    return str(value)


register_adapter(Engine.MYSQL, MySQLAdapter)
