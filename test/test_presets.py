"""The coding-agent presets, the PATH check, and what a failing harness says.

Claude Code, Codex and Cursor each have a non-interactive spelling that is easy
to get subtly wrong, and a wrong one fails quietly: an agent that cannot write
files exits 0 and looks like it worked. So the flags are pinned here, and each
preset is run against a stand-in executable to prove where the issue goes —
stdin for two of them, the command line for Cursor.
"""

import asyncio
import os
import stat
from pathlib import Path

import pytest
from conftest import RecordingHarness
from typer.testing import CliRunner

from chord import daemon, harness, watcher
from chord.cli import app
from chord.harness import CommandHarness, HarnessError, build, missing
from chord.linear import IssuePage
from chord.routing import Router

runner = CliRunner()


def on_path(monkeypatch, *found):
    """Only `found` are installed, as far as Chord can tell."""
    monkeypatch.setattr(
        harness.shutil, "which", lambda name: f"/bin/{name}" if name in found else None
    )


def stand_in(directory: Path, name: str) -> Path:
    """An executable called `name` that writes down its arguments and stdin."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text(
        "#!/bin/sh\n"
        'out="$(dirname "$0")/record"\n'
        ': > "$out.args"\n'
        'for arg in "$@"; do printf "%s\\n" "$arg" >> "$out.args"; done\n'
        'cat > "$out.stdin"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


# --- the presets, by name ---


@pytest.mark.parametrize(
    ("name", "argv"),
    [
        ("claude", ["/bin/claude", "-p", "--permission-mode", "acceptEdits"]),
        ("codex", ["/bin/codex", "exec", "--sandbox", "workspace-write", "-"]),
        ("cursor", ["/bin/cursor-agent", "-p", "--force", "--trust"]),
    ],
)
def test_each_preset_runs_its_tools_non_interactive_command(monkeypatch, name, argv):
    on_path(monkeypatch, "claude", "codex", "cursor-agent")
    built = build(name)
    assert isinstance(built, CommandHarness)
    assert built.argv == argv
    assert built.name == name, "the banner should say claude, not a path"


def test_a_curated_table_naming_a_preset_gets_the_preset(monkeypatch):
    """`command = ["claude"]` means the preset, as `["t3"]` means T3."""
    on_path(monkeypatch, "claude")
    built = build(["claude"])
    assert isinstance(built, CommandHarness)
    assert built.argv[1:] == ["-p", "--permission-mode", "acceptEdits"]


def test_a_longer_command_is_left_exactly_as_written(monkeypatch):
    on_path(monkeypatch, "claude")
    built = build(["claude", "-p", "--model", "opus"])
    assert isinstance(built, CommandHarness)
    assert built.argv == ["claude", "-p", "--model", "opus"]


def test_cursor_falls_back_to_its_newer_name(monkeypatch):
    on_path(monkeypatch, "agent")
    built = build("cursor")
    assert isinstance(built, CommandHarness)
    assert built.argv[0] == "/bin/agent"
    assert built.prompt_as_argument


def test_a_missing_preset_says_what_to_install(monkeypatch):
    on_path(monkeypatch)
    with pytest.raises(HarnessError, match=r"`claude` isn't on your PATH.*Claude Code"):
        build("claude")


# --- where the issue goes ---


def test_claude_reads_the_issue_on_stdin(tmp_path, monkeypatch):
    stand_in(tmp_path / "bin", "claude")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    asyncio.run(build("claude").send("the issue", cwd=tmp_path))
    record = tmp_path / "bin" / "record"
    assert (record.with_suffix(".stdin")).read_text() == "the issue"
    assert (record.with_suffix(".args")).read_text().split() == [
        "-p",
        "--permission-mode",
        "acceptEdits",
    ]


def test_the_opencode_default_runs_opencode_run_with_the_issue_on_stdin(
    tmp_path, monkeypatch
):
    """Plain `opencode` is the TUI, which waits for a person who isn't there.

    `opencode run` with no message argument reads the prompt from stdin when
    stdin isn't a terminal (https://opencode.ai/docs/cli/#run), so the issue
    goes there, as for every other stdin harness, and nothing else is passed.
    """
    stand_in(tmp_path / "bin", "opencode")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    built = build("opencode")
    assert built.name == "opencode"
    asyncio.run(built.send("the issue", cwd=tmp_path))
    record = tmp_path / "bin" / "record"
    assert record.with_suffix(".stdin").read_text() == "the issue"
    assert record.with_suffix(".args").read_text().split() == ["run"]


def test_codex_reads_the_issue_on_stdin(tmp_path, monkeypatch):
    stand_in(tmp_path / "bin", "codex")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    asyncio.run(build("codex").send("the issue", cwd=tmp_path))
    record = tmp_path / "bin" / "record"
    assert record.with_suffix(".stdin").read_text() == "the issue"
    assert record.with_suffix(".args").read_text().split()[-1] == "-"


def test_cursor_gets_the_issue_as_its_last_argument(tmp_path, monkeypatch):
    """Cursor's print mode rejects a prompt on stdin, so it goes on the line."""
    stand_in(tmp_path / "bin", "cursor-agent")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    asyncio.run(build("cursor").send("line one\nline two", cwd=tmp_path))
    record = tmp_path / "bin" / "record"
    args = record.with_suffix(".args").read_text()
    assert args.startswith("-p\n--force\n--trust\n")
    assert args.endswith("line one\nline two\n"), "the issue wasn't one argument"
    assert record.with_suffix(".stdin").read_text() == ""


