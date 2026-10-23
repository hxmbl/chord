"""T3 Code CLI integration, with a local executable standing in for `t3`."""

import asyncio
import json
import os
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from chord import config, harness, watcher
from chord.cli import app
from chord.linear import IssuePage
from chord.routing import Router

runner = CliRunner()


def fake_t3(tmp_path, monkeypatch, *, start_output=None):
    """Install a fake CLI which records argv/stdin and emits scripted JSON."""
    directory = tmp_path / "bin"
    directory.mkdir()
    log = tmp_path / "calls.jsonl"
    executable = directory / "t3"
    start_json = json.dumps(
        start_output
        or {"threadId": "thread-1", "projectId": "project-1", "runId": "run-1"}
    )
    executable.write_text(
        f"""#!{sys.executable}
import json
import os
import sys

argv = sys.argv[1:]
stdin = sys.stdin.read()
with open(os.environ["T3_TEST_LOG"], "a") as stream:
    stream.write(json.dumps({{"argv": argv, "stdin": stdin, "cwd": os.getcwd()}}) + "\\n")
command = argv[1]
if command == "start":
    print({start_json!r})
else:
    raise SystemExit(64)
"""
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("T3_TEST_LOG", str(log))
    return executable, log


def calls_from(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_t3_dispatch_uses_resolved_project_and_safe_stdin(tmp_path, monkeypatch):
    _, log = fake_t3(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    options = config.T3Options(
        provider="provider-id",
        model="model-slug",
        worktree="main",
        branch="chord/eng-1",
    )

    result = asyncio.run(harness.T3CodeHarness(options).send("task body", cwd=project))

    assert result == {"thread_id": "thread-1", "project_id": "project-1"}
    call = calls_from(log)[0]
    assert call["argv"] == [
        "thread",
        "start",
        ".",
        "-",
        "--json",
        "--title",
        "Chord task",
        "--provider",
        "provider-id",
        "--model",
        "model-slug",
        "--worktree",
        "main",
        "--branch",
        "chord/eng-1",
    ]
    assert call["stdin"] == "task body"
    assert call["cwd"] == str(project)


def test_t3_missing_cli_has_installation_guidance(monkeypatch):
    monkeypatch.setattr(harness.shutil, "which", lambda _: None)
    with pytest.raises(harness.HarnessError, match="Install T3 Code"):
        harness.T3CodeHarness()


def test_t3_malformed_launch_output_is_held_as_ambiguous(tmp_path, monkeypatch):
    directory = tmp_path / "bin"
    directory.mkdir()
    executable = directory / "t3"
    executable.write_text(f"#!{sys.executable}\nprint('{{}}')\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ.get('PATH', '')}")

    with pytest.raises(harness.T3ClientError, match="prevent a duplicate") as caught:
        asyncio.run(harness.T3CodeHarness().send("task", cwd=tmp_path))
    assert caught.value.uncertain


def test_t3_timeout_is_reported_as_ambiguous(tmp_path, monkeypatch):
    directory = tmp_path / "bin"
    directory.mkdir()
    executable = directory / "t3"
    executable.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(harness, "HARNESS_TIMEOUT", 0.05)

    with pytest.raises(harness.T3ClientError, match="timed out") as caught:
        asyncio.run(harness.T3CodeHarness().send("task", cwd=tmp_path))
    assert caught.value.uncertain


class LinearStub:
    async def issues_for(self, _filter):
        return IssuePage(
            [{"id": "issue-id", "identifier": "ENG-1", "title": "A task"}], False
        )

    async def comments(self, _issue_id):
        return []

    async def label_history(self, _issue_id):
        return []


def repo(path):
    path.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def make_watcher(tmp_path, cli, state_path):
    project = repo(tmp_path / "project")
    return watcher.Watcher(
        LinearStub(),
        Router("Chord", "t3", factory=lambda _spec: cli),
        1,
        state_path,
        start_dir=project,
    )


def test_t3_dispatch_hands_over_once_and_keeps_no_thread_mapping(tmp_path, monkeypatch):
    fake_t3(tmp_path, monkeypatch)
    state = tmp_path / "state.json"
    cli = harness.T3CodeHarness()

    asyncio.run(make_watcher(tmp_path, cli, state).poll())

    saved = json.loads(state.read_text())
    assert "t3_threads" not in saved, "the thread id is T3 Code's, not Chord's"
    assert "issue-id" not in saved.get("t3_pending", {})

    restarted = make_watcher(tmp_path, cli, state)
    asyncio.run(restarted.poll())
    assert restarted.handed_over == 1
    assert len(calls_from(tmp_path / "calls.jsonl")) == 1, "sent exactly once"


def test_chord_has_no_t3_tunnel_commands(tmp_path, monkeypatch):
    """Dispatch and let go: there is deliberately no way back into a thread."""
    _, log = fake_t3(tmp_path, monkeypatch)

    for argv in (
        ["t3", "show", "ENG-1"],
        ["t3", "send", "ENG-1", "continue"],
    ):
        result = runner.invoke(app, argv)
        assert result.exit_code != 0, result.output
        assert "No such command" in result.output
    assert calls_from(log) == []


def test_interrupted_t3_dispatch_is_not_repeated(tmp_path, monkeypatch):
    fake_t3(tmp_path, monkeypatch)
    state = tmp_path / "state.json"
    cli = harness.T3CodeHarness()
    first = make_watcher(tmp_path, cli, state)
    first._mark_t3_pending("issue-id", "ENG-1")

    second = make_watcher(tmp_path, cli, state)
    asyncio.run(second.poll())

    assert calls_from(tmp_path / "calls.jsonl") == []
    saved = json.loads(state.read_text())
    assert saved["t3_pending"]["issue-id"]["identifier"] == "ENG-1"


def test_uncertain_t3_failure_stays_pending_and_does_not_retry(tmp_path, monkeypatch):
    fake_t3(tmp_path, monkeypatch)
    state = tmp_path / "state.json"
    cli = harness.T3CodeHarness()

    async def uncertain(_prompt, cwd=None):
        raise harness.T3ClientError("launch response lost", uncertain=True)

    cli.send = uncertain
    first = make_watcher(tmp_path, cli, state)
    asyncio.run(first.poll())
    assert "issue-id" in json.loads(state.read_text())["t3_pending"]

    second = make_watcher(tmp_path, cli, state)
    asyncio.run(second.poll())
    assert "issue-id" in json.loads(state.read_text())["t3_pending"]


def test_t3_configuration_validates_documented_option_pairs(tmp_path):
    path = tmp_path / "chord.toml"
    path.write_text('[t3]\nprovider = "provider-id"\n')
    with pytest.raises(config.ConfigError, match="must be set together"):
        config.load(path)

    path.write_text('[t3]\nbranch = "chord/eng-1"\n')
    with pytest.raises(config.ConfigError, match="requires `t3.worktree`"):
        config.load(path)
