"""Notifications: the two halves of one banner, and whether it gets anywhere.

These matter more than their size suggests. The failure they guard against is
invisible by construction — a notification that reaches no daemon raises nothing,
so "Chord never told me anything" and "nothing happened" look identical from the
outside. And the readability half is the one nobody notices is broken until a
banner says "done" over an exception message.
"""

import asyncio
import signal
import sys
from types import SimpleNamespace

import pytest

from chord import notify

# --- helpers ---


def on(monkeypatch, platform: str) -> None:
    """Pretend we're on `platform`, without touching the real `sys`."""
    monkeypatch.setattr(notify, "sys", SimpleNamespace(platform=platform))


class Recorder:
    """Stands in for `_deliver`, and remembers what it was asked to deliver."""

    def __init__(self, results: list[bool] | None = None) -> None:
        self.commands: list[list[str]] = []
        self.results = list(results or [])

    async def __call__(self, command: list[str]) -> bool:
        self.commands.append(command)
        return self.results.pop(0) if self.results else True

    @property
    def banners(self) -> list[tuple[str, str]]:
        """The (title, message) of every route that was posted.

        Read back off the argv rather than off a recorded argument, because
        that is where a title that got mangled on the way would show up.
        """
        return [
            (
                command[command.index("-title") + 1],
                command[command.index("-message") + 1],
            )
            if "-message" in command
            else (command[-2], command[-1])
            for command in self.commands
        ]


def recorder(monkeypatch, results: list[bool] | None = None) -> Recorder:
    rec = Recorder(results)
    monkeypatch.setattr(notify, "_deliver", rec)
    return rec


def no_tools(monkeypatch) -> None:
    """A machine with neither macOS notifier installed."""
    monkeypatch.setattr(notify, "_terminal_notifier", lambda: None)
    monkeypatch.setattr(notify, "_osascript", lambda: None)


def only_osascript(monkeypatch) -> None:
    """The common macOS: the built-in, and no cask installed."""
    monkeypatch.setattr(notify, "_terminal_notifier", lambda: None)
    monkeypatch.setattr(notify, "_osascript", lambda: "/usr/bin/osascript")


def run(coro):
    return asyncio.run(coro)


# --- the two halves of a banner ---


def test_each_end_says_what_happened_and_why(monkeypatch):
    """A title you can act on, over a line that tells you how."""
    on(monkeypatch, "darwin")
    only_osascript(monkeypatch)
    rec = recorder(monkeypatch)

    run(notify.received("ENG-201", "opencode"))
    run(notify.failed("ENG-201", "`opencode` exited 1."))
    run(notify.skipped("ENG-201", "'Chord/typo' names a harness chord.toml lacks."))
    run(notify.refused("ENG-201", "sam added 'Chord', and that isn't allowed."))

    assert rec.banners == [
        ("ENG-201 received", "Sent to opencode."),
        ("ENG-201 failed", "`opencode` exited 1."),
        ("ENG-201 skipped", "'Chord/typo' names a harness chord.toml lacks."),
        ("ENG-201 refused", "sam added 'Chord', and that isn't allowed."),
    ]


def test_a_hand_over_is_announced_once_not_at_both_ends(monkeypatch):
    """One banner per issue, which is the point.

    It was two: "started" when the hand-over began and "finished" when it ended.
    Two notifications saying one thing — the issue went to a harness — and a
    backlog of ten is twenty banners, which is how a notification gets ignored.
    """
    on(monkeypatch, "darwin")
    only_osascript(monkeypatch)
    rec = recorder(monkeypatch)

    run(notify.received("ENG-201", "opencode"))

    assert len(rec.banners) == 1
    assert not hasattr(notify, "finished"), "there is no second end to announce"


def test_the_four_ends_are_distinguishable_at_a_glance(monkeypatch):
    """Four outcomes, four titles. A reader who sees only the title has to be
    able to tell a working agent from a refused one."""
    on(monkeypatch, "darwin")
    only_osascript(monkeypatch)
    rec = recorder(monkeypatch)

    for verb in ("received", "failed", "skipped", "refused"):
        run(getattr(notify, verb)("ENG-201", "because"))

    titles = [title for title, _ in rec.banners]
    assert titles == [
        "ENG-201 received",
        "ENG-201 failed",
        "ENG-201 skipped",
        "ENG-201 refused",
    ]
    assert len(set(titles)) == len(titles)


