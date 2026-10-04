"""The daemon findings: per-project state, not signalling a stranger's pid, and
two concurrent `chord start`s not orphaning a watcher."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from chord import daemon, paths


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    """Point every daemon path at a temp dir, so no test can read or write the
    real `~/.chord`."""
    home = tmp_path / "home" / ".chord"
    monkeypatch.setattr(daemon, "HOME", home)
    monkeypatch.setattr(daemon, "PROJECT", "testproj")
    monkeypatch.setattr(daemon, "PROJECT_HOME", home / "testproj")
    monkeypatch.setattr(daemon, "LOG_FILE", home / "testproj" / "chord.log")
    monkeypatch.setattr(daemon, "PID_FILE", home / "testproj" / "chord.pid")
    monkeypatch.setattr(daemon, "STATE_FILE", home / "testproj" / "state.json")
    monkeypatch.setattr(daemon, "LOCK_FILE", home / "testproj" / "chord.lock")
    return tmp_path / "project"


# --- 6. state is per project ---


def test_state_paths_are_namespaced_per_project():
    """The docstring claimed two checkouts could each watch their own project,
    which was false while every path was a fixed file under ~/.chord."""
    assert daemon.PROJECT_HOME != daemon.HOME
    for path in (daemon.LOG_FILE, daemon.PID_FILE, daemon.STATE_FILE):
        assert path.parent == daemon.PROJECT_HOME


def test_two_projects_get_different_state(tmp_path, monkeypatch):
    """`daemon` imports `project_root` by name, so that's the binding to patch."""
    monkeypatch.setattr(daemon, "HOME", tmp_path / "home")

    a, b = tmp_path / "alpha", tmp_path / "beta"
    a.mkdir()
    b.mkdir()

    seen = []
    for root in (a, b):
        monkeypatch.setattr(daemon, "project_root", lambda root=root: root)
        seen.append(daemon._slug())
    assert seen[0] != seen[1]


def test_same_project_resolves_to_the_same_slug(tmp_path, monkeypatch):
    """Including from a subdirectory, which is the whole point of the lookup."""
    root = tmp_path / "app"
    (root / "src" / "deep").mkdir(parents=True)
    (root / "chord.toml").write_text("label = 'x'\n")
    monkeypatch.setattr(daemon, "HOME", tmp_path / "home")

    monkeypatch.setattr(daemon, "project_root", lambda: paths.project_root(root))
    from_root = daemon._slug()
    monkeypatch.setattr(
        daemon, "project_root", lambda: paths.project_root(root / "src" / "deep")
    )
    assert daemon._slug() == from_root


def test_slug_is_filesystem_safe(tmp_path, monkeypatch):
    awkward = tmp_path / "my project (v2)!"
    awkward.mkdir()
    monkeypatch.setattr(daemon, "HOME", tmp_path / "home")
    monkeypatch.setattr(daemon, "project_root", lambda: awkward)
    slug = daemon._slug()
    assert set(slug) <= set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    )


# --- 8. never signal a process that isn't ours ---


def test_is_our_process_recognises_a_chord_watcher():
    """The check has to actually match the real spawn, argv and all."""
    proc = subprocess.Popen(
        [
            os.sys.executable,
            "-c",
            "import time; time.sleep(30)",
            "chord.cli",
            "_serve",
        ]
    )
    try:
        assert daemon.is_our_process(proc.pid) is True
    finally:
        proc.kill()
        proc.wait()


def test_is_our_process_rejects_an_unrelated_process():
    stranger = subprocess.Popen(["sleep", "30"])
    try:
        assert daemon.is_our_process(stranger.pid) is False
    finally:
        stranger.kill()
        stranger.wait()


def test_is_our_process_is_false_for_a_dead_pid():
    dead = subprocess.Popen(["true"])
    dead.wait()
    assert daemon.is_our_process(dead.pid) is False


def test_stop_refuses_a_stale_pid_and_clears_the_file(isolated_state):
    """A pid file that outlived a reboot, naming somebody else's process."""
    stranger = subprocess.Popen(["sleep", "30"])
    try:
        daemon.PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        daemon.PID_FILE.write_text(f"{stranger.pid}\n{time.time()}\n")

        with pytest.raises(daemon.NotOurs):
            daemon.stop()

        assert not daemon.PID_FILE.exists(), "stale file should be cleared"
        assert stranger.poll() is None, "the unrelated process was signalled"
    finally:
        stranger.kill()
        stranger.wait()


# --- 9. two concurrent starts must not orphan a watcher ---


