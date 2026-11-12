"""Password storage: the operating system keyring, with a Windows DPAPI
encrypted file as a fallback when the Credential Manager refuses access.

This module is the *only* place secrets are persisted. The SQLite catalog,
manifest files and logs must never contain passwords.
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

import keyring
import keyring.errors

from dbnero import APP_NAME

log = logging.getLogger(__name__)

SERVICE_NAME = f"{APP_NAME}.connections"


class CredentialStoreUnavailable(RuntimeError):
    """Raised when no usable OS keyring backend exists (e.g. headless Linux)."""

    def __init__(self, detail: str = "") -> None:
        msg = (
            "No secure credential store is available. On Linux install and unlock a "
            "Secret Service provider (GNOME Keyring or KWallet)."
        )
        super().__init__(f"{msg} {detail}".strip())


class CredentialStore(Protocol):
    def set_password(self, connection_id: str, password: str) -> None: ...
    def get_password(self, connection_id: str) -> str | None: ...
    def delete_password(self, connection_id: str) -> None: ...


class KeyringCredentialStore:
    def __init__(self, service_name: str = SERVICE_NAME) -> None:
        self._service = service_name

    @staticmethod
    def is_available() -> bool:
        backend = keyring.get_keyring()
        priority = getattr(backend, "priority", 0)
        name = type(backend).__module__
        return priority > 0 and not name.startswith(
            ("keyring.backends.fail", "keyring.backends.null")
        )

    # Backends raise more than KeyringError: the Windows Credential Manager
    # raises pywintypes.error (e.g. 1312 "A specified logon session does not
    # exist" in remote or service sessions), Secret Service raises D-Bus
    # errors. Any backend failure means "no usable store", never a crash.

    def set_password(self, connection_id: str, password: str) -> None:
        try:
            keyring.set_password(self._service, connection_id, password)
        except Exception as exc:
            raise CredentialStoreUnavailable(_describe(exc)) from exc

    def get_password(self, connection_id: str) -> str | None:
        try:
            value: str | None = keyring.get_password(self._service, connection_id)
        except Exception as exc:
            raise CredentialStoreUnavailable(_describe(exc)) from exc
        return value

    def delete_password(self, connection_id: str) -> None:
        try:
            keyring.delete_password(self._service, connection_id)
        except keyring.errors.PasswordDeleteError:
            log.debug("No stored password for connection %s", connection_id)
        except Exception as exc:
            raise CredentialStoreUnavailable(_describe(exc)) from exc


def _describe(exc: BaseException) -> str:
    """Short error description that never contains a secret."""
    args: tuple[Any, ...] = tuple(getattr(exc, "args", ()))
    if len(args) >= 3 and isinstance(args[0], int):  # pywintypes.error(code, func, text)
        return f"{args[1]} failed with Windows error {args[0]}: {args[2]}"
    return type(exc).__name__


class InMemoryCredentialStore:
    """Non-persistent store for tests."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def set_password(self, connection_id: str, password: str) -> None:
        self._data[connection_id] = password

    def get_password(self, connection_id: str) -> str | None:
        return self._data.get(connection_id)

    def delete_password(self, connection_id: str) -> None:
        self._data.pop(connection_id, None)


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, decrypt: bool) -> bytes:
    """Encrypt/decrypt with the current Windows user's DPAPI key."""
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        raise CredentialStoreUnavailable("Windows data protection is not available.")
    buf = ctypes.create_string_buffer(data, len(data))
    inp = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    fn = windll.crypt32.CryptUnprotectData if decrypt else windll.crypt32.CryptProtectData
    ui_forbidden = 0x1
    if not fn(ctypes.byref(inp), None, None, None, None, ui_forbidden, ctypes.byref(out)):
        code = windll.kernel32.GetLastError()
        raise CredentialStoreUnavailable(f"Windows data protection failed with error {code}")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        windll.kernel32.LocalFree(out.pbData)


