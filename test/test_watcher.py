"""Poll and hand-over behaviour, end to end with Linear stubbed out."""

import asyncio
import json

import pytest
from conftest import RecordingHarness, router_for

from chord import harness as harnesses
from chord import linear, watcher
from chord.linear import IssuePage


class LinearStub:
    def __init__(
        self, issues, comments=None, error=None, delay=0, truncated=False, history=None
    ):
        self._issues = issues
        self._truncated = truncated
        self._comments = comments or []
        self._error = error
        self._delay = delay
        self._history = history or []
        self.asked_for = []
        self.history_asked_for = []

    async def issues_for(self, filter):
        self.asked_for.append(filter)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        return IssuePage(self._issues, self._truncated)

    async def label_history(self, issue_id):
        self.history_asked_for.append(issue_id)
        if isinstance(self._history, Exception):
            raise self._history
        return list(self._history)

    async def comments(self, issue_id):
        if isinstance(self._comments, Exception):
            raise self._comments
        return self._comments


def issue(n, **extra):
    return {
        "id": str(n),
        "identifier": f"ENG-{n}",
        "title": f"Issue {n}",
        "description": f"do {n}",
        "createdAt": f"2026-01-0{n}T00:00:00Z",
        **extra,
    }


def build(tmp_path, linear_stub, harness_stub=None, harnesses=None, name="spy"):
    return watcher.Watcher(
        linear_stub,
        router_for(harness_stub, harnesses=harnesses, name=name),
        1,
        tmp_path / "state.json",
    )


def test_hands_over_each_new_issue(tmp_path):
    spy = RecordingHarness("spy")
    w = build(tmp_path, LinearStub([issue(1), issue(2)]), spy)
    asyncio.run(w.poll())
    assert len(spy.prompts) == 2
    assert "do 1" in spy.prompts[0]


def test_backlog_goes_oldest_first(tmp_path):
    spy = RecordingHarness("spy")
    unordered = [issue(3), issue(1), issue(2)]
    w = build(tmp_path, LinearStub(unordered), spy)
    asyncio.run(w.poll())
    assert "ENG-1" in spy.prompts[0]
    assert "ENG-3" in spy.prompts[2]


def test_an_issue_is_not_offered_twice(tmp_path):
    spy = RecordingHarness("spy")
    stub = LinearStub([issue(1)])
    w = build(tmp_path, stub, spy)
    asyncio.run(w.poll())
    asyncio.run(w.poll())
    assert len(spy.prompts) == 1


def test_state_is_written_to_disk(tmp_path):
    stub = LinearStub([issue(1)])
    w = build(tmp_path, stub, RecordingHarness("spy"))
    asyncio.run(w.poll())
    saved = json.loads((tmp_path / "state.json").read_text())
    assert "1" in saved["handed_over"]


def test_state_survives_a_restart(tmp_path):
    """The reason state is on disk rather than in the process."""
    asyncio.run(build(tmp_path, LinearStub([issue(1)]), RecordingHarness("spy")).poll())

    spy = RecordingHarness("spy")
    w = build(tmp_path, LinearStub([issue(1)]), spy)
    assert w.handed_over == 1
    asyncio.run(w.poll())
    assert spy.prompts == [], "a restart re-offered the backlog"


def test_linear_failure_is_reported_and_survived(tmp_path, capsys):
    error = linear.LinearError("Linear is down")
    w = build(tmp_path, LinearStub([], error=error), RecordingHarness("spy"))
    asyncio.run(w.poll())
    assert "Linear is down" in capsys.readouterr().out


def test_a_repeated_failure_is_logged_once(tmp_path, capsys):
    """A watcher runs for days; a log nobody reads is worse than no log."""
    stub = LinearStub([], error=linear.LinearError("still down"))
    w = build(tmp_path, stub, RecordingHarness("spy"))
    asyncio.run(w.poll())
    asyncio.run(w.poll())
    asyncio.run(w.poll())
    assert capsys.readouterr().out.count("still down") == 1


