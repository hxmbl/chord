"""The command layer, and the harnesses — both changed, and both easy to get
wrong in ways the other tests can't see."""

import time

import pytest
from typer.testing import CliRunner

from chord import daemon, harness, watcher
from chord.cli import app

runner = CliRunner()


# --- harness ---


def test_print_harness_writes_the_issue(capsys):
    import asyncio

    asyncio.run(harness.PrintHarness().send("the issue"))
    assert capsys.readouterr().out == "the issue"


def test_opencode_harness_is_recognized():
    """The opencode harness should be built-in and recognized by build()."""
    h = harness.build("opencode")
    assert isinstance(h, harness.OpenCodeHarness)
    assert h.name == "opencode"


def test_command_harness_runs_and_captures(capsys):
    import asyncio

    h = harness.CommandHarness(["cat"])
    asyncio.run(h.send("hello"))
    assert "hello" in capsys.readouterr().out


def test_command_harness_names_itself():
    h = harness.CommandHarness(["claude", "-p"])
    assert h.name == "claude -p"


def test_missing_harness_is_a_harness_error():
    import asyncio

    h = harness.CommandHarness(["definitely-not-a-real-command-xyz"])
    with pytest.raises(harness.HarnessError, match="isn't on your PATH"):
        asyncio.run(h.send("x"))


def test_failing_harness_raises_with_the_exit_code():
    import asyncio

    h = harness.CommandHarness(["sh", "-c", "exit 3"])
    with pytest.raises(harness.HarnessError, match="exited 3"):
        asyncio.run(h.send("x"))


def test_build_returns_the_print_harness_by_name():
    built = harness.build("print")
    assert isinstance(built, harness.PrintHarness)


def test_build_returns_a_command_for_an_argv():
    built = harness.build(["claude", "-p"])
    assert isinstance(built, harness.CommandHarness)


def test_build_splits_a_command_string_without_a_shell(monkeypatch):
    monkeypatch.setattr(harness.shutil, "which", lambda name: "/bin/tool")
    built = harness.build('tool --message "hello world"')
    assert isinstance(built, harness.CommandHarness)
    assert built.argv == ["/bin/tool", "--message", "hello world"]


def test_build_resolves_a_bare_name_on_path():
    """The shortcut that makes `harness = "cat"` work."""
    built = harness.build("cat")
    assert isinstance(built, harness.CommandHarness)


def test_build_refuses_an_unknown_harness():
    with pytest.raises(harness.HarnessError, match="Don't know a harness"):
        harness.build("definitely-not-a-real-command-xyz")


# --- watcher state file ---


def test_state_round_trips(tmp_path):
    path = tmp_path / "state.json"
    watcher.Watcher.__init__  # noqa: B018 - documenting intent
    import json

    path.write_text(json.dumps({"handed_over": {"1": "2026-01-01T00:00:00Z"}}))
    assert watcher.read_state(path) == {"1": "2026-01-01T00:00:00Z"}


def test_missing_state_is_a_first_run(tmp_path):
    assert watcher.read_state(tmp_path / "absent.json") == {}


def test_corrupt_state_names_the_way_out(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    with pytest.raises(watcher.StateError, match="Delete it"):
        watcher.read_state(path)


def test_state_that_is_not_a_state_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"something": "else"}')
    with pytest.raises(watcher.StateError, match="doesn't look like"):
        watcher.read_state(path)


def test_issue_ids_are_normalised(tmp_path):
    """The dedupe bug: ids stringified on one end but not the other."""
    import json

    path = tmp_path / "state.json"
    path.write_text(json.dumps({"handed_over": {"7": "2026-01-01T00:00:00Z"}}))
    state = watcher.read_state(path)
    assert watcher._key(7) in state
    assert watcher._key("7") in state


# --- cli ---


def test_info_runs_offline(keyring_backend, no_env, monkeypatch):
    monkeypatch.setattr(daemon, "PID_FILE", no_env / "absent.pid")
    monkeypatch.setattr(daemon, "STATE_FILE", no_env / "absent.json")
    result = runner.invoke(app, ["info"])
    assert result.exit_code == 0
    assert "Chord" in result.stdout
    assert "not connected" in result.stdout


def test_info_distinguishes_stale_from_absent(stored_token, no_env, monkeypatch):
    """A token that is present but past max age is a different problem from no
    token at all, and says so."""
    stored_token(obtained_at=time.time() - 48 * 3600)
    monkeypatch.setattr(daemon, "PID_FILE", no_env / "absent.pid")
    monkeypatch.setattr(daemon, "STATE_FILE", no_env / "absent.json")
    result = runner.invoke(app, ["info"])
    assert "past max age" in result.stdout


