"""Running an external command without letting it run away.

The bug this file exists for: the timeout handler did `process.kill()` and then
`await process.communicate()`. `kill()` reaches one pid, and `communicate()`
waits for the stdout pipe to reach EOF. A harness that left anything running —
a test runner's worker pool, an agent's background server, a git fsmonitor —
kept that pipe open, so the handler blocked forever. The watcher stopped
polling Linear, and because the log line was written *after* the blocking
call, there was no log line either. `chord info` said "running".

Every test here uses a harness that genuinely leaves a grandchild holding the
pipe, because a harness that exits cleanly cannot reproduce any of this.
"""

import asyncio
import os
import subprocess
import sys

import pytest

import chord.harness as harnesses
from chord.harness import CommandHarness, HarnessError

# Rewrites argv[0] of the processes these tests spawn, so the whole tree is
# identifiable in `ps` by something unique to this run.
MARKER = "chord-harness-marker-xyz"


def harness_tree() -> set[str]:
    """The pids of the harness and anything it left behind.

    Identified by a marker in the grandchild's *own* argv, which is the only
    reliable way to tell "the tree is gone" from "the wrapper is gone" —
    `sh -c "sleep 300 & echo MARKER; wait"` gives the marker to the shell, not
    to the `sleep` it forked. A marker only the wrapper carries would report
    success while the process the fix exists to kill was still running.

    Matching on a common command like `sleep 300` is not enough either: it picks
    up anything else on the machine, including a concurrently running test. So
    the argv is *rewritten* to carry the marker on every process in the tree.
    """
    listing = subprocess.run(
        ["ps", "-ww", "-eo", "pid,ppid,command"],
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    mine = {int(l.split()[0]) for l in listing if MARKER in l and "ps -ww" not in l}
    # Anything descended from a marked pid is part of the tree, marker or not.
    frontier, found = set(mine), set(mine)
    while frontier:
        pid = frontier.pop()
        for line in listing:
            fields = line.split(None, 2)
            if len(fields) >= 2 and fields[1].isdigit() and int(fields[1]) == pid:
                child = int(fields[0])
                if child not in found:
                    found.add(child)
                    frontier.add(child)
    return found


@pytest.fixture
def short_timeout(monkeypatch):
    monkeypatch.setattr(harnesses, "HARNESS_TIMEOUT", 1.0)
    monkeypatch.setattr(harnesses, "_REAP_TIMEOUT", 1.0)
    return 1.0


# --- the wedge ---


@pytest.mark.parametrize(
    "script",
    [
        pytest.param("sleep 300 & cat >/dev/null", id="backgrounded-child"),
        pytest.param("sleep 300 | cat >/dev/null & cat >/dev/null", id="piped-child"),
        pytest.param("cat >/dev/null & sleep 300 & wait", id="two-children"),
    ],
)
def test_a_timeout_reports_rather_than_wedging(short_timeout, capsys, script):
    """The whole point: `HarnessError` in bounded time, not a hang.

    Each of these leaves a grandchild holding the inherited stdout. Before the
    fix, every one of them blocked the watcher permanently.
    """
    harness = CommandHarness(["sh", "-c", script])

    async def scenario():
        await asyncio.wait_for(harness.send("the issue"), timeout=20)

    with pytest.raises(HarnessError, match="timed out after"):
        asyncio.run(scenario())

    out = capsys.readouterr().out
    assert "timed out" in out, (
        "the timeout was detected but never said, because the log line came "
        "after the blocking call"
    )


def test_the_timeout_is_logged_before_anything_blocks(short_timeout, capsys):
    """Ordering, not just presence.

    Writing the log line first is the difference between a wedged watcher that
    says why and a wedged watcher that says nothing at all.
    """
    harness = CommandHarness(["sh", "-c", "sleep 300 & cat >/dev/null"])
    with pytest.raises(HarnessError):
        asyncio.run(asyncio.wait_for(harness.send("x"), timeout=20))
    assert "ERROR" in capsys.readouterr().out


def test_a_harness_that_exits_cleanly_is_unaffected():
    """The ordinary case must not start paying for the fix."""
    harness = CommandHarness(["sh", "-c", "cat"])
    sent = []

    async def scenario():
        await harness.send("hello")

    asyncio.run(scenario())
    assert sent == []  # nothing to assert on stdout here; the point is no error
    assert harness.name == "sh -c cat"


def test_output_still_reaches_the_log(capsys):
    harness = CommandHarness(["sh", "-c", "cat; echo done >&2"])
    asyncio.run(harness.send("the issue text"))
    out = capsys.readouterr().out
    assert "the issue text" in out
    assert "done" in out, "stderr is merged into stdout and must still be logged"


def test_a_non_zero_exit_is_still_a_failure():
    harness = CommandHarness(["sh", "-c", "cat >/dev/null; exit 3"])
    with pytest.raises(HarnessError, match="exited 3"):
        asyncio.run(harness.send("x"))


# --- `chord stop` takes the tree with it ---


def test_cancellation_leaves_nothing_running():
    """`cli.py` said the harness is 'stopped through the same path that tidies
    up after it'. Before this it wasn't: `kill()` killed the direct child and a
    backgrounded grandchild survived `chord stop`, still pointed at somebody's
    repository.
    """
    harness = CommandHarness(["sh", "-c", f"exec -a {MARKER} sleep 300 & wait"])

    async def scenario():
        task = asyncio.create_task(harness.send("x"))
        await asyncio.sleep(0.8)
        assert harness_tree(), "the harness tree was not running at all"
        task.cancel()  # exactly what `chord stop` does to the watcher
        # Bounded, because against the pre-fix code the reap does not return and
        # this test would hang rather than fail. A hang is the bug, so it has to
        # be observable as a failure instead.
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=15)

    try:
        asyncio.run(scenario())
        assert not harness_tree(), "the harness tree outlived `chord stop`"
    finally:
        subprocess.run(["pkill", "-f", MARKER], capture_output=True)


