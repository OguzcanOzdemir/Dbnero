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