def test_a_refusal_does_not_read_as_a_mistake(monkeypatch):
    """It is not one. It is the allowlist working, and filing it under the same
    verb as a typo in chord.toml would be the wrong thing to imply."""
    on(monkeypatch, "darwin")
    only_osascript(monkeypatch)
    rec = recorder(monkeypatch)

    run(notify.refused("ENG-201", "sam is not in allowed_actors."))
    run(notify.skipped("ENG-201", "Chord/typo names no harness."))

    assert rec.banners[0][0] != rec.banners[1][0]


def test_a_failure_never_claims_to_have_finished(monkeypatch):
    """The bug this shape exists to make impossible.

    It happened: a hand-over that raised sent "Chord done" over "ENG-201 did not
    finish". One banner that says there is nothing to do, and why there is.
    """
    on(monkeypatch, "darwin")
    only_osascript(monkeypatch)
    rec = recorder(monkeypatch)

    run(notify.failed("ENG-201", RuntimeError("exited 1")))

    (title, message), = rec.banners
    assert title == "ENG-201 failed"
    for wrong in ("done", "finished", "complete"):
        assert wrong not in title.lower(), f"{title!r} contradicts {message!r}"


def test_the_reason_survives_into_the_body(monkeypatch):
    """Not summarised away: it is the half that makes the banner actionable."""
    on(monkeypatch, "darwin")
    only_osascript(monkeypatch)
    rec = recorder(monkeypatch)

    run(notify.failed("ENG-201", "Harness `claude` timed out after 1800s."))

    assert rec.banners == [("ENG-201 failed", "Harness `claude` timed out after 1800s.")]


def test_a_multi_line_reason_is_flattened(monkeypatch):
    """An exception message can be several lines, and carries terminal escapes.

    A banner is rendered by another process, and a newline in a notification
    body is either truncated or a second banner nobody can tie to an issue.
    """
    on(monkeypatch, "darwin")
    only_osascript(monkeypatch)
    rec = recorder(monkeypatch)

    run(notify.failed("ENG-201", "line one\nline two\x1b[31m red"))

    (_title, message), = rec.banners
    assert "\n" not in message and "\x1b" not in message
    assert message == "line one line two [31m red"


def test_outcomes_for_one_issue_share_a_group(monkeypatch):
    """So a later outcome replaces an earlier one instead of stacking.

    The case that needs this is a retry: an issue skipped for an uncurated
    label gets the fix, is handed over, and the banner that says so replaces the
    one saying it was skipped rather than sitting under it. Someone reading
    Notification Center then sees the current state rather than the history.
    """
    on(monkeypatch, "darwin")
    monkeypatch.setattr(notify, "_terminal_notifier", lambda: "/bin/tn")
    monkeypatch.setattr(notify, "_osascript", lambda: None)
    rec = recorder(monkeypatch)

    run(notify.skipped("ENG-201", "Chord/typo names no harness."))
    run(notify.received("ENG-201", "opencode"))

    groups = {command[command.index("-group") + 1] for command in rec.commands}
    assert len(groups) == 1, "two outcomes for one issue should land in one group"


def test_different_issues_do_not_share_a_group(monkeypatch):
    """Otherwise one issue's outcome wipes another's banner."""
    on(monkeypatch, "darwin")
    monkeypatch.setattr(notify, "_terminal_notifier", lambda: "/bin/tn")
    monkeypatch.setattr(notify, "_osascript", lambda: None)
    rec = recorder(monkeypatch)

    run(notify.received("ENG-201", "opencode"))
    run(notify.received("ENG-202", "opencode"))

    groups = {command[command.index("-group") + 1] for command in rec.commands}
    assert len(groups) == 2


# --- getting it to the daemon ---


def test_the_text_goes_over_as_arguments_not_as_source(monkeypatch):
    """An issue title is somebody else's words.

    The usual spelling of this call concatenates the title into the script, so a
    quote in one is a way to run code off an issue. Every title and message has
    to arrive as its own argv element, with nothing of it inside the source.
    """
    on(monkeypatch, "darwin")
    only_osascript(monkeypatch)
    rec = recorder(monkeypatch)

    hostile = '"; do shell script "touch /tmp/pwned"; "'
    run(notify.failed("ENG-201", hostile))

    (command,) = rec.commands
    script = command[command.index("-e") + 1]
    assert hostile not in script
    assert "do shell script" not in script
    assert command[-1] == hostile, "it should be one argument, passed as-is"


