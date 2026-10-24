"""Knowing the issue reached the harness, without Chord having to scream.

The watcher confirms delivery — the harness started, took the whole issue, and
was still running (or had exited 0) a moment later — and records it quietly:
one log line, and a status in `state.json` that `chord info` reads while the
harness is still working. The "received" banner fires at that same moment when
`[notify] received` is on (the default); with it off, the quiet record is the
whole confirmation. Failing to deliver stays loud, as it always was.

Delivery is kept apart from the harness's own result, because "it got there"
is worth knowing half an hour before "it finished".
"""

import asyncio
import json
import time

import pytest
from conftest import RecordingHarness
from typer.testing import CliRunner

import chord.harness as harnesses
from chord import daemon, watcher
from chord.cli import app
from chord.harness import CommandHarness, HarnessError
from chord.linear import IssuePage
from chord.routing import Router

runner = CliRunner()

ISSUE = {
    "id": "abc",
    "identifier": "ENG-9",
    "title": "Fix it",
    "createdAt": "Thu 2026-01-01 12:00 AM GMT (UTC+00:00)",
    "labels": {"nodes": [{"name": "Chord"}]},
}


@pytest.fixture(autouse=True)
def quick_grace(monkeypatch):
    """A short startup window, so a long-running stand-in doesn't cost a second."""
    monkeypatch.setattr(harnesses, "DELIVERY_GRACE", 0.2)


class Linear:
    async def issues_for(self, filter):
        return IssuePage([dict(ISSUE)], False)

    async def label_history(self, issue_id):
        return []

    async def comments(self, issue_id):
        return []


def watching(tmp_path, harness, *, notify_received=True):
    router = Router("Chord", "x", factory=lambda spec: harness)
    return watcher.Watcher(
        Linear(), router, 1, tmp_path / "state.json", notify_received=notify_received
    )


def delivery(tmp_path) -> dict:
    return json.loads((tmp_path / "state.json").read_text())["deliveries"]["abc"]


# --- what the harness can confirm ---


def test_delivery_is_confirmed_while_the_harness_is_still_running():
    """The whole point: it arrived, and the harness hasn't finished yet."""
    h = CommandHarness(["sh", "-c", "cat >/dev/null; sleep 1"])
    told = []

    async def scenario():
        task = asyncio.create_task(
            h.send("the issue", on_delivered=lambda: told.append(1))
        )
        deadline = time.monotonic() + 5
        while not told and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        running = not task.done()
        await task
        return running

    assert asyncio.run(scenario()), "delivery was only confirmed once it finished"
    assert told == [1], "confirmed exactly once"


def test_a_harness_that_reads_and_exits_cleanly_was_delivered():
    told = []
    asyncio.run(CommandHarness(["cat"]).send("x", on_delivered=lambda: told.append(1)))
    assert told == [1]


def test_a_harness_that_dies_on_startup_never_got_it():
    """A pipe accepting the bytes proves nothing if the reader is already dead."""
    told = []
    h = CommandHarness(["sh", "-c", "echo 'bad flag' >&2; exit 2"])
    with pytest.raises(HarnessError, match="exited 2: bad flag"):
        asyncio.run(h.send("x", on_delivered=lambda: told.append(1)))
    assert told == []


def test_a_harness_that_hangs_up_on_its_input_never_got_it():
    """It closed stdin before the issue was written, and kept running.

    Whatever it is doing, it isn't this issue, so it is stopped and the failure
    is loud. The issue is big enough that the write is still in progress when
    the harness hangs up, which is what makes this deterministic.
    """
    told = []
    h = CommandHarness(["sh", "-c", "exec 0<&-; sleep 5"])
    started = time.monotonic()
    with pytest.raises(HarnessError, match="closed its input"):
        asyncio.run(h.send("x" * 1_000_000, on_delivered=lambda: told.append(1)))
    assert told == []
    assert time.monotonic() - started < 4, "the harness was left running"


def test_a_harness_that_hung_up_before_the_first_write_never_got_it():
    """The race behind the one above: the input closed before anything was
    written. asyncio closes the pipe without an error then, and on some
    versions the write is dropped and reported as delivered."""

    async def scenario():
        process = await asyncio.create_subprocess_exec(
            "sh", "-c", "exec 0<&-; sleep 5", stdin=asyncio.subprocess.PIPE
        )
        try:
            assert process.stdin is not None
            for _ in range(100):  # until asyncio has noticed the hang-up
                if process.stdin.is_closing():
                    break
                await asyncio.sleep(0.02)
            return await harnesses._feed(process.stdin, b"x" * 1_000_000)
        finally:
            process.kill()
            await process.wait()

    assert asyncio.run(scenario()) is False


