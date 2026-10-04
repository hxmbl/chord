"""What happens when Linear's answer is not what the query asked for.

Linear is the other end of this, and it is allowed to surprise us. These tests
are the boundary cases: a node that is not a record, a connection of the wrong
shape, a page that promises more and delivers nothing.

The bug this file exists for was a one-liner. `poll()` sorted the page with
`key=lambda i: i.get("createdAt") or ""` — a sort key evaluated *outside* the
per-issue handler, so a single `null` in `nodes` raised out of `poll()`, out of
`run()`, and killed the daemon. `poll()` documents that it never raises; there
is no supervisor and no restart, so an exception there means Chord stops
watching until a person notices and runs `chord start` again, and the only
trace is a traceback in a log file nobody was pointed at.
"""

import asyncio

import pytest

from chord import linear, watcher
from chord.linear import IssuePage, LinearError


class LinearStub:
    """Answers `issues_for` with whatever it is given, verbatim."""

    def __init__(self, issues):
        self._issues = issues

    async def issues_for(self, filter):
        return IssuePage(self._issues, False)

    async def label_history(self, issue_id):
        return []

    async def comments(self, issue_id):
        return []


class Exploding:
    """A paging layer that goes wrong in a way `_poll` cannot see."""

    def __init__(self, exc):
        self._exc = exc

    async def issues_for(self, filter):
        raise self._exc

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


def good(n=2):
    return {
        "id": str(n),
        "identifier": f"ENG-{n}",
        "title": "good",
        "description": "do it",
        "createdAt": "2026-01-02T00:00:00Z",
    }


def poll_once(tmp_path, payload, spy=None, lin=None):
    spy = spy if spy is not None else Spy()
    from chord.routing import Router

    router = Router("Chord", "spy", factory=lambda s: spy)
    client = lin if lin is not None else LinearStub(payload)
    return (
        watcher.Watcher(client, router, 1, tmp_path / "state.json"),
        spy,
    )


# --- poll() keeps its promise ---


@pytest.mark.parametrize(
    "junk",
    [None, "a string", 42, [], {"no": "id"}, True],
    ids=["null", "string", "int", "list", "missing-fields", "bool"],
)
def test_a_junk_node_does_not_kill_the_watcher(tmp_path, capsys, junk):
    """One unusable entry costs that entry, not the page and not the daemon."""
    spy = Spy()
    w, spy = poll_once(tmp_path, [junk, good()], spy)

    # A hang here is the bug, so the bound is part of the assertion.
    asyncio.run(asyncio.wait_for(w.poll(), timeout=10))

    assert len(spy.prompts) == 1, "the good issue behind the junk one was lost"
    assert w.handed_over == 1


def test_a_page_of_only_junk_still_completes(tmp_path, capsys):
    spy = Spy()
    w, spy = poll_once(tmp_path, [None, None, None], spy)
    asyncio.run(asyncio.wait_for(w.poll(), timeout=10))
    assert spy.prompts == []
    assert w.handed_over == 0


@pytest.mark.parametrize(
    "exc",
    [
        AttributeError("'NoneType' object has no attribute 'get'"),
        KeyError("nodes"),
        TypeError("unhashable type"),
        ValueError("bad json"),
        RuntimeError("a bug in the paging loop"),
    ],
)
def test_poll_survives_anything_the_paging_layer_throws(tmp_path, capsys, exc):
    """`poll()`'s guard has to be the outermost edge, not the request.

    A bug in the paging loop is not something a person can fix by waiting, but
    it is still not worth losing the watcher over.
    """
    w, _ = poll_once(tmp_path, None, lin=Exploding(exc))
    asyncio.run(asyncio.wait_for(w.poll(), timeout=10))

    out = capsys.readouterr().out
    assert "couldn't read Linear" in out
    assert type(exc).__name__ in out, "the reason has to name itself to be actionable"


def test_the_reason_is_reported_once_in_a_row(tmp_path, capsys):
    """A watcher runs for days; a log nobody reads is worse than no log."""
    w, _ = poll_once(tmp_path, None, lin=Exploding(RuntimeError("still broken")))
    for _ in range(3):
        asyncio.run(w.poll())
    assert capsys.readouterr().out.count("still broken") == 1


