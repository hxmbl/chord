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
    except (OSError, RuntimeError):
        return
