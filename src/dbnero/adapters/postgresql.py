"""PostgreSQL adapter: psycopg for metadata, pg_dump/pg_restore for backups."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dbnero.adapters.base import (
    AdapterError,
    ConnectionFailed,
    JobContext,
    OperationCancelled,
    ToolNotFound,
    register_adapter,
)
from dbnero.adapters.identifiers import quote_identifier
from dbnero.adapters.process import ProcessFailed, run_tool
from dbnero.adapters.tools import find_tool, tool_version
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
    ValidationResult,
    utc_now,
)

if TYPE_CHECKING:
    import psycopg

SYSTEM_DATABASES = frozenset({"template0", "template1"})
FORMAT_LABEL = "pg_custom"
_EXCLUDED_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")
_PG_DUMP_TABLE_RE = re.compile(r"dumping contents of table \"?([^\"]+)\"?")


def _quote(name: str) -> str:
    return quote_identifier(name, Engine.POSTGRESQL)


class PostgreSQLAdapter:
    engine = Engine.POSTGRESQL

    def __init__(
        self,
        profile: ConnectionProfile,
        password: str | None,
        tool_paths: dict[str, str] | None = None,
    ) -> None:
        self.profile = profile
        self._password = password
        self._tool_paths = tool_paths or {}

    # -- connection helpers ---------------------------------------------

    def _maintenance_db(self) -> str:
        return self.profile.default_database or "postgres"

    def _admin_db(self, target: str) -> str:
        """Database to connect to while creating or dropping ``target``."""
        db = self._maintenance_db()
        return "postgres" if db == target else db

    @contextmanager
    def _connect(self, database: str | None = None) -> Iterator[psycopg.Connection[Any]]:
        import psycopg

        try:
            conn = psycopg.connect(
                host=self.profile.host,
                port=self.profile.port,
                user=self.profile.username or None,
                password=self._password or None,
                dbname=database or self._maintenance_db(),
                sslmode=self.profile.ssl_mode.value,
                connect_timeout=int(self.profile.options.get("connect_timeout", "10")),
                application_name="dbnero",
                autocommit=True,
            )
        except psycopg.Error as exc:
            raise ConnectionFailed(_pg_error(exc)) from None
        try:
            yield conn
        finally:
            conn.close()

    def _env(self) -> dict[str, str]:
        env = {
            "PGHOST": self.profile.host,
            "PGPORT": str(self.profile.port),
            "PGSSLMODE": self.profile.ssl_mode.value,
            "PGAPPNAME": "dbnero",
            "PGCONNECT_TIMEOUT": self.profile.options.get("connect_timeout", "10"),
        }
        if self.profile.username:
            env["PGUSER"] = self.profile.username
        if self._password:
            env["PGPASSWORD"] = self._password
        return env

    def _tool(self, name: str) -> Path:
        path = find_tool(name, self._tool_paths.get(name))
        if path is None:
            raise ToolNotFound(name)
        return path

    # -- protocol -------------------------------------------------------

    def backup_format(self, options: dict[str, Any]) -> tuple[str, str]:
        return "dump", FORMAT_LABEL

    def test_connection(self) -> str:
        with self._connect() as conn:
            row = conn.execute("SHOW server_version").fetchone()
        return str(row[0]) if row else ""

    def server_version_num(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SHOW server_version_num").fetchone()
        return int(row[0]) if row else 0

    def list_databases(self, include_system: bool = False) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT datname FROM pg_database WHERE datallowconn OR %s ORDER BY datname",
                (include_system,),
            ).fetchall()
        names = [r[0] for r in rows]
        if include_system:
            return names
        return [n for n in names if n not in SYSTEM_DATABASES]

    def create_database(self, name: str) -> None:
        import psycopg

        with self._connect(self._admin_db(name)) as conn:
            try:
                conn.execute(f"CREATE DATABASE {_quote(name)}")
            except psycopg.Error as exc:
                raise AdapterError(_pg_error(exc)) from None

    def database_size_bytes(self, database: str) -> int | None:
        with self._connect() as conn:
            row = conn.execute("SELECT pg_database_size(%s)", (database,)).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def drop_database(self, name: str) -> None:
        import psycopg

        if name in SYSTEM_DATABASES or name == "postgres":
            raise AdapterError(f"Refusing to drop database {name!r}")
        with self._connect(self._admin_db(name)) as conn:
            try:
                version = int(conn.execute("SHOW server_version_num").fetchone()[0])  # type: ignore[index]
                if version >= 130000:
                    conn.execute(f"DROP DATABASE IF EXISTS {_quote(name)} WITH (FORCE)")
                else:
                    # PostgreSQL 12 and older: end other sessions first.
                    conn.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = %s AND pid <> pg_backend_pid()",
                        (name,),
                    )
                    conn.execute(f"DROP DATABASE IF EXISTS {_quote(name)}")
            except psycopg.Error as exc:
                raise AdapterError(_pg_error(exc)) from None

    def backup(
        self,
        database: str,
        destination: Path,
        options: dict[str, Any],
        ctx: JobContext | None = None,
    ) -> BackupResult:
        pg_dump = self._tool("pg_dump")
        server_version = self.test_connection()
        dump_version = tool_version(pg_dump) or ""
        warnings = _version_warnings(dump_version, server_version, "pg_dump")
        if any(w.startswith("ERROR") for w in warnings):
            raise AdapterError(warnings[0].removeprefix("ERROR: "))

        args: list[str | Path] = [
            pg_dump,
            "--format=custom",
            "--no-password",
            "--verbose",
            f"--file={destination}",
            f"--dbname={_dbname_conninfo(database)}",
        ]
        if (level := options.get("compress_level")) is not None:
            args.append(f"--compress={int(level)}")

        def on_line(line: str) -> None:
            if ctx is not None and (m := _PG_DUMP_TABLE_RE.search(line)):
                ctx.progress(None, f"pg_dump: {m.group(1)}")

        run_tool(args, env=self._env(), ctx=ctx, on_stderr_line=on_line)
        return BackupResult(
            file_path=destination,
            backup_format=FORMAT_LABEL,
            engine_version=server_version,
            tool_versions={"pg_dump": dump_version},
            warnings=[w for w in warnings if not w.startswith("ERROR")],
        )

    def restore(
        self,
        source: Path,
        target_database: str,
        options: dict[str, Any],
        ctx: JobContext | None = None,
    ) -> RestoreResult:
        pg_restore = self._tool("pg_restore")
        overwrite = bool(options.get("overwrite"))
        exists = target_database in self.list_databases(include_system=True)
        if target_database in SYSTEM_DATABASES:
            raise AdapterError(f"Cannot restore into system database {target_database!r}")
        if exists and not overwrite:
            raise AdapterError(f"Target database {target_database!r} already exists")

        if exists:
            # Replace: drop and recreate so the result matches the backup exactly.
            if ctx:
                ctx.progress(None, f"Dropping existing database {target_database}")
            self.drop_database(target_database)
        self.create_database(target_database)

        args: list[str | Path] = [
            pg_restore,
            "--no-password",
            "--verbose",
            "--exit-on-error",
            f"--dbname={_dbname_conninfo(target_database)}",
        ]
        if options.get("no_owner", True):
            args += ["--no-owner", "--no-privileges"]
        args.append(source)

        def on_line(line: str) -> None:
            if ctx is not None and "processing item" in line:
                ctx.progress(None, f"pg_restore: {line.split(':', 1)[-1].strip()}")

        try:
            run_tool(args, env=self._env(), ctx=ctx, on_stderr_line=on_line)
        except ProcessFailed as exc:
            raise AdapterError(f"pg_restore failed: {exc}") from exc
        return RestoreResult(target_database=target_database)

    def validate_backup(self, source: Path, ctx: JobContext | None = None) -> ValidationResult:
        checks: list[str] = []
        errors: list[str] = []
        if not source.is_file():
            return ValidationResult(False, [], [f"File not found: {source}"])
        size = source.stat().st_size
        with source.open("rb") as fh:
            magic = fh.read(5)
        if magic != b"PGDMP":
            errors.append("Not a PostgreSQL custom-format archive (missing PGDMP header)")
            return ValidationResult(False, checks, errors)
        checks.append(f"Header OK, size {size} bytes")
        try:
            pg_restore = self._tool("pg_restore")
        except ToolNotFound as exc:
            checks.append(f"Skipped TOC check: {exc}")
            return ValidationResult(True, checks, errors)

        out: list[bytes] = []
        try:
            run_tool([pg_restore, "--list", source], stdout_sink=out.append, timeout=600, ctx=ctx)
        except OperationCancelled:
            raise
        except AdapterError as exc:
            errors.append(f"pg_restore --list failed: {exc}")
            return ValidationResult(False, checks, errors)
        entries = [
            ln
            for ln in b"".join(out).decode("utf-8", "replace").splitlines()
            if ln and not ln.startswith(";")
        ]
        checks.append(f"Archive TOC readable ({len(entries)} entries)")

        # Read every data block by rendering the archive to a discarded script;
        # this catches truncated or corrupted archives that still have a TOC.
        try:
            run_tool(
                [pg_restore, f"--file={os.devnull}", source],
                ctx=ctx,
            )
        except OperationCancelled:
            raise
        except AdapterError as exc:
            errors.append(f"Archive data is unreadable: {exc}")
            return ValidationResult(False, checks, errors)
        checks.append("All archive data blocks readable")
        return ValidationResult(True, checks, errors)

    # -- schema inspection ---------------------------------------------

    def inspect_schema(self, database: str) -> SchemaSnapshot:
        with self._connect(database) as conn:
            version = str(conn.execute("SHOW server_version").fetchone()[0])  # type: ignore[index]
            snap = SchemaSnapshot(
                engine=self.engine.value,
                database=database,
                engine_version=version,
                captured_at_utc=utc_now(),
            )
            tables: dict[tuple[str, str], Table] = {}
            for schema, name, est_rows in conn.execute(
                """
                SELECT n.nspname, c.relname, c.reltuples::bigint
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind IN ('r', 'p')
                  AND n.nspname <> ALL(%s) AND n.nspname NOT LIKE 'pg_temp%%'
                  AND NOT c.relispartition
                ORDER BY 1, 2
                """,
                (list(_EXCLUDED_SCHEMAS),),
            ):
                tables[(schema, name)] = Table(
                    schema=schema, name=name, row_count=max(int(est_rows), 0) or None
                )

            for schema, table, col, dtype, notnull, default, ordinal in conn.execute(
                """
                SELECT n.nspname, c.relname, a.attname,
                       format_type(a.atttypid, a.atttypmod), a.attnotnull,
                       pg_get_expr(d.adbin, d.adrelid), a.attnum
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                WHERE a.attnum > 0 AND NOT a.attisdropped AND c.relkind IN ('r', 'p')
                  AND n.nspname <> ALL(%s)
                ORDER BY 1, 2, a.attnum
                """,
                (list(_EXCLUDED_SCHEMAS),),
            ):
                if (t := tables.get((schema, table))) is not None:
                    t.columns.append(Column(col, dtype, not notnull, default, int(ordinal)))

            for schema, table, idx_name, is_pk, is_unique, cols in conn.execute(
                """
                SELECT n.nspname, c.relname, ic.relname, i.indisprimary, i.indisunique,
                       ARRAY(
                         SELECT pg_get_indexdef(i.indexrelid, k + 1, true)
                         FROM generate_subscripts(i.indkey, 1) AS k ORDER BY k
                       )
                FROM pg_index i
                JOIN pg_class c ON c.oid = i.indrelid
                JOIN pg_class ic ON ic.oid = i.indexrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname <> ALL(%s)
                ORDER BY 1, 2, 3
                """,
                (list(_EXCLUDED_SCHEMAS),),
            ):
                if (t := tables.get((schema, table))) is None:
                    continue
                if is_pk:
                    t.primary_key = list(cols)
                else:
                    t.indexes.append(Index(idx_name, list(cols), bool(is_unique)))

            for (
                schema,
                table,
                fk_name,
                cols,
                ref_schema,
                ref_table,
                ref_cols,
                upd,
                dele,
            ) in conn.execute(
                """
                    SELECT n.nspname, c.relname, con.conname,
                           ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(n, o)
                                 JOIN pg_attribute a ON a.attrelid = con.conrelid
                                  AND a.attnum = k.n ORDER BY k.o),
                           rn.nspname, rc.relname,
                           ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY k(n, o)
                                 JOIN pg_attribute a ON a.attrelid = con.confrelid
                                  AND a.attnum = k.n ORDER BY k.o),
                           con.confupdtype, con.confdeltype
                    FROM pg_constraint con
                    JOIN pg_class c ON c.oid = con.conrelid
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    JOIN pg_class rc ON rc.oid = con.confrelid
                    JOIN pg_namespace rn ON rn.oid = rc.relnamespace
                    WHERE con.contype = 'f' AND n.nspname <> ALL(%s)
                    ORDER BY 1, 2, 3
                    """,
                (list(_EXCLUDED_SCHEMAS),),
            ):
                if (t := tables.get((schema, table))) is not None:
                    t.foreign_keys.append(
                        ForeignKey(
                            fk_name,
                            list(cols),
                            f"{ref_schema}.{ref_table}",
                            list(ref_cols),
                            on_delete=_FK_ACTIONS.get(dele),
                            on_update=_FK_ACTIONS.get(upd),
                        )
                    )
            snap.tables = list(tables.values())

            routines: list[Routine] = []
            for schema, name, definition in conn.execute(
                """
                SELECT n.nspname, c.relname, pg_get_viewdef(c.oid, true)
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind IN ('v', 'm') AND n.nspname <> ALL(%s)
                ORDER BY 1, 2
                """,
                (list(_EXCLUDED_SCHEMAS),),
            ):
                routines.append(Routine(schema, name, "view", definition))
            for schema, name, kind, definition in conn.execute(
                """
                SELECT n.nspname,
                       p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')',
                       CASE p.prokind WHEN 'p' THEN 'procedure' ELSE 'function' END,
                       pg_get_functiondef(p.oid)
                FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                LEFT JOIN pg_depend d ON d.objid = p.oid AND d.deptype = 'e'
                WHERE n.nspname <> ALL(%s) AND p.prokind IN ('f', 'p') AND d.objid IS NULL
                ORDER BY 1, 2
                """,
                (list(_EXCLUDED_SCHEMAS),),
            ):
                routines.append(Routine(schema, name, kind, definition))
            for schema, table, name, definition in conn.execute(
                """
                SELECT n.nspname, c.relname, t.tgname, pg_get_triggerdef(t.oid, true)
                FROM pg_trigger t
                JOIN pg_class c ON c.oid = t.tgrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE NOT t.tgisinternal AND n.nspname <> ALL(%s)
                ORDER BY 1, 2, 3
                """,
                (list(_EXCLUDED_SCHEMAS),),
            ):
                routines.append(Routine(schema, f"{table}.{name}", "trigger", definition))
            snap.routines = routines
        return snap

    # -- data access ----------------------------------------------------

    def count_rows(self, database: str, schema: str, table: str) -> int:
        with self._connect(database) as conn:
            row = conn.execute(f"SELECT count(*) FROM {_quote(schema)}.{_quote(table)}").fetchone()
        return int(row[0]) if row else 0

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
        order = ", ".join(_quote(c) for c in order_by)
        sql = f"SELECT {cols} FROM {_quote(schema)}.{_quote(table)}"
        if order:
            sql += f" ORDER BY {order}"
        with self._connect(database) as conn:
            conn.autocommit = False
            with conn.cursor(name="dbnero_iter") as cur:
                cur.itersize = batch_size
                cur.execute(sql)
                yield from cur
            conn.rollback()


_FK_ACTIONS = {
    "a": "NO ACTION",
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}


def _major(version: str) -> int | None:
    m = re.match(r"(\d+)", version)
    return int(m.group(1)) if m else None


def _version_warnings(tool_version_str: str, server_version: str, tool: str) -> list[str]:
    tool_major, server_major = _major(tool_version_str), _major(server_version)
    if tool_major is None or server_major is None:
        return [f"Could not compare {tool} and server versions"]
    if tool_major < server_major:
        return [
            f"ERROR: {tool} {tool_version_str} is older than server {server_version}; "
            f"install PostgreSQL client tools {server_major} or newer"
        ]
    return []


def _pg_error(exc: Exception) -> str:
    text = str(exc).strip().splitlines()
    return text[0] if text else type(exc).__name__


def _dbname_conninfo(name: str) -> str:
    """Build a conninfo string so names containing '=' are not misparsed."""
    escaped = name.replace("\\", "\\\\").replace("'", "\\'")
    return f"dbname='{escaped}'"


register_adapter(Engine.POSTGRESQL, PostgreSQLAdapter)
