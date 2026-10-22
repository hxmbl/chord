"""The token renews itself. Nobody runs `chord refresh` any more.

Linear's access tokens last 24 hours and a watcher runs for days. The watcher
renews the token an hour before it runs out, and again whenever Linear refuses
the one it holds. Linear rotates the refresh token on every exchange, so two
Chords renewing at once must not both spend it: the second one waits for the
lock, sees the first one's new pair, and uses that.
"""

import asyncio
import contextlib
import json
import time

import pytest

from chord import cli, credentials, watcher
from chord.linear import IssuePage, Unauthorised
from chord.routing import Router


@pytest.fixture(autouse=True)
def private_lock(monkeypatch, tmp_path):
    """Keep the renewal lock out of the real home directory."""
    monkeypatch.setattr(credentials, "_lock_file", lambda: tmp_path / "renew.lock")


@pytest.fixture
def exchange(monkeypatch):
    """A fake Linear token endpoint that hands out numbered pairs."""
    calls: list[str] = []

    async def fake(self, url, refresh_token=None, **kwargs):
        calls.append(refresh_token)
        n = len(calls)
        return {
            "access_token": f"access-{n}",
            "refresh_token": f"refresh-{n}",
            "expires_at": time.time() + 24 * 3600,
        }

    monkeypatch.setattr(
        "authlib.integrations.httpx_client.AsyncOAuth2Client.refresh_token", fake
    )
    monkeypatch.setattr(credentials, "_client_secrets", lambda: ("id", "secret"))
    return calls


def stored(keyring_backend) -> dict:
    return json.loads(keyring_backend.get_password("chord", "linear"))


# --- when a token is due ---


def test_a_fresh_token_is_not_renewed(stored_token, no_env, exchange):
    stored_token(expires_at=time.time() + 20 * 3600)
    code, content = asyncio.run(credentials.renew_if_due())
    assert code == credentials.OK
    assert content["access_token"] == "access"
    assert exchange == []


def test_a_token_inside_the_margin_is_renewed_and_stored_first(
    stored_token, no_env, exchange, keyring_backend
):
    stored_token(
        expires_at=time.time() + 30 * 60, obtained_at=time.time() - 23.5 * 3600
    )
    code, content = asyncio.run(credentials.renew_if_due())
    assert code == credentials.OK
    assert exchange == ["refresh"]
    assert content["access_token"] == "access-1"
    # The new pair is in the keychain, not just in hand.
    assert stored(keyring_backend)["refresh_token"] == "refresh-1"


def test_force_renews_a_token_that_looks_fresh(stored_token, no_env, exchange):
    """Linear refused it, whatever its clock says."""
    stored_token(expires_at=time.time() + 20 * 3600)
    code, _ = asyncio.run(credentials.renew_if_due(force=True))
    assert code == credentials.OK
    assert exchange == ["refresh"]


def test_a_short_max_age_does_not_renew_every_poll(
    stored_token, no_env, exchange, monkeypatch
):
    monkeypatch.setattr(credentials, "max_age", lambda: 600)
    creds = stored_token(expires_at=time.time() + 24 * 3600, obtained_at=time.time())
    # Due a quarter of the token's life before it runs out, not an hour.
    assert credentials.renew_at(creds) == pytest.approx(time.time() + 450, abs=5)
    asyncio.run(credentials.renew_if_due())
    assert exchange == []


# --- two Chords, one refresh token ---


def test_a_renewal_another_chord_just_made_is_used_not_repeated(
    stored_token, no_env, exchange, keyring_backend, monkeypatch
):
    stored_token(expires_at=time.time() + 60, obtained_at=time.time() - 24 * 3600)

    @contextlib.contextmanager
    def lock_held_by_someone_who_renewed():
        # While we waited for the lock, the other Chord stored its new pair.
        keyring_backend.set_password(
            "chord",
            "linear",
            json.dumps(
                {
                    "access_token": "theirs",
                    "refresh_token": "their-refresh",
                    "expires_at": time.time() + 24 * 3600,
                    "obtained_at": time.time(),
                }
            ),
        )
        yield

    monkeypatch.setattr(credentials, "_renewal_lock", lock_held_by_someone_who_renewed)
    code, content = asyncio.run(credentials.renew_if_due(force=True))
    assert code == credentials.OK
    assert content["access_token"] == "theirs"
    assert exchange == [], "spent a refresh token Linear had already rotated"


