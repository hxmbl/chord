"""T3 Code CLI integration, with a local executable standing in for `t3`."""

import asyncio
import json
import os
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from chord import config, daemon, harness, watcher
from chord.cli import app
from chord.linear import IssuePage
from chord.routing import Router

runner = CliRunner()


def fake_t3(tmp_path, monkeypatch, *, start_output=None, show_output=None, send_output=None):
    """Install a fake CLI which records argv/stdin and emits scripted JSON."""
    directory = tmp_path / "bin"
    directory.mkdir()
    log = tmp_path / "calls.jsonl"
    executable = directory / "t3"
    start_json = json.dumps(
        start_output
        or {"threadId": "thread-1", "projectId": "project-1", "runId": "run-1"}
    )
    show_json = json.dumps(
        show_output
        or {
            "thread": {
                "threadId": "thread-1",
                "status": "running",
                "pendingRequestCount": 0,
            },
            "messages": [{"role": "assistant", "text": "working"}],
        }
    )
    send_json = json.dumps(
        send_output or {"threadId": "thread-1", "runId": "run-2", "delivery": "sent"}
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
elif command == "show":
    print({show_json!r})
elif command == "send":
    result = json.loads({send_json!r})
    print(json.dumps(result))
    if "status" in result:
        raise SystemExit(0 if result["status"] == "completed" else 2 if result["status"] == "waiting" else 1)
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


def test_t3_follow_up_and_inspection_parse_documented_shapes(tmp_path, monkeypatch):
    _, log = fake_t3(tmp_path, monkeypatch)
    cli = harness.T3CodeHarness()

    shown = asyncio.run(cli.show_thread("thread-1"))
    sent = asyncio.run(cli.send_follow_up("thread-1", "continue"))

    assert shown["thread"]["status"] == "running"
    assert shown["messages"][0]["text"] == "working"
    assert sent["delivery"] == "sent"
    calls = calls_from(log)
    assert calls[0]["argv"] == ["thread", "show", "--json", "thread-1"]
    assert calls[1]["argv"] == ["thread", "send", "thread-1", "-", "--json"]
    assert calls[1]["stdin"] == "continue"


@pytest.mark.parametrize(
    ("status", "returncode"),
    [("completed", 0), ("failed", 1), ("waiting", 2)],
)
def test_t3_wait_distinguishes_completion_failure_and_input(
    tmp_path, monkeypatch, status, returncode
):
    result = {
        "threadId": "thread-1",
        "runId": "run-2",
        "status": status,
        "reply": "finished" if status == "completed" else None,
        "error": "provider failed" if status == "failed" else None,
    }
    directory = tmp_path / "bin"
    directory.mkdir()
    executable = directory / "t3"
    encoded = json.dumps(result)
    executable.write_text(
        f"#!{sys.executable}\nprint({encoded!r})\nraise SystemExit({returncode})\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ.get('PATH', '')}")

    parsed = asyncio.run(
        harness.T3CodeHarness().send_follow_up("thread-1", "continue", wait=True)
    )
    assert parsed["status"] == status
    assert parsed["reply"] == result["reply"]


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
    executable.write_text(
        f"#!{sys.executable}\nimport time\ntime.sleep(30)\n"
    )
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


def test_t3_thread_mapping_survives_restart(tmp_path, monkeypatch):
    fake_t3(tmp_path, monkeypatch)
    state = tmp_path / "state.json"
    cli = harness.T3CodeHarness()

    asyncio.run(make_watcher(tmp_path, cli, state).poll())

    saved = json.loads(state.read_text())
    assert saved["t3_threads"]["issue-id"] == {
        "thread_id": "thread-1",
        "identifier": "ENG-1",
    }
    assert "issue-id" not in saved.get("t3_pending", {})

    restarted = make_watcher(tmp_path, cli, state)
    asyncio.run(restarted.poll())
    assert restarted.handed_over == 1


def test_chord_commands_use_only_the_saved_issue_thread(tmp_path, monkeypatch):
    _, log = fake_t3(tmp_path, monkeypatch)
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "handed_over": {"issue-id": "now"},
                "t3_threads": {
                    "issue-id": {"thread_id": "thread-1", "identifier": "ENG-1"}
                },
            }
        )
    )
    monkeypatch.setattr(daemon, "STATE_FILE", state)

    shown = runner.invoke(app, ["t3", "show", "ENG-1"])
    sent = runner.invoke(app, ["t3", "send", "issue-id", "continue"])
    unknown = runner.invoke(app, ["t3", "show", "thread-not-mapped"])

    assert shown.exit_code == 0, shown.output
    assert "ENG-1  thread-1  running" in shown.output
    assert sent.exit_code == 0, sent.output
    assert "Follow-up sent" in sent.output
    assert unknown.exit_code == 1
    assert len(calls_from(log)) == 2


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [("completed", 0), ("failed", 1), ("waiting", 2)],
)
def test_chord_follow_up_reports_terminal_state(
    tmp_path, monkeypatch, status, exit_code
):
    _, log = fake_t3(
        tmp_path,
        monkeypatch,
        send_output={
            "threadId": "thread-1",
            "runId": "run-2",
            "status": status,
            "reply": "finished" if status == "completed" else None,
            "error": "provider failed" if status == "failed" else None,
        },
    )
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "handed_over": {"issue-id": "now"},
                "t3_threads": {
                    "issue-id": {"thread_id": "thread-1", "identifier": "ENG-1"}
                },
            }
        )
    )
    monkeypatch.setattr(daemon, "STATE_FILE", state)

    result = runner.invoke(app, ["t3", "send", "ENG-1", "continue", "--wait"])

    assert result.exit_code == exit_code, result.output
    assert f"T3 run status: {status}." in result.output
    assert len(calls_from(log)) == 1


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