def test_a_missing_discussion_still_hands_over(tmp_path, capsys):
    spy = RecordingHarness("spy")
    stub = LinearStub([issue(1)], comments=linear.LinearError("no comments"))
    asyncio.run(build(tmp_path, stub, spy).poll())
    assert len(spy.prompts) == 1
    assert "no discussion" in capsys.readouterr().out


def test_discussion_reaches_the_harness(tmp_path):
    spy = RecordingHarness("spy")
    stub = LinearStub(
        [issue(1)],
        comments=[
            {"body": "here's why", "user": {"name": "Ana"}, "createdAt": "2026-01-01"}
        ],
    )
    asyncio.run(build(tmp_path, stub, spy).poll())
    assert "here's why" in spy.prompts[0]
    assert "Ana" in spy.prompts[0]


def test_a_missing_harness_is_recorded_anyway(tmp_path, capsys):
    """So a broken harness doesn't wedge the queue behind it forever."""

    class Broken:
        name = "broken"

        async def send(self, prompt):
            raise harnesses.HarnessError("`nope` isn't on your PATH.")

    stub = LinearStub([issue(1), issue(2)])
    w = build(tmp_path, stub, Broken())
    asyncio.run(w.poll())
    assert "didn't finish" in capsys.readouterr().out
    assert w.handed_over == 2, "the queue was blocked behind the broken harness"


def test_issue_with_no_id_is_skipped_not_re_offered(tmp_path, capsys):
    spy = RecordingHarness("spy")
    stub = LinearStub([{"identifier": "ENG-9", "title": "no id"}])
    w = build(tmp_path, stub, spy)
    asyncio.run(w.poll())
    assert spy.prompts == [], "an unrecordable issue was handed over anyway"
    assert "no id from Linear" in capsys.readouterr().out


def test_run_logs_then_polls(tmp_path, capsys):
    w = build(tmp_path, LinearStub([issue(1)]), RecordingHarness("spy"))

    async def once():
        task = asyncio.create_task(w.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(once())
    out = capsys.readouterr().out
    assert "Watching Linear for issues labelled 'chord'" in out
    assert "Handing each one to spy" in out


def test_poll_timeout_is_reported(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(watcher, "POLL_TIMEOUT", 0.01)
    w = build(tmp_path, LinearStub([issue(1)], delay=1), RecordingHarness("spy"))
    asyncio.run(w.poll())
    assert "didn't answer" in capsys.readouterr().out


def test_state_trim_keeps_the_newest(tmp_path):
    """A still-labelled issue must not reappear just because the file got long."""

    stub = LinearStub([issue(1)])
    w = build(tmp_path, stub, RecordingHarness("spy"))
    monkeypatch_cap = watcher.STATE_CAP
    try:
        watcher.STATE_CAP = 3
        for n in range(1, 8):
            stub._issues = [issue(n)]
            asyncio.run(w.poll())
    finally:
        watcher.STATE_CAP = monkeypatch_cap

    saved = json.loads((tmp_path / "state.json").read_text())["handed_over"]
    assert len(saved) == 3
    assert set(saved) == {"5", "6", "7"}, f"kept the wrong entries: {sorted(saved)}"


# --- linear ---


def test_linear_rejects_an_expired_token():
    """The failure has to stay a status code, never a body."""
    import httpx

    class Response:
        status_code = 401
        text = "SECRET-BEARING-BODY"

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
        with pytest.raises(linear.LinearError) as caught:
            asyncio.run(linear.Linear("token")._query("q {}", {}))
    finally:
        httpx.AsyncClient = original

    assert "401" in str(caught.value)
    assert "chord refresh" in str(caught.value)
    assert "SECRET-BEARING-BODY" not in str(caught.value)


def test_linear_survives_non_json():
    import httpx

    class Response:
        status_code = 200

        def json(self):
            raise ValueError("no json")

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
        with pytest.raises(linear.LinearError, match="isn't JSON"):
            asyncio.run(linear.Linear("t")._query("q {}", {}))
    finally:
        httpx.AsyncClient = original
