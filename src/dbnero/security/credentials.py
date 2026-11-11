"""Password storage in the operating system keyring.

This is the *only* place secrets are persisted. The SQLite catalog, manifest
files and logs must never contain passwords.
"""

from __future__ import annotations

import logging
from typing import Protocol

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

    def set_password(self, connection_id: str, password: str) -> None:
        try:
            keyring.set_password(self._service, connection_id, password)
        except keyring.errors.KeyringError as exc:
            raise CredentialStoreUnavailable(type(exc).__name__) from exc

    def get_password(self, connection_id: str) -> str | None:
        try:
            value: str | None = keyring.get_password(self._service, connection_id)
        except keyring.errors.KeyringError as exc:
            raise CredentialStoreUnavailable(type(exc).__name__) from exc
        return value

    def delete_password(self, connection_id: str) -> None:
        try:
            keyring.delete_password(self._service, connection_id)
        except keyring.errors.PasswordDeleteError:
            log.debug("No stored password for connection %s", connection_id)
        except keyring.errors.KeyringError as exc:
            raise CredentialStoreUnavailable(type(exc).__name__) from exc


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
