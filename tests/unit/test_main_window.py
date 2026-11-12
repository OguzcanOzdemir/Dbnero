"""UI tests: drive the real pages against the in-memory fake adapters."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QMessageBox

from dbnero.core.models import BackupStatus, Engine, JobStatus
from dbnero.ui.main_window import PAGES, MainWindow


@pytest.fixture
def window(qtbot, world, monkeypatch):
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda *a, **k: None))
    world.app.settings.first_run_completed = True
    w = MainWindow(world.app)
    qtbot.addWidget(w)
    yield w
    w.bridge.detach()


def _wait_jobs(qtbot, app):
    qtbot.waitUntil(lambda: not app.jobs.active_jobs(), timeout=10000)
    qtbot.wait(50)


def test_navigation(window):
    assert window.nav.count() == len(PAGES) + 1  # one group spacer
    assert window.current_page_key() == "overview"
    for spec in PAGES:
        window.show_page(spec.key)
        assert window.current_page_key() == spec.key


def test_connections_page_save_and_test(qtbot, window, world):
    page = window.connections
    page.new_profile()
    page.name.setText("new-mysql")
    page.engine.setCurrentIndex(page.engine.findData(Engine.MYSQL))
    assert page.port.value() == 3306
    assert page.mssql_box.isHidden()
    page.host.setText("my1")
    page.username.setText("root")
    page.password.setText("pw!")
    page.save_profile()
    saved = next(p for p in world.app.connections.list_profiles() if p.name == "new-mysql")
    assert world.app.credentials.get_password(saved.id) == "pw!"
    assert page.password.text() == ""

    page.test_connection()
    qtbot.waitUntil(lambda: "successful" in page.status.text(), timeout=5000)
    page.discover()
    qtbot.waitUntil(lambda: page.databases.count() == 1, timeout=5000)

    page.engine.setCurrentIndex(page.engine.findData(Engine.MSSQL))
    assert not page.mssql_box.isHidden()


def test_backup_archive_restore_compare_flow(qtbot, window, world):
    app = world.app
    page = window.backup
    page.connection.setCurrentIndex(page.connection.findData(world.pg.id))
    qtbot.waitUntil(lambda: page.db_list.count() == 2, timeout=5000)
    page._check_all(True)
    assert "2 databases" in page.summary.text()
    page.description.setText("ui test")
    page.tags.setText("ui, test")
    page.start()
    _wait_jobs(qtbot, app)
    qtbot.waitUntil(lambda: "2 succeeded" in page.result.label.text(), timeout=5000)
    assert "Batch report" in page.log.toPlainText()

    archive = window.archive
    archive.reload()
    assert archive.table.rowCount() == 2
    archive.tag.setCurrentIndex(archive.tag.findData("ui"))
    assert archive.table.rowCount() == 2
    archive.table.selectRow(0)
    rec = archive.selected()
    assert rec is not None and rec.status is BackupStatus.SUCCEEDED
    assert "SHA-256" in archive.details.toPlainText()

    archive.validate_selected()
    _wait_jobs(qtbot, app)

    # Restore via archive -> restore page wiring
    archive.btn_restore.click()
    assert window.current_page_key() == "restore"
    restore = window.restore
    assert restore.backup.currentData() == rec.backup_id
    restore.target_conn.setCurrentIndex(restore.target_conn.findData(world.pg2.id))
    qtbot.waitUntil(lambda: not restore._loading, timeout=5000)
    # A refresh (e.g. another job finished) keeps the chosen server.
    restore.reload()
    assert restore.target_conn.currentData() == world.pg2.id
    qtbot.waitUntil(lambda: not restore._loading, timeout=5000)
    # The backup's own database is proposed; pg2 does not have it yet.
    assert restore.target_db.currentText() == rec.database_name
    assert "will be created" in restore.name_hint.text()
    restore.target_db.setEditText("restored_db")
    restore.run_preflight()
    qtbot.waitUntil(lambda: restore._report is not None, timeout=5000)
    assert not restore._report.target_exists
    assert restore.btn_start.isEnabled()
    restore.start()
    _wait_jobs(qtbot, app)
    qtbot.waitUntil(lambda: "Restore finished" in restore.status.text(), timeout=5000)
    assert "restored_db" in world.servers["pg2"].databases

    # Replacing an existing database: it is in the list and needs a confirmation tick.
    restore.select_backup(rec.backup_id)
    restore.target_conn.setCurrentIndex(restore.target_conn.findData(world.pg2.id))
    qtbot.waitUntil(lambda: not restore._loading, timeout=5000)
    assert restore.target_db.findText("restored_db") >= 0
    restore.target_db.setCurrentText("restored_db")
    assert "will be replaced" in restore.name_hint.text()
    restore.run_preflight()
    qtbot.waitUntil(lambda: restore._report is not None, timeout=5000)
    assert restore._report.target_exists and not restore.confirm.isHidden()
    assert not restore.btn_start.isEnabled()
    restore.confirm.setChecked(True)
    assert restore.btn_start.isEnabled()
    restore.start()
    _wait_jobs(qtbot, app)
    qtbot.waitUntil(lambda: "Restore finished" in restore.status.text(), timeout=5000)

    # Compare backup with live database
    window.archive.select_backup(rec.backup_id)
    window.archive.btn_compare.click()
    assert window.current_page_key() == "compare"
    cmp = window.compare
    cmp.right.preselect_live(world.pg.id, rec.database_name)
    qtbot.waitUntil(lambda: cmp.right.database.currentText() == rec.database_name, timeout=5000)
    assert not cmp.right.database.isEditable()
    cmp.include_data.setChecked(True)
    cmp.scratch.setCurrentIndex(cmp.scratch.findData(world.pg2.id))
    cmp.run()
    _wait_jobs(qtbot, app)
    qtbot.waitUntil(lambda: cmp._result is not None, timeout=5000)
    assert cmp._result.schema.identical and cmp._result.data.identical
    assert cmp.data_table.rowCount() == 2

    jobs = [j for j in app.catalog.list_jobs()]
    assert {j.status for j in jobs} == {JobStatus.SUCCEEDED}
    window.show_page("jobs")
    assert window.jobs.table.rowCount() == len(jobs)
    window.jobs.table.selectRow(0)
    assert window.jobs.logs.toPlainText()

    window.show_page("overview")
    assert window.overview.card_total.value.text() == "3"  # incl. protective backup


def test_settings_page_saves(window, world):
    page = window.settings_page
    page.keep_last.setValue(3)
    page.tool_edits["pg_dump"].setText("/opt/pg/bin/pg_dump")
    page.save()
    assert world.app.settings.retention_keep_last == 3
    assert world.app.settings.tool_paths == {"pg_dump": "/opt/pg/bin/pg_dump"}
    assert world.app.settings_path.exists()


def test_tool_report_html():
    from dbnero.adapters.tools import ToolInfo
    from dbnero.core.connection_service import EngineToolReport
    from dbnero.ui.settings_page import tool_report_html

    reports = [
        EngineToolReport(Engine.POSTGRESQL, [ToolInfo("pg_dump", None, None)]),
        EngineToolReport(Engine.MSSQL, [], ["ODBC Driver 18 for SQL Server"]),
    ]
    html = tool_report_html(reports, keyring_ok=False)
    assert "pg_dump</b> not found" in html and "How to install" in html
    assert "ODBC Driver 18" in html and "Password storage" in html
