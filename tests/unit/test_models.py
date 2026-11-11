from dbnero.core.models import BackupRecord, ConnectionProfile, Engine


def test_engine_defaults():
    assert Engine.POSTGRESQL.default_port == 5432
    assert Engine.MYSQL.default_port == 3306
    assert Engine.MSSQL.default_port == 1433
    assert Engine("mssql").display_name == "Microsoft SQL Server"


def test_created_at_is_aware_utc():
    p = ConnectionProfile(name="x", engine=Engine.MYSQL, host="h", port=1)
    assert p.created_at_utc.tzinfo is not None
    assert p.server_label == "h:1"


def test_short_id():
    r = BackupRecord(
        connection_id="c",
        engine=Engine.MYSQL,
        database_name="d",
        server_label="s",
        backup_format="sql",
        file_path=__import__("pathlib").Path("x.sql"),
        backup_id="abcd1234-0000-0000-0000-000000000000",
    )
    assert r.short_id == "abcd1234"
