"""Reacting to Linear sooner, by listening rather than waiting.

Chord's poll loop is the source of truth: whatever the interval is, the work
gets done. This is an accelerator, and deliberately a small one. A WebSocket
from Linear says "something moved", and the watcher polls straight away instead
of sitting out the rest of its interval.

That framing is what keeps this honest, and it is why none of the failure modes
here are serious:

- The socket can't be opened, or drops, or the token is stale: the interval
  poll still finds the work. Worst case is latency.
- Events missed while disconnected are still caught by the next poll.
- A duplicate event just causes a poll that finds nothing new.

Subscriptions are not a replacement for polling. Linear's own documentation
recommends webhooks for anything that must not miss an event, precisely because
a subscription only delivers while your process is connected. Keeping both means
the fast path can be wrong and the slow path can be slow, and neither can lose
an issue.
"""

import asyncio
import contextlib
from typing import Any

from chord.event_source import EventSource
from chord.text import one_line

SUBSCRIPTION_ENDPOINT = "wss://api.linear.app/graphql"

# One root field per subscription, which is what the graphql-transport-ws
# protocol expects. Linear's `IssueSubscriptionFilter` can narrow by assignee,
# project, state, parent and team — but not by label — so every issue event in
# the workspace arrives and the route labels are checked afterwards, in the
# poll. That is a little wasteful and completely harmless: the worst outcome of
# a spurious event is a poll that finds nothing.
#
# Neither event is needed for correctness. `issueCreated` catches a new issue;
# `issueUpdated` catches a label being added to one that already exists, which
# is what routes an issue to a different harness. If Linear also fires
# something for the third case and we miss it, the interval poll picks it up a
# minute later.
ISSUES_CHANGED = """
subscription IssuesChanged {
  issueCreated { id }
}
"""

ISSUE_UPDATED = """
subscription IssueUpdated {
  issueUpdated { id }
}
"""


def _queries() -> list[tuple[str, Any]]:
    """The subscription documents, parsed.

    `gql` is imported lazily so that a Chord installed without the `live`
    extra still imports, and returns a parsed request object rather than a raw
    string, which is what `Client.subscribe` expects.
    """
    from gql import gql

    return [
        ("created", gql(ISSUES_CHANGED)),
        ("updated", gql(ISSUE_UPDATED)),
    ]


# How long to wait before retrying a connection that failed, and the ceiling
# for the backoff. Generous at the bottom: a token that needs `chord refresh`
# won't fix itself in five seconds, and hammering Linear helps nobody.
RETRY_MIN = 5.0
RETRY_MAX = 60.0

# How long to wait for Linear to acknowledge the connection before giving up on
# an attempt. Bounds a half-open socket that will never deliver.
ACK_TIMEOUT = 15.0


class Unavailable(Exception):
    """The live connection can't be used. Not fatal: polling carries on."""


def available() -> bool:
    """Whether the optional subscription support is installed."""
    try:
        import gql
        import gql.transport.websockets  # noqa: F401
    except ImportError:
        return False
    return True


class Subscription(EventSource):
    """A best-effort live connection that wakes the watcher early."""

    consume_pending_when_inactive = False

    def __init__(self, access_token: str) -> None:
        super().__init__()
        self._token = access_token
        self._report: Any = None
        self._task: asyncio.Task | None = None
        self._closing = False

    @property
    def connected(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def active(self) -> bool:
        return self.connected

    def report_problems_to(self, callback) -> None:
        """Where to say why the connection is unhappy.

        The watcher passes its own once-only logger, so a socket that stays
        down produces one line rather than one per interval for days.
        """
        self._report = callback

    async def start(self) -> None:
        if not available():
            return
        self._closing = False
        self._task = asyncio.create_task(self._run(), name="chord-subscribe")

    async def stop(self) -> None:
        self._closing = True
        self._wake()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        """Hold the connection open, reconnecting when it drops.

        Reconnection lives here rather than in the watcher so that a long
        outage is one background task retrying, not the poll loop growing
        reconnection logic of its own.
        """
        backoff = RETRY_MIN
        while not self._closing:
            try:
                await self._session()
                backoff = RETRY_MIN  # A clean close is not a failure.
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._report:
                    self._report(describe(exc))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RETRY_MAX)
            else:
                if self._closing:
                    return
                # The stream ended without us asking. Reconnect promptly.
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RETRY_MAX)

    async def _session(self) -> None:
        from gql import Client
        from gql.transport.websockets import WebsocketsTransport

        transport = WebsocketsTransport(
            url=SUBSCRIPTION_ENDPOINT,
            # The token goes in the handshake header, same as every other
            # request, and is never logged.
            headers={"Authorization": f"Bearer {self._token}"},
            connect_timeout=ACK_TIMEOUT,
            ack_timeout=ACK_TIMEOUT,
        )
        async with Client(
            transport=transport,
            # No introspection over the wire: the queries here are static, and
            # asking Linear to describe its whole schema to start a listener
            # would be absurd.
            fetch_schema_from_transport=False,
            execute_timeout=None,
        ) as session:
            listeners = [
                asyncio.create_task(self._listen(session, query), name=name)
                for name, query in _queries()
            ]
            try:
                # Returns when any listener ends, which means the socket is gone.
                done, pending = await asyncio.wait(
                    listeners, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                for task in done:
                    # Re-raise so a subscription error is visible.
                    task.result()
            finally:
                for task in listeners:
                    task.cancel()
                await asyncio.gather(*listeners, return_exceptions=True)

    async def _listen(self, session: Any, query: str) -> None:
        async for _result in session.subscribe(query):
            # The payload isn't used: the poll re-reads whatever matters. The
            # only thing wanted from an event is "go and look now".
            self._wake()


def describe(exc: BaseException) -> str:
    """A one-line description of why the live connection isn't working."""
    return one_line(f"{type(exc).__name__}: {exc}")
