import sys

import keyring
import pytest
from keyring.backend import KeyringBackend

from dbnero.security.credentials import InMemoryCredentialStore, KeyringCredentialStore


class _DictKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.data = {}

    def set_password(self, service, username, password):
        self.data[(service, username)] = password

    def get_password(self, service, username):
        return self.data.get((service, username))

    def delete_password(self, service, username):
        import keyring.errors

        if (service, username) not in self.data:
            raise keyring.errors.PasswordDeleteError(username)
        del self.data[(service, username)]


@pytest.fixture
def fake_keyring():
    previous = keyring.get_keyring()
    backend = _DictKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


def test_keyring_roundtrip(fake_keyring):
    store = KeyringCredentialStore()
    store.set_password("conn-1", "pw")
    assert store.get_password("conn-1") == "pw"
    assert ("dbnero.connections", "conn-1") in fake_keyring.data
    store.delete_password("conn-1")
    assert store.get_password("conn-1") is None
    store.delete_password("conn-1")  # idempotent


def test_in_memory():
    s = InMemoryCredentialStore()
    s.set_password("a", "b")
    assert s.get_password("a") == "b"
    s.delete_password("a")
    assert s.get_password("a") is None


def test_session_fallback_when_keyring_unavailable():
    from dbnero.security.credentials import (
        CredentialStoreUnavailable,
        SessionFallbackCredentialStore,
    )

    class Broken:
        def set_password(self, *a):
            raise CredentialStoreUnavailable("x")

        def get_password(self, *a):
            raise CredentialStoreUnavailable("x")

        def delete_password(self, *a):
            raise CredentialStoreUnavailable("x")

    store = SessionFallbackCredentialStore(Broken())
    assert store.get_password("a") is None
    assert not store.is_available()
    store.set_password("a", "pw")
    assert store.get_password("a") == "pw"
    store.delete_password("a")
    assert store.get_password("a") is None


def test_session_fallback_uses_primary(fake_keyring):
    from dbnero.security.credentials import SessionFallbackCredentialStore

    store = SessionFallbackCredentialStore()
    store.set_password("a", "pw")
    assert fake_keyring.data[("dbnero.connections", "a")] == "pw"
    assert store.is_available() and store.get_password("a") == "pw"


def test_windows_credential_error_falls_back_to_session(monkeypatch):
    """Windows error 1312 (no logon session) must not crash the app."""
    from dbnero.security.credentials import (
        CredentialStoreUnavailable,
        KeyringCredentialStore,
        SessionFallbackCredentialStore,
    )

    class WinError(Exception):  # stands in for pywintypes.error
        pass

    def boom(*_a):
        raise WinError(1312, "CredRead", "A specified logon session does not exist.")

    monkeypatch.setattr(keyring, "get_password", boom)
    monkeypatch.setattr(keyring, "set_password", boom)
    monkeypatch.setattr(keyring, "delete_password", boom)

    with pytest.raises(CredentialStoreUnavailable, match="1312"):
        KeyringCredentialStore().get_password("x")

    store = SessionFallbackCredentialStore()
    assert store.get_password("conn") is None
    assert store.is_available() is False
    store.set_password("conn", "pw")
    assert store.get_password("conn") == "pw"
    store.delete_password("conn")
    assert store.get_password("conn") is None


class _BrokenStore:
    def set_password(self, *a):
        from dbnero.security.credentials import CredentialStoreUnavailable

        raise CredentialStoreUnavailable("1312")

    get_password = set_password
    delete_password = set_password


def _xor(data: bytes) -> bytes:
    return bytes(b ^ 0x5A for b in data)


def test_encrypted_file_store_roundtrip(tmp_path):
    from dbnero.security.credentials import EncryptedFileCredentialStore

    path = tmp_path / "credentials.dat"
    store = EncryptedFileCredentialStore(path, encrypt=_xor, decrypt=_xor)
    assert store.get_password("a") is None
    store.set_password("a", "s3cret")
    store.set_password("b", "other")
    assert "s3cret" not in path.read_text("utf-8")
    again = EncryptedFileCredentialStore(path, encrypt=_xor, decrypt=_xor)
    assert again.get_password("a") == "s3cret"
    again.delete_password("a")
    assert again.get_password("a") is None and again.get_password("b") == "other"


def test_keyring_failure_falls_back_to_encrypted_file(tmp_path):
    """Windows 1312: passwords are still saved, in the DPAPI file."""
    from dbnero.security.credentials import (
        EncryptedFileCredentialStore,
        SessionFallbackCredentialStore,
    )

    path = tmp_path / "credentials.dat"
    file_store = EncryptedFileCredentialStore(path, encrypt=_xor, decrypt=_xor)
    store = SessionFallbackCredentialStore(_BrokenStore(), fallbacks=[file_store])
    assert store.is_available()
    store.set_password("conn", "pw")
    assert path.exists()
    # A new run (fresh store objects) still finds the password.
    store2 = SessionFallbackCredentialStore(
        _BrokenStore(),
        fallbacks=[EncryptedFileCredentialStore(path, encrypt=_xor, decrypt=_xor)],
    )
    assert store2.get_password("conn") == "pw"


def test_session_only_password(fake_keyring):
    from dbnero.security.credentials import SessionFallbackCredentialStore

    store = SessionFallbackCredentialStore()
    store.set_password("a", "saved")
    store.set_session_password("a", "temp")
    assert ("dbnero.connections", "a") not in fake_keyring.data
    assert store.get_password("a") == "temp" and store.is_session_only("a")
    store.set_password("a", "saved2")
    assert not store.is_session_only("a")
    assert fake_keyring.data[("dbnero.connections", "a")] == "saved2"


def test_connection_service_remember_flag(fake_keyring, tmp_path):
    from dbnero.core.connection_service import ConnectionService
    from dbnero.core.models import ConnectionProfile, Engine
    from dbnero.core.settings import Settings
    from dbnero.security.credentials import SessionFallbackCredentialStore
    from dbnero.storage.catalog import Catalog

    catalog = Catalog(tmp_path / "c.db")
    svc = ConnectionService(catalog, SessionFallbackCredentialStore(), Settings())
    p = ConnectionProfile(name="x", engine=Engine.POSTGRESQL, host="h", port=5432, username="u")
    svc.save(p, "pw", remember=False)
    assert svc.has_password(p.id) and svc.password_is_session_only(p.id)
    assert not fake_keyring.data
    svc.save(p, None, remember=True)  # tick "Save password" without retyping
    assert fake_keyring.data[("dbnero.connections", p.id)] == "pw"
    assert not svc.password_is_session_only(p.id)
    catalog.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI")
def test_real_dpapi_roundtrip(tmp_path):
    from dbnero.security.credentials import EncryptedFileCredentialStore

    store = EncryptedFileCredentialStore(tmp_path / "credentials.dat")
    store.set_password("a", "pässwörd")
    assert EncryptedFileCredentialStore(tmp_path / "credentials.dat").get_password("a") == (
        "pässwörd"
    )
