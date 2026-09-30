"""Keeping the watcher running behind the CLI: its process, its pid, its log.

State lives under `~/.chord/sessions/<project>`, one directory per project, so two
checkouts can each watch their own Linear project without fighting over a pid
file, and so nothing Chord writes lands in the repository. The project is
identified by the directory holding its `chord.toml`, which means running Chord
from a subdirectory still finds the same state.
"""

import contextlib
import fcntl
import hashlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

from chord.paths import project_root

HOME = Path.home() / ".chord"

# How long `chord stop` waits for the watcher to go away. The watcher is
# usually asleep between polls, so this is generous; a harness mid-run is the
# slow case, and it gets killed rather than waited on indefinitely.
STOP_TIMEOUT = 10

# Enough to catch a watcher that fails the moment it starts, which is the
# common case for a bad config or a missing command, without making every start
# feel slow.
SETTLE = 0.3


def _slug() -> str:
    """A short, stable, filesystem-safe name for this project.

    The readable prefix is for the person looking in `~/.chord`; the hash is
    what actually separates two projects called `app`. The path is resolved
    first, so `/app` and `/app/.` don't become two different projects.
    """
    root = project_root()
    digest = hashlib.sha256(str(root).encode()).hexdigest()[:10]
    name = "".join(c if c.isalnum() or c in "-_" else "-" for c in root.name)
    return f"{name[:32] or 'project'}-{digest}"


PROJECT = _slug()
PROJECT_HOME = HOME / "sessions" / PROJECT
LOG_FILE = PROJECT_HOME / "chord.log"
PID_FILE = PROJECT_HOME / "chord.pid"
STATE_FILE = PROJECT_HOME / "state.json"
LOCK_FILE = PROJECT_HOME / "chord.lock"

# The command the detached process actually runs. Hidden from `chord --help`
# because it is an implementation detail, not something to type.
SERVE_COMMAND = "_serve"

# A watcher always has this in its command line, which is how `chord stop`
# tells "our watcher that outlived its pid file" from an unrelated process
# that inherited a recycled id.
PROCESS_MARKER = "chord.cli"


class AlreadyRunning(Exception):
    def __init__(self, pid: int) -> None:
        super().__init__(pid)
        self.pid = pid


class StartFailed(Exception):
    """The watcher started and immediately stopped."""


class StopFailed(Exception):
    """The watcher was asked to stop and didn't."""


class NotOurs(Exception):
    """The pid in the pid file is live, but it isn't a Chord watcher."""


def _ensure_home() -> None:
    PROJECT_HOME.mkdir(parents=True, exist_ok=True)


@contextlib.contextmanager
def _start_lock() -> Iterator[None]:
    """Serialise `start` against itself.

    The old order — check for a watcher, spawn, sleep, then write the pid file
    — left a window in which two `chord start`s both saw nothing running and
    both spawned. The loser of the race was then unreachable: the pid file
    named the winner, so `chord stop` could never find it. Holding an exclusive
    lock across check-and-write closes that window.
    """
    _ensure_home()
    handle = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)  # Closing releases the lock.


def running_pid() -> int | None:
    """The watcher's process id, or None if nothing of ours is running.

    The pid file outlives a crash and a reboot, so the process behind the
    number is checked before the number is believed. This stays a cheap
    liveness check, because `chord info` calls it and shouldn't pay for a
    subprocess; `stop` is the one that has to be certain, and it calls
    `is_our_process` before signalling anything.
    """
    try:
        lines = PID_FILE.read_text().splitlines()
        pid = int(lines[0].strip())
    except (OSError, IndexError, ValueError):
        return None

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        # Something is there, and it isn't ours to signal or to stop claiming
        # the file.
        return None
    return pid


def is_our_process(pid: int) -> bool:
    """Whether `pid` is a Chord watcher rather than something that reused the id.

    A pid file can outlive a reboot, and pids get reused, so "something is
    listening" isn't the same as "it is ours". Sending SIGTERM to an unrelated
    process on the strength of a stale file is much worse than refusing, so this
    is only ever used to *veto* a kill, never to authorise one on its own.
    """
    try:
        finished = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False  # No `ps`, or it wouldn't answer: don't signal blind.
    if finished.returncode != 0:
        return False
    return PROCESS_MARKER in finished.stdout


def started_at() -> float | None:
    """When the running watcher started, if it recorded it."""
    try:
        lines = PID_FILE.read_text().splitlines()
        return float(lines[1].strip())
    except (OSError, IndexError, ValueError):
        return None


def start() -> int:
    """Start a detached watcher and return its process id.

    The child is a fresh interpreter rather than a fork, so it inherits no
    event loop, sockets or half-read buffers from the command that launched it.
    """
    with _start_lock():
        already = running_pid()
        if already is not None:
            raise AlreadyRunning(already)

        # Each start begins a new log. What you want after `chord start` is this
        # run, including anything that went wrong in the first second.
        log = LOG_FILE.open("w")
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "chord.cli", SERVE_COMMAND],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                # A new session detaches the watcher from the terminal, so closing
                # the terminal doesn't take it down and its Ctrl+C never reaches
                # it. `chord stop` is how it ends.
                start_new_session=True,
            )
        finally:
            log.close()

        time.sleep(SETTLE)
        if process.poll() is not None:
            raise StartFailed(
                f"The watcher stopped straight away (exit {process.returncode}). "
                f"What it said is in {LOG_FILE}."
            )

        # Replace rather than truncate, so a concurrent reader never sees a
        # half-written file and decides the watcher isn't running.
        pending = PID_FILE.with_suffix(".pid.tmp")
        pending.write_text(f"{process.pid}\n{time.time()}\n")
        os.replace(pending, PID_FILE)
        return process.pid


def stop() -> int | None:
    """Stop the watcher. Returns the process id it was, or None if there was
    nothing to stop."""
    pid = running_pid()
    if pid is None:
        # Clear a file left behind by a crash, so `chord start` isn't refused
        # by a watcher that no longer exists.
        PID_FILE.unlink(missing_ok=True)
        return None

    if not is_our_process(pid):
        # A stale file pointing at somebody else's process. Report it rather
        # than signalling, and drop the file so the next start isn't blocked.
        PID_FILE.unlink(missing_ok=True)
        raise NotOurs(
            f"{PID_FILE} names pid {pid}, which is running but isn't a Chord "
            f"watcher, so it was left alone. The file has been removed."
        )

    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        if running_pid() is None:
            return pid
        time.sleep(0.1)

    # Not escalating to SIGKILL on its own. A harness part-way through an issue
    # is doing real work, and killing it silently is worse than saying so.
    raise StopFailed(
        f"The watcher (pid {pid}) hadn't stopped after {STOP_TIMEOUT}s. It may be "
        f"waiting on a harness; check {LOG_FILE} before killing it yourself."
    )


def release() -> None:
    """Drop the pid file on the way out, but only if it's still ours. A second
    Chord may have started and written its own by now."""
    if running_pid() == os.getpid():
        PID_FILE.unlink(missing_ok=True)
