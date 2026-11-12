"""Application logging with secret masking."""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
import tempfile
import threading
from pathlib import Path

_MASK = "******"

# key=value / key: value style secrets in connection strings and messages
_KV_PATTERN = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token)(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s;,&]+)"
)
# scheme://user:password@host
_URL_PATTERN = re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^:/@\s]+:)([^@\s]+)(@)")


def mask_secrets(text: str, extra_secrets: set[str] | None = None) -> str:
    text = _KV_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}{_MASK}", text)
    text = _URL_PATTERN.sub(lambda m: f"{m.group(1)}{_MASK}{m.group(3)}", text)
    for secret in extra_secrets or ():
        if secret:
            text = text.replace(secret, _MASK)
    return text


class SecretMaskingFilter(logging.Filter):
    """Masks known secrets and secret-looking patterns in every log record.

    Passwords read from the keyring can be registered with ``register`` so
    that they are scrubbed even if they appear verbatim in tool output.
    """

    def __init__(self) -> None:
        super().__init__()
        self._secrets: set[str] = set()
        self._lock = threading.Lock()

    def register(self, secret: str) -> None:
        if secret:
            with self._lock:
                self._secrets.add(secret)

    def filter(self, record: logging.LogRecord) -> bool:
        with self._lock:
            secrets = set(self._secrets)
        message = record.getMessage()
        masked = mask_secrets(message, secrets)
        if masked != message:
            record.msg = masked
            record.args = None
        return True


masking_filter = SecretMaskingFilter()


def configure_logging(log_dir: Path, level: int = logging.INFO) -> Path | None:
    """Log to a rotating file (and stderr when there is one). Never fails:
    if the log folder is not writable, fall back to the temp folder, then to
    no file at all. Returns the log file in use."""
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    handlers: list[logging.Handler] = []
    log_file: Path | None = None
    for directory in (log_dir, Path(tempfile.gettempdir()) / "dbnero-logs"):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            candidate = directory / "dbnero.log"
            handlers.append(
                logging.handlers.RotatingFileHandler(
                    candidate, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
                )
            )
            log_file = candidate
            break
        except OSError:
            continue
    # Windowed (frozen) apps on Windows have no stderr at all.
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    for handler in handlers:
        handler.setFormatter(fmt)
        handler.addFilter(masking_filter)
        root.addHandler(handler)
    return log_file