def test_cancellation_still_reaches_the_watcher_through_the_outer_guard(tmp_path):
    """The outer `except Exception` must not swallow how `chord stop` works."""

    class Cancel:
        async def issues_for(self, filter):
            raise asyncio.CancelledError

        async def label_history(self, issue_id):
            return []

        async def comments(self, issue_id):
            return []

    w, _ = poll_once(tmp_path, None, lin=Cancel())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(w.poll())


def test_older_issues_still_go_first(tmp_path, capsys):
    """The sort key had to survive being a function."""
    spy = Spy()
    issues = [
        {"id": "3", "identifier": "E-3", "title": "c", "description": "d",
         "createdAt": "2026-03-01T00:00:00Z"},
        {"id": "1", "identifier": "E-1", "title": "a", "description": "d",
         "createdAt": "2026-01-01T00:00:00Z"},
        None,
        {"id": "2", "identifier": "E-2", "title": "b", "description": "d",
         "createdAt": "2026-02-01T00:00:00Z"},
    ]
    w, spy = poll_once(tmp_path, issues, spy)
    asyncio.run(asyncio.wait_for(w.poll(), timeout=10))
    assert [p.split()[1] for p in spy.prompts] == ["E-1", "E-2", "E-3"]


def test_an_issue_with_no_timestamp_is_still_handed_over(tmp_path, capsys):
    """A missing `createdAt` sorts first rather than being dropped."""
    spy = Spy()
    w, spy = poll_once(tmp_path, [{"id": "9", "identifier": "E-9", "title": "t",
                                   "description": "d"}], spy)
    asyncio.run(asyncio.wait_for(w.poll(), timeout=10))
    assert len(spy.prompts) == 1


# --- the boundary: what reaches Chord is a record or nothing ---


def _client(monkeypatch, payload):
    client = linear.Linear("token")

    async def fake(query, variables):
        return payload

    monkeypatch.setattr(client, "_query", fake)
    return client


@pytest.mark.parametrize(
    "junk", [None, "a string", 42, True], ids=["null", "string", "int", "bool"]
)
def test_unusable_nodes_are_dropped_at_the_boundary(monkeypatch, junk):
    """The check belongs where the data enters, not at each call site."""
    client = _client(
        monkeypatch,
        {"issues": {"nodes": [junk, good()], "pageInfo": {"hasNextPage": False}}},
    )
    page = asyncio.run(client.issues_for({"or": []}))
    assert [i["id"] for i in page.issues] == ["2"]
    assert page.truncated is False


def test_a_dropped_node_is_said_out_loud(monkeypatch, capsys):
    """Silently discarding data means the issue simply never arrives.

    That is the failure this whole exercise is about: an issue that vanishes
    with nothing in the log to explain it.
    """
    client = _client(
        monkeypatch,
        {"issues": {"nodes": [None, good()], "pageInfo": {"hasNextPage": False}}},
    )
    asyncio.run(client.issues_for({"or": []}))
    assert "unreadable" in capsys.readouterr().out


def test_nodes_that_is_not_a_list_is_an_empty_page(monkeypatch):
    client = _client(
        monkeypatch,
        {"issues": {"nodes": None, "pageInfo": {"hasNextPage": False}}},
    )
    assert asyncio.run(client.issues_for({"or": []})).issues == []


@pytest.mark.parametrize(
    "payload", [{"issues": "nope"}, {"issues": 42}, {"issues": ["a"]}]
)
def test_a_connection_of_the_wrong_shape_is_reported(monkeypatch, payload):
    """Better a loud failure than a silently empty page.

    Treating a malformed connection as "no issues" means every issue in the
    workspace is quietly skipped, and the log says the backlog is done.
    """
    client = _client(monkeypatch, payload)
    with pytest.raises(LinearError, match="wrong shape"):
        asyncio.run(client.issues_for({"or": []}))