def test_terminal_notifier_is_preferred_when_it_is_there(monkeypatch):
    """It is a signed bundle, so the banner is attributed to it and not to
    Script Editor — which is the only reason it is worth preferring."""
    on(monkeypatch, "darwin")
    monkeypatch.setattr(notify, "_terminal_notifier", lambda: "/opt/tn")
    monkeypatch.setattr(notify, "_osascript", lambda: "/usr/bin/osascript")
    rec = recorder(monkeypatch)

    run(notify.received("ENG-201", "opencode"))

    assert len(rec.commands) == 1
    assert rec.commands[0][0] == "/opt/tn"


def test_osascript_covers_the_machine_with_no_cask_on_it(monkeypatch):
    """The common case, and the reason the fallback is not optional."""
    on(monkeypatch, "darwin")
    monkeypatch.setattr(notify, "_terminal_notifier", lambda: None)
    monkeypatch.setattr(notify, "_osascript", lambda: "/usr/bin/osascript")
    rec = recorder(monkeypatch)

    run(notify.received("ENG-201", "opencode"))

    assert len(rec.commands) == 1
    assert rec.commands[0][0] == "/usr/bin/osascript"


def test_one_notifier_failing_is_not_a_reason_to_drop_the_banner(monkeypatch):
    """The two fail for different reasons, so the second is worth a try."""
    on(monkeypatch, "darwin")
    monkeypatch.setattr(notify, "_terminal_notifier", lambda: "/opt/tn")
    monkeypatch.setattr(notify, "_osascript", lambda: "/usr/bin/osascript")
    rec = recorder(monkeypatch, [False])

    run(notify.received("ENG-201", "opencode"))

    assert len(rec.commands) == 2, "it should have tried osascript after"


def test_a_successful_notifier_is_not_asked_twice(monkeypatch):
    on(monkeypatch, "darwin")
    monkeypatch.setattr(notify, "_terminal_notifier", lambda: "/opt/tn")
    monkeypatch.setattr(notify, "_osascript", lambda: "/usr/bin/osascript")
    rec = recorder(monkeypatch, [True])

    run(notify.received("ENG-201", "opencode"))

    assert len(rec.commands) == 1


def test_a_machine_with_nothing_to_notify_with_is_silent(monkeypatch):
    """Which is a normal state, not an error. `chord info` is what reports it."""
    on(monkeypatch, "darwin")
    no_tools(monkeypatch)
    rec = recorder(monkeypatch)

    run(notify.received("ENG-201", "opencode"))

    assert rec.commands == []


# --- the desktop_notifier library, off macOS ---


def test_the_library_still_serves_linux(monkeypatch):
    """Kept, because there it works: Linux goes to the session daemon over
    D-Bus, and that has never needed a bundle."""
    on(monkeypatch, "linux")
    sent = []

    class Fake:
        async def send(self, title, message):
            sent.append((title, message))

    monkeypatch.setitem(sys.modules, "desktop_notifier", SimpleNamespace(DesktopNotifier=Fake))
    run(notify.received("ENG-201", "opencode"))

    assert sent == [("ENG-201 received", "Sent to opencode.")]


def test_a_missing_library_off_macos_is_silent(monkeypatch):
    on(monkeypatch, "linux")
    monkeypatch.setitem(sys.modules, "desktop_notifier", None)
    run(notify.received("ENG-201", "opencode"))  # must not raise


def test_no_failure_mode_ever_escapes(monkeypatch):
    """The one property that outranks all the others.

    This call sits between choosing a harness and running it. Anything that gets
    out stops the hand-over before the agent is invoked, and the issue then
    isn't recorded, so it is retried on every poll for the life of the process.
    """
    on(monkeypatch, "darwin")

    async def explode(*_):
        raise RuntimeError("the notifier is having a bad day")

    monkeypatch.setattr(notify, "_deliver", explode)
    run(notify.received("ENG-201", "opencode"))