def test_a_prompt_on_the_command_line_is_delivered_once_it_is_running(tmp_path):
    """Cursor's mode: nothing on stdin, so delivery is "started and stayed up"."""
    told = []
    h = CommandHarness(["sh", "-c", 'sleep 0.5; test -n "$0"'], prompt_as_argument=True)
    asyncio.run(h.send("the issue", on_delivered=lambda: told.append(1)))
    assert told == [1]


def test_the_print_harness_confirms_too(capsys):
    told = []
    asyncio.run(harnesses.PrintHarness().send("x", on_delivered=lambda: told.append(1)))
    assert told == [1]


# --- what the watcher records ---


def test_the_state_says_delivered_before_the_harness_finishes(tmp_path, banners):
    """`chord info` reads this file, so this is what somebody sees mid-run."""
    h = CommandHarness(["sh", "-c", "cat >/dev/null; sleep 1"], "claude")
    w = watching(tmp_path, h, notify_received=False)
    seen = []

    async def scenario():
        task = asyncio.create_task(w.poll())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not task.done():
            path = tmp_path / "state.json"
            if path.exists() and "deliveries" in path.read_text():
                seen.append(delivery(tmp_path)["status"])
                break
            await asyncio.sleep(0.02)
        await task

    asyncio.run(scenario())

    assert seen == [watcher.DELIVERED]
    after = delivery(tmp_path)
    assert after["status"] == watcher.FINISHED
    assert after["harness"] == "claude" and after["identifier"] == "ENG-9"
    assert after["delivered_at"] <= after["ended_at"]
    assert banners.said == [], "with `received` off, a delivery that worked is quiet"


def test_the_received_banner_fires_on_delivery_not_at_the_end(tmp_path, banners):
    """The banner says it got there, so it goes out while the harness works.

    Not as the hand-over starts (a harness that dies on startup never got it)
    and not when it finishes (that can be half an hour later).
    """
    h = CommandHarness(["sh", "-c", "cat >/dev/null; sleep 1"], "claude")
    w = watching(tmp_path, h)
    while_running = []

    async def scenario():
        task = asyncio.create_task(w.poll())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not task.done():
            if banners.said:
                while_running.append(list(banners.said))
                break
            await asyncio.sleep(0.02)
        await task

    asyncio.run(scenario())

    assert while_running == [[("ENG-9 received", "Delivered to claude.")]]
    assert banners.titles == ["ENG-9 received"], "one banner, not one per end"


def test_delivery_is_one_calm_log_line(tmp_path, capsys, banners):
    asyncio.run(watching(tmp_path, RecordingHarness("claude")).poll())
    out = capsys.readouterr().out
    assert "INFO    delivered to claude." in out
    assert "claude finished." in out
    assert "WARNING" not in out.split("delivered to claude.")[1]


def test_delivered_then_failed_is_told_apart_from_never_delivered(tmp_path, banners):
    h = CommandHarness(["sh", "-c", "cat >/dev/null; sleep 0.4; exit 3"], "claude")
    asyncio.run(watching(tmp_path, h).poll())
    entry = delivery(tmp_path)
    assert entry["status"] == watcher.FAILED
    assert "delivered_at" in entry, "it did arrive, and that is worth keeping"
    # It did arrive, so it was announced; the failure lands second and, sharing
    # the issue's group, replaces it.
    assert banners.titles == ["ENG-9 received", "ENG-9 failed"]


def test_received_off_still_fails_loudly(tmp_path, banners):
    h = CommandHarness(["sh", "-c", "cat >/dev/null; sleep 0.4; exit 3"], "claude")
    asyncio.run(watching(tmp_path, h, notify_received=False).poll())
    assert delivery(tmp_path)["status"] == watcher.FAILED
    assert banners.titles == ["ENG-9 failed"], "a failure stays loud"


