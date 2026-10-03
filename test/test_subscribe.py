"""The live connection, and the rule that makes it safe to have.

The important property isn't that events arrive — it's that the poll loop is
correct whether or not they do. A watcher with a working socket, a broken
socket, and no socket at all must all hand over every issue exactly once.
"""

import asyncio

import pytest
from conftest import RecordingHarness, router_for

from chord import subscribe, watcher
from chord.linear import LinearError


class StubLinear:
    """Hands over whatever it is given, and can be told to misbehave."""

    def __init__(self, issues):
        self._issues = issues
        self.polls = 0
        self.failed = False

    async def issues_for(self, filter):
        from chord.linear import IssuePage

        self.polls += 1
        if self.failed:
            raise LinearError("stub is broken")
        return IssuePage(self._issues, False)

    async def label_history(self, issue_id):
        return []

    async def comments(self, issue_id):
        return []


def issue(n):
    return {
        "id": str(n),
        "identifier": f"ENG-{n}",
        "title": f"Issue {n}",
        "description": f"do {n}",
        "createdAt": f"2026-01-{n:02d}T00:00:00Z",
    }


# --- the Subscription object ---


def test_available_reflects_the_install():
    assert subscribe.available() is True  # the `live` extra is installed here


def test_wait_times_out_when_never_started():
    """With no connection, `wait` is just the interval sleep."""
    sub = subscribe.Subscription("token")
    assert sub.connected is False
    assert asyncio.run(sub.wait(0.01)) is False


def test_an_event_wakes_the_waiter():
    async def scenario():
        sub = subscribe.Subscription("token")
        sub._wake()
        return await sub.wait(0.5)

    assert asyncio.run(scenario()) is False, "no connection means no events"

    async def connected():
        sub = subscribe.Subscription("token")
        sub._task = asyncio.create_task(asyncio.sleep(30))  # looks alive
        try:
            sub._wake()
            return await sub.wait(0.5)
        finally:
            sub._task.cancel()

    assert asyncio.run(connected()) is True


def test_wake_advances_the_generation():
    sub = subscribe.Subscription("token")
    before = sub._generation
    sub._wake()
    assert sub._generation > before


def test_a_second_event_is_not_lost_by_the_clear():
    """`wait` clears the event, so a stale one must not look like news."""

    async def scenario():
        sub = subscribe.Subscription("token")
        sub._task = asyncio.create_task(asyncio.sleep(30))
        sub._wake()
        # First wait consumes the pending event.
        assert await sub.wait(0.5) is True
        try:
            # With nothing new, the next wait must time out rather than return
            # True again off the back of the same event.
            return await sub.wait(0.01)
        finally:
            sub._task.cancel()

    assert asyncio.run(scenario()) is False


def test_stop_is_safe_when_never_started():
    sub = subscribe.Subscription("token")
    asyncio.run(sub.stop())  # must not hang or raise


def test_a_change_during_the_poll_is_not_discarded():
    """The version with a bare `event.clear()` threw this away: an event that
    arrived while the caller was busy polling was cleared before anybody read
    it, and the watcher sat out the rest of its interval for nothing.
    """

    async def scenario():
        sub = subscribe.Subscription("token")
        sub._task = asyncio.create_task(asyncio.sleep(30))
        try:
            # The caller was busy: a change lands before it starts waiting.
            sub._wake()
            return await sub.wait(0.5)
        finally:
            sub._task.cancel()

    assert asyncio.run(scenario()) is True


def test_wait_times_out_when_nothing_happens():
    async def scenario():
        sub = subscribe.Subscription("token")
        sub._task = asyncio.create_task(asyncio.sleep(30))
        try:
            return await sub.wait(0.02)
        finally:
            sub._task.cancel()

    assert asyncio.run(scenario()) is False


def test_problems_are_reported_to_the_callback():
    """A dead socket says so once, through whoever asked to hear it."""
    reported: list[str] = []

    async def scenario():
        sub = subscribe.Subscription("token")
        sub.report_problems_to(reported.append)
        # Force the connection attempt to fail immediately.
        sub._session = lambda: _raise(RuntimeError("no route to host"))
        await sub.start()
        await asyncio.sleep(0.2)
        await sub.stop()

    async def _raise(exc):
        raise exc

    asyncio.run(scenario())
    assert reported, "the failure was swallowed"
    assert "no route to host" in reported[0]


