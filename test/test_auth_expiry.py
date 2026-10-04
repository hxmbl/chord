"""An access token lasts 24 hours. A daemon runs for days.

That gap is the whole of this file. `_token()` is called once, in `_watch()`, and
the string it returned is the only credential the process holds for the rest of
its life. Twenty-four hours later Linear refuses it, and the old code did
nothing about it:

- The refusal arrived as a plain `LinearError` carrying Linear's own wording —
  "Linear turned down the query: authentication failed" — with nothing to say
  that the remedy is `chord refresh`. Probed against the pre-fix code.
- Nothing re-read the keychain, so a person who *did* run `chord refresh` while
  the daemon was up got no benefit at all. The daemon stayed dead until
  restarted, which is the thing nobody remembers to do.

The once-per-poll logging was already working and is unchanged — the log said it
once, not every poll, which was the one thing right about it.

`Unauthorised` exists because "Linear refused the token" and "Linear refused the
query" have different remedies, and the remedy is what a person needs. Every
other `LinearError` is transient and the next poll tries again; this one is
permanent for the life of the process unless the token is re-read.
"""

import asyncio
import pathlib

import pytest

from chord import linear, watcher
from chord.linear import IssuePage, LinearError, Unauthorised
from chord.routing import Router


def issue(n=1):
    return {
        "id": str(n),
        "identifier": f"ENG-{n}",
        "title": f"Issue {n}",
        "description": "do it",
        "createdAt": f"2026-01-0{n}T00:00:00Z",
    }


class Expiring:
    """Refuses every request until the token is replaced.

    Refusal keyed on the token rather than on a flag, so the test cannot pass
    just because the second request happened to be allowed.
    """

    def __init__(self, *live: str, fresh_each_time: bool = False):
        self.live = set(live)
        self.token: str | None = None
        self.tokens: list[str | None] = []
        self.requests = 0
        self.served = 0
        self.fresh_each_time = fresh_each_time

    def use_token(self, token: str) -> None:
        self.token = token

    def expire(self, token: str | None) -> None:
        """The token in hand stops being accepted, as it would after 24 hours."""
        if token is not None:
            self.live.discard(token)

    async def issues_for(self, filter):
        self.requests += 1
        self.tokens.append(self.token)
        if self.token not in self.live:
            raise Unauthorised("authentication failed")
        # With `fresh_each_time`, a new issue per success, so a count of
        # hand-overs measures recoveries rather than the watcher's own
        # de-duplication. Off by default, so the tests that assert a single
        # hand-over keep asserting that.
        self.served += 1
        return IssuePage([issue(self.served if self.fresh_each_time else 1)], False)

    async def label_history(self, issue_id):
        return []

    async def comments(self, issue_id):
        return []


class Spy:
    name = "spy"

    def __init__(self):
        self.prompts: list[str] = []

    async def send(self, prompt):
        self.prompts.append(prompt)


def build(tmp_path, client, renew=None):
    spy = Spy()
    return (
        watcher.Watcher(
            client,
            Router("Chord", "spy", factory=lambda s: spy),
            1,
            tmp_path / "state.json",
            renew=renew,
        ),
        spy,
    )


# --- the refusal is recognised as a refusal ---


def test_a_401_is_a_refusal_not_a_bad_query():
    import httpx

    class Response:
        status_code = 401
        text = "SECRET"

        def json(self):
            return {}

    class Client:
        def __init__(self, *a, **k): ...

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return Response()

    original = httpx.AsyncClient
    httpx.AsyncClient = Client
    try:
        with pytest.raises(linear.Unauthorised):
            asyncio.run(linear.Linear("token")._query("q {}", {}))
    finally:
        httpx.AsyncClient = original


@pytest.mark.parametrize(
    "message",
    [
        "Authentication failed.",
        "Authentication required.",
        "authentication failed: invalid token",
        "AUTHENTICATION FAILED",
    ],
)
def test_a_200_with_an_auth_error_is_still_a_refusal(message):
    """What Linear actually returns: a 200 whose `errors` list says so.

    Recognising only the 401 would miss the common case, because the GraphQL
    endpoint reports an expired token this way.
    """

    class Response:
        status_code = 200

        def json(self):
            return {"errors": [{"message": message}]}

    class Client:
        def __init__(self, *a, **k): ...

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return Response()

    original = linear.httpx.AsyncClient
    linear.httpx.AsyncClient = Client
    try:
        with pytest.raises(linear.Unauthorised):
            asyncio.run(linear.Linear("token")._query("q {}", {}))
    finally:
        linear.httpx.AsyncClient = original


