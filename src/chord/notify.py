"""Desktop notifications: what happened to an issue, and why.

**One banner per issue, whichever way it went.** That is the whole shape. A
watcher can have a hundred issues in a backlog and the only thing worth saying
about any of them is how it ended — so the ends are four verbs, exactly one of
which fires for any issue, and there is no notification for a step along the way.

It was two banners per issue once: "started" when the hand-over began and
"finished" when it ended, which is two notifications to say one thing. The
interesting half of a hand-over is that it happened; the interesting half of the
failure is that it did not; and the middle is a step, not an event.

Every banner is two halves. A title naming what happened, and a line underneath
saying why — "ENG-201 failed" over "`opencode` exited 1". A banner is read at a
glance by somebody who did not ask for it, so the title has to stand on its own
and the reason has to be underneath it rather than woven into it.

The four verbs exist rather than one `send(title, message)` taking two free
strings because that is what lets the halves disagree. It happened: a hand-over
that raised sent "Chord done" over "ENG-201 did not finish", which is the one
banner nobody can act on — the title says there is nothing to do and the body
says why there is.

Which is also why they never arrived. macOS attributes every banner to a bundle
identity, and `UNUserNotificationCenter` — the only Apple API that takes a title
and a body — reads that identity off whatever is running, so it works inside a
signed `.app` and nowhere else. Chord is `python3` in a virtualenv: a bare,
unsigned executable that is in no bundle at all. `desktop_notifier` checks for
this, finds no bundle, hands back a dummy backend, and drops the notification on
the floor. It never raised, because nothing had gone wrong; there was simply
nobody listening.

So on macOS the daemon is asked by a route that needs no bundle:

* `terminal-notifier`, when the person has it. It is itself a signed app bundle,
  so the banner is attributed to something called terminal-notifier rather than
  to Script Editor, and `-group` keeps one banner per issue.
* `osascript`, which is in every macOS and installs nothing. The banner is
  attributed to Script Editor, which is cosmetic. The alternative is no banner.

Both are handed the text as arguments rather than as source. The usual spelling
— `osascript -e 'display notification "…' with title "…"'` — builds a script by
concatenation, and an issue title is somebody else's words, so a quote in one
would be a way to run code off an issue.

`desktop_notifier` is kept for Linux and Windows, where it does work. There the
backend is a D-Bus call to the session's own notification daemon, which has
never needed a bundle.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import os
import shutil
import signal
import sys
from typing import Any

from chord.text import one_line

# The prefix of a notification group. Notifications in one group replace one
# another rather than stacking, which is the whole reason a watcher can announce
# both ends of a hand-over without leaving a wall of banners behind.
GROUP = "chord"

# How long a notifier gets. It is a one-shot command whose entire job is to hand
# a string to another process, so anything slower is wedged — and the watcher is
# the wrong place to wait on that, because it is in the middle of a hand-over.
NOTIFY_TIMEOUT = 5.0

# Bound on reaping a notifier that had to be killed. This is tidying up after
# something that already went wrong, not the work itself, so it is short.
_REAP_TIMEOUT = 5.0

# `display notification` written as a handler rather than a one-liner, so the text
# arrives in `argv` instead of being pasted into the source. AppleScript gives a
# script this shape for exactly that reason: `osascript` hands it whatever
# follows, and `item 1` is the title because that is the order `with title` wants
# it in.
_DISPLAY = (
    "on run argv\n"
    "\tdisplay notification (item 2 of argv) with title (item 1 of argv)\n"
    "end run"
)

# By absolute path. The watcher is detached from the terminal that started it, so
# it inherits whatever PATH that had, and a bare `osascript` is a PATH lookup in
# service of something that has been in /usr/bin since macOS 10.8.
OSASCRIPT = "/usr/bin/osascript"

# `shutil.which` behind a name of our own, so that taking the lookup away in a
# test does not take `shutil` away from the rest of Chord — `harness.build`
# resolves commands the same way and needs the real one to find them.
_find = shutil.which


async def received(thing: str, where: str) -> None:
    """`thing` has been picked up and handed to `where`.

    One banner for the whole hand-over, fired as it happens rather than when it
    ends — a harness can run for half an hour, and the news is that work started
    where you didn't start it. There is deliberately no `finished` to go with it:
    see the module docstring.
    """
    await send(f"{thing} received", f"Sent to {where}.", group=_group(thing))


async def failed(thing: str, why: object) -> None:
    """`thing` did not finish, and `why` is the reason it gives.

    The reason is an exception more often than not, and it is the half that
    makes the notification actionable, so it goes in the body rather than being
    summarised away into the title.
    """
    await send(f"{thing} failed", one_line(why), group=_group(thing))


async def skipped(thing: str, why: object) -> None:
    """`thing` will not be worked on, and `why` is the reason.

    Announced, because the alternative is an issue sitting in Linear looking
    exactly like work in progress. The fix is in the log — curating a harness,
    installing the command — and the banner is the nudge that says the fix is
    needed at all.
    """
    await send(f"{thing} skipped", one_line(why), group=_group(thing))


async def refused(thing: str, why: object) -> None:
    """Somebody asked for `thing` and is not allowed to.

    Its own verb because it is not a malfunction: the allowlist did the one
    thing it is there for, and the person who set it up wants to know it fired.
    Sharing a verb with `skipped` would file it under typos in chord.toml, which
    is the opposite of what it is.
    """
    await send(f"{thing} refused", one_line(why), group=_group(thing))


async def send(title: str, message: str, *, group: str | None = None) -> None:
    """Post one notification, without ever making a watcher fail.

    Both halves are flattened here rather than by the callers, because this is
    the last place before untrusted text reaches a notification daemon: the
    identifier is Linear's, and the reason is whatever a command printed.

    Best-effort all the way down. Every failure ends in silence, because a
    desktop notification is never worth an issue not being worked on — and this
    call sits between choosing a harness and running it, so anything allowed to
    escape stops the hand-over before the agent is even invoked.
    """
    try:
        headline = one_line(title, 120)
        detail = one_line(message)
        if sys.platform == "darwin":
            for command in _macos(headline, detail, group):
                if await _deliver(command):
                    return
            return
        await _library(headline, detail)
    except asyncio.CancelledError:
        # `chord stop` reaches the watcher this way. Swallowing it would make the
        # notifier unkillable, and unkillable is the failure mode this whole
        # module exists to avoid.
        raise
    except Exception:
        # The guard used to name two exception types, and everything outside
        # them took the issue with it: a `TimeoutExpired` from a subprocess, an
        # `AttributeError` from a library's D-Bus backend on Linux. The watcher
        # then did not record the issue, so it was retried on every poll forever,
        # and the log said "internal error handing over ENG-1" rather than
        # anything about notifications. Silence is the correct outcome for all
        # of them.
        return


def backend() -> str:
    """Which route a notification would take here, for `chord info`.

    Offline and cheap — a platform check, a `which`, an existence test — and it
    exists only because the failure it would report is invisible by
    construction. A notification that reaches no daemon raises nothing, so the
    only way to know one is going somewhere is to ask before sending it.
    """
    if sys.platform == "darwin":
        if _terminal_notifier() is not None:
            return "terminal-notifier"
        return "osascript" if _osascript() is not None else "none"
    return "desktop_notifier" if _have_library() else "none"


def _group(thing: str) -> str:
    """The group an issue's notifications belong in.

    Per issue rather than one group for the whole watcher, so finishing one issue
    does not wipe the banner for another that is still running.
    """
    return f"{GROUP}-{one_line(thing, 64)}"


def _terminal_notifier() -> str | None:
    """Where terminal-notifier is, if the person has it.

    Looked up per notification rather than cached, because which routes exist is
    a property of the machine and a watcher routinely outlives the `brew
    install` that adds one.
    """
    return _find("terminal-notifier")


def _osascript() -> str | None:
    """Where osascript is, preferring the absolute path. See `OSASCRIPT`."""
    if os.path.exists(OSASCRIPT):
        return OSASCRIPT
    return _find("osascript")


def _have_library() -> bool:
    """Whether `desktop_notifier` is importable, without importing it.

    `find_spec` rather than a real import: this runs in `chord info`, which is
    meant to stay quick and to answer questions that have nothing to do with
    whether a notification library works.
    """
    return importlib.util.find_spec("desktop_notifier") is not None


def _macos(title: str, message: str, group: str | None) -> list[list[str]]:
    """The routes to Notification Centre on this machine, best first.

    Both of them, when both are here. They fail for different reasons, and a
    notification is worth one more attempt before it is given up on: which one
    is installed is a fact about the machine rather than about the notification,
    so it is asked for rather than configured.
    """
    commands: list[list[str]] = []

    notifier = _terminal_notifier()
    if notifier is not None:
        command = [
            notifier,
            "-title",
            title,
            "-message",
            message,
            # Collapse this issue's banners into one. Without it, a watcher that
            # announces both ends of a hand-over leaves the finished banner
            # stacked under the started one, and the second is the only one
            # still true.
            "-group",
            group or GROUP,
        ]
        commands.append(command)

    osascript = _osascript()
    if osascript is not None:
        commands.append([osascript, "-e", _DISPLAY, title, message])

    return commands


async def _library(title: str, message: str) -> None:
    """`desktop_notifier`, for the platforms where its backend works.

    Linux reaches the session's notification daemon over D-Bus and needs no
    bundle; Windows goes through WinRT. macOS is the one that cannot work from a
    script, which is why it does not come through here.
    """
    try:
        from desktop_notifier import DesktopNotifier
    except ImportError:
        return
    notifier: Any = DesktopNotifier()
    await notifier.send(title=title, message=message)


async def _deliver(command: list[str]) -> bool:
    """Run one notifier, and say whether it delivered.

    Bounded, and killed rather than left: this runs mid hand-over, so a
    notifier that hangs would hold up the issue behind it to announce a
    notification nobody needs. It gets its own session so the signal reaches
    anything it spawned too, for the same reason `harness` gives each harness
    one.

    Every pipe is `DEVNULL`. There is nothing to read here — the notifier's exit
    status is the whole answer, and a pipe nobody drains is how a subprocess
    turns into a watcher that hangs on its own bookkeeping.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False

    try:
        await asyncio.wait_for(process.wait(), NOTIFY_TIMEOUT)
    except TimeoutError:
        await _stop(process)
        return False
    except asyncio.CancelledError:
        await _stop(process)
        raise

    return process.returncode == 0


async def _stop(process: asyncio.subprocess.Process) -> None:
    """Kill a notifier that overran, and reap it rather than leaving a zombie."""
    with contextlib.suppress(OSError):
        os.killpg(process.pid, signal.SIGKILL)
    # Best-effort, and shielded: on the cancellation path the wait would be
    # cancelled again immediately, and abandoning it leaves the reap undone.
    with contextlib.suppress(Exception):
        await asyncio.shield(asyncio.wait_for(process.wait(), _REAP_TIMEOUT))