def test_a_missing_pageinfo_ends_the_walk_instead_of_looping(monkeypatch):
    """No pageInfo means no cursor, so there is nowhere to ask for more."""
    client = _client(monkeypatch, {"issues": {"nodes": [good()]}})
    page = asyncio.run(client.issues_for({"or": []}))
    assert len(page.issues) == 1
    assert page.truncated is False


# --- a connection that lies about having more ---


def test_an_endless_connection_terminates(monkeypatch, capsys):
    """`hasNextPage: true` forever, with a cursor each time and nothing in it.

    The walk's budget counts nodes, and here nodes is what never arrives — so
    the node budget alone cannot end it. Before the page ceiling this asked
    Linear forever, once every 30ms or so, from a `comments()` call that nothing
    wraps in a timeout. The reply to a question that is never answered is to
    stop asking it.
    """
    calls = {"n": 0}

    async def fake(query, variables):
        calls["n"] += 1
        return {
            "issues": {
                "nodes": [],
                "pageInfo": {"hasNextPage": True, "endCursor": f"c{calls['n']}"},
            }
        }

    client = linear.Linear("token")
    monkeypatch.setattr(client, "_query", fake)

    nodes, truncated = asyncio.run(client._paged("q", "issues", {}, 1000))
    assert nodes == []
    assert truncated is True
    assert calls["n"] == linear.MAX_PAGES_PER_WALK
    assert "stopped reading" in capsys.readouterr().out


def test_a_cursor_that_repeats_itself_terminates(monkeypatch):
    """Same cursor, same empty page, forever. Two requests, then stop."""
    calls = {"n": 0}

    async def fake(query, variables):
        calls["n"] += 1
        return {
            "issues": {
                "nodes": [],
                "pageInfo": {"hasNextPage": True, "endCursor": "same-cursor"},
            }
        }

    client = linear.Linear("token")
    monkeypatch.setattr(client, "_query", fake)
    _, truncated = asyncio.run(client._paged("q", "issues", {}, 1000))
    assert calls["n"] == 2, "asked for the same page again after the cursor repeated"
    assert truncated is True


def test_an_endless_comment_connection_cannot_wedge_the_watcher(monkeypatch):
    """`comments()` has no timeout around it, so this is the one that matters.

    An issue whose discussion never finishes being read took the whole watcher
    with it, on the first such issue, with no log line.
    """
    async def fake(query, variables):
        return {
            "issue": {
                "comments": {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": True, "endCursor": "always-more"},
                }
            }
        }

    client = linear.Linear("token")
    monkeypatch.setattr(client, "_query", fake)
    assert asyncio.run(asyncio.wait_for(client.comments("x"), timeout=30)) == []


def test_a_normal_walk_is_unaffected_by_the_ceilings(monkeypatch):
    """The page ceiling must not fire on real data."""
    pages = [
        ([good(n) for n in range(1, 4)], True, "c1"),
        ([good(n) for n in range(4, 7)], True, "c2"),
        ([good(7)], False, None),
    ]
    calls = {"n": 0}

    async def fake(query, variables):
        nodes, more, cursor = pages[calls["n"]]
        calls["n"] += 1
        return {
            "issues": {"nodes": nodes,
                       "pageInfo": {"hasNextPage": more, "endCursor": cursor}}
        }

    client = linear.Linear("token")
    monkeypatch.setattr(client, "_query", fake)
    page = asyncio.run(client.issues_for({"or": []}))
    assert len(page.issues) == 7
    assert page.truncated is False
    assert calls["n"] == 3


# --- the GraphQL envelope ---


class Response:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def query_with(monkeypatch, payload, status=200):
    class Client:
        def __init__(self, *a, **k): ...

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return Response(payload, status)

    monkeypatch.setattr(linear.httpx, "AsyncClient", Client)
    return asyncio.run(linear.Linear("token")._query("query {}", {}))


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"errors": [{"message": "boom"}]}, "turned down"),
        ({"data": None}, "no data"),
        ({"data": [1, 2]}, "no data"),
        ({"nothing": "here"}, "no data"),
    ],
)
def test_the_envelope_is_validated(monkeypatch, payload, expected):
    with pytest.raises(LinearError, match=expected):
        query_with(monkeypatch, payload)