def test_a_refused_refresh_says_to_sign_in_again(
    stored_token, no_env, monkeypatch
):
    from chord.credentials import _oauth_clients

    _client, OAuthError = _oauth_clients()

    async def refused(self, url, refresh_token=None, **kwargs):
        raise OAuthError(error="invalid_grant")

    monkeypatch.setattr(
        "authlib.integrations.httpx_client.AsyncOAuth2Client.refresh_token", refused
    )
    monkeypatch.setattr(credentials, "_client_secrets", lambda: ("id", "secret"))
    stored_token(expires_at=time.time() - 10)
    code, content = asyncio.run(credentials.renew_if_due())
    assert code == credentials.FAILED
    assert "chord setup" in content


# --- the watcher ---


class Client:
    def __init__(self, *live):
        self.live = set(live)
        self.token = None
        self.tokens = []

    def use_token(self, token):
        self.token = token

    async def issues_for(self, filter):
        self.tokens.append(self.token)
        if self.token not in self.live:
            raise Unauthorised("authentication failed")
        return IssuePage(
            [
                {
                    "id": "1",
                    "identifier": "ENG-1",
                    "title": "t",
                    "description": "d",
                    "createdAt": "2026-01-01T00:00:00Z",
                }
            ],
            False,
        )

    async def label_history(self, issue_id):
        return []

    async def comments(self, issue_id):
        return []


class Spy:
    name = "spy"

    def __init__(self):
        self.prompts = []

    async def send(self, prompt, cwd=None):
        self.prompts.append(prompt)


def build(tmp_path, client, freshen, renew=None):
    spy = Spy()
    w = watcher.Watcher(
        client,
        Router("Chord", "spy", factory=lambda s: spy),
        1,
        tmp_path / "state.json",
        renew=renew,
        freshen=freshen,
    )
    return w, spy


def test_the_watcher_renews_before_linear_refuses(tmp_path, capsys):
    client = Client("renewed")
    asked = []

    async def freshen(force):
        asked.append(force)
        return "renewed"

    w, spy = build(tmp_path, client, freshen)
    asyncio.run(w.poll())

    assert asked[0] is False, "the proactive check is not forced"
    assert client.tokens == ["renewed"], "polled before adopting the new token"
    assert len(spy.prompts) == 1
    assert "Renewed the Linear token." in capsys.readouterr().out


def test_a_refused_token_is_renewed_with_linear_and_the_poll_retried(tmp_path):
    client = Client("renewed")
    asked = []

    async def freshen(force):
        asked.append(force)
        return "renewed" if force else None

    w, spy = build(tmp_path, client, freshen, renew=lambda: None)
    asyncio.run(w.poll())

    assert asked == [False, True]
    assert client.tokens == [None, "renewed"]
    assert len(spy.prompts) == 1


def test_a_failed_renewal_is_logged_and_the_poll_goes_on(tmp_path, capsys):
    """How often it is said is the freshener's back-off, tested below."""
    client = Client(None)  # the token we hold still works

    async def freshen(force):
        raise RuntimeError("Linear is down")

    w, spy = build(tmp_path, client, freshen)
    asyncio.run(w.poll())

    out = capsys.readouterr().out
    assert "couldn't renew the Linear token: Linear is down" in out
    assert len(spy.prompts) == 1


# --- the daemon's freshener ---


def test_the_freshener_leaves_the_keychain_alone_until_due(
    stored_token, no_env, monkeypatch
):
    stored_token(expires_at=time.time() + 20 * 3600)
    calls = []

    async def spy(force=False):
        calls.append(force)
        return credentials.OK, {"access_token": "x", "expires_at": time.time() + 1}

    fresh = cli._Freshener()
    monkeypatch.setattr(credentials, "renew_if_due", spy)
    assert asyncio.run(fresh(False)) is None
    assert calls == []
    # Forced (Linear refused it) goes through regardless of the clock.
    assert asyncio.run(fresh(True)) == "x"
    assert calls == [True]


def test_the_freshener_backs_off_after_a_failure(stored_token, no_env, monkeypatch):
    stored_token(expires_at=time.time() - 10)
    calls = []

    async def failing(force=False):
        calls.append(force)
        return credentials.FAILED, "Linear refused the refresh"

    fresh = cli._Freshener()
    monkeypatch.setattr(credentials, "renew_if_due", failing)
    with pytest.raises(RuntimeError):
        asyncio.run(fresh(False))
    # Even a forced attempt waits out the back-off.
    assert asyncio.run(fresh(True)) is None
    assert calls == [False]
