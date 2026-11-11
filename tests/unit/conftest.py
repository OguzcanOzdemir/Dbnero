from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from dbnero.core.app import AppContext
from dbnero.core.models import ConnectionProfile, Engine
from dbnero.core.settings import Settings
from dbnero.security.credentials import InMemoryCredentialStore
from dbnero.storage.catalog import Catalog
from tests.unit.fakes import FakeAdapter, FakeServer, sample_db


@dataclass
class World:
    app: AppContext
    servers: dict[str, FakeServer]
    pg: ConnectionProfile
    pg2: ConnectionProfile
    my: ConnectionProfile


@pytest.fixture
def world(tmp_path, monkeypatch) -> Iterator[World]:
    servers: dict[str, FakeServer] = {}

    def fake_create(profile, password, tool_paths=None):
        server = servers.setdefault(profile.host, FakeServer())
        return FakeAdapter(profile, password, server)

    monkeypatch.setattr("dbnero.core.connection_service.create_adapter", fake_create)
    monkeypatch.setattr("dbnero.core.backup_service.create_adapter", fake_create)
    monkeypatch.setattr("dbnero.core.restore_service.find_tool", lambda *a, **k: "/bin/true")

    settings = Settings(archive_root=str(tmp_path / "archive"), min_free_space_mb=0)
    app = AppContext.create(
        settings,
        catalog=Catalog(tmp_path / "catalog.sqlite3"),
        credentials=InMemoryCredentialStore(),
        settings_path=tmp_path / "settings.json",
    )
    pg = ConnectionProfile(name="pg", engine=Engine.POSTGRESQL, host="pg1", port=5432, username="u")
    pg2 = ConnectionProfile(
        name="pg2", engine=Engine.POSTGRESQL, host="pg2", port=5432, username="u"
    )
    my = ConnectionProfile(name="my", engine=Engine.MYSQL, host="my1", port=3306, username="u")
    for p in (pg, pg2, my):
        app.connections.save(p, "secret-pw")
    servers["pg1"] = FakeServer(databases={"crm": sample_db(), "erp": sample_db()})
    servers["pg2"] = FakeServer()
    servers["my1"] = FakeServer(databases={"shop": sample_db()})
    yield World(app, servers, pg, pg2, my)
    app.close()