def test_cancellation_still_travels(monkeypatch):
    """`chord stop` reaches the watcher as a CancelledError. Swallowing it here
    would make the watcher unstoppable, which is what this module is for."""
    on(monkeypatch, "darwin")
    only_osascript(monkeypatch)

    async def cancel(*_):
        raise asyncio.CancelledError

    monkeypatch.setattr(notify, "_deliver", cancel)
    with pytest.raises(asyncio.CancelledError):
        run(notify.received("ENG-201", "opencode"))


# --- delivering, for real ---


def test_a_notifier_that_exits_nonzero_did_not_deliver():
    assert run(notify._deliver(["sh", "-c", "exit 3"])) is False


def test_a_notifier_that_exits_zero_did():
    assert run(notify._deliver(["true"])) is True


def test_a_command_that_does_not_exist_is_not_an_error():
    """`which` can disagree with the filesystem, and a stale path is not a
    reason to interrupt a hand-over."""
    assert run(notify._deliver(["/nonexistent/notifier-xyz"])) is False


def test_a_wedged_notifier_is_killed_and_given_up_on(monkeypatch):
    """Bounded, because this runs mid hand-over. A notifier that hangs would
    hold up the issue behind it to announce a notification nobody needs."""
    monkeypatch.setattr(notify, "NOTIFY_TIMEOUT", 0.2)
    assert run(notify._deliver(["sleep", "30"])) is False


def test_a_wedged_notifier_is_actually_killed(monkeypatch):
    """Not just abandoned: the process has to go, or it outlives the watcher.

    Asserted on the child's own exit status rather than by looking for the
    process in a process table — `pgrep -f sleep` matches every `sleep` on the
    machine, including the one the test above is still reaping.
    """
    monkeypatch.setattr(notify, "NOTIFY_TIMEOUT", 0.2)

    async def scenario():
        process = await asyncio.create_subprocess_exec(
            "sleep", "30", start_new_session=True
        )
        await notify._stop(process)
        # Reaped and signalled, rather than left running: SIGKILL to the group
        # shows up as a negative return code.
        assert process.returncode == -signal.SIGKILL

    run(scenario())


def test_a_notifier_that_overruns_is_given_up_on(monkeypatch):
    """And the hand-over carries on regardless."""
    monkeypatch.setattr(notify, "NOTIFY_TIMEOUT", 0.2)
    assert run(notify._deliver(["sleep", "30"])) is False


# --- what `chord info` reports ---


def test_backend_says_what_will_actually_be_used(monkeypatch):
    """Asked before sending, because the alternative is finding out by never
    being told anything."""
    on(monkeypatch, "darwin")
    monkeypatch.setattr(notify, "_terminal_notifier", lambda: None)
    monkeypatch.setattr(notify, "_osascript", lambda: "/usr/bin/osascript")
    assert notify.backend() == "osascript"

    monkeypatch.setattr(notify, "_terminal_notifier", lambda: "/opt/tn")
    assert notify.backend() == "terminal-notifier"


def test_backend_admits_when_nothing_can_notify(monkeypatch):
    on(monkeypatch, "darwin")
    no_tools(monkeypatch)
    assert notify.backend() == "none"


def test_backend_asks_about_the_library_without_importing_it(monkeypatch):
    """`chord info` stays quick and offline; importing a notification library to
    answer "would a notification arrive" is the wrong trade."""
    on(monkeypatch, "linux")
    monkeypatch.setattr(notify, "_have_library", lambda: True)
    assert notify.backend() == "desktop_notifier"

    monkeypatch.setattr(notify, "_have_library", lambda: False)
    assert notify.backend() == "none"


def test_osascript_is_found_by_absolute_path(monkeypatch, tmp_path):
    """The watcher is detached from the terminal that started it and inherits
    whatever PATH that had, so a bare `osascript` is a lookup it may lose."""
    monkeypatch.setattr(notify, "OSASCRIPT", str(tmp_path / "absent"))
    monkeypatch.setattr(notify, "_find", lambda name: None)
    assert notify._osascript() is None

    on_disk = tmp_path / "osascript"
    on_disk.write_text("")
    monkeypatch.setattr(notify, "OSASCRIPT", str(on_disk))
    assert notify._osascript() == str(on_disk)


def test_a_group_name_is_bounded():
    """An identifier is Linear's, and the group ends up on a command line."""
    assert len(notify._group("ENG-201")) == len(notify.GROUP) + 1 + len("ENG-201")
    assert len(notify._group("E" * 500)) <= len(notify.GROUP) + 65