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


def test_schedules_page_create_run_delete(qtbot, window, world, monkeypatch):
    from PySide6.QtCore import Qt, QTime

    from dbnero.core.models import ScheduleFrequency, ScheduleRunStatus

    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    page = window.schedules
    window.show_page("schedules")
    page.new_schedule()
    page.name.setText("Nightly")
    page.connection.setCurrentIndex(page.connection.findData(world.pg.id))
    qtbot.waitUntil(lambda: page.db_list.count() == 2, timeout=5000)
    page.db_list.item(0).setCheckState(Qt.CheckState.Checked)  # crm
    page.repeat.setCurrentIndex(page.repeat.findData("days"))
    assert not page.days_row.isHidden() and not page.time.isHidden()
    for i, box in enumerate(page.day_boxes):
        box.setChecked(i in (0, 2, 4))
    page.time.setTime(QTime(1, 30))
    page.keep.setValue(3)
    page.save_schedule()

    [sched] = world.app.schedules.list_all()
    assert sched.databases == ["crm"] and sched.weekdays == [0, 2, 4]
    assert sched.time_of_day == "01:30" and sched.frequency is ScheduleFrequency.DAILY
    assert sched.keep_last == 3
    assert "Mon, Wed, Fri at 01:30" in page.list.item(0).text()
    # Background runs are registered with the OS scheduler (a fake here).
    qtbot.waitUntil(
        lambda: any(c[:2] == ["launchctl", "load"] for c in world.os_calls), timeout=5000
    )

    page.run_now()
    _wait_jobs(qtbot, world.app)
    qtbot.waitUntil(lambda: "backed up" in page.status.text(), timeout=5000)
    got = world.app.schedules.get(sched.id)
    assert got.last_status is ScheduleRunStatus.SUCCEEDED
    assert window.archive.table.rowCount() == 1
    assert "Last run OK" in page.last_run.text()

    # Every few hours hides the time and days.
    page.repeat.setCurrentIndex(page.repeat.findData("hours"))
    assert page.time.isHidden() and page.days_row.isHidden() and not page.hours.isHidden()

    page.delete_schedule()
    assert world.app.schedules.list_all() == []
    qtbot.waitUntil(
        lambda: any(c[:2] == ["launchctl", "unload"] for c in world.os_calls), timeout=5000
    )
    assert world.app.catalog.list_backups()  # its backups stay


def test_open_app_runs_due_schedules(qtbot, window, world):
    from datetime import UTC, datetime, timedelta

    from dbnero.core.models import Schedule, ScheduleRunStatus

    sched = Schedule(
        name="hourly",
        connection_id=world.pg.id,
        databases=["erp"],
        created_at_utc=datetime.now(UTC) - timedelta(days=2),
    )
    world.app.schedules.save(sched)
    job = window.run_due_schedules()
    assert job is not None
    assert window.run_due_schedules() is None  # one run at a time
    _wait_jobs(qtbot, world.app)
    assert world.app.schedules.get(sched.id).last_status is ScheduleRunStatus.SUCCEEDED
    assert window.run_due_schedules() is None  # nothing due any more


def test_missed_backup_notification_on_home(qtbot, window, world):
    from datetime import UTC, datetime, timedelta

    from dbnero.core.models import Schedule

    # Daily at 02:00, created three days ago, never run: the app was closed.
    world.app.schedules.save(
        Schedule(
            name="Nightly",
            connection_id=world.pg.id,
            databases=["crm"],
            created_at_utc=datetime.now(UTC) - timedelta(days=3),
        )
    )
    assert window.run_due_schedules() is not None
    _wait_jobs(qtbot, world.app)
    qtbot.wait(100)
    # The window checks by itself when the job ends; each note is announced once.
    assert window.check_notifications() == []
    window._seen_notes.clear()
    assert [n.title for n in window.check_notifications()] == ["Missed backup: Nightly"]
    page = window.overview
    assert not page.notes_card.isHidden()
    assert "Missed backup: Nightly" in page.notes_box.itemAt(0).widget().label.text()
    assert window.nav.item(window._row_for_key["overview"]).text() == "Home  (1)"
    page.dismiss_notifications()
    assert page.notes_card.isHidden()
    assert window.nav.item(window._row_for_key["overview"]).text() == "Home"


def test_legal_acceptance(qtbot, tmp_path, monkeypatch):
    from dbnero import main as main_mod
    from dbnero.core.settings import Settings
    from dbnero.legal import LEGAL_VERSION, privacy_text, terms_text
    from dbnero.ui.legal_dialog import LegalDialog

    monkeypatch.setattr(main_mod, "settings_path", lambda: tmp_path / "settings.json")
    s = Settings()

    class Decline:
        def __init__(self, require_acceptance):
            assert require_acceptance

        def exec(self):
            return 0

    class Accept(Decline):
        def exec(self):
            return 1

    assert not main_mod.ensure_legal_accepted(s, Decline)
    assert s.legal_accepted_version == ""
    assert main_mod.ensure_legal_accepted(s, Accept)
    assert s.legal_accepted_version == LEGAL_VERSION and s.legal_accepted_at
    assert (tmp_path / "settings.json").exists()
    # Asked only once per version.
    assert main_mod.ensure_legal_accepted(s, Decline)

    dlg = LegalDialog(require_acceptance=True)
    qtbot.addWidget(dlg)
    assert not dlg.btn_accept.isEnabled()
    dlg.accept_box.setChecked(True)
    assert dlg.btn_accept.isEnabled()
    assert "not liable for any loss of data" in terms_text()
    assert "does not collect" in privacy_text()


def test_site_legal_pages_match_app_text():
    import html
    from pathlib import Path

    from dbnero.legal import privacy_text, terms_text

    site = Path(__file__).resolve().parents[2] / "site"
    for page, text in (("terms.html", terms_text()), ("privacy.html", privacy_text())):
        html_text = (site / page).read_text("utf-8")
        for line in text.splitlines()[2:]:
            line = line.strip().lstrip("- ")
            if line:
                assert html.escape(line, quote=False) in html_text, line
