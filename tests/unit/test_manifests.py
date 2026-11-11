import json
from datetime import UTC, datetime

import pytest

from dbnero.core import models as m
from dbnero.core.catalog_service import CatalogService
from dbnero.storage.archive import backup_paths
from dbnero.storage.manifests import InvalidManifest, read_manifest, write_manifest


def _record(root):
    ts = datetime(2026, 11, 11, 20, 40, tzinfo=UTC)
    rec = m.BackupRecord(
        connection_id="c1",
        engine=m.Engine.MYSQL,
        database_name="shop",
        server_label="mysql:3306",
        backup_format="sql.gz",
        file_path=root,
        created_at_utc=ts,
        tags=["a"],
        status=m.BackupStatus.SUCCEEDED,
        checksum_sha256="ff",
        file_size=3,
        tool_versions={"mysqldump": "8.0.39"},
    )
    paths = backup_paths(
        root, rec.engine, rec.server_label, rec.database_name, ts, rec.short_id, "sql.gz"
    )
    paths.backup.parent.mkdir(parents=True)
    paths.backup.write_bytes(b"abc")
    rec.file_path = paths.backup
    rec.schema_snapshot_path = paths.schema
    return rec, paths


def test_roundtrip(tmp_path):
    rec, paths = _record(tmp_path)
    write_manifest(rec, paths.manifest)
    assert read_manifest(paths.manifest) == rec


def test_manifest_is_portable_and_secret_free(tmp_path):
    rec, paths = _record(tmp_path)
    write_manifest(rec, paths.manifest)
    data = json.loads(paths.manifest.read_text("utf-8"))
    assert data["file_name"] == rec.file_path.name
    assert str(tmp_path) not in paths.manifest.read_text("utf-8")
    assert not any("pass" in k for k in data)

    moved = tmp_path.parent / (tmp_path.name + "_moved")
    tmp_path.rename(moved)
    new_manifest = moved / paths.manifest.relative_to(tmp_path)
    assert read_manifest(new_manifest).file_path.parent == new_manifest.parent


def test_invalid(tmp_path):
    bad = tmp_path / "x.manifest.json"
    bad.write_text("{}")
    with pytest.raises(InvalidManifest):
        read_manifest(bad)
    bad.write_text('{"format_version": 99}')
    with pytest.raises(InvalidManifest):
        read_manifest(bad)


def test_rebuild_catalog_from_archive(tmp_path, catalog):
    rec, paths = _record(tmp_path / "arch")
    write_manifest(rec, paths.manifest)
    (tmp_path / "arch" / "junk.manifest.json").write_text("nope")

    svc = CatalogService(catalog)
    report = svc.import_from_archive(tmp_path / "arch")
    assert report.imported == 1 and len(report.invalid) == 1
    assert catalog.get_backup(rec.backup_id).database_name == "shop"

    again = svc.import_from_archive(tmp_path / "arch")
    assert again.imported == 0 and again.skipped_existing == 1


def test_overview_stats(tmp_path, catalog):
    from dbnero.core.catalog_service import CatalogService

    rec, paths = _record(tmp_path / "arch")
    catalog.add_backup(rec)
    (paths.backup.parent / "x.dump.partial").write_bytes(b"x")
    svc = CatalogService(catalog)
    stats = svc.overview(tmp_path / "arch")
    assert stats.total_backups == 1 and stats.succeeded == 1 and stats.archive_bytes == 3
    assert len(stats.stale_partials) == 1 and 0 <= stats.disk_used_percent <= 100
    assert svc.cleanup_stale_partials(tmp_path / "arch") == 1
    assert svc.overview(tmp_path / "missing" / "dir").total_backups == 1


def test_import_batch_backups_recreates_batch(tmp_path, catalog):
    """Batch backups reference the batches table; importing must recreate it."""
    from dbnero.core.catalog_service import CatalogService

    rec, paths = _record(tmp_path / "arch")
    rec.batch_id = "batch-1"
    rec.backup_mode = m.BackupMode.BATCH
    write_manifest(rec, paths.manifest)
    report = CatalogService(catalog).import_from_archive(tmp_path / "arch")
    assert report.imported == 1 and report.invalid == []
    assert catalog.get_backup(rec.backup_id).batch_id == "batch-1"