def test_info_reports_a_fresh_token(stored_token, no_env, monkeypatch):
    stored_token(expires_at=time.time() + 3600)
    monkeypatch.setattr(daemon, "PID_FILE", no_env / "absent.pid")
    monkeypatch.setattr(daemon, "STATE_FILE", no_env / "absent.json")
    result = runner.invoke(app, ["info"])
    assert "connected" in result.stdout


def test_stop_with_nothing_running(monkeypatch, no_env):
    monkeypatch.setattr(daemon, "PID_FILE", no_env / "absent.pid")
    result = runner.invoke(app, ["stop"])
    assert result.exit_code == 0
    assert "Nothing was watching" in result.stdout


def test_stop_refuses_a_stranger_and_explains(monkeypatch, no_env):
    """The refusal has to surface as a failure the person can act on, not as a
    silent success."""
    import subprocess
    import sys

    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        pid_file = no_env / "chord.pid"
        pid_file.write_text(f"{stranger.pid}\n{time.time()}\n")
        monkeypatch.setattr(daemon, "PID_FILE", pid_file)
        monkeypatch.setattr(daemon, "is_our_process", lambda pid: False)

        result = runner.invoke(app, ["stop"])

        assert result.exit_code == 1
        assert "isn't a Chord watcher" in result.output
        assert stranger.poll() is None, "the unrelated process was signalled"
    finally:
        stranger.kill()
        stranger.wait()


def test_watch_without_a_log(monkeypatch, no_env):
    monkeypatch.setattr(daemon, "LOG_FILE", no_env / "absent.log")
    monkeypatch.setattr(daemon, "PID_FILE", no_env / "absent.pid")
    monkeypatch.setattr(daemon, "running_pid", lambda: None)
    result = runner.invoke(app, ["watch"])
    assert result.exit_code == 1
    assert "hasn't been started" in result.output


def test_start_validates_before_spawning(monkeypatch, no_env):
    """A bad setup must reach the person who typed the command."""
    monkeypatch.setattr(daemon, "STATE_FILE", no_env / "absent.json")
    monkeypatch.setattr(daemon, "PID_FILE", no_env / "absent.pid")
    spawned = []
    monkeypatch.setattr(daemon, "start", lambda: spawned.append(1) or 999)
    result = runner.invoke(app, ["start"])
    assert result.exit_code == 1
    assert not spawned, "spawned a watcher despite an unconfigured setup"


def test_start_happy_path(stored_token, no_env, monkeypatch, tmp_path):
    """The whole command, with only the spawn and the keychain stubbed."""
    stored_token()
    config = tmp_path / "chord.toml"
    config.write_text('label = "ready"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(daemon, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(daemon, "PID_FILE", tmp_path / "chord.pid")

    started = {}
    monkeypatch.setattr(
        daemon, "start", lambda: started.setdefault("pid", 4242) or 4242
    )

    result = runner.invoke(app, ["start"])

    assert result.exit_code == 0, result.output
    assert "Watching in the background (pid 4242)" in result.stdout
    assert started["pid"] == 4242


def test_start_reports_an_already_running_watcher(
    stored_token, no_env, monkeypatch, tmp_path
):
    stored_token()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(daemon, "STATE_FILE", tmp_path / "state.json")

    def refuse():
        raise daemon.AlreadyRunning(999)

    monkeypatch.setattr(daemon, "start", refuse)
    result = runner.invoke(app, ["start"])

    assert result.exit_code == 1
    assert "Already watching (pid 999)" in result.output


def test_start_reports_a_watcher_that_died(stored_token, no_env, monkeypatch, tmp_path):
    stored_token()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(daemon, "STATE_FILE", tmp_path / "state.json")

    def fail():
        raise daemon.StartFailed("it stopped straight away")

    monkeypatch.setattr(daemon, "start", fail)
    result = runner.invoke(app, ["start"])

    assert result.exit_code == 1
    assert "stopped straight away" in result.output


def test_start_reports_a_corrupt_state_file(
    stored_token, no_env, monkeypatch, tmp_path
):
    """Checked before anything is spawned, so the person who typed it hears."""
    stored_token()
    monkeypatch.chdir(tmp_path)
    corrupt = tmp_path / "state.json"
    corrupt.write_text("{not json")
    monkeypatch.setattr(daemon, "STATE_FILE", corrupt)
    spawned = []
    monkeypatch.setattr(daemon, "start", lambda: spawned.append(1))

    result = runner.invoke(app, ["start"])

    assert result.exit_code == 1
    assert not spawned
    assert "Delete it" in result.output


def test_start_rejects_an_unknown_harness(stored_token, no_env, monkeypatch, tmp_path):
    stored_token()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "chord.toml").write_text(
        'harness = "definitely-not-a-real-command-xyz"\n'
    )
    monkeypatch.setattr(daemon, "STATE_FILE", tmp_path / "state.json")
    spawned = []
    monkeypatch.setattr(daemon, "start", lambda: spawned.append(1))

    result = runner.invoke(app, ["start"])

    assert result.exit_code == 1
    assert not spawned
    assert "Don't know a harness" in result.output


