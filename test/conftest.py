"""Shared fixtures. The keychain is faked in every test that touches it, so a
test run can never read or write a real one."""

import json
import time
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend

from chord import notify, service
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


@pytest.fixture(autouse=True)
def keyring_backend(monkeypatch):
    """Install a fake keyring for the duration of a test.

    Autouse because the module docstring's promise — a test run never reads or
    writes a real keychain — was only true for tests that remembered to ask.
    The ones that didn't tested the *not set up* paths and passed only on a
    machine whose keychain happens to be empty, which is the same mistake as a
    test that passes only because of the maintainer's hardware: it fails on
    any machine that has run `chord setup`, and in CI once a job seeds the
    credentials that the real-process tests in `test_stale_pid.py` need their
    *subprocesses* to find. Those children read the environment's keyring, not
    this in-memory one, so the two requirements do not collide.
    """
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
def no_service_manager(monkeypatch):
    """Keep every test away from the machine's own systemd or launchd.

    `chord start`, `stop`, `watch`, `info` and `setup` all ask whether this
    project has a service. On a developer's Linux box the honest answer would
    send them to a real `systemctl --user`, so by default there is no service
    manager at all; the service tests supply one of their own.
    """
    monkeypatch.setattr(service, "current", lambda: None)

    def refuse(*args, **kwargs):
        raise AssertionError(f"a test reached a real service manager: {args}")

    monkeypatch.setattr(service, "_run", refuse)


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

    Records the working directory it was handed as well as the prompt, because
    that is what a real `CommandHarness` does with it and the tests that care
    about where the work happens can only prove it through this.
    """

    def __init__(self, name: str = "stub", fail: Exception | None = None) -> None:
        self.name = name
        self.prompts: list[str] = []
        self.working_directories: list[Path | None] = []
        self.fail = fail

    async def send(self, prompt: str, cwd: Path | None = None) -> None:
        # Recorded before the failure check: a harness that can't be launched
        # was still given a directory, and that is worth being able to assert.
        self.working_directories.append(cwd)
        if self.fail:
            raise self.fail
        self.prompts.append(prompt)


class Banners:
    """Records the banners a run would have posted.

    Patched at `notify.send` rather than at the verbs, because the verbs take
    `(thing, why)` and the thing worth asserting on is the title and body a
    person actually reads — the whole point of `send` is that those two halves
    cannot be assembled wrongly at the call site.

    Lives here rather than in the delivery tests because "what did this run
    tell the user" is asked about by more than delivery.
    """

    def __init__(self):
        self.said: list[tuple[str, str]] = []

    async def send(self, title, message, *, group=None):
        self.said.append((title, message))

    @property
    def titles(self) -> list[str]:
        return [title for title, _ in self.said]

    @property
    def bodies(self) -> list[str]:
        return [body for _, body in self.said]


@pytest.fixture
def banners(monkeypatch):
    """Capture the notifications a run would have posted."""
    rec = Banners()
    monkeypatch.setattr(notify, "send", rec.send)
    return rec


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