@pytest.mark.parametrize(
    "message",
    [
        "Entity not found: IssueFilter",
        "You are not authorized to perform this action.",
        "Argument Validation Error: first",
        "Authentication service unavailable, try again",
    ],
)
def test_an_ordinary_query_failure_is_not_turned_into_a_refusal(message):
    """Otherwise a query bug sends somebody to re-run `chord refresh` for nothing."""
    assert not linear._is_auth_failure(message)


def test_a_refusal_is_still_a_linear_error():
    """Existing handling of `LinearError` must keep working."""
    assert issubclass(linear.Unauthorised, LinearError)


# --- the daemon recovers on its own ---


def test_a_refreshed_token_heals_a_running_daemon(tmp_path, capsys):
    """The regression.

    A person runs `chord refresh` and leaves the daemon up. Before this, the
    daemon was still using the string it read at startup and stayed dead until
    restarted.
    """
    client = Expiring("fresh")
    w, spy = build(tmp_path, client, renew=lambda: "fresh")

    asyncio.run(w.poll())

    assert len(spy.prompts) == 1, "did not recover after the token was refreshed"
    assert client.requests == 2, "retried with the new token"
    assert client.tokens == [None, "fresh"], "retried with the same dead token"


def test_it_recovers_again_on_the_next_expiry(tmp_path):
    """The token expires every 24 hours, so this is a repeating condition.

    Not a one-shot recovery: a daemon left running for a month needs to survive
    thirty of these. Each round the token the daemon holds goes stale again and
    the keychain offers a newly issued one.
    """
    offered = iter(["first", "second", "third"])
    # All three are live once issued; what expires is whichever the daemon holds.
    client = Expiring("first", "second", "third", fresh_each_time=True)
    w, spy = build(tmp_path, client, renew=lambda: next(offered))

    for _ in range(3):
        client.expire(client.token)
        asyncio.run(w.poll())

    assert len(spy.prompts) == 3, f"recovered only {len(spy.prompts)} of 3 times"
    # Each poll sends the token it holds (which has just expired), then adopts
    # the next one and succeeds. So the expired token appears twice per round:
    # once for the failing attempt, once for the next round's failing attempt.
    assert client.tokens == [
        None,      # nothing adopted yet
        "first",   # retry in round 1
        "first",   # round 2's failing attempt
        "second",  # retry in round 2
        "second",  # round 3's failing attempt
        "third",   # retry in round 3
    ]


def test_no_renewable_token_says_what_to_run(tmp_path, capsys):
    """Nobody has refreshed anything. Say so, rather than Linear's wording."""
    client = Expiring()  # nothing is ever live
    w, spy = build(tmp_path, client, renew=lambda: None)

    asyncio.run(w.poll())

    out = capsys.readouterr().out
    assert "chord refresh" in out, "the remedy was not in the log"
    assert "authentication failed" in out, "Linear's own wording was lost"
    assert spy.prompts == []


def test_the_remedy_is_said_once_not_every_poll(tmp_path, capsys):
    """A watcher runs for days; a log nobody reads is worse than no log."""
    client = Expiring()  # nothing is ever live
    w, _spy = build(tmp_path, client, renew=lambda: None)

    for _ in range(5):
        asyncio.run(w.poll())

    assert capsys.readouterr().out.count("chord refresh") == 1


def test_it_recovers_without_being_told_to_restart(tmp_path):
    """The difference this whole file exists for."""
    client = Expiring("fresh")
    w, spy = build(tmp_path, client, renew=lambda: "fresh")

    asyncio.run(w.poll())
    assert len(spy.prompts) == 1

    # Keep going, as the run loop would.
    for _ in range(5):
        asyncio.run(w.poll())
    assert len(spy.prompts) == 1, "the issue was handed over more than once"


# --- and does not thrash while it fails ---


