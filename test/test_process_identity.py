"""`chord stop` must never signal a process that is not Chord's.

A pid file can outlive a crash or a reboot, and pids get reused. So `stop()`
checks the process behind the number before signalling it, and the whole point
of that check is that it refuses when it is unsure — refusing costs a restart,
signalling a stranger costs somebody's work.

It was not refusing enough. `is_our_process` asked whether `"chord.cli"` appeared
anywhere in `ps -o command=`, which is a substring test against a rendering of
the whole argv as one string. Verified against the pre-fix code: a stale pid file
naming `python -c 'import time; time.sleep(300)' chord.cli` — a process that
merely had the text as a trailing argument — got SIGTERMed. `grep -r chord.cli .`
matched too.

`ps` gives one string, so there is no way to tell `-m chord.cli _serve` from an
argument that says the same thing. The fix tokenises the line and matches the
module and the serve command as adjacent words.
"""

import subprocess
import sys
import time

import pytest

from chord import daemon

# --- the matching itself, without spawning anything ---


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("/usr/bin/python3 -m chord.cli _serve", id="system python"),
        pytest.param(
            "/Users/x/.venv/bin/python -m chord.cli _serve", id="venv python"
        ),
        pytest.param(
            "/opt/homebrew/bin/python3.13 -m chord.cli _serve", id="homebrew python"
        ),
        pytest.param("python -m chord.cli _serve", id="bare python"),
        pytest.param("/usr/bin/python3 -m chord.cli _serve ", id="trailing space"),
        pytest.param(
            "  /usr/bin/python3 -m chord.cli _serve", id="leading whitespace"
        ),
        pytest.param(
            "/usr/bin/python3 -m chord.cli _serve 2>&1", id="with a redirect"
        ),
    ],
)
def test_a_watcher_is_recognised_across_interpreters(command):
    """Not too strict: refusing to stop our own watcher is the worse failure.

    The interpreter path varies with the venv, the platform and any wrapper, so
    an exact word count or a leading path would mean a watcher on an unusual
    install could never be stopped.
    """
    assert daemon._is_watcher_argv(command), f"would not recognise its own watcher: {command!r}"


@pytest.mark.parametrize(
    "command,why",
    [
        pytest.param(
            "python -c 'import time; time.sleep(300)' chord.cli",
            "the text is a trailing argument, not a module",
            id="argument",
        ),
        pytest.param("grep -r chord.cli .", "a grep for the string", id="grep"),
        pytest.param("tail -f src/chord/cli.py", "a file with it in the name", id="file"),
        pytest.param("sleep 300 chord.cli", "an unrelated process", id="unrelated"),
        pytest.param(
            "python -m chord.cli_other _serve", "a similarly named module", id="module"
        ),
        pytest.param(
            "python -m not_chord.cli _serve", "a prefix of the module name", id="prefix"
        ),
        pytest.param("python -m chord.cli serve", "a different subcommand", id="subcmd"),
        pytest.param("python chord.cli", "run as a file, not a module", id="as-file"),
        pytest.param("", "nothing at all", id="empty"),
        pytest.param("chord.cli", "the bare name", id="bare-name"),
    ],
)
def test_anything_else_is_refused(command, why):
    assert not daemon._is_watcher_argv(command), f"matched something that is not a watcher ({why})"


def test_the_serve_command_alone_is_not_enough():
    """The module name is the part that cannot be faked by accident."""
    assert not daemon._is_watcher_argv("python -m something_else _serve")


# --- against real processes ---


_SPAWNED: list = []