def test_a_problem_description_is_one_line():
    assert "\n" not in subscribe.describe(RuntimeError("a\nb"))


# --- the integration: the poll is still the source of truth ---


class FakeSubscription:
    """Stands in for the live connection, so the loop is testable offline."""

    def __init__(self, results):
        self._results = list(results)
        self.started = False
        self.stopped = False
        self.waited = 0

    def report_problems_to(self, callback):
        self._report = callback

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def wait(self, timeout):
        self.waited += 1
        if self._results:
            return self._results.pop(0)
        await asyncio.sleep(timeout)
        return False


def run_watcher(watcher_obj, ticks):
    async def scenario():
        task = asyncio.create_task(watcher_obj.run())
        for _ in range(ticks):
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_events_make_the_watcher_poll_sooner(tmp_path, capsys):
    """The point of the whole exercise: an event cuts the wait short."""
    stub = StubLinear([issue(1)])
    sub = FakeSubscription([True, True, True])
    w = watcher.Watcher(
        stub, router_for(), 60, tmp_path / "s.json", subscription=sub
    )

    run_watcher(w, ticks=12)

    assert stub.polls >= 4, f"only polled {stub.polls} times despite three events"
    assert sub.waited >= 4


def test_no_events_still_polls_on_the_interval(tmp_path):
    """The slow path has to work on its own."""
    stub = StubLinear([issue(1)])
    sub = FakeSubscription([])
    w = watcher.Watcher(
        stub, router_for(), 0.01, tmp_path / "s.json", subscription=sub
    )

    run_watcher(w, ticks=20)
    assert stub.polls >= 3, (
        f"the interval fallback stopped working ({stub.polls} polls)"
    )


def test_a_broken_socket_does_not_stop_the_poll_loop(tmp_path):
    """The failure mode that matters: no events, but work still gets done."""
    stub = StubLinear([issue(1), issue(2)])
    harness = RecordingHarness()

    class Dead:
        def report_problems_to(self, cb): ...

        async def start(self): ...

        async def stop(self): ...

        async def wait(self, timeout):
            await asyncio.sleep(0.005)
            return False  # never any news, ever

    w = watcher.Watcher(
        stub, router_for(harness), 60, tmp_path / "s.json", subscription=Dead()
    )
    run_watcher(w, ticks=14)

    assert len(harness.prompts) == 2, f"only offered {len(harness.prompts)} of 2"


def test_no_subscription_at_all_still_works(tmp_path):
    """Chord without the live extra has to be a complete product."""
    stub = StubLinear([issue(1)])
    harness = RecordingHarness()
    w = watcher.Watcher(stub, router_for(harness), 60, tmp_path / "s.json")

    run_watcher(w, ticks=10)
    assert len(harness.prompts) == 1


def test_each_issue_is_offered_once_with_events_firing(tmp_path):
    """A chatty socket must not cause repeated hand-overs."""
    stub = StubLinear([issue(1)])
    harness = RecordingHarness()
    sub = FakeSubscription([True] * 10)
    w = watcher.Watcher(
        stub, router_for(harness), 60, tmp_path / "s.json", subscription=sub
    )

    run_watcher(w, ticks=20)
    assert len(harness.prompts) == 1, f"offered {len(harness.prompts)} times"


def test_a_broken_linear_still_gets_reported_once(tmp_path, capsys):
    stub = StubLinear([])
    stub.failed = True
    w = watcher.Watcher(stub, router_for(), 60, tmp_path / "s.json")

    async def scenario():
        for _ in range(3):
            await w.poll()
            await asyncio.sleep(0)

    asyncio.run(scenario())
    assert capsys.readouterr().out.count("stub is broken") == 1


def test_subscription_problem_is_announced_with_the_fallback(tmp_path, capsys):
    """A person should learn that live updates aren't working, and that the
    watcher is still going."""
    stub = StubLinear([])
    w = watcher.Watcher(stub, router_for(), 45, tmp_path / "s.json")

    for _ in range(3):
        w.note_subscription_problem("connection refused")
    out = capsys.readouterr().out

    assert "live updates unavailable" in out
    assert "polling every 45s" in out
    assert out.count("live updates unavailable") == 1, "repeated the same complaint"