@pytest.mark.parametrize(
    "payload",
    [["not", "an", "object"], "a string", 42, None],
    ids=["list", "string", "int", "null"],
)
def test_a_body_that_is_not_an_object_is_reported(monkeypatch, payload):
    """GraphQL answers 200 with whatever it likes; the shape is not guaranteed.

    `body.get(...)` on a list raises `AttributeError`, which used to escape as
    an internal error on the poll path — and, on the per-issue path, as an issue
    that is never delivered and never recorded.
    """
    with pytest.raises(LinearError):
        query_with(monkeypatch, payload)


@pytest.mark.parametrize(
    "payload",
    [{"errors": "a string"}, {"errors": {"message": "x"}}, {"errors": ["boom"]},
     {"errors": [None]}, {"errors": [{"message": None}]}],
    ids=["errors-string", "errors-dict", "errors-list-of-str", "errors-null",
         "errors-no-message"],
)
def test_a_malformed_errors_list_is_reported(monkeypatch, payload):
    """`errors[0].get("message")` on a string or a dict raises `AttributeError`
    or `KeyError`."""
    with pytest.raises(LinearError):
        query_with(monkeypatch, payload)


def test_a_real_error_message_is_still_reported_in_full(monkeypatch, capsys):
    """Validating the envelope must not lose the message when it is there."""
    with pytest.raises(LinearError, match="the query was malformed"):
        query_with(monkeypatch, {"errors": [{"message": "the query was malformed"}]})


def test_a_hostile_error_message_is_still_flattened(monkeypatch):
    """The status-and-detail rule stays: no body echoed, escapes removed."""
    hostile = "\x1b[2J\x1b[H forged log line"
    with pytest.raises(LinearError) as caught:
        query_with(monkeypatch, {"errors": [{"message": hostile}]})
    assert "\x1b" not in str(caught.value)


def test_a_non_json_body_is_still_reported(monkeypatch):
    with pytest.raises(LinearError, match="isn't JSON"):
        query_with(monkeypatch, ValueError("no json"))


def test_an_error_message_from_linear_is_reported(monkeypatch):
    """Linear's own explanation is the useful half of a failed query.

    It is echoed, because a query Chord cannot fix is diagnosed from the message
    and the alternative is "no detail given" for every failure. What is *not*
    echoed is Chord's own request — which is where the bearer token would be.
    """
    with pytest.raises(LinearError) as caught:
        query_with(monkeypatch, {"errors": [{"message": "Unknown field 'nope'."}]})
    assert "Unknown field 'nope'." in str(caught.value)


def test_a_message_containing_a_token_is_not_the_request_being_echoed(monkeypatch):
    """The rule being pinned precisely, so a future change can be checked.

    A message that happens to mention a bearer token is Linear's text, and
    Linear is read-only — there is no token for it to leak. What must never
    happen is Chord putting its own `Authorization` header into the message.
    """
    with pytest.raises(LinearError) as caught:
        query_with(monkeypatch, {"errors": [{"message": "invalid bearer token"}]})
    assert "invalid bearer token" in str(caught.value)


def test_the_authorization_header_never_reaches_an_error_message(monkeypatch):
    """The real invariant: the token lives in one place and stays there.

    `_headers` is the only holder of it, and no failure path interpolates it.
    """
    client = linear.Linear("super-secret-token")
    assert client._headers["Authorization"] == "Bearer super-secret-token"
    with pytest.raises(LinearError) as caught:
        query_with(monkeypatch, {"errors": [{"message": "denied"}]})
    assert "super-secret-token" not in str(caught.value)


def test_a_missing_issue_is_empty_rather_than_an_error(monkeypatch):
    """`issue: null` is how Linear says no such issue — a real, common answer."""
    client = _client(monkeypatch, {"issue": None})
    assert asyncio.run(client.comments("gone")) == []


def test_a_missing_issue_key_is_empty_too(monkeypatch):
    client = _client(monkeypatch, {})
    assert asyncio.run(client.comments("gone")) == []

# --- the limit is enforced, not requested ---


