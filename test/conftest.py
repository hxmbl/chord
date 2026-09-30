"""Shared fixtures. The keychain is faked in every test that touches it, so a
test run can never read or write a real one."""

import json
import time

import keyring
import pytest
from keyring.backend import KeyringBackend


class FakeKeyring(KeyringBackend):
    """An in-memory keyring, so tests never touch the OS one."""

    priority = 99

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self.store.get((service, account))

    def set_password(self, service: str, account: str, password: str) -> None:
        self.store[(service, account)] = password

    def delete_password(self, service: str, account: str) -> None:
        self.store.pop((service, account), None)


@pytest.fixture
def keyring_backend(monkeypatch):
    """Install a fake keyring for the duration of a test."""
    fake = FakeKeyring()
    monkeypatch.setattr(keyring, "get_keyring", lambda: fake)
    monkeypatch.setattr(keyring, "set_password", fake.set_password)
    monkeypatch.setattr(keyring, "get_password", fake.get_password)
    return fake


@pytest.fixture
def stored_token(keyring_backend):
    """A fresh, valid token in the fake keychain."""

    def put(**overrides):
        creds = {
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_at": time.time() + 3600,
            "obtained_at": time.time(),
        }
        creds.update(overrides)
        keyring_backend.set_password("chord", "linear", json.dumps(creds))
        return creds

    return put


@pytest.fixture
def no_env(monkeypatch, tmp_path):
    """Run with an empty environment and a CWD that has no `.env`."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CHORD_MAX_AGE", raising=False)
    return tmp_path
