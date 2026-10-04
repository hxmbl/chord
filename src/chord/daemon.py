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
from typing import Any

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

# The watcher's own argv. `start` runs `[python, "-m", "chord.cli", "_serve"]`,
# and `chord stop` compares against that rather than looking for a substring.
#
# The substring version was `PROCESS_MARKER in ps_output`, and that matched any
# process with `chord.cli` anywhere in its command line — verified against the
# pre-fix code, where a stale pid file naming
# `python -c 'import time; time.sleep(300)' chord.cli` got SIGTERMed. A grep
# over the source tree matched too. `ps -o command=` renders the whole argv as
# one string, so there is no way to tell `-m chord.cli _serve` from a trailing
# argument that happens to say the same thing.
#
# So this is tokenised and matched as tokens. `-m` followed by `chord.cli`
# followed by `_serve` is a watcher; a filename, an argument, or a grep pattern
# is not.
PROCESS_MODULE = "chord.cli"
PROCESS_MARKER = PROCESS_MODULE


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
    # 0700, not the umask's default. Everything under here is about this project
    # and nobody else's: the log holds issue titles, comment bodies and Linear's
    # own error text, and the state file holds which issues have been worked on.
    # None of that is a secret, but it is not anybody else's business either.
    #
    # The lock file was already opened 0600, which is the inconsistency this
    # fixes: the code treats these as private in one place and not in the rest.
    # It is also worth doing because the mode is explicit rather than inherited,
    # so it does not change with the umask -- a stricter shell leaves the log
    # 0600 and a laxer one used to leave it 0644.
    with contextlib.suppress(OSError):
        PROJECT_HOME.chmod(0o700)


def _open_private(path: Path, mode: str = "w") -> Any:
    """Open `path` for writing, readable only by its owner.

    The mode passed to `os.open` is only honoured when the file does *not*
    already exist, so an older Chord's 0644 log stays 0644 through it. The
    `chmod` afterwards is what actually fixes an existing file, and it is why
    upgrading tightens the permissions rather than only applying to new ones.
    """
    handle = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.fchmod(handle, 0o600)
    except OSError:
        # A filesystem without permission bits still works; it just cannot be
        # made private, which is not a reason to refuse to write the log.
        os.close(handle)
        handle = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        return os.fdopen(handle, mode)
    except BaseException:
        os.close(handle)
        raise


def _write_private(path: Path, body: str) -> None:
    """Replace `path` with `body`, privately."""
    pending = path.with_suffix(path.suffix + ".tmp")
    with _open_private(pending) as handle:
        handle.write(body)
        handle.flush()
        # Flushed to the device, not just to the page cache. `replace` is atomic
        # as a rename, so a reader never sees half a file — but a rename of data
        # still in the cache does not survive losing power, and that costs the
        # same thing a lost write always costs: the work is done again.
        os.fsync(handle.fileno())
    pending.replace(path)
    # `replace` carries the new file's mode across, so this is belt and braces —
    # and it is what fixes a file left behind by an older Chord.
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


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

    Matched on argv *tokens*, not on a substring. `ps` renders the command line
    as one string, so `"chord.cli" in output` was true for any process that
    merely mentioned it anywhere -- including `python -c '...' chord.cli` and
    `grep -r chord.cli .`, both of which were SIGTERMed off a stale pid file.
    This checks for `-m chord.cli _serve`, in that order and as separate tokens.
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
    return _is_watcher_argv(finished.stdout)


def _is_watcher_argv(command: str) -> bool:
    """Whether this command line is `python -m chord.cli _serve`.

    `ps` gives one space-separated string, and quoting varies by platform and
    shell, so this matches the module name and the serve command as adjacent
    words anywhere in the line rather than demanding a specific shape. That is
    narrower than a bare substring -- a file called `chord.cli.py`, a grep for
    the string, or an argument that ends in it all fail -- while still working
    across the quoting differences between `ps` implementations.

    Deliberately does not require an exact word count or a leading interpreter
    path: those vary (`/usr/bin/python3`, a venv, a wrapper) and requiring them
    would mean refusing to stop our own watcher on a machine where the paths
    differ, which is the failure that matters more.
    """
    words = command.split()
    for index in range(len(words) - 1):
        if words[index] == PROCESS_MODULE and words[index + 1] == SERVE_COMMAND:
            return True
        if words[index] == SERVE_COMMAND and words[index + 1] == PROCESS_MODULE:
            return True
    return False


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
        log = _open_private(LOG_FILE)
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
        _write_private(PID_FILE, f"{process.pid}\n{time.time()}\n")
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
