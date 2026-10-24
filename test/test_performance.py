"""The performance instrumentation, measured by what it writes down.

The decorator exists to inform a rewrite decision, so a log line that is wrong
is worse than no log line at all: a function that absorbed its own failure and
was still reported as a success is a measurement that points the wrong way. The
line is routed through `chord.logging` rather than printed, so it obeys the
level setting and the one-line flattening like everything else.
"""

import asyncio
import functools

import pytest

from chord import performance


@pytest.fixture(autouse=True)
def debug_level(monkeypatch):
    """PERF lines are DEBUG, so these tests turn DEBUG on."""
    monkeypatch.setenv("CHORD_LOG_LEVEL", "DEBUG")


def test_perf_lines_are_quiet_by_default(capsys, monkeypatch):
    monkeypatch.delenv("CHORD_LOG_LEVEL")

    @performance.instrumented
    def work():
        return "done"

    work()
    assert perf_lines(capsys) == []


def perf_lines(capsys):
    return [line for line in capsys.readouterr().out.splitlines() if "PERF" in line]


def test_a_successful_call_is_measured_and_named(capsys):
    @performance.instrumented
    def work():
        return "done"

    assert work() == "done"
    (line,) = perf_lines(capsys)
    assert "file=test_performance.py" in line
    assert f"func={work.__module__}.{work.__qualname__}" in line
    assert "success=true" in line
    assert "duration_ms=" in line
    # Routed through the shared logging surface, not printed raw.
    assert "DEBUG" in line


def test_a_raising_call_is_logged_as_a_failure(capsys):
    @performance.instrumented
    def work():
        raise ValueError("no")

    with pytest.raises(ValueError):
        work()
    assert "success=false" in perf_lines(capsys)[0]


def test_a_false_return_is_the_outcome_when_the_function_says_so(capsys):
    """`poll` and the webhook handler answer failures rather than raise them."""

    @performance.instrumented
    def work():
        return False

    assert work() is False
    assert "success=false" in perf_lines(capsys)[0]


def test_a_true_return_is_a_success(capsys):
    @performance.instrumented
    def work():
        return True

    assert work() is True
    assert "success=true" in perf_lines(capsys)[0]


@pytest.mark.parametrize("value", [0, "", [], None])
def test_a_non_bool_return_is_not_read_as_an_outcome(capsys, value):
    """Only a real `bool` carries the answer; a falsy list is not a failure."""

    @performance.instrumented
    def work():
        return value

    work()
    assert "success=true" in perf_lines(capsys)[0]


def test_a_cancellation_is_a_clean_stop_and_still_travels(capsys):
    """`chord stop` cancels `Watcher.run`; that is not a failure to report.

    The line changes, but the exception must still reach whatever asked for the
    stop — swallowing it would make the watcher unstoppable.
    """

    @performance.instrumented
    async def work():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(work())
    assert "success=true" in perf_lines(capsys)[0]


def test_an_async_failure_is_logged_and_propagated(capsys):
    @performance.instrumented
    async def work():
        raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        asyncio.run(work())
    assert "success=false" in perf_lines(capsys)[0]


def test_a_caller_without_a_code_object_is_still_measured(capsys):
    """`_log` used `func.__code__` directly, which raised on anything without one.

    A partial or a callable object is not a plain function, and the decorator is
    applied by hand; crashing the instrumentation while measuring would be a
    worse failure than a blank file name.
    """
    performance._log(functools.partial(print), 0.0, True)
    (line,) = perf_lines(capsys)
    assert "file=?" in line
    assert "func=functools.partial" in line
    assert "success=true" in line


def test_the_wrapper_keeps_the_function_identity():
    @performance.instrumented
    def named():
        return 1

    assert named.__name__ == "named"