def test_a_harness_that_never_got_it_is_loud_and_says_so(tmp_path, capsys, banners):
    h = CommandHarness(["sh", "-c", "exit 4"], "claude")
    asyncio.run(watching(tmp_path, h).poll())
    entry = delivery(tmp_path)
    assert entry["status"] == watcher.UNDELIVERED
    assert "delivered_at" not in entry
    assert "it never reached claude." in capsys.readouterr().out
    assert banners.titles == ["ENG-9 failed"]


def test_a_missing_command_is_not_delivered(tmp_path, banners):
    h = CommandHarness(["/nonexistent/claude"], "claude")
    asyncio.run(watching(tmp_path, h).poll())
    assert delivery(tmp_path)["status"] == watcher.UNDELIVERED
    assert banners.titles == ["ENG-9 failed"]


def test_the_record_survives_a_restart(tmp_path, banners):
    asyncio.run(watching(tmp_path, RecordingHarness("claude")).poll())
    again = watching(tmp_path, RecordingHarness("claude"))
    assert again.handed_over == 1
    assert watcher.read_deliveries(tmp_path / "state.json")[0]["status"] == (
        watcher.FINISHED
    )


def test_a_state_file_from_before_deliveries_still_loads(tmp_path):
    (tmp_path / "state.json").write_text(
        '{"handed_over": {"x": "2026-01-01T00:00:00Z"}}'
    )
    assert watcher.read_deliveries(tmp_path / "state.json") == []
    assert watching(tmp_path, RecordingHarness()).handed_over == 1


def test_a_mangled_delivery_record_is_dropped_not_fatal(tmp_path):
    """A record for people, never consulted on whether to run anything."""
    (tmp_path / "state.json").write_text(
        '{"handed_over": {}, "deliveries": {"a": 3, "b": {"status": 1}}}'
    )
    assert watcher.read_deliveries(tmp_path / "state.json") == []


# --- what `chord info` shows ---


def info_with(tmp_path, monkeypatch, deliveries, pid):
    (tmp_path / "chord.toml").write_text('harness = "print"\n')
    (tmp_path / "state.json").write_text(
        json.dumps(
            {"handed_over": dict.fromkeys(deliveries, "x"), "deliveries": deliveries}
        )
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(daemon, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(daemon, "running_pid", lambda: pid)
    result = runner.invoke(app, ["info"])
    assert result.exit_code == 0, result.output
    return result.output


def stamp(seconds_ago: float = 0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


def test_info_shows_a_delivery_that_is_still_running(no_env, monkeypatch, tmp_path):
    now = stamp()
    local = time.strftime("%H:%M", time.localtime(time.time()))
    out = info_with(
        tmp_path,
        monkeypatch,
        {
            "abc": {
                "identifier": "ENG-9",
                "harness": "claude",
                "status": "delivered",
                "delivered_at": now,
            }
        },
        pid=4242,
    )
    assert f"delivery   ENG-9 delivered to claude at {local}, still running" in out


def test_info_lists_the_newest_first_and_tells_each_outcome(
    no_env, monkeypatch, tmp_path
):
    out = info_with(
        tmp_path,
        monkeypatch,
        {
            "1": {
                "identifier": "ENG-1",
                "harness": "codex",
                "status": "not delivered",
                "ended_at": stamp(60),
            },
            "2": {
                "identifier": "ENG-2",
                "harness": "claude",
                "status": "failed",
                "delivered_at": stamp(50),
                "ended_at": stamp(40),
            },
            "3": {
                "identifier": "ENG-3",
                "harness": "opencode",
                "status": "finished",
                "delivered_at": stamp(30),
                "ended_at": stamp(20),
            },
        },
        pid=None,
    )
    lines = [line.strip() for line in out.splitlines()]
    first = next(i for i, line in enumerate(lines) if line.startswith("delivery"))
    assert "ENG-3 delivered to opencode at" in lines[first]
    assert "finished" in lines[first]
    assert "ENG-2 delivered to claude at" in lines[first + 1]
    assert "failed" in lines[first + 1]
    assert "ENG-1 NOT delivered to codex" in lines[first + 2]


def test_info_shows_what_the_opencode_default_runs(no_env, monkeypatch, tmp_path):
    (tmp_path / "chord.toml").write_text('harness = "opencode"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(daemon, "STATE_FILE", tmp_path / "absent.json")
    monkeypatch.setattr(daemon, "running_pid", lambda: None)
    result = runner.invoke(app, ["info"])
    assert "opencode  (opencode run)" in result.output
    assert "delivery" not in result.output, "nothing delivered, nothing to show"