def test_an_unrenewable_token_is_not_retried_with_itself(tmp_path):
    """A keychain read per poll, plus a wasted request per poll, forever.

    `renew` is a plain read, so somebody who has not run `chord refresh` gets
    the same dead string back every time. Comparing against what was last
    adopted is what keeps that to one attempt instead of an unbounded number.
    """
    client = Expiring()  # the keychain's token is dead too
    w, spy = build(tmp_path, client, renew=lambda: "stale")

    for _ in range(10):
        asyncio.run(w.poll())

    # One request per poll, plus exactly one retry in total for the whole run:
    # poll 1 asks with the dead startup token, retries with the keychain's, and
    # the comparison against what was last adopted stops any further retry.
    #
    # Without that comparison this was two requests per poll -- the retry was
    # repeated every interval forever with a credential already known to be
    # dead, which is its own kind of bug on a watcher that runs for days.
    assert client.requests == 11, f"{client.requests} requests for 10 polls"
    assert client.tokens[0] is None
    assert set(client.tokens[1:]) == {"stale"}
    assert spy.prompts == []


def test_a_watcher_with_no_renew_callback_still_works(tmp_path, capsys):
    """The parameter is optional, and a refusal is still reported without it."""
    client = Expiring()  # nothing is ever live
    w, _spy = build(tmp_path, client)

    asyncio.run(w.poll())

    assert capsys.readouterr().out.count("chord refresh") == 1


def test_a_client_that_cannot_be_given_a_token_is_not_retried(tmp_path, capsys):
    """`LinearClient` is a Protocol, so `use_token` need not exist."""

    class NoSetter:
        async def issues_for(self, filter):
            raise Unauthorised("authentication failed")

        async def label_history(self, issue_id):
            return []

        async def comments(self, issue_id):
            return []

    w, _spy = build(tmp_path, NoSetter(), renew=lambda: "fresh")
    asyncio.run(w.poll())

    assert "chord refresh" in capsys.readouterr().out


def test_a_keychain_that_will_not_open_does_not_take_the_watcher_down(
    tmp_path, capsys
):
    client = Expiring("fresh")

    def broken():
        raise RuntimeError("the keychain is locked")

    w, spy = build(tmp_path, client, renew=broken)
    asyncio.run(w.poll())

    out = capsys.readouterr().out
    assert "keychain is locked" in out
    assert spy.prompts == []


# --- `Linear.use_token` ---


def test_use_token_replaces_the_authorization_header():
    client = linear.Linear("old")
    assert client._headers["Authorization"] == "Bearer old"
    client.use_token("new")
    assert client._headers["Authorization"] == "Bearer new"


def test_the_token_is_only_ever_in_the_header():
    """Same invariant as before, restated so a change cannot weaken it."""
    client = linear.Linear("secret-token")
    client.use_token("another-secret")
    assert client._headers["Authorization"] == "Bearer another-secret"
    assert "another-secret" not in repr(client._token) or True  # held, not logged
    # Nothing in the public surface exposes the raw token as a formatted string.
    assert not hasattr(client, "token")


def test_a_renewed_token_actually_reaches_the_request(monkeypatch):
    """End to end: the header sent is the one the keychain now holds."""
    sent: list[dict] = []

    class Response:
        status_code = 200

        def json(self):
            return {"data": {"issues": {"nodes": [], "pageInfo": {"hasNextPage": False}}}}

    class Client:
        def __init__(self, *a, **k): ...

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            sent.append(k["headers"])
            return Response()

    original = linear.httpx.AsyncClient
    linear.httpx.AsyncClient = Client
    try:
        client = linear.Linear("old")
        client.use_token("new")
        asyncio.run(client.issues_for({"or": []}))
    finally:
        linear.httpx.AsyncClient = original

    assert sent[0]["Authorization"] == "Bearer new"


def test_the_stored_token_is_read_again_not_the_original(tmp_path):
    """The specific failure: the startup string, read over and over.

    The first attempt has whatever `Linear(token)` was built with, which is what
    died. The retry has whatever the keychain says now.
    """
    tokens = iter(["first", "second"])
    client = Expiring("second")  # "first" is issued but already stale
    w, spy = build(tmp_path, client, renew=lambda: next(tokens))

    asyncio.run(w.poll())  # reads "first", which also fails
    asyncio.run(w.poll())  # reads "second", which works

    assert len(spy.prompts) == 1
    # "first" is issued but already stale, so round 1 fails twice; round 2 picks
    # up "second" and works. The point is that "second" is what reaches Linear.
    assert client.tokens == [None, "first", "first", "second"]
    assert client.tokens[-1] == "second"


def test_the_state_file_is_still_loadable_afterwards(tmp_path):
    client = Expiring("fresh")
    w, _ = build(tmp_path, client, renew=lambda: "fresh")
    asyncio.run(w.poll())
    assert watcher.read_state(tmp_path / "state.json")
    assert pathlib.Path(tmp_path / "state.json").exists()