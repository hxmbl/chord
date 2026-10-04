"""Shared fixtures. The keychain is faked in every test that touches it, so a
test run can never read or write a real one."""

import json
import time

import keyring
import pytest
from keyring.backend import KeyringBackend

from chord import notify
from chord.routing import Router


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


@pytest.fixture(autouse=True)
def no_notifications(monkeypatch):
    """Keep the suite from spawning real notifiers.

    Delivery was a no-op on macOS for as long as it went through
    `desktop_notifier`, so this was never a problem. It is a subprocess now, at
    a few hundred milliseconds a pop, and the watcher's own tests run on a
    wall-clock budget measured in hundredths of a second — so an unstubbed
    notification stops the hand-over from finishing, which reads as the harness
    never being called.

    Patched at the lookup rather than at `_deliver`, so the tests that are about
    delivery still exercise it — including the ones that call `_deliver`
    directly with their own argv.
    """
    monkeypatch.setattr(notify, "_find", lambda name: None)
    monkeypatch.setattr(notify, "OSASCRIPT", "/nonexistent/osascript")


class RecordingHarness:
    """A harness that writes down what it was handed, and does nothing else.

    Most tests aren't about a harness. They're about which route was chosen, so
    they need something that records the prompt without running an agent. One
    definition here rather than four that differ only in `name`.
    """

    def __init__(self, name: str = "stub", fail: Exception | None = None) -> None:
        self.name = name
        self.prompts: list[str] = []
        self.fail = fail

    async def send(self, prompt: str) -> None:
        if self.fail:
            raise self.fail
        self.prompts.append(prompt)


def router_for(
    harness: RecordingHarness | None = None,
    label: str = "chord",
    harnesses: dict | None = None,
    name: str = "stub",
) -> Router:
    """A Router that hands the same harness back for every route.

    The curation table is real — `harnesses` goes straight through — but the
    building step is stubbed, because what these tests are about is which name
    reached the Router. Whether a name maps to a runnable command is
    `harness.build`'s business and has its own tests.
    """
    built = harness if harness is not None else RecordingHarness(name)
    return Router(label, "stub", harnesses or {}, factory=lambda spec: built)
