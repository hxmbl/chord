"""Poll and hand-over behaviour, end to end with Linear stubbed out."""

import asyncio
import json
import subprocess

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


def build(
    tmp_path, linear_stub, harness_stub=None, harnesses=None, name="spy", start_dir=None
):
    return watcher.Watcher(
        linear_stub,
        router_for(harness_stub, harnesses=harnesses, name=name),
        1,
        tmp_path / "state.json",
        start_dir=start_dir,
    )


def git_repo(path):
    """A directory at `path` that `git` will work in."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    return path


def test_the_harness_is_given_the_project_root(tmp_path):
    """An issue that names no repository is worked on where Chord was started.

    The end-to-end version of the fallback: the directory the watcher resolved
    is the directory the harness was actually handed, not merely one mentioned
    in the log.
    """
    start = git_repo(tmp_path / "workspace")
    spy = RecordingHarness("spy")

    w = build(tmp_path, LinearStub([issue(1)]), spy, start_dir=start)
    asyncio.run(w.poll())

    assert spy.working_directories == [start]


def test_a_named_repository_reaches_the_harness(tmp_path):
    """A `[Chord_Working_Path:]` line moves the work, not just the log."""
    start = git_repo(tmp_path / "workspace")
    elsewhere = git_repo(tmp_path / "elsewhere" / "billing")
    spy = RecordingHarness("spy")

    stub = LinearStub([issue(1, description=f'[Chord_Working_Path: "{elsewhere}"]')])
    w = build(tmp_path, stub, spy, start_dir=start)
    asyncio.run(w.poll())

    assert spy.working_directories == [elsewhere.resolve()]


def test_the_project_title_moves_the_work(tmp_path):
    """A Linear project naming a directory beside the root."""
    start = git_repo(tmp_path / "workspace")
    billing = git_repo(start / "billing")
    spy = RecordingHarness("spy")

    stub = LinearStub([issue(1, project={"name": "billing"})])
    w = build(tmp_path, stub, spy, start_dir=start)
    asyncio.run(w.poll())

    assert spy.working_directories == [billing]


def test_a_wrong_repository_is_refused_and_retried(tmp_path, monkeypatch, banners):
    """A bad path stops the hand-over, and the issue is still retried.

    Not recorded, because nothing was handed over — and the missing directory
    is the kind of thing somebody fixes. Recording it would write the issue
    into `handed_over` and lose the work permanently, which is the one outcome
    a repository mistake must never cause.
    """
    start = plain = tmp_path / "workspace"
    plain.mkdir()
    spy = RecordingHarness("spy")
    said = banners

    stub = LinearStub(
        [issue(1, description=f'[Chord_Working_Path: "{tmp_path / "nope"}"]')]
    )
    w = build(tmp_path, stub, spy, start_dir=start)
    asyncio.run(w.poll())

    assert spy.prompts == []
    assert "isn't a Git rep" in said.bodies[-1]

    # Still offered on the next poll, once the directory exists.
    git_repo(tmp_path / "nope")
    asyncio.run(w.poll())

    assert len(spy.prompts) == 1, "the issue was not picked up again"
    assert "Issue 1" in spy.prompts[0]
    assert spy.working_directories == [(tmp_path / "nope").resolve()]


def test_a_refused_issue_never_touches_a_repository(tmp_path, monkeypatch, banners):
    """Authorisation is checked before Chord goes looking for the work.

    An issue nobody was allowed to request should not have its filesystem
    probed and should not produce a notice saying where Chord thought it was
    going. Both are things an unauthorised person can learn from.
    """
    start = git_repo(tmp_path / "workspace")
    spy = RecordingHarness("spy")
    said = banners

    stub = LinearStub(
        [issue(1, project={"name": "billing"})],
        history=[],
    )
    w = watcher.Watcher(
        stub,
        router_for(spy, label="Chord"),
        1,
        tmp_path / "state.json",
        start_dir=start,
    )
    w._authorise = _refuse
    asyncio.run(w.poll())

    assert spy.prompts == []
    assert spy.working_directories == [], "resolved a repository for a refused issue"
    assert all("repository" not in body for body in said.bodies)


async def _refuse(self, issue, identifier, issue_id, chosen):
    """An authorisation that says no, without touching state."""
    return False


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


def test_a_per_issue_problem_is_logged_once_not_every_poll(tmp_path, capsys):
    """The once-in-a-row rule covers problems raised *during* a healthy poll.

    `_poll` used to clear `_last_problem` on every poll that wasn't truncated,
    so a problem raised in the per-issue loop — here, a repository that isn't
    there — was logged again on every interval. That is exactly the log spam
    the once-only rule exists to prevent.
    """
    start = tmp_path / "workspace"
    start.mkdir()
    # A valid route label, so the repository problem is the only one raised and
    # the once-in-a-row comparison is actually exercised.
    stub = LinearStub(
        [
            issue(
                1,
                labels={"nodes": [{"name": "chord"}]},
                description=f'[Chord_Working_Path: "{tmp_path / "nope"}"]',
            )
        ]
    )
    w = build(tmp_path, stub, RecordingHarness("spy"), start_dir=start)

    for _ in range(3):
        asyncio.run(w.poll())

    # The message is truncated by `one_line`, so assert on a prefix that survives.
    assert capsys.readouterr().out.count("ENG-1 skipped") == 1


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

        async def send(self, prompt, cwd=None):
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


def test_state_trim_never_forgets_an_issue_still_in_the_page(tmp_path):
    """An id in the page being walked survives trimming, cap or no cap.

    Insertion order is hand-over order, and Linear does not order the page, so
    an issue handed over early can sit at the head of the dictionary while it is
    still in view. Trimming to the cap alone would evict it, and a later poll
    would offer the same work again although it never left the page.
    """
    stub = LinearStub([issue(1), issue(2), issue(3)])
    w = build(tmp_path, stub, RecordingHarness("spy"))
    original = watcher.STATE_CAP
    try:
        watcher.STATE_CAP = 2  # deliberately below MAX_ISSUES_PER_POLL
        asyncio.run(w.poll())

        saved = json.loads((tmp_path / "state.json").read_text())["handed_over"]
        assert set(saved) == {"1", "2", "3"}, (
            f"an in-page issue was evicted: {sorted(saved)}"
        )

        # Once an id is no longer in the page, the cap applies again.
        stub._issues = [issue(4)]
        asyncio.run(w.poll())
    finally:
        watcher.STATE_CAP = original

    saved = json.loads((tmp_path / "state.json").read_text())["handed_over"]
    assert set(saved) == {"3", "4"}, f"the cap stopped applying: {sorted(saved)}"


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
        with pytest.raises(linear.Unauthorised) as caught:
            asyncio.run(linear.Linear("token")._query("q {}", {}))
    finally:
        httpx.AsyncClient = original

    assert "401" in str(caught.value)
    assert "SECRET-BEARING-BODY" not in str(caught.value)
    # A refusal is its own type, but still a LinearError, so handling that
    # catches LinearError is unaffected.
    assert isinstance(caught.value, linear.LinearError)
    # The remedy is the watcher's to give, because only the watcher knows
    # whether a renewal is possible. Asserted in test_auth_expiry.py; this is
    # the classification, not the wording.
    assert "token" in str(caught.value)


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
