import pytest

from dbnero.core.connection_service import ValidationError, install_hint
from dbnero.core.models import AuthType, ConnectionProfile, Engine


def test_password_goes_to_credential_store_only(world):
    svc = world.app.connections
    assert world.app.credentials.get_password(world.pg.id) == "secret-pw"
    adapter = svc.adapter_for(world.pg.id)
    assert adapter.password == "secret-pw"
    with world.app.catalog._engine.connect() as conn:
        dump = "\n".join(str(r) for r in conn.exec_driver_sql("SELECT * FROM connections"))
    assert "secret-pw" not in dump


def test_save_keeps_or_clears_password(world):
    svc = world.app.connections
    world.pg.host = "pg1"
    svc.save(world.pg)  # None keeps
    assert svc.has_password(world.pg.id)
    svc.save(world.pg, "")
    assert not svc.has_password(world.pg.id)


def test_validation(world):
    svc = world.app.connections
    bad = ConnectionProfile(name="", engine=Engine.MYSQL, host="h", port=1, username="u")
    with pytest.raises(ValidationError):
        svc.save(bad)
    dup = ConnectionProfile(name="PG", engine=Engine.MYSQL, host="h", port=1, username="u")
    with pytest.raises(ValidationError, match="already exists"):
        svc.save(dup)
    win = ConnectionProfile(
        name="w", engine=Engine.MYSQL, host="h", port=1, auth_type=AuthType.WINDOWS
    )
    with pytest.raises(ValidationError, match="Windows"):
        svc.save(win)
    port = ConnectionProfile(name="p", engine=Engine.MYSQL, host="h", port=70000, username="u")
    with pytest.raises(ValidationError, match="Port"):
        svc.save(port)


def test_delete_removes_password(world):
    world.app.connections.delete(world.my.id)
    assert world.app.credentials.get_password(world.my.id) is None
    assert all(p.id != world.my.id for p in world.app.connections.list_profiles())


def test_test_and_list(world):
    svc = world.app.connections
    assert svc.test(world.pg) == "16.1"
    assert svc.list_databases(world.pg.id) == ["crm", "erp"]
    assert "sysdb" in svc.list_databases(world.pg.id, include_system=True)


def test_install_hints():
    assert "apt" in install_hint(Engine.POSTGRESQL, "linux")
    assert "brew" in install_hint(Engine.MYSQL, "darwin")
    assert "ODBC" in install_hint(Engine.MSSQL, "win32")
