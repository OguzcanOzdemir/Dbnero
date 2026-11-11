"""Safe wrapper around external CLI tools (pg_dump, mysqldump, ...).

Rules enforced here:
* never ``shell=True``; arguments are always a list
* secrets go through ``env`` or temporary option files, never argv
* cancellation via ``JobContext`` terminates the child process
* stderr is captured (bounded) for error reporting
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from dbnero.adapters.base import AdapterError, JobContext, OperationCancelled

_POLL_SECONDS = 0.2
_STDERR_TAIL_LINES = 200


@dataclass(slots=True)
class ProcessResult:
    returncode: int
    stderr: str


class ProcessFailed(AdapterError):
    def __init__(self, program: str, returncode: int, stderr: str) -> None:
        tail = stderr.strip().splitlines()[-5:]
        detail = "\n".join(tail)
        super().__init__(
            f"{program} exited with code {returncode}" + (f":\n{detail}" if detail else "")
        )
        self.program = program
        self.returncode = returncode
        self.stderr = stderr


def run_tool(
    args: Sequence[str | os.PathLike[str]],
    *,
    env: Mapping[str, str] | None = None,
    stdin: IO[bytes] | None = None,
    stdout: IO[bytes] | None = None,
    ctx: JobContext | None = None,
    timeout: float | None = None,
    check: bool = True,
) -> ProcessResult:
    """Run an external program, streaming stdin/stdout to the given file objects."""
    argv = [os.fspath(a) for a in args]
    if not argv:
        raise ValueError("args must not be empty")

    full_env = dict(os.environ)
    if env:
        full_env.update(env)

    try:
        proc = subprocess.Popen(  # noqa: S603 - argv list, no shell
            argv,
            stdin=stdin if stdin is not None else subprocess.DEVNULL,
            stdout=stdout if stdout is not None else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=full_env,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise AdapterError(f"Could not start {argv[0]}: {exc.strerror}") from exc

    stderr_lines: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)

    def _drain() -> None:
        if proc.stderr is None:
            return
        for raw in proc.stderr:
            stderr_lines.append(raw.decode("utf-8", errors="replace").rstrip("\n"))

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()

    waited = 0.0
    try:
        while True:
            try:
                proc.wait(timeout=_POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                waited += _POLL_SECONDS
                if ctx is not None and ctx.cancelled:
                    _terminate(proc)
                    raise OperationCancelled from None
                if timeout is not None and waited >= timeout:
                    _terminate(proc)
                    raise AdapterError(f"{argv[0]} timed out after {timeout:.0f}s") from None
    finally:
        reader.join(timeout=5)

    stderr = "\n".join(stderr_lines)
    if check and proc.returncode != 0:
        raise ProcessFailed(Path(argv[0]).name, proc.returncode, stderr)
    return ProcessResult(proc.returncode, stderr)


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@contextmanager
def secret_file(content: str, suffix: str = "") -> Iterator[Path]:
    """Write ``content`` to a private (0600) temp file and delete it afterwards.

    Used for ``.pgpass`` and MySQL ``--defaults-extra-file`` so that passwords
    never appear in argv.
    """
    fd, name = tempfile.mkstemp(prefix="dbnero-", suffix=suffix)
    path = Path(name)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        yield path
    finally:
        path.unlink(missing_ok=True)