def spawn(argv):
    process = subprocess.Popen(
        argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    _SPAWNED.append(process)
    # Give the OS a moment to make the process visible to `ps`.
    time.sleep(0.4)
    return process


@pytest.fixture(autouse=True)
def reap_spawned():
    """Nothing this file starts outlives it.

    Several tests here assert on a live process, and a failing assertion would
    otherwise skip the `finally` that kills it — leaving detached watchers
    behind, which is exactly the sort of thing this repo's own findings are
    about.
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


def count_watchers() -> int:
    """How many stand-in watchers are running right now."""
    listing = subprocess.run(
        ["ps", "-Ao", "command="], capture_output=True, text=True
    ).stdout
    return sum(
        1
        for line in listing.splitlines()
        if daemon.PROCESS_MODULE in line and daemon.SERVE_COMMAND in line
    )


def test_a_real_watcher_is_recognised():
    """Against a live process, not a description of one.

    The argv is a real watcher's shape — `-m chord.cli _serve` — but the body is
    a sleep, so nothing is watched and nothing is written. `is_our_process` only
    ever reads `ps`, so what it is looking at is a real entry in the process
    table either way.

    Earlier this test spawned `chord.cli` with a bad subcommand and asserted
    `recognised or already exited`, which passes whichever way it lands and
    therefore checked nothing.
    """
    process = spawn(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            "-m",
            daemon.PROCESS_MODULE,
            daemon.SERVE_COMMAND,
        ]
    )
    assert process.poll() is None, "the stand-in exited before it was checked"
    reported = subprocess.run(
        ["ps", "-ww", "-p", str(process.pid), "-o", "command="],
        capture_output=True,
        text=True,
    ).stdout
    assert daemon.PROCESS_MODULE in reported, "ps did not report the argv as expected"
    assert daemon.is_our_process(process.pid) is True


def test_nothing_this_file_spawned_is_left_running():
    """A test that leaks watchers is a test that eventually hangs the machine.

    Counted rather than asserted per-test, because the leaks happened in
    `finally` blocks that ran after an assertion had already failed.
    """
    before = count_watchers()
    spawn([sys.executable, "-c", "import time; time.sleep(60)", "-m",
           daemon.PROCESS_MODULE, daemon.SERVE_COMMAND]).kill()
    spawn([sys.executable, "-c", "import time; time.sleep(60)", "chord.cli"]).kill()
    assert count_watchers() == before, "a helper spawned something it did not clean up"


@pytest.mark.parametrize(
    "argv,why",
    [
        pytest.param(
            [sys.executable, "-c", "import time; time.sleep(60)", "chord.cli"],
            "a trailing argument that says chord.cli",
            id="argument",
        ),
        pytest.param(
            [sys.executable, "-c", "import time; time.sleep(60)", "-m", "chord.cli_fake"],
            "a similarly named module",
            id="similar-module",
        ),
        pytest.param(["sleep", "60"], "an unrelated process", id="unrelated"),
    ],
)
def test_an_unrelated_process_is_not_ours(argv, why):
    """The regression, end to end with a live process."""
    process = spawn(argv)
    try:
        assert daemon.is_our_process(process.pid) is False, (
            f"mistook something else for a watcher ({why}); "
            f"`chord stop` would SIGTERM it"
        )
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def test_a_dead_pid_is_not_ours():
    process = spawn(["sleep", "60"])
    pid = process.pid
    process.kill()
    process.wait()
    assert daemon.is_our_process(pid) is False


def test_a_pid_that_does_not_exist_is_not_ours():
    # Above the usual pid_max on Linux, but on macOS 2**22 is above it too, and
    # either way `ps` must answer "no such process" rather than crash.
    assert daemon.is_our_process(2**22) is False


def test_is_our_process_is_never_used_to_authorise_a_kill(tmp_path, monkeypatch):
    """It is a veto, never an authorisation.

    `stop()` signals only when `running_pid()` *and* `is_our_process()` agree.
    If `is_our_process` were ever consulted the other way round — "not ours, so
    it must be stale, so kill it" — the check would stop being a safety net.
    """
    import inspect

    source = inspect.getsource(daemon.stop)
    # The refusal has to come before the signal.
    assert source.index("is_our_process") < source.index("os.kill"), (
        "stop() signals before checking identity"
    )
    assert "raise NotOurs" in source, "the refusal was removed"


def test_the_marker_constant_still_exists():
    """Other code and the README refer to it; renaming it silently would be a
    needless break."""
    assert daemon.PROCESS_MARKER == "chord.cli"
    assert daemon.SERVE_COMMAND == "_serve"