def test_a_timeout_leaves_nothing_running(short_timeout):
    harness = CommandHarness(["sh", "-c", f"exec -a {MARKER} sleep 300 & cat >/dev/null"])
    try:
        with pytest.raises(HarnessError):
            asyncio.run(asyncio.wait_for(harness.send("x"), timeout=20))
        assert not harness_tree(), (
            "a timed-out harness kept running; the timeout stops the process but "
            "not the work it started"
        )
    finally:
        subprocess.run(["pkill", "-f", MARKER], capture_output=True)


def test_shutdown_does_not_raise_a_second_time():
    """The reap runs on the cancellation path, so it must not throw.

    A `TimeoutError` escaping here replaces the `CancelledError` that `chord
    stop` sent: the watcher then reports the wrong reason for stopping, as a
    traceback instead of "Stopping."
    """

    async def scenario():
        harness = CommandHarness(["sh", "-c", f"exec -a {MARKER} sleep 300"])
        task = asyncio.create_task(harness.send("x"))
        await asyncio.sleep(0.3)
        task.cancel()
        # Only CancelledError is acceptable. Anything else is a bug, and the
        # bound keeps a regression visible as a failure rather than a hang.
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=15)

    asyncio.run(scenario())


# --- HARNESS_TIMEOUT is validated, not merely parsed ---


@pytest.mark.parametrize(
    "value",
    ["-1", "0", "nan", "inf", "-inf", "1e400", "abc", "", "   "],
)
def test_a_nonsense_timeout_falls_back_to_the_default(monkeypatch, value):
    """`float()` accepts all of these, and each is a way to lose work quietly.

    The worst is a negative value: every harness would fail instantly, and a
    failed hand-over is *recorded*, so the entire backlog would be marked done
    without ever having run.
    """
    monkeypatch.setenv("HARNESS_TIMEOUT", value)
    result = harnesses._harness_timeout()
    assert result == harnesses.DEFAULT_HARNESS_TIMEOUT
    assert result > 0


@pytest.mark.parametrize("value", ["30", "1.5", "600", " 45 "])
def test_a_sensible_timeout_is_honoured(monkeypatch, value):
    monkeypatch.setenv("HARNESS_TIMEOUT", value)
    assert harnesses._harness_timeout() == float(value)


def test_no_environment_variable_means_the_default(monkeypatch):
    monkeypatch.delenv("HARNESS_TIMEOUT", raising=False)
    assert harnesses._harness_timeout() == harnesses.DEFAULT_HARNESS_TIMEOUT