def test_start_rejects_a_curated_harness_that_cannot_run(
    stored_token, no_env, monkeypatch, tmp_path
):
    """A curated route nobody can run is a mistake in a file, so it is caught
    by the person who made it rather than by the first issue that trips it."""
    stored_token()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "chord.toml").write_text(
        'harness = "print"\n'
        "[harnesses.oops]\ncommand = 'definitely-not-a-real-command-xyz'\n"
    )
    monkeypatch.setattr(daemon, "STATE_FILE", tmp_path / "state.json")
    spawned = []
    monkeypatch.setattr(daemon, "start", lambda: spawned.append(1))

    result = runner.invoke(app, ["start"])

    assert result.exit_code == 1
    assert not spawned
    assert "Chord/oops" in result.output, "the failing route label must be named"


def test_start_accepts_curated_harnesses_that_can_run(
    stored_token, no_env, monkeypatch, tmp_path
):
    stored_token()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "chord.toml").write_text(
        'harness = "print"\n'
        '[harnesses."opencode/tiny"]\ncommand = "cat"\n'
        '[harnesses.claude]\ncommand = "claude -p"\n'
    )
    monkeypatch.setattr(daemon, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(daemon, "PID_FILE", tmp_path / "chord.pid")
    monkeypatch.setattr(daemon, "start", lambda: 4242)

    result = runner.invoke(app, ["start"])

    assert result.exit_code == 0, result.output


def test_info_lists_every_route(keyring_backend, no_env, monkeypatch, tmp_path):
    """`chord info` is where you check what a label in Linear will do."""
    config = tmp_path / ".config" / "chord" / "chord.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        'label = "Chord"\n'
        'harness = "opencode"\n'
        '[harnesses."opencode/space-bunny-free"]\n'
        'command = ["opencode", "run", "--model", "space-bunny-free"]\n'
        '[harnesses.claude]\ncommand = "claude -p"\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(daemon, "PID_FILE", tmp_path / "absent.pid")
    monkeypatch.setattr(daemon, "STATE_FILE", tmp_path / "absent.json")

    result = runner.invoke(app, ["info"])

    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "Chord" in out and "opencode" in out
    assert "Chord/claude" in out and "claude -p" in out
    assert "Chord/opencode/space-bunny-free" in out
    assert "--model space-bunny-free" in out


def test_info_says_what_the_default_is_when_nothing_is_curated(
    keyring_backend, no_env, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(daemon, "PID_FILE", tmp_path / "absent.pid")
    monkeypatch.setattr(daemon, "STATE_FILE", tmp_path / "absent.json")

    result = runner.invoke(app, ["info"])

    assert result.exit_code == 0, result.output
    assert "Chord  opencode" in result.stdout, "the default route should still be listed"
    assert result.stdout.count("Chord/") == 1, "only the watching line should mention a route"


def test_info_rejects_a_curation_typo(no_env, monkeypatch, tmp_path):
    """Same rule as everywhere else: a config Chord can't act on is one line."""
    (tmp_path / "chord.toml").write_text('[harnesses.a]\nnope = "x"\n')
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["info"])
    assert result.exit_code == 1
    assert "doesn't know" in result.output


def test_start_needs_credentials(no_env, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(daemon, "STATE_FILE", tmp_path / "state.json")
    spawned = []
    monkeypatch.setattr(daemon, "start", lambda: spawned.append(1))

    result = runner.invoke(app, ["start"])

    assert result.exit_code == 1
    assert not spawned
    assert "chord setup" in result.output


def test_setup_command_is_wired():
    result = runner.invoke(app, ["--help"])
    assert "setup" in result.stdout
    assert "_serve" not in result.stdout, "internal command should be hidden"
