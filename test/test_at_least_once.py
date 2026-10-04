"""Chord's contract is at-least-once, and these tests are about the margin.

`README.md` says "Each issue is handed over once", and the mechanism is an id
recorded in the state file after the harness has run. So every instant between
"the agent has done the work" and "the id is on disk" is an instant in which the
same work can be done again. The contract is deliberate and stays: closing that
window entirely would mean recording *before* the run, which turns a possible
duplicate into a possible loss, and a possible loss of somebody's work is worse
than a possible duplicate of it.

So this file pins what can be closed without that trade, and pins the remaining
window as a documented limitation rather than pretending it is not there.

What is closed here:
  - a `BaseException` escaping the per-issue handler, which left the issue
    unrecorded and re-ran the work on every poll for the life of the process;
  - a state write that was atomic as a rename but never reached the device, so
    it did not survive losing power.

What is not, and cannot be:
  - a `SIGKILL` (or a power cut) between the harness returning and the record
    being written. Verified against the real thing below.
"""

import asyncio
import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys

import pytest
from conftest import router_for

from chord import watcher
from chord.linear import IssuePage


def issue(n=1):
    return {
        "id": str(n),
        "identifier": f"ENG-{n}",
        "title": f"Issue {n}",
        "description": "do it",
        "createdAt": "2026-01-01T00:00:00Z",
    }


class Stub:
    def __init__(self, issues=None):
        self.issues = issues if issues is not None else [issue()]

    async def issues_for(self, filter):
        return IssuePage(self.issues, False)

    async def label_history(self, issue_id):
        return []

    async def comments(self, issue_id):
        return []


class Counting:
    """A harness that counts how many times it was asked to do the work."""

    name = "counting"

    def __init__(self):
        self.runs = 0

    async def send(self, prompt):
        self.runs += 1


class Escapes(Counting):
    """Raises after doing the work, including things outside `Exception`."""

    name = "escaping"

    def __init__(self, exc=None):
        super().__init__()
        self.exc = exc or KeyboardInterrupt("power cut")

    async def send(self, prompt):
        await super().send(prompt)
        raise self.exc


def build(tmp_path, harness, issues=None):
    return watcher.Watcher(
        Stub(issues), router_for(harness), 1, tmp_path / "state.json"
    )


def poll(w, times=1):
    buf = io.StringIO()
    for _ in range(times):
        with contextlib.redirect_stdout(buf):
            try:
                asyncio.run(w.poll())
            except (KeyboardInterrupt, SystemExit):
                pass
    return buf.getvalue()


# --- cause 2: a BaseException escaping the per-issue handler ---


def test_a_keyboard_interrupt_does_not_re_run_the_work(tmp_path):
    """The regression.

    `poll()` catches `Exception`, so anything outside it skipped the per-issue
    handler and left the issue unrecorded. The same work was then handed to the
    agent again on every poll, for as long as the process lived.
    """
    harness = Escapes()
    w = build(tmp_path, harness)

    poll(w, times=5)

    assert harness.runs == 1, f"the work was repeated {harness.runs} times"
    assert w.handed_over == 1


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(KeyboardInterrupt(), id="keyboard-interrupt"),
        pytest.param(SystemExit(1), id="system-exit"),
        pytest.param(RuntimeError("ordinary"), id="not-a-base-exception"),
    ],
)
def test_anything_escaping_the_handler_records_the_issue(tmp_path, exc):
    harness = Escapes(exc)
    w = build(tmp_path, harness)
    out = poll(w, times=3)
    assert harness.runs == 1, out
    assert w.handed_over == 1, out


def test_the_exception_still_propagates(tmp_path):
    """Recording it must not swallow it.

    An exception that reached here is something the process needs to act on --
    that is the whole reason it is not an `Exception`. Swallowing it would leave
    a watcher that has stopped working while looking perfectly healthy.
    """
    harness = Escapes()
    w = build(tmp_path, harness)
    with contextlib.redirect_stdout(io.StringIO()), pytest.raises(KeyboardInterrupt):
        asyncio.run(w.poll())
    assert w.handed_over == 1


def test_cancellation_still_reaches_the_watcher(tmp_path):
    """`chord stop` must not be swallowed by the new guard.

    This is the one `BaseException` that is meant to travel, so it is checked
    explicitly rather than assumed.
    """
    class Cancels:
        name = "cancelling"

        async def send(self, prompt):
            raise asyncio.CancelledError

    w = build(tmp_path, Cancels())
    with (
        contextlib.redirect_stdout(io.StringIO()),
        pytest.raises(asyncio.CancelledError),
    ):
        asyncio.run(w.poll())


def test_a_harness_failure_is_unaffected(tmp_path):
    """The existing policy: a harness that is missing is recorded, and said."""
    from chord.harness import HarnessError

    class Missing:
        name = "missing"

        async def send(self, prompt):
            raise HarnessError("`nope` isn't on your PATH.")

    w = build(tmp_path, Missing())
    poll(w, times=3)
    assert w.handed_over == 1


