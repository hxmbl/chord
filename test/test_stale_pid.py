"""A stale pid file must not be able to wedge `chord start`.

`running_pid()` is a *liveness* check on purpose — `chord info` calls it
constantly, and a subprocess each time would be a poor trade for a more precise
answer. That trade is defensible for a status line and much less defensible for
the command you type when you want Chord to work.

Verified against the pre-fix code: a pid file naming a live process that is not
a Chord watcher — a recycled id after a reboot, the ordinary way this happens —
made `start` refuse with `Already running (pid N)`, where N belongs to somebody
else. `stop` clears the file correctly, so the recovery exists; it is just not
something a person discovers while trying to get Chord running.
"""

import subprocess
import sys
import time

import pytest

from chord import daemon


@pytest.fixture
def session(tmp_path, monkeypatch):
    home = tmp_path / "sessions" / "testproj"
    monkeypatch.setattr(daemon, "PROJECT_HOME", home)
    monkeypatch.setattr(daemon, "LOG_FILE", home / "chord.log")
    monkeypatch.setattr(daemon, "PID_FILE", home / "chord.pid")
    monkeypatch.setattr(daemon, "LOCK_FILE", home / "chord.lock")
    home.mkdir(parents=True, exist_ok=True)
    return home


_SPAWNED: list = []


def spawn(argv):
    process = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    _SPAWNED.append(process)
    time.sleep(0.4)
    return process


@pytest.fixture(autouse=True)
def reap_spawned():
    """Nothing this file starts outlives it, even when an assertion fails first.

    These tests spawn real processes on purpose, and a `finally` is skipped the
    moment the assertion before it raises — which would leave a detached watcher
    behind for every failure, making the next run's machine worse.
    """
    yield
    for process in _SPAWNED:
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    _SPAWNED.clear()


def plant(session, pid):
    daemon.PID_FILE.write_text(f"{pid}\n{time.time()}\n")


def test_a_stale_file_naming_a_stranger_does_not_block_start(session, spawn_watcher):
    """The regression.

    `start` is not a status line, so it can afford the identity check that
    `running_pid` skips.
    """
    stranger = spawn(["sleep", "120"])
    try:
        plant(session, stranger.pid)
        assert daemon.running_pid() == stranger.pid, "the liveness check should see it"

        pid = daemon.start()
        assert pid == FakeProcess.pid
    finally:
        if stranger.poll() is None:
            stranger.kill()
        stranger.wait()


class FakeProcess:
    """Just enough of a `Popen` for `start` to write a pid file.

    Patching `subprocess.Popen` would also catch the subprocesses pytest spawns
    for its own output capture, which need a real process with `communicate` and
    `kill`. `daemon` spawns through a single module attribute for that reason,
    so patching that one attribute leaves the rest of the world alone.
    """

    pid = 424242

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def spawn_watcher(monkeypatch):
    """Make `daemon.start` spawn a `FakeProcess` and nothing else."""

    def fake_popen(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(daemon, "_Popen", fake_popen, raising=False)


def test_start_clears_the_stale_file_and_replaces_it(session, spawn_watcher):
    stranger = spawn(["sleep", "120"])
    try:
        plant(session, stranger.pid)
        daemon.start()
        assert daemon.PID_FILE.read_text().splitlines()[0] == str(FakeProcess.pid)
    finally:
        if stranger.poll() is None:
            stranger.kill()
        stranger.wait()


def test_the_stranger_is_left_alone(session, spawn_watcher):
    """The file is cleared. The process is not touched."""
    stranger = spawn(["sleep", "120"])
    try:
        plant(session, stranger.pid)
        daemon.start()
        assert stranger.poll() is None, "start signalled a process that is not ours"
    finally:
        if stranger.poll() is None:
            stranger.kill()
        stranger.wait()


def test_a_real_watcher_is_still_refused_a_second_start(session):
    """The check must not become so eager that it lets two watchers run."""
    watcher = spawn([sys.executable, "-m", "chord.cli", daemon.SERVE_COMMAND])
    try:
        plant(session, watcher.pid)
        assert daemon.is_our_process(watcher.pid) is True
        with pytest.raises(daemon.AlreadyRunning) as caught:
            daemon.start()
        assert caught.value.pid == watcher.pid
    finally:
        if watcher.poll() is None:
            watcher.kill()
        watcher.wait()


def test_the_pid_file_survives_a_correct_refusal(session):
    """Refusing must not clear the file of a watcher that is genuinely running."""
    watcher = spawn([sys.executable, "-m", "chord.cli", daemon.SERVE_COMMAND])
    try:
        plant(session, watcher.pid)
        with pytest.raises(daemon.AlreadyRunning):
            daemon.start()
        assert daemon.PID_FILE.exists()
        assert daemon.PID_FILE.read_text().splitlines()[0] == str(watcher.pid)
    finally:
        if watcher.poll() is None:
            watcher.kill()
        watcher.wait()


def test_running_pid_stays_a_liveness_check(session):
    """The cheap check `chord info` relies on is deliberately unchanged.

    This is the tradeoff being made explicitly: one pid read for the status
    line, and an identity check only where a wrong answer costs something.
    """
    stranger = spawn(["sleep", "60"])
    try:
        plant(session, stranger.pid)
        assert daemon.running_pid() == stranger.pid
    finally:
        if stranger.poll() is None:
            stranger.kill()
        stranger.wait()


def test_info_is_unaffected(session):
    """`chord info` still reports the stale pid rather than hiding it."""
    stranger = spawn(["sleep", "60"])
    try:
        plant(session, stranger.pid)
        from chord.cli import _daemon_line

        line = _daemon_line(daemon.running_pid())
        assert str(stranger.pid) in line
    finally:
        if stranger.poll() is None:
            stranger.kill()
        stranger.wait()