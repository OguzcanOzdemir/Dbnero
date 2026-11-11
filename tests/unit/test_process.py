import sys
import threading
import time

import pytest

from dbnero.adapters.base import AdapterError, JobContext, OperationCancelled
from dbnero.adapters.process import ProcessFailed, run_tool, secret_file

PY = sys.executable


def test_stdout_to_file(tmp_path):
    out = tmp_path / "out.bin"
    with out.open("wb") as fh:
        run_tool([PY, "-c", "import sys; sys.stdout.write('hello')"], stdout=fh)
    assert out.read_text() == "hello"


def test_env_is_passed_not_argv(tmp_path):
    out = tmp_path / "o"
    with out.open("wb") as fh:
        run_tool(
            [PY, "-c", "import os,sys; sys.stdout.write(os.environ['PGPASSWORD'])"],
            env={"PGPASSWORD": "s3cret"},
            stdout=fh,
        )
    assert out.read_text() == "s3cret"


def test_failure_raises_with_stderr():
    with pytest.raises(ProcessFailed) as exc:
        run_tool([PY, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"])
    assert exc.value.returncode == 3
    assert "boom" in str(exc.value)


def test_no_check_returns_code():
    res = run_tool([PY, "-c", "import sys; sys.exit(2)"], check=False)
    assert res.returncode == 2


def test_missing_program():
    with pytest.raises(AdapterError):
        run_tool(["definitely-not-a-real-program-xyz"])


def test_cancel_terminates():
    ctx = JobContext()
    threading.Timer(0.3, ctx.cancel).start()
    start = time.monotonic()
    with pytest.raises(OperationCancelled):
        run_tool([PY, "-c", "import time; time.sleep(30)"], ctx=ctx)
    assert time.monotonic() - start < 10


def test_timeout():
    with pytest.raises(AdapterError, match="timed out"):
        run_tool([PY, "-c", "import time; time.sleep(30)"], timeout=0.5)


def test_secret_file_private_and_removed():
    with secret_file("[client]\npassword=x\n", suffix=".cnf") as path:
        assert path.read_text().startswith("[client]")
        if sys.platform != "win32":
            assert (path.stat().st_mode & 0o777) == 0o600
    assert not path.exists()