# --- cause 3: the record itself ---


def test_the_record_is_written_atomically(tmp_path):
    """A reader never sees half a file, so it cannot conclude the daemon is idle."""
    harness = Counting()
    w = build(tmp_path, harness)
    poll(w)
    path = tmp_path / "state.json"
    assert json.loads(path.read_text())["handed_over"]
    assert not list(tmp_path.glob("*.tmp")), "a temporary file was left behind"


def test_the_record_reaches_the_device(tmp_path):
    """`replace` is atomic as a rename; it is not atomic as a write to disk.

    Without the flush, the bytes can sit in the page cache and be lost to a
    power cut, which costs exactly what a lost record costs: the work runs
    again. This asserts the call is made, since a real power cut is not
    something a test can stage.
    """
    calls = []
    real = os.fsync

    def spy(fd):
        calls.append(fd)
        return real(fd)

    harness = Counting()
    w = build(tmp_path, harness)
    original = os.fsync
    os.fsync = spy
    try:
        poll(w)
    finally:
        os.fsync = original

    assert calls, "the state file was replaced without being flushed to the device"
    assert w.handed_over == 1


def test_an_unwritable_state_file_does_not_stop_the_watcher(tmp_path, monkeypatch):
    """A disk that will not take the write must not stop the work.

    The in-memory set still de-duplicates within the process, so this costs at
    most one duplicate after a restart, which is the documented behaviour.
    """
    harness = Counting()
    path = tmp_path / "state.json"
    w = watcher.Watcher(Stub(), router_for(harness), 1, path)

    # The state file is written through `daemon._write_private`, which opens a
    # temporary file with `os.open` and renames it over the destination. Patched
    # at `os.open` because that is what it actually calls — patching `io.open`
    # was a leftover from before the write went private, and it silently tested
    # nothing: the file was written normally and the assertion below could not
    # fail.
    real_open = os.open

    def refuse(path, flags, *a, **k):
        if str(path).endswith(".tmp"):
            raise OSError(28, "No space left on device")
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(os, "open", refuse)
    out = poll(w, times=3)

    assert harness.runs == 1, "lost the in-memory record and re-ran the work"
    assert "couldn't record" in out, "the failure was not reported"
    assert not path.exists(), "a refused write should leave nothing behind"


# --- cause 1: the window that stays ---


SIGKILL_CHILD = """
import asyncio, json, os, pathlib, signal, sys
sys.path.insert(0, "src")
from chord import watcher
from chord.linear import IssuePage
from chord.routing import Router

ISSUE = {"id": "1", "identifier": "ENG-1", "title": "t", "description": "d",
         "createdAt": "2026-01-01T00:00:00Z"}

class Stub:
    async def issues_for(self, f): return IssuePage([ISSUE], False)
    async def label_history(self, i): return []
    async def comments(self, i): return []

class H:
    name = "h"
    async def send(self, prompt):
        print("WORK DONE", flush=True)
        os.kill(os.getpid(), signal.SIGKILL)

path = pathlib.Path(sys.argv[1])
asyncio.run(watcher.Watcher(Stub(), Router("Chord", "h", factory=lambda s: H()),
                            1, path).poll())
"""


@pytest.mark.slow
def test_a_kill_between_the_work_and_the_record_repeats_the_work(tmp_path):
    """The window that stays, pinned so nobody closes it by accident.

    There is no fix for this that does not change the contract. Recording
    before the run would mean an issue that never got worked on is never
    offered again, and losing somebody's work is worse than repeating it. So
    this asserts the duplicate, and `README.md` states it in as many words.

    A real `SIGKILL`, not a simulated one, because the interesting part is that
    nothing unwinds: no `finally`, no `atexit`, no flush of anything Chord was
    in the middle of.
    """
    script = tmp_path / "child.py"
    script.write_text(SIGKILL_CHILD)
    state = tmp_path / "state.json"
    root = pathlib.Path(__file__).resolve().parent.parent

    worked = 0
    for _ in range(3):
        done = subprocess.run(
            [sys.executable, str(script), str(state)],
            capture_output=True,
            text=True,
            cwd=root,
            timeout=60,
        )
        worked += "WORK DONE" in done.stdout

    assert worked == 3, "expected the work to be repeated every time"
    assert not state.exists(), "a killed process should have recorded nothing"


def test_the_limitation_is_written_down():
    """A contract that is not stated cannot be relied on."""
    readme = pathlib.Path(__file__).resolve().parent.parent / "README.md"
    text = readme.read_text().lower()
    assert "at-least-once" in text, (
        "README does not state the delivery guarantee, so 'handed over once' "
        "reads as exactly-once"
    )
    assert "duplicat" in text, "README does not mention the duplicate case"