class EncryptedFileCredentialStore:
    """Passwords in a file, each one encrypted with Windows DPAPI (only the
    same Windows user on the same machine can decrypt it). Used when the
    Credential Manager is unavailable, e.g. Windows error 1312."""

    def __init__(
        self,
        path: Path,
        encrypt: Callable[[bytes], bytes] | None = None,
        decrypt: Callable[[bytes], bytes] | None = None,
    ) -> None:
        self.path = path
        self._encrypt = encrypt or (lambda b: _dpapi(b, decrypt=False))
        self._decrypt = decrypt or (lambda b: _dpapi(b, decrypt=True))
        self._lock = threading.Lock()

    @staticmethod
    def supported() -> bool:
        return sys.platform == "win32"

    def _read(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise CredentialStoreUnavailable(type(exc).__name__) from exc
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def _write(self, data: dict[str, str]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.path)
        except OSError as exc:
            raise CredentialStoreUnavailable(type(exc).__name__) from exc

    def set_password(self, connection_id: str, password: str) -> None:
        token = base64.b64encode(self._encrypt(password.encode("utf-8"))).decode("ascii")
        with self._lock:
            data = self._read()
            data[connection_id] = token
            self._write(data)

    def get_password(self, connection_id: str) -> str | None:
        with self._lock:
            token = self._read().get(connection_id)
        if token is None:
            return None
        try:
            raw = base64.b64decode(token)
        except ValueError as exc:
            raise CredentialStoreUnavailable("Stored password is damaged") from exc
        return self._decrypt(raw).decode("utf-8")

    def delete_password(self, connection_id: str) -> None:
        with self._lock:
            data = self._read()
            if data.pop(connection_id, None) is not None:
                self._write(data)


class SessionFallbackCredentialStore:
    """Saves passwords in the OS keyring, then in each fallback store in turn
    when one is unavailable. With none left, passwords are kept in memory for
    the current session only and ``persistent`` is False so the UI can say so.

    ``set_session_password`` keeps a password for this session only, on request
    (the user unticked "Save password")."""

    def __init__(
        self,
        primary: CredentialStore | None = None,
        fallbacks: Sequence[CredentialStore] = (),
    ) -> None:
        self._stores: list[CredentialStore] = [primary or KeyringCredentialStore(), *fallbacks]
        self._session = InMemoryCredentialStore()
        self._probed = False

    @property
    def persistent(self) -> bool:
        return bool(self._stores)

    def _fallback(self, exc: CredentialStoreUnavailable) -> None:
        failed = type(self._stores.pop(0)).__name__
        if self._stores:
            log.warning("%s unavailable, using %s: %s", failed, type(self._stores[0]).__name__, exc)
        else:
            log.warning("Credential store unavailable, using session-only passwords: %s", exc)

    def is_available(self) -> bool:
        """True when passwords are saved persistently. Probes the store once,
        because a backend can be installed but refuse access (Windows 1312)."""
        if not self._probed:
            self._probed = True
            while self._stores:
                try:
                    self._stores[0].get_password("__dbnero_probe__")
                    break
                except CredentialStoreUnavailable as exc:
                    self._fallback(exc)
        return self.persistent

    def set_password(self, connection_id: str, password: str) -> None:
        self._session.delete_password(connection_id)
        while self._stores:
            try:
                self._stores[0].set_password(connection_id, password)
                return
            except CredentialStoreUnavailable as exc:
                self._fallback(exc)
        self._session.set_password(connection_id, password)

    def set_session_password(self, connection_id: str, password: str) -> None:
        """Keep the password until dbnero closes and remove any saved copy."""
        self._delete_saved(connection_id)
        self._session.set_password(connection_id, password)

    def is_session_only(self, connection_id: str) -> bool:
        return self._session.get_password(connection_id) is not None

    def get_password(self, connection_id: str) -> str | None:
        value = self._session.get_password(connection_id)
        while value is None and self._stores:
            try:
                return self._stores[0].get_password(connection_id)
            except CredentialStoreUnavailable as exc:
                self._fallback(exc)
        return value

    def delete_password(self, connection_id: str) -> None:
        self._session.delete_password(connection_id)
        self._delete_saved(connection_id)

    def _delete_saved(self, connection_id: str) -> None:
        while self._stores:
            try:
                self._stores[0].delete_password(connection_id)
                return
            except CredentialStoreUnavailable as exc:
                self._fallback(exc)


def default_credential_store(config_dir: Path) -> SessionFallbackCredentialStore:
    """Keyring first; on Windows an encrypted file next to the settings second."""
    fallbacks: list[CredentialStore] = []
    if EncryptedFileCredentialStore.supported():
        fallbacks.append(EncryptedFileCredentialStore(config_dir / "credentials.dat"))
    return SessionFallbackCredentialStore(fallbacks=fallbacks)
