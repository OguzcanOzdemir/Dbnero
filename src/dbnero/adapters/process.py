"""Safe wrapper around external CLI tools (pg_dump, mysqldump, ...).

Rules enforced here:
* never ``shell=True``; arguments are always a list
* secrets go through ``env`` or temporary option files, never argv
* cancellation via ``JobContext`` terminates the child process
* stderr is captured (bounded) for error reporting
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from dbnero.adapters.base import AdapterError, JobContext, OperationCancelled

_POLL_SECONDS = 0.2
_STDERR_TAIL_LINES = 200
_CHUNK = 1024 * 1024


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
    stdin_source: Iterable[bytes] | None = None,
    stdout_sink: Callable[[bytes], None] | None = None,
    on_stderr_line: Callable[[str], None] | None = None,
    ctx: JobContext | None = None,
    timeout: float | None = None,
    check: bool = True,
) -> ProcessResult:
    """Run an external program.

    stdin/stdout may be real file objects (passed straight to the child) or,
    for streaming transforms such as gzip, an iterable of input chunks
    (``stdin_source``) and a callback receiving output chunks (``stdout_sink``).
    """
    argv = [os.fspath(a) for a in args]
    if not argv:
        raise ValueError("args must not be empty")
    if stdin is not None and stdin_source is not None:
        raise ValueError("Use either stdin or stdin_source")
    if stdout is not None and stdout_sink is not None:
        raise ValueError("Use either stdout or stdout_sink")

    full_env = dict(os.environ)
    if env:
        full_env.update(env)

    stdin_arg: Any = stdin if stdin is not None else subprocess.DEVNULL
    if stdin_source is not None:
        stdin_arg = subprocess.PIPE
    stdout_arg: Any = stdout if stdout is not None else subprocess.DEVNULL
    if stdout_sink is not None:
        stdout_arg = subprocess.PIPE

    try:
        proc = subprocess.Popen(  # noqa: S603 - argv list, no shell
            argv,
            stdin=stdin_arg,
            stdout=stdout_arg,
            stderr=subprocess.PIPE,
            env=full_env,
            shell=False,
        )
    except OSError as exc:
        raise AdapterError(f"Could not start {argv[0]}: {exc.strerror}") from exc

    stderr_lines: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
    errors: list[BaseException] = []

    def _drain_stderr() -> None:
        if proc.stderr is None:
            return
        for raw in proc.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            stderr_lines.append(line)
            if on_stderr_line is not None:
                try:
                    on_stderr_line(line)
                except Exception as exc:
                    errors.append(exc)

    def _pump_stdout() -> None:
        assert proc.stdout is not None and stdout_sink is not None  # noqa: S101
        try:
            while chunk := proc.stdout.read(_CHUNK):
                stdout_sink(chunk)
        except BaseException as exc:
            errors.append(exc)
            proc.kill()

    def _feed_stdin() -> None:
        assert proc.stdin is not None and stdin_source is not None  # noqa: S101
        try:
            for chunk in stdin_source:
                proc.stdin.write(chunk)
        except BrokenPipeError:
            pass  # child exited early; its return code tells the story
        except BaseException as exc:
            errors.append(exc)
            proc.kill()
        finally:
            with contextlib.suppress(OSError):
                proc.stdin.close()

    threads = [threading.Thread(target=_drain_stderr, daemon=True)]
    if stdout_sink is not None:
        threads.append(threading.Thread(target=_pump_stdout, daemon=True))
    if stdin_source is not None:
        threads.append(threading.Thread(target=_feed_stdin, daemon=True))
    for t in threads:
        t.start()

    started = time.monotonic()
    try:
        while True:
            try:
                proc.wait(timeout=_POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                if ctx is not None and ctx.cancelled:
                    _terminate(proc)
                    raise OperationCancelled from None
                if timeout is not None and time.monotonic() - started >= timeout:
                    _terminate(proc)
                    raise AdapterError(f"{argv[0]} timed out after {timeout:.0f}s") from None
    finally:
        for t in threads:
            t.join(timeout=30)

    if errors:
        first = errors[0]
        if isinstance(first, AdapterError):
            raise first
        raise AdapterError(f"Streaming I/O for {Path(argv[0]).name} failed: {first}") from first

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
