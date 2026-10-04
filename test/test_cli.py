"""The command layer, and the harnesses — both changed, and both easy to get
wrong in ways the other tests can't see."""

import re
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


def _strip_lead(line: str) -> str:
    """One line of `chord info` with its 13-column indent removed."""
    return line[len("  labels     ") :] if line.startswith("  labels     ") else line[13:]


def label_column(out: str) -> list[str]:
    """The labels in the `labels` block, in order.

    Read off the aligned column rather than by taking the first word of every
    line: the block is a label column followed by prose, and the prose is
    full sentences. A label is the only line whose first token is followed by the
    column's padding.
    """
    labels = []
    for line in out.splitlines():
        rest = _strip_lead(line)
        match = re.match(r"^(\S+)\s{2,}\S", rest)
        if match:
            labels.append(match.group(1))
    return labels


def flatten(out: str) -> str:
    """The output as one line, so an assertion survives being re-wrapped."""
    return " ".join(out.split())


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
    assert label_column(result.stdout) == ["Chord"], "and it is the only route"


def test_info_showcases_the_labels_it_accepts(keyring_backend, no_env, monkeypatch, tmp_path):
    """The list somebody has to create in Linear by hand, as a list.

    Chord never creates a label, so this set and the labels in the workspace are
    two things kept in step by hand. `chord info` is the only place the first can
    be read before it matters.
    """
    (tmp_path / "chord.toml").write_text(
        'label = "Chord"\nharness = "opencode"\n'
        '[harnesses.claude]\ncommand = ["claude", "-p"]\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(daemon, "PID_FILE", tmp_path / "absent.pid")
    monkeypatch.setattr(daemon, "STATE_FILE", tmp_path / "absent.json")

    result = runner.invoke(app, ["info"])

    assert result.exit_code == 0, result.output
    assert label_column(result.stdout) == ["Chord", "Chord/claude"]


def test_info_says_that_adding_a_label_is_the_trigger(
    keyring_backend, no_env, monkeypatch, tmp_path
):
    """The security half, stated where the list is.

    The labels are the trigger surface: adding one is what makes this machine run
    a harness. And the honest answer to "what about everything else that starts
    with the prefix" is that it stops the issue — not that it quietly runs
    somewhere. Silence about that would leave the grammar looking wider than the
    routes.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(daemon, "PID_FILE", tmp_path / "absent.pid")
    monkeypatch.setattr(daemon, "STATE_FILE", tmp_path / "absent.json")

    result = runner.invoke(app, ["info"])

    assert result.exit_code == 0, result.output
    out = flatten(result.stdout)
    assert "Add these in Linear by hand" in out
    assert "makes this machine run a harness" in out
    assert "never run on the default harness" in out


def test_info_names_the_prefix_the_watch_actually_uses(
    keyring_backend, no_env, monkeypatch, tmp_path
):
    """The watched set is wider than the routable set, and says so.

    `Router.filter()` asks Linear for the bare label *and anything starting with
    the prefix*, curated or not. Printing `<label>/<harness>` there implied the
    two sets were the same, which is exactly the assumption that would let a
    label nobody curated look harmless.
    """
    (tmp_path / "chord.toml").write_text('label = "Pick"\nharness = "opencode"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(daemon, "PID_FILE", tmp_path / "absent.pid")
    monkeypatch.setattr(daemon, "STATE_FILE", tmp_path / "absent.json")

    result = runner.invoke(app, ["info"])

    assert result.exit_code == 0, result.output
    assert "issues labelled 'Pick' or anything starting Pick/" in result.stdout
    assert "<harness>" not in result.stdout


def test_info_says_where_notifications_go(keyring_backend, no_env, monkeypatch, tmp_path):
    """Asked before one is sent, because the failure is invisible by
    construction: a notification that reaches no daemon raises nothing."""
    from chord import notify

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(daemon, "PID_FILE", tmp_path / "absent.pid")
    monkeypatch.setattr(daemon, "STATE_FILE", tmp_path / "absent.json")
    monkeypatch.setattr(notify, "backend", lambda: "terminal-notifier")

    result = runner.invoke(app, ["info"])

    assert result.exit_code == 0, result.output
    assert "notify     terminal-notifier" in result.stdout


def test_info_admits_when_no_notifier_exists(keyring_backend, no_env, monkeypatch, tmp_path):
    from chord import notify

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(daemon, "PID_FILE", tmp_path / "absent.pid")
    monkeypatch.setattr(daemon, "STATE_FILE", tmp_path / "absent.json")
    monkeypatch.setattr(notify, "backend", lambda: "none")

    result = runner.invoke(app, ["info"])

    assert result.exit_code == 0, result.output
    assert "no notifications are sent" in result.stdout


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


# --- `chord help` is a spelling of `chord --help` ---


def test_help_is_the_same_output_as_the_help_flag():
    """The whole point, so it is asserted as equality rather than by eye.

    `chord help` reads the answer from the same context `--help` is answered
    from, which is what keeps them from drifting apart as commands are added.
    """
    assert runner.invoke(app, ["help"]).output == runner.invoke(app, ["--help"]).output


def test_help_lists_the_commands():
    result = runner.invoke(app, ["help"])
    assert result.exit_code == 0
    for command in ("setup", "refresh", "start", "stop", "watch", "info"):
        assert command in result.stdout


def test_help_hides_the_internal_command():
    result = runner.invoke(app, ["help"])
    assert "_serve" not in result.stdout


@pytest.mark.parametrize("command", ["setup", "refresh", "start", "stop", "watch", "info"])
def test_help_with_a_command_matches_that_command_s_help_flag(command):
    """`chord help start` is what people type when they have forgotten the flags."""
    assert (
        runner.invoke(app, ["help", command]).output
        == runner.invoke(app, [command, "--help"]).output
    )


def test_help_for_a_command_names_that_command_in_the_usage():
    """The usage line must be `chord start`, not the bare `chord`."""
    result = runner.invoke(app, ["help", "start"])
    assert "start" in result.stdout
    assert "Start watching Linear in the background." in result.stdout


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_with_a_help_flag_is_the_same_request_as_bare_help(flag):
    """`chord help --help` should not report a missing command called `--help`."""
    assert runner.invoke(app, ["help", flag]).output == runner.invoke(app, ["help"]).output


def test_help_for_an_unknown_command_says_so_and_lists_the_real_ones():
    result = runner.invoke(app, ["help", "nonsense"])
    assert result.exit_code != 0
    assert "nonsense" in result.output
    assert "start" in result.output, "the error should say what is available"


def test_help_will_not_expose_the_internal_command():
    """`_serve` is hidden from the listing, so it is not reachable by name either.

    The error may still say what was asked for — that is how the person finds
    out they typed it wrong. What it must not do is hand over the hidden
    command's help or list it as available.
    """
    result = runner.invoke(app, ["help", "_serve"])
    assert result.exit_code != 0
    assert "Internal." not in result.output, "showed the hidden command's help"
    available = result.output.split("These are the ones:")[1]
    assert "_serve" not in available, "listed a command the listing hides"


def test_bare_help_succeeds():
    assert runner.invoke(app, ["help"]).exit_code == 0


def test_the_help_command_lists_itself():
    """It is a command, so it appears like one. Not hidden, not a special case."""
    assert "help" in runner.invoke(app, ["help"]).stdout
