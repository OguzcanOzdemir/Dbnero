import threading
import time

from dbnero.core.jobs import JobEventType, JobManager
from dbnero.core.models import JobKind, JobStatus


def test_success_failure_and_logs(catalog):
    mgr = JobManager(catalog, max_workers=2)
    events = []
    mgr.add_listener(lambda e: events.append((e.type, e.job.title)))

    def ok(h):
        h.log("working")
        h.ctx.progress(0.5, "half")
        return 42

    def boom(h):
        raise RuntimeError("password=hunter2 failed")

    j1 = mgr.submit(JobKind.BACKUP, "ok", ok)
    j2 = mgr.submit(JobKind.BACKUP, "boom", boom)
    assert mgr.wait(j1) == 42
    mgr.wait(j2)
    assert j1.status is JobStatus.SUCCEEDED and j2.status is JobStatus.FAILED
    assert "hunter2" not in j2.error
    recs = {r.job_id: r for r in catalog.list_jobs()}
    assert recs[j1.job_id].status is JobStatus.SUCCEEDED
    assert recs[j2.job_id].error_message == j2.error
    assert any(m == "working" for _, _, m in catalog.job_logs(j1.job_id))
    assert (JobEventType.PROGRESS, "ok") in events and (JobEventType.FINISHED, "boom") in events
    mgr.shutdown()


def test_cancel_running_and_queued(catalog):
    mgr = JobManager(catalog, max_workers=1)
    started = threading.Event()

    def slow(h):
        started.set()
        for _ in range(500):
            h.ctx.raise_if_cancelled()
            time.sleep(0.01)

    running = mgr.submit(JobKind.BACKUP, "slow", slow)
    queued = mgr.submit(JobKind.BACKUP, "queued", slow)
    started.wait(5)
    mgr.cancel(queued.job_id)
    mgr.cancel(running.job_id)
    mgr.wait(running)
    assert running.status is JobStatus.CANCELLED
    assert queued.status is JobStatus.CANCELLED
    assert not mgr.active_jobs()
    mgr.shutdown()


def test_recover_interrupted(catalog):
    from dbnero.core.models import utc_now
    from dbnero.storage.catalog import JobRecord

    catalog.add_job(JobRecord("old", JobKind.RESTORE, JobStatus.RUNNING, "t", utc_now()))
    mgr = JobManager(catalog)
    assert mgr.recover_interrupted() == 1
    assert catalog.list_jobs()[0].status is JobStatus.FAILED
    mgr.shutdown()
