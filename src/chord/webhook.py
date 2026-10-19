"""A tiny local HTTP webhook receiver.

Linear (or a forwarding service such as a tunnel) POSTs an event here. The
payload is intentionally only a wake-up signal: the watcher re-reads Linear,
which keeps label filtering and deduplication in one place and makes malformed
or duplicated webhook payloads harmless.

Because the payload is only a wake-up signal, authenticating it is cheap and
worthwhile: the worst an unauthorised caller can otherwise do is spend Linear
rate limit on a machine that already has a valid token, repeatedly, for free.
`webhook_secret` requires the `X-Chord-Secret` header to match, and is compared
in constant time.
"""

import asyncio
import contextlib
import hmac
import logging

from chord.event_source import EventSource
from chord.performance import instrumented

_log = logging.getLogger(__name__)

# Bounds on the request. Both reads had no timeout, which is what made `stop()`
# hang below; the body read is the worse of the two because a client only has to
# declare a `Content-Length` and then go quiet.
READ_TIMEOUT = 5.0
MAX_BODY = 1_000_000
# Enough for a request line, a handful of headers, and their terminator. Passed
# to `asyncio.start_server` as the `StreamReader` limit, so `readuntil` raises
# `LimitOverrunError` for a header block past this and `_respond` answers 400
# rather than buffering an unbounded amount. (The body is bounded separately, by
# `MAX_BODY` and the `readexactly` below it.)
MAX_HEAD = 32 * 1024

SECRET_HEADER = b"x-chord-secret"


class Webhook(EventSource):
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 23842,
        path: str = "/webhook",
        secret: str = "",
    ) -> None:
        super().__init__()
        self.host = host
        self.port = port
        self.path = path
        self.secret = secret
        self._server: asyncio.AbstractServer | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}{self.path}"

    @property
    def listening(self) -> bool:
        return self._server is not None

    @property
    def active(self) -> bool:
        return self.listening

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self.host, self.port, limit=MAX_HEAD
        )
        socket = self._server.sockets[0]
        self.port = socket.getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @instrumented
    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> bool:
        """Answer one request and report whether it was a success.

        Returns whether the request got a `200 OK`. The instrumentation needs
        that: this method answers failures (400, 401, 404, a read timeout)
        rather than raising them, so "did not raise" would report every rejected
        request as a success.

        A writer is always closed, whatever happens — including cancellation
        (`chord stop`). `status` and `body` used to be assigned only inside the
        `try`, and cancellation re-raised before the write/close block, so a
        cancelled request leaked its socket and the `finally` could raise
        `UnboundLocalError` from the cleanup itself. The outer `finally` below
        is what makes the close unconditional.
        """
        status = b"400 Bad Request"
        body = b"bad request\n"
        try:
            try:
                async with asyncio.timeout(READ_TIMEOUT):
                    status, body = await self._respond(reader)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
                pass
            except asyncio.CancelledError:
                # `chord stop`. Reply as best we can in the `finally`, then let
                # it continue.
                raise
            except (ConnectionError, OSError):
                # The client went away mid-request. Nothing to reply to.
                return False
            except Exception:
                # Never take the listener down over one request. `warning` rather
                # than `error`: a malformed request is expected traffic from
                # something that is not Linear, and the log is a long-lived
                # daemon's log that a person reads when the watcher misbehaves.
                # The traceback is kept because reaching here means the request
                # was not one of the shapes handled below.
                _log.warning(
                    "unexpected error handling a webhook request", exc_info=True
                )
                status, body = b"400 Bad Request", b"bad request\n"

            writer.write(
                b"HTTP/1.1 "
                + status
                + b"\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            with contextlib.suppress(ConnectionError, OSError):
                await writer.drain()
            return status == b"200 OK"
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def _respond(self, reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
        """The reply for one request. Raises on anything malformed."""
        # No explicit header-size check here: `readuntil` raises
        # `LimitOverrunError` first if the terminator has not arrived within
        # MAX_HEAD, and that is caught as a bad request above. An earlier
        # version returned 431 for this and could never reach it, because
        # `readuntil` refuses to buffer that much in the first place.
        headers = await reader.readuntil(b"\r\n\r\n")

        lines = headers.split(b"\r\n")
        request_line = lines[0].decode("latin1").split(" ")
        # `split(" ", 2)` raises on a request line with fewer than three parts,
        # which a malformed request has no trouble producing. Answered here as a
        # bad request rather than falling through to the catch-all, which logged
        # a traceback for what is ordinary garbage on a public socket.
        if len(request_line) != 3:
            return b"400 Bad Request", b"bad request\n"
        method, path, _ = request_line
        length = 0
        secret = b""
        for line in lines[1:]:
            name, _, value = line.partition(b":")
            name = name.strip().lower()
            value = value.strip()
            if name == b"content-length":
                # Bounded before it is believed, so a client cannot make us
                # reserve an arbitrary amount of memory by asserting a number.
                try:
                    length = min(int(value), MAX_BODY)
                except ValueError:
                    return b"400 Bad Request", b"bad request\n"
                if length < 0:
                    # `readexactly` rejects a negative size, which reached the
                    # catch-all and logged a traceback for a request that is
                    # simply malformed.
                    return b"400 Bad Request", b"bad request\n"
            elif name == SECRET_HEADER:
                secret = value

        if method != "POST" or path != self.path:
            return b"404 Not Found", b"not found\n"

        if not self._authorised(secret):
            # Checked before the body is read, so an unauthorised caller cannot
            # make us wait on bytes it is not allowed to send.
            #
            # Logged because otherwise this is invisible: every delivery gets a
            # 401, the webhook silently degrades to interval polling, and a
            # secret that cannot be transmitted the way an HTTP header is
            # encoded looks exactly like a wrong secret. The offered value is
            # never echoed.
            _log.warning("rejected a webhook request carrying the wrong secret")
            return b"401 Unauthorized", b"unauthorized\n"

        await reader.readexactly(length)
        self._wake()
        return b"200 OK", b"ok\n"

    def _authorised(self, offered: bytes) -> bool:
        """Whether `offered` is the shared secret.

        `hmac.compare_digest` rather than `==`: this is a secret, and a
        byte-by-byte comparison leaks how much of a guess was right. That is not
        a practical attack over a local socket, but it costs one function call to
        not do it and the alternative is a comparison that reads like a mistake.

        The secret is compared against every byte form a sender might put on the
        wire. HTTP header values are bytes, and a sender encodes as latin-1 (the
        header default) or UTF-8; matching only the UTF-8 form meant a
        non-ASCII secret configured here and sent the standard way was refused
        forever. A value outside both encodings (a lone surrogate) cannot be
        transmitted at all, so there is nothing to accept and it is refused.
        """
        if not self.secret:
            return True
        return any(
            hmac.compare_digest(offered, candidate)
            for candidate in _wire_forms(self.secret)
        )


def _wire_forms(secret: str) -> list[bytes]:
    """The byte forms of `secret` a client could plausibly send.

    Latin-1 first, because that is what an HTTP header value is defined as on
    the wire; UTF-8 as well, because a modern client may encode a header that
    way and refusing it would be a lockout rather than a safeguard.
    """
    forms: list[bytes] = []
    for encoding in ("latin-1", "utf-8"):
        try:
            forms.append(secret.encode(encoding))
        except UnicodeEncodeError:
            continue
    return forms