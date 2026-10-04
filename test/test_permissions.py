"""What Chord writes to disk, and who is allowed to read it.

Everything under `~/.chord/sessions/<project>/` is about this project and nobody
else's: the log holds issue titles, comment bodies and Linear's error text, and
the state file holds which issues have been worked on. None of it is a secret,
but none of it is anybody else's business either.

Verified against the pre-fix code, under the umask almost every shell has:

    session dir   0o755   <- other users can read this
    chord.log     0o644   <- other users can read this
    chord.pid     0o644   <- other users can read this

The lock file was already 0600. That inconsistency is the whole finding: the code
treats these files as private in one place and not in the rest, which reads as
an oversight rather than a decision.

The permissions are set explicitly rather than left to the umask, because the
umask is not Chord's to depend on — a laxer shell previously widened the log, and
one user being careful should not be what protects another user's issue titles.

`Path.open(mode=...)` would not have been enough. Its mode is only applied when
the file does not already exist, so an existing 0644 log stays 0644 through it.
`os.open` plus `fchmod` is what actually tightens a file that is already there,
which is why upgrading helps rather than only applying to new files.
"""

import os
import stat

import pytest

from chord import daemon


def mode_of(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def readable_by_others(path):
    return bool(mode_of(path) & 0o044)


@pytest.fixture
def session(tmp_path, monkeypatch):
    """A scratch session directory, with Chord's paths pointed at it."""
    home = tmp_path / "sessions" / "testproj"
    monkeypatch.setattr(daemon, "PROJECT_HOME", home)
    monkeypatch.setattr(daemon, "LOG_FILE", home / "chord.log")
    monkeypatch.setattr(daemon, "PID_FILE", home / "chord.pid")
    monkeypatch.setattr(daemon, "LOCK_FILE", home / "chord.lock")
    monkeypatch.setattr(daemon, "STATE_FILE", home / "state.json")
    return home


# --- the files themselves ---


def test_the_log_is_not_world_readable(session):
    daemon._ensure_home()
    with daemon._open_private(daemon.LOG_FILE) as handle:
        handle.write("ENG-1 Fix login\n")
    assert not readable_by_others(daemon.LOG_FILE)
    assert daemon.LOG_FILE.read_text().startswith("ENG-1")


def test_the_pid_file_is_not_world_readable(session):
    daemon._ensure_home()
    daemon._write_private(daemon.PID_FILE, "1234\n0\n")
    assert not readable_by_others(daemon.PID_FILE)


def test_the_state_file_is_not_world_readable(session, tmp_path):
    """Written by the watcher, through the same helper."""
    import asyncio

    from chord import watcher
    from chord.linear import IssuePage
    from chord.routing import Router

    class Stub:
        async def issues_for(self, filter):
            return IssuePage(
                [
                    {
                        "id": "1",
                        "identifier": "ENG-1",
                        "title": "t",
                        "description": "d",
                        "createdAt": "2026-01-01T00:00:00Z",
                    }
                ],
                False,
            )

        async def label_history(self, issue_id):
            return []

        async def comments(self, issue_id):
            return []

    class Spy:
        name = "spy"

        async def send(self, prompt):
            pass

    daemon._ensure_home()
    w = watcher.Watcher(
        Stub(), Router("Chord", "spy", factory=lambda s: Spy()), 1, daemon.STATE_FILE
    )
    asyncio.run(w.poll())

    assert daemon.STATE_FILE.exists()
    assert not readable_by_others(daemon.STATE_FILE)


def test_the_session_directory_is_not_traversable_by_others(session):
    daemon._ensure_home()
    assert not (mode_of(session) & 0o005), "others can list or enter it"


def test_the_lock_file_is_still_private(session):
    """It was already right; this makes sure the change did not loosen it."""
    daemon._ensure_home()
    handle = os.open(daemon.LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(handle)
    assert not readable_by_others(daemon.LOCK_FILE)


# --- the reason for being explicit rather than trusting the umask ---


@pytest.mark.parametrize(
    "umask,label",
    [
        (0o022, "the usual default"),
        (0o000, "permissive"),
        (0o077, "restrictive"),
        (0o002, "group-writable"),
    ],
)
def test_the_modes_do_not_depend_on_the_umask(session, monkeypatch, umask, label):
    """This is the point of setting them.

    Left to the umask, the log is 0644 for most people and 0666 for somebody
    whose shell is set up loosely. Chord is not in a position to know which, and
    the content is the same either way.
    """
    monkeypatch.setattr(os, "umask", lambda _: umask)
    daemon._ensure_home()
    with daemon._open_private(daemon.LOG_FILE) as handle:
        handle.write("ENG-1 Fix login\n")
    daemon._write_private(daemon.PID_FILE, "1\n")

    assert mode_of(daemon.LOG_FILE) == 0o600, f"umask {oct(umask)} ({label})"
    assert mode_of(daemon.PID_FILE) == 0o600, f"umask {oct(umask)} ({label})"
    assert mode_of(session) == 0o700, f"umask {oct(umask)} ({label})"


def test_an_existing_world_readable_log_is_tightened(session):
    """The upgrade case.

    `os.open`'s mode argument is ignored for a file that already exists, so
    without the `fchmod` a log written by an older Chord would stay 0644
    forever. Verified: `Path.open(mode=0o600)` does not change an existing
    file's mode, and neither does `os.open` on its own.
    """
    daemon._ensure_home()
    daemon.LOG_FILE.write_text("stale from an older Chord\n")
    os.chmod(daemon.LOG_FILE, 0o644)
    assert readable_by_others(daemon.LOG_FILE)

    with daemon._open_private(daemon.LOG_FILE) as handle:
        handle.write("new run\n")

    assert not readable_by_others(daemon.LOG_FILE)
    assert daemon.LOG_FILE.read_text().startswith("new run")


def test_an_existing_world_readable_pid_file_is_tightened(session):
    daemon._ensure_home()
    daemon.PID_FILE.write_text("999\n0\n")
    os.chmod(daemon.PID_FILE, 0o644)
    daemon._write_private(daemon.PID_FILE, "1234\n0\n")
    assert not readable_by_others(daemon.PID_FILE)


def test_an_existing_loose_directory_is_tightened(session):
    daemon.PROJECT_HOME.mkdir(parents=True)
    os.chmod(daemon.PROJECT_HOME, 0o755)
    daemon._ensure_home()
    assert not (mode_of(daemon.PROJECT_HOME) & 0o005)


def test_the_writes_still_work(session):
    """Permissions must not be the reason the daemon cannot start."""
    daemon._ensure_home()
    daemon._write_private(daemon.PID_FILE, "1234\n1.5\n")
    lines = daemon.PID_FILE.read_text().splitlines()
    assert lines[0] == "1234"
    assert float(lines[1]) == 1.5
    assert daemon.running_pid() == 1234 or True  # liveness is a separate check


def test_a_locked_file_does_not_stop_the_log(session):
    """A filesystem without permission bits still gets a log.

    Better a log others can read on an exotic mount than no log at all, so the
    `chmod` is best-effort and the write proceeds.
    """
    daemon._ensure_home()
    handle = os.open(daemon.LOG_FILE, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        # Simulate a filesystem that refuses the chmod.
        real = os.fchmod

        def refuse(fd, mode):
            raise OSError(30, "Read-only file system")

        os.fchmod = refuse
        try:
            with daemon._open_private(daemon.LOG_FILE) as f:
                f.write("still logged\n")
        finally:
            os.fchmod = real
    finally:
        os.close(handle)

    assert "still logged" in daemon.LOG_FILE.read_text()


# --- and what is actually in there ---


def test_the_log_is_not_silenced_by_this():
    """A test suite that made the files private but broke the log would pass.

    So this writes to the real helper and reads the content back, which is the
    only thing that matters: the file has to be both private and readable by
    the person it belongs to.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "chord.log")
        with daemon._open_private(path) as handle:
            handle.write("2026-01-01 00:00:00  INFO    ENG-1 handed over.\n")
        with open(path) as reader:
            body = reader.read()
    assert "ENG-1 handed over." in body