def test_an_issue_too_long_for_the_command_line_is_refused_before_running(
    tmp_path, monkeypatch
):
    stand_in(tmp_path / "bin", "cursor-agent")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    long = "x" * (harness.MAX_PROMPT_ARGUMENT + 1)
    with pytest.raises(HarnessError, match="too long for `cursor`"):
        asyncio.run(build("cursor").send(long, cwd=tmp_path))
    assert not (tmp_path / "bin" / "record.args").exists(), "it ran anyway"


# --- the PATH check ---


def test_missing_names_the_command_that_isnt_installed(monkeypatch):
    on_path(monkeypatch, "claude")
    assert missing(["claude", "-p"]) is None
    assert missing(["my-agent", "--go"]) == "`my-agent` isn't on your PATH."
    assert missing("opencode") == "`opencode` isn't on your PATH."
    assert missing("print") is None
    assert "Cursor CLI" in (missing("cursor") or "")
    assert missing(["cursor"]) == missing("cursor")


def test_a_command_given_by_path_is_not_second_guessed(monkeypatch):
    """It runs in the issue's repository, which isn't known before an issue."""
    on_path(monkeypatch)
    assert missing(["./scripts/on-issue.sh"]) is None


def test_problems_lists_every_route_that_cannot_start(monkeypatch):
    on_path(monkeypatch, "claude")
    router = Router(
        "Chord",
        "opencode",
        {"fast": ["claude"], "mine": ["my-agent"], "cur": "cursor"},
    )
    found = {route.label: reason for route, reason in router.problems()}
    assert set(found) == {"Chord", "Chord -> mine", "Chord -> cur"}
    assert "opencode" in found["Chord"]
    assert "my-agent" in found["Chord -> mine"]
    assert "cursor-agent" in found["Chord -> cur"]


def test_start_refuses_a_route_whose_tool_is_missing(
    stored_token, no_env, monkeypatch, tmp_path
):
    stored_token()
    (tmp_path / "chord.toml").write_text(
        'harness = "print"\n\n[harnesses.review]\ncommand = ["codex"]\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(daemon, "STATE_FILE", tmp_path / "state.json")
    on_path(monkeypatch)
    spawned = []
    monkeypatch.setattr(daemon, "start", lambda: spawned.append(1) or 1)

    result = runner.invoke(app, ["start"])

    assert result.exit_code == 1
    assert not spawned, "started a watcher that would fail every review issue"
    assert "Chord -> review can't be run" in result.output
    assert "Codex" in result.output


def test_info_marks_a_route_that_cannot_start(no_env, monkeypatch, tmp_path):
    (tmp_path / "chord.toml").write_text(
        'harness = "print"\n\n[harnesses.review]\ncommand = ["codex"]\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(daemon, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(daemon, "running_pid", lambda: None)
    on_path(monkeypatch)

    result = runner.invoke(app, ["info"])

    assert result.exit_code == 0, result.output
    assert "Chord -> review  codex" in result.output
    assert "! can't run: `codex` isn't on your PATH" in result.output


# --- what a failure says ---


def test_a_non_zero_exit_carries_the_last_thing_it_said():
    h = CommandHarness(["sh", "-c", "echo working; echo 'auth expired' >&2; exit 4"])
    with pytest.raises(HarnessError, match=r"exited 4: auth expired\. Its full output"):
        asyncio.run(h.send("x"))


def test_a_silent_failure_says_it_was_silent():
    h = CommandHarness(["sh", "-c", "exit 2"])
    with pytest.raises(HarnessError, match="exited 2 without saying why"):
        asyncio.run(h.send("x"))


def test_a_timeout_says_how_to_allow_longer(monkeypatch):
    monkeypatch.setattr(harness, "HARNESS_TIMEOUT", 0.2)
    h = CommandHarness(["sleep", "5"])
    with pytest.raises(HarnessError, match="timed out.*HARNESS_TIMEOUT"):
        asyncio.run(h.send("x"))


def test_a_failed_hand_over_says_how_to_retry_it(tmp_path, capsys, banners):
    """Recorded, as before, so it isn't retried forever — and the log says how."""

    class Stub:
        async def issues_for(self, filter):
            return IssuePage(
                [
                    {
                        "id": "abc",
                        "identifier": "ENG-7",
                        "title": "t",
                        "createdAt": "2026-01-01T00:00:00Z",
                        "labels": {"nodes": [{"name": "Chord"}]},
                    }
                ],
                False,
            )

        async def label_history(self, issue_id):
            return []

        async def comments(self, issue_id):
            return []

    failing = RecordingHarness("claude", fail=HarnessError("`claude` exited 1: no."))
    w = watcher.Watcher(
        Stub(), Router("Chord", "x", factory=lambda s: failing), 1, tmp_path / "s.json"
    )
    asyncio.run(w.poll())

    out = capsys.readouterr().out
    assert "ENG-7 didn't finish: `claude` exited 1: no." in out
    assert "to retry it: drop abc from s.json." in out
    assert w.handed_over == 1
    assert banners.titles[-1] == "ENG-7 failed"


def test_setup_offers_every_preset_it_finds(monkeypatch):
    from chord import setup as guided

    on_path(monkeypatch, "codex", "cursor-agent")
    assert guided.installed_tools() == ["codex", "cursor"]
