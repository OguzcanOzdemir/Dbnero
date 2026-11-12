"""Register dbnero with the operating system's scheduler so scheduled backups
also run while the app is closed.

One small recurring OS task starts ``dbnero --run-due`` every few minutes; it
runs whatever schedules are due and exits. It runs as the signed-in user, so
the saved passwords (keychain / Windows DPAPI) are available to it.

* Windows: Task Scheduler (``schtasks``)
* macOS:   a LaunchAgent (``~/Library/LaunchAgents``)
* Linux:   a systemd user timer (``~/.config/systemd/user``)
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

INTERVAL_MINUTES = 5
TASK_NAME = "dbnero scheduled backups"
LAUNCH_AGENT_LABEL = "app.dbnero.scheduler"
SYSTEMD_UNIT = "dbnero-scheduler"
RUN_DUE_FLAG = "--run-due"

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class OsSchedulerError(RuntimeError):
    pass


def _default_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    return subprocess.run(  # noqa: S603 - fixed argument list, no shell
        list(args), capture_output=True, text=True, timeout=60, creationflags=flags
    )


def launch_command() -> list[str]:
    """The command that runs due schedules with this installation of dbnero."""
    appimage = os.environ.get("APPIMAGE")
    if appimage:  # inside an AppImage sys.executable is a temporary mount
        return [appimage, RUN_DUE_FLAG]
    if getattr(sys, "frozen", False):
        return [sys.executable, RUN_DUE_FLAG]
    python = Path(sys.executable)
    if sys.platform == "win32":
        pythonw = python.with_name("pythonw.exe")
        if pythonw.exists():  # no console window every few minutes
            python = pythonw
    return [str(python), "-m", "dbnero", RUN_DUE_FLAG]


@dataclass(slots=True)
class OsScheduler:
    platform: str = sys.platform
    home: Path = field(default_factory=Path.home)
    run: Runner = _default_runner

    @property
    def supported(self) -> bool:
        if self.platform == "win32" or self.platform == "darwin":
            return True
        return self.platform.startswith("linux") and shutil.which("systemctl") is not None

    @property
    def description(self) -> str:
        return {
            "win32": "Windows Task Scheduler",
            "darwin": "macOS launchd",
        }.get(self.platform, "a systemd user timer")

    def warning(self) -> str | None:
        """A reason the registered command would not work later, if any."""
        cmd = launch_command()
        if self.platform == "darwin" and "/AppTranslocation/" in cmd[0]:
            return "Move dbnero to the Applications folder first, then turn this on again."
        return None

    # -- public -----------------------------------------------------------------

    def is_installed(self) -> bool:
        if self.platform == "win32":
            return self._ok(["schtasks", "/Query", "/TN", TASK_NAME])
        if self.platform == "darwin":
            return self._plist_path().exists()
        return self._timer_path().exists()

    def install(self) -> None:
        cmd = launch_command()
        if self.platform == "win32":
            self._check(
                [
                    "schtasks", "/Create", "/F", "/TN", TASK_NAME,
                    "/SC", "MINUTE", "/MO", str(INTERVAL_MINUTES),
                    "/TR", subprocess.list2cmdline(cmd),
                ]
            )  # fmt: skip
        elif self.platform == "darwin":
            path = self._plist_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as fh:
                plistlib.dump(
                    {
                        "Label": LAUNCH_AGENT_LABEL,
                        "ProgramArguments": cmd,
                        "StartInterval": INTERVAL_MINUTES * 60,
                        "RunAtLoad": True,
                        "ProcessType": "Background",
                        "StandardOutPath": "/dev/null",
                        "StandardErrorPath": "/dev/null",
                    },
                    fh,
                )
            self.run(["launchctl", "unload", str(path)])  # ignore "not loaded"
            self._check(["launchctl", "load", "-w", str(path)])
        else:
            if not self.supported:
                raise OsSchedulerError(
                    "systemd was not found, so dbnero cannot start itself on a schedule. "
                    "Keep dbnero open for scheduled backups to run."
                )
            unit_dir = self._timer_path().parent
            unit_dir.mkdir(parents=True, exist_ok=True)
            exec_line = " ".join(_systemd_quote(a) for a in cmd)
            (unit_dir / f"{SYSTEMD_UNIT}.service").write_text(
                "[Unit]\nDescription=dbnero scheduled backups\n\n"
                f"[Service]\nType=oneshot\nExecStart={exec_line}\n",
                "utf-8",
            )
            self._timer_path().write_text(
                "[Unit]\nDescription=Run dbnero scheduled backups\n\n"
                f"[Timer]\nOnActiveSec=1min\nOnBootSec=2min\nOnUnitActiveSec={INTERVAL_MINUTES}min\n"
                "Persistent=true\n\n[Install]\nWantedBy=timers.target\n",
                "utf-8",
            )
            self._check(["systemctl", "--user", "daemon-reload"])
            self._check(["systemctl", "--user", "enable", "--now", f"{SYSTEMD_UNIT}.timer"])

    def uninstall(self) -> None:
        if self.platform == "win32":
            if self.is_installed():
                self._check(["schtasks", "/Delete", "/F", "/TN", TASK_NAME])
        elif self.platform == "darwin":
            path = self._plist_path()
            if path.exists():
                self.run(["launchctl", "unload", "-w", str(path)])
                path.unlink(missing_ok=True)
        else:
            timer = self._timer_path()
            if timer.exists():
                self.run(["systemctl", "--user", "disable", "--now", f"{SYSTEMD_UNIT}.timer"])
                timer.unlink(missing_ok=True)
                timer.with_name(f"{SYSTEMD_UNIT}.service").unlink(missing_ok=True)
                self.run(["systemctl", "--user", "daemon-reload"])

    # -- helpers ----------------------------------------------------------------

    def _plist_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"

    def _timer_path(self) -> Path:
        return self.home / ".config" / "systemd" / "user" / f"{SYSTEMD_UNIT}.timer"

    def _ok(self, args: Sequence[str]) -> bool:
        try:
            return self.run(args).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _check(self, args: Sequence[str]) -> None:
        try:
            proc = self.run(args)
        except (OSError, subprocess.SubprocessError) as exc:
            raise OsSchedulerError(f"{args[0]} could not be started: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise OsSchedulerError(
                f"{args[0]} failed: {detail[-1] if detail else f'exit code {proc.returncode}'}"
            )


def _systemd_quote(arg: str) -> str:
    if arg and all(c.isalnum() or c in "/._-+=:," for c in arg):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'