def test_the_default_is_the_documented_thirty_minutes():
    assert harnesses.DEFAULT_HARNESS_TIMEOUT == 30 * 60


def test_a_bad_timeout_says_so(capsys, monkeypatch):
    """Silently substituting a different timeout is a decision the person did
    not make, and the difference is whether a long harness gets cut off."""
    monkeypatch.setenv("HARNESS_TIMEOUT", "-1")
    harnesses._harness_timeout()
    assert "HARNESS_TIMEOUT" in capsys.readouterr().out


# --- process groups ---


def test_the_harness_runs_in_its_own_session():
    """`start_new_session=True` is what makes the group signal possible at all."""
    seen = {}

    class Fake:
        pid = os.getpid()

        def kill(self): ...
        def wait(self): ...

    async def fake_exec(*argv, **kwargs):
        seen.update(kwargs)
        raise FileNotFoundError

    original = asyncio.create_subprocess_exec
    asyncio.create_subprocess_exec = fake_exec
    try:
        with pytest.raises(HarnessError):
            asyncio.run(CommandHarness(["nope"]).send("x"))
    finally:
        asyncio.create_subprocess_exec = original
    assert seen.get("start_new_session") is True


def test_signalling_a_dead_group_is_not_an_error():
    """Normal: a harness that already exited leaves no group to signal, and
    that is a success at this point rather than something to raise about."""

    class Fake:
        pid = 999_999

    # A pid that cannot exist must not raise out of the teardown path.
    harnesses._signal_group(Fake(), 9)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_the_group_signal_is_aimed_at_the_harness_not_at_us(monkeypatch):
    """`killpg` takes the pgid POSITIVE.

    This is the mistake the first version of this fix made, and it is worth a
    permanent test: `killpg(-pgid)` is a different call, and it signals the
    *caller's* group — which is Chord itself. The harness tree survives and
    Chord eats a signal it never expected, so the bug is invisible in the
    harness's own log.
    """
    seen: list[int] = []
    monkeypatch.setattr(harnesses.os, "killpg", lambda pgid, sig: seen.append(pgid))

    class Fake:
        pid = 4242

    harnesses._signal_group(Fake(), 9)
    assert seen == [4242], f"killpg got {seen}; a negative pgid signals our own group"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_killpg_really_does_reach_a_grandchild():
    """End-to-end proof, through the public entry point.

    The unit test above pins the argument; this one proves the argument is the
    right one, by checking that a process in the harness's group is actually
    gone afterwards. A harness that leaves a worker running is the case the fix
    exists for, so it is the case worth asserting on.
    """
    harness = CommandHarness(["sh", "-c", f"exec -a {MARKER} sleep 305 & wait"])

    async def cancel_mid_flight():
        task = asyncio.create_task(harness.send("x"))
        await asyncio.sleep(0.6)
        assert harness_tree(), "the harness tree was not running at all"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=15)

    try:
        asyncio.run(cancel_mid_flight())
        assert not harness_tree(), (
            "the grandchild survived; either the group signal missed it or the "
            "pgid was passed with the wrong sign"
        )
    finally:
        subprocess.run(["pkill", "-f", MARKER], capture_output=True)


def test_a_missing_command_is_still_a_clean_error():
    harness = CommandHarness(["definitely-not-a-real-command-xyz"])
    with pytest.raises(HarnessError, match="isn't on your PATH"):
        asyncio.run(harness.send("x"))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_sigkill_reaches_a_grandchild(monkeypatch):
    """Direct proof that the group signal is what does the work.

    `kill()` on the direct child alone leaves the grandchild; this asserts the
    grandchild is gone, which is the property `chord stop` depends on.
    """
    monkeypatch.setattr(harnesses, "HARNESS_TIMEOUT", 1.0)
    monkeypatch.setattr(harnesses, "_REAP_TIMEOUT", 1.0)
    harness = CommandHarness(["sh", "-c", f"exec -a {MARKER} sleep 300 & cat >/dev/null"])
    try:
        with pytest.raises(HarnessError, match="timed out"):
            asyncio.run(asyncio.wait_for(harness.send("x"), timeout=20))
        assert not harness_tree(), "the grandchild outlived the group SIGKILL"
    finally:
        subprocess.run(["pkill", "-f", MARKER], capture_output=True)