RACE_SCRIPT = """
import sys, json, pathlib
sys.path.insert(0, {src!r})
from chord import daemon
daemon.PROJECT_HOME = pathlib.Path({home!r})
daemon.PID_FILE = daemon.PROJECT_HOME / "chord.pid"
daemon.LOCK_FILE = daemon.PROJECT_HOME / "chord.lock"
daemon.LOG_FILE = daemon.PROJECT_HOME / "chord.log"
daemon.project_root = lambda: pathlib.Path({root!r})
import subprocess, os, time
# A stand-in for the watcher. Two things it has to get right, both of which the
# old `sleep 30` got wrong now that `start` verifies identity rather than just
# liveness: the argv has to read like a real watcher's (`-m chord.cli _serve`),
# and the patch must target `daemon._Popen` rather than `subprocess.Popen`,
# because `is_our_process` runs `ps` through `subprocess.run` — which would
# otherwise spawn another stand-in instead of `ps`, and report nothing.
class Fake:
    returncode = None
    def __init__(self, p): self._p = p
    def poll(self): return self._p.poll()
    @property
    def pid(self): return self._p.pid
real = subprocess.Popen
def popen(*args, **kw):
    return Fake(real([sys.executable, "-c",
                      "import time; time.sleep(30)", "-m",
                      "chord.cli", daemon.SERVE_COMMAND]))
daemon._Popen = popen
try:
    pid = daemon.start()
    print(json.dumps({{"ok": True, "pid": pid}}))
    # The stand-in watcher outlives this process. A real one is detached and
    # does exactly that, and it matters here: the winner used to SIGKILL its
    # own stand-in on the way out, so the loser would find a pid that was
    # already gone, fail to identify it, and start a second watcher. That is
    # the test destroying the evidence the race depends on, not a race.
    time.sleep(4)
except daemon.AlreadyRunning as exc:
    print(json.dumps({{"ok": False, "pid": exc.pid}}))
"""


def test_concurrent_starts_leave_no_orphaned_watcher(tmp_path):
    """Two real processes racing to `start`. Before the lock, both saw an empty
    pid file, both spawned, and the loser's watcher was unreachable by
    `chord stop`. Exactly one must win, and the pid file must name that one.
    """
    home = tmp_path / "home" / ".chord"
    home.mkdir(parents=True)
    root = tmp_path / "project"
    root.mkdir()
    script = RACE_SCRIPT.format(
        src=str(Path(daemon.__file__).parent.parent),
        home=str(home),
        root=str(root),
    )
    path = tmp_path / "racer.py"
    path.write_text(script)

    env = {**os.environ, "PYTHONPATH": str(Path(daemon.__file__).parent.parent)}
    procs = [
        subprocess.Popen(
            [os.sys.executable, str(path)], stdout=subprocess.PIPE, text=True, env=env
        )
        for _ in range(2)
    ]
    results = []
    for proc in procs:
        out, _ = proc.communicate(timeout=60)
        results.append(out.strip().splitlines()[-1] if out.strip() else "")

    # Each racer leaves its stand-in watcher running for four seconds so the
    # other one can still identify it — see RACE_SCRIPT — and this test tidies
    # them up rather than letting them be adopted by init.
    for line in results:
        if '"ok": true' in line:
            try:
                os.kill(json.loads(line)["pid"], 9)
            except (OSError, ValueError):
                pass

    winners = [r for r in results if '"ok": true' in r]
    assert len(winners) == 1, f"expected one winner, got {results}"
    assert any('"ok": false' in r for r in results), (
        f"expected one refusal, got {results}"
    )


def test_pid_file_is_written_atomically(isolated_state, monkeypatch):
    """A reader must never see a half-written pid file and conclude that
    nothing is running."""
    daemon._ensure_home()

    class FakeProcess:
        pid = 99
        returncode = 0

        def poll(self):
            return None

    monkeypatch.setattr(daemon, "running_pid", lambda: None)
    # `_Popen`, not `subprocess.Popen`: the daemon spawns through its own module
    # attribute so a test can replace the child without also catching the
    # processes the test runner spawns for output capture.
    monkeypatch.setattr(daemon, "_Popen", lambda *a, **k: FakeProcess())
    monkeypatch.setattr(daemon.time, "sleep", lambda _s: None)

    seen_during_write = []
    real_replace = os.replace

    def watching_replace(src, dst):
        # Whatever a concurrent reader sees mid-write must not be a short file.
        seen_during_write.append(Path(dst).read_text() if Path(dst).exists() else None)
        return real_replace(src, dst)

    monkeypatch.setattr(daemon.os, "replace", watching_replace)

    daemon.start()
    assert seen_during_write == [None], "destination was clobbered in place"
    assert daemon.PID_FILE.read_text().startswith("99\n")
    assert not list(daemon.PROJECT_HOME.glob("*.tmp"))


# --- 5. paths resolve upward ---


def test_find_upwards_locates_a_parent_file(tmp_path):
    (tmp_path / "chord.toml").write_text("label = 'x'\n")
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert paths.find_upwards("chord.toml", start=deep) == tmp_path / "chord.toml"


def test_find_upwards_prefers_the_nearest(tmp_path):
    (tmp_path / "chord.toml").write_text("")
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "chord.toml").write_text("")
    assert (
        paths.find_upwards("chord.toml", start=tmp_path / "a")
        == tmp_path / "a" / "chord.toml"
    )


def test_find_upwards_returns_the_path_it_would_have_used(tmp_path):
    """So a caller can name the file it expected."""
    assert (
        paths.find_upwards("nothing-here", start=tmp_path) == tmp_path / "nothing-here"
    )


def test_project_root_prefers_the_config_directory(tmp_path):
    (tmp_path / "chord.toml").write_text("")
    deep = tmp_path / "x" / "y"
    deep.mkdir(parents=True)
    assert paths.project_root(deep) == tmp_path


def test_project_root_falls_back_to_the_start(tmp_path):
    deep = tmp_path / "x"
    deep.mkdir()
    assert paths.project_root(deep) == deep
