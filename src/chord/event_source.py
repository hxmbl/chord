"""Shared wake-up and generation-counter behavior for live event sources."""

import asyncio
from abc import ABC, abstractmethod


class EventSource(ABC):
    """An optional source that can wake the watcher's next poll early."""

    consume_pending_when_inactive = True

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._generation = 0
        self._consumed = 0

    @property
    @abstractmethod
    def active(self) -> bool:
        """Whether this source currently has a live event connection."""

    async def wait(self, timeout: float) -> bool:
        if not self.active and (
            not self.consume_pending_when_inactive
            or self._generation == self._consumed
        ):
            await asyncio.sleep(timeout)
            return False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._generation == self._consumed:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            self._event.clear()
            try:
                await asyncio.wait_for(self._event.wait(), remaining)
            except TimeoutError:
                return False
        self._consumed = self._generation
        return True

    def _wake(self) -> None:
        self._generation += 1
        self._event.set()
