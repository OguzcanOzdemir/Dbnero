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