def test_a_page_that_ignores_first_cannot_exceed_the_limit(monkeypatch):
    """`limit` is Chord's ceiling, so it is applied rather than asked for.

    `first` tells Linear how many to send, and a server that ignores it returns
    whatever it likes. Verified against the pre-fix code, where a page of 50
    against a limit of 10 produced 50 nodes — so `MAX_ISSUES_PER_POLL` was a
    suggestion, and `truncated` (which is what makes the watcher tell a person
    their backlog is capped) was reporting on a limit that had been passed.
    """
    calls = {"n": 0}

    async def greedy(query, variables):
        calls["n"] += 1
        return {
            "issues": {
                "nodes": [{"id": str(i), "createdAt": "2026-01-01"} for i in range(50)],
                "pageInfo": {"hasNextPage": True, "endCursor": "always-more"},
            }
        }

    client = linear.Linear("token")
    monkeypatch.setattr(client, "_query", greedy)
    nodes, truncated = asyncio.run(client._paged("q", "issues", {}, 10))

    assert len(nodes) == 10, f"got {len(nodes)} against a limit of 10"
    assert truncated is True, "a capped page must say it was capped"
    assert calls["n"] == 1, "should not have asked again once the limit was met"


def test_the_limit_is_enforced_across_pages_not_just_within_one(monkeypatch):
    """A walk that ends up over the limit must be cut back, not just a page."""
    calls = {"n": 0}

    async def chatty(query, variables):
        calls["n"] += 1
        return {
            "issues": {
                "nodes": [{"id": f"{calls['n']}-{i}"} for i in range(8)],
                "pageInfo": {"hasNextPage": True, "endCursor": f"c{calls['n']}"},
            }
        }

    client = linear.Linear("token")
    monkeypatch.setattr(client, "_query", chatty)
    nodes, truncated = asyncio.run(client._paged("q", "issues", {}, 12))

    assert len(nodes) == 12, f"got {len(nodes)} against a limit of 12"
    assert truncated is True
    assert calls["n"] >= 2, "should have needed more than one page to reach the limit"


def test_asking_for_exactly_what_is_left_asks_for_nothing_next_time(monkeypatch):
    """The `first` argument must shrink, or the query is nonsense on the last page."""
    asked = []

    async def record(query, variables):
        asked.append(variables["first"])
        return {
            "issues": {
                "nodes": [{"id": "1"}, {"id": "2"}, {"id": "3"}],
                "pageInfo": {"hasNextPage": True, "endCursor": "c"},
            }
        }

    client = linear.Linear("token")
    monkeypatch.setattr(client, "_query", record)
    asyncio.run(client._paged("q", "issues", {}, 7))

    # Each request asks only for what is still needed. They do not have to add
    # up to the limit, because the server is free to send fewer than it was
    # asked for -- here it sends 3 each time, so the walk stops after the third
    # page with 6 of 7. What matters is that no request ever asks for more than
    # is left, which is what would make the query nonsensical on the last page.
    assert asked[0] == 7, "the first page should ask for the whole limit"
    assert all(1 <= n <= 7 for n in asked), f"asked for {asked}"
    assert asked == sorted(asked, reverse=True), f"never asked for more later: {asked}"


def test_a_normal_multi_page_walk_is_untouched(monkeypatch):
    """The enforcement must not cost anything on well-behaved data."""
    pages = [
        (["1", "2", "3"], True, "c1"),
        (["4", "5", "6"], True, "c2"),
        (["7"], False, None),
    ]
    calls = {"n": 0}

    async def ok(query, variables):
        nodes, more, cursor = pages[calls["n"]]
        calls["n"] += 1
        return {
            "issues": {
                "nodes": [{"id": n} for n in nodes],
                "pageInfo": {"hasNextPage": more, "endCursor": cursor},
            }
        }

    client = linear.Linear("token")
    monkeypatch.setattr(client, "_query", ok)
    page = asyncio.run(client.issues_for({"or": []}))

    assert [i["id"] for i in page.issues] == ["1", "2", "3", "4", "5", "6", "7"]
    assert page.truncated is False
    assert calls["n"] == 3
