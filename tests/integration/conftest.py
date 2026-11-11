"""Fixtures for tests against real database servers.

Servers default to the ports in ``docker-compose.test.yml``; override with
environment variables (see ``_cfg``). Tests for an engine are skipped when
its server is unreachable or its driver/tools are missing.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from dbnero.adapters.base import AdapterError, DatabaseAdapter, create_adapter
from dbnero.core.models import ConnectionProfile, Engine, SslMode

SEED_DIR = Path(__file__).parent / "seed"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/integration" in str(item.fspath).replace(os.sep, "/"):
            item.add_marker(pytest.mark.integration)


def _env(name: str, default: str) -> str:
    return os.environ.get(f"DBNERO_IT_{name}", default)


@dataclass
class EngineTarget:
    profile: ConnectionProfile
    password: str

    def adapter(self) -> DatabaseAdapter:
        return create_adapter(self.profile, self.password)


def _cfg(engine: Engine) -> EngineTarget:
    if engine is Engine.POSTGRESQL:
        profile = ConnectionProfile(
            name="it-pg",
            engine=engine,
            host=_env("PG_HOST", "localhost"),
            port=int(_env("PG_PORT", "55432")),
            username=_env("PG_USER", "postgres"),
            ssl_mode=SslMode.DISABLE,
        )
        return EngineTarget(profile, _env("PG_PASSWORD", "Pg_Test_123"))
    if engine is Engine.MYSQL:
        profile = ConnectionProfile(
            name="it-mysql",
            engine=engine,
            host=_env("MYSQL_HOST", "127.0.0.1"),
            port=int(_env("MYSQL_PORT", "53306")),
            username=_env("MYSQL_USER", "root"),
            ssl_mode=SslMode.PREFER,
        )
        return EngineTarget(profile, _env("MYSQL_PASSWORD", "My_Test_123"))
    options = {
        "trust_server_certificate": "true",
        "server_backup_dir": _env("MSSQL_SERVER_DIR", "/var/opt/mssql/dbnero-share"),
    }
    local_dir = _env("MSSQL_LOCAL_DIR", str(Path(__file__).parents[2] / ".mssql-share"))
    if local_dir:
        options["local_backup_dir"] = local_dir
    profile = ConnectionProfile(
        name="it-mssql",
        engine=engine,
        host=_env("MSSQL_HOST", "localhost"),
        port=int(_env("MSSQL_PORT", "51433")),
        username=_env("MSSQL_USER", "sa"),
        ssl_mode=SslMode.REQUIRE,
        options=options,
    )
    return EngineTarget(profile, _env("MSSQL_PASSWORD", "Mssql_Test_123!"))


_available: dict[Engine, str | None] = {}


def _check(engine: Engine) -> EngineTarget:
    target = _cfg(engine)
    if engine not in _available:
        try:
            target.adapter().test_connection()
            _available[engine] = None
        except (AdapterError, ImportError) as exc:
            _available[engine] = f"{engine.display_name} not available: {exc}"
    if _available[engine]:
        pytest.skip(_available[engine])
    return target


@pytest.fixture(params=list(Engine), ids=[e.value for e in Engine])
def target(request: pytest.FixtureRequest) -> EngineTarget:
    return _check(request.param)


def seed_database(target: EngineTarget, name: str) -> None:
    adapter = target.adapter()
    adapter.create_database(name)
    sql = (SEED_DIR / f"{target.profile.engine.value}.sql").read_text("utf-8")
    engine = target.profile.engine
    if engine is Engine.POSTGRESQL:
        import psycopg

        p = target.profile
        with psycopg.connect(
            host=p.host,
            port=p.port,
            user=p.username,
            password=target.password,
            dbname=name,
            autocommit=True,
        ) as conn:
            conn.execute(sql)  # type: ignore[call-overload]
    elif engine is Engine.MYSQL:
        import mysql.connector

        p = target.profile
        conn = mysql.connector.connect(
            host=p.host, port=p.port, user=p.username, password=target.password, database=name
        )
        cur = conn.cursor()
        for stmt in [s.strip() for s in sql.split(";\n") if s.strip()]:
            cur.execute(stmt)
        conn.commit()
        conn.close()
    else:
        import pyodbc

        conn = pyodbc.connect(adapter.connection_string(name), autocommit=True)  # type: ignore[attr-defined]
        for batch in [b.strip() for b in sql.split("\nGO") if b.strip()]:
            conn.cursor().execute(batch)
        conn.close()


@pytest.fixture
def seeded(target: EngineTarget) -> Iterator[tuple[EngineTarget, str]]:
    name = f"dbn_it_{uuid.uuid4().hex[:8]}"
    seed_database(target, name)
    yield target, name
    target.adapter().drop_database(name)


@pytest.fixture
def scratch_db_name(target: EngineTarget) -> Iterator[str]:
    name = f"dbn_it_r_{uuid.uuid4().hex[:8]}"
    yield name
    with contextlib.suppress(AdapterError):
        target.adapter().drop_database(name)
