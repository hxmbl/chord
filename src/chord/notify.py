"""Best-effort desktop notifications through the user's notification daemon."""

import asyncio
from typing import Any


async def send(title: str, message: str) -> None:
    """Notify without making a desktop or headless watcher fail."""
    try:
        from desktop_notifier import DesktopNotifier
    except ImportError:
        return
    try:
        notifier: Any = DesktopNotifier()
        await notifier.send(title=title, message=message)
    except asyncio.CancelledError:
        raise
    except Exception:
        # A desktop notification is never worth an issue not being worked on.
        #
        # This call sits between choosing a harness and running it, so anything
        # it lets escape stops the hand-over before the harness is even invoked.
        # The guard used to name two exception types, and anything outside them
        # — a `TimeoutExpired` from the `osascript` this library shells out to,
        # an `AttributeError` from its dbus backend on Linux — took the issue
        # with it. The watcher then did not record it, so it was retried on
        # every poll, forever, and the log said "internal error handing over
        # ENG-1" rather than anything about notifications.
        #
        # Best-effort means best-effort: silence is the correct outcome for every
        # failure mode here, and a notifier that cannot be reached is not a
        # problem a person reading the log needs to solve.
        return
