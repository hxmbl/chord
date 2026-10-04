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

_log = logging.getLogger(__name__)

# Bounds on the request. Both reads had no timeout, which is what made `stop()`
# hang below; the body read is the worse of the two because a client only has to
# declare a `Content-Length` and then go quiet.
READ_TIMEOUT = 5.0
MAX_BODY = 1_000_000
# Enough for a request line, a handful of headers, and their terminator. The
# default 64KiB limit is generous enough and raising it is not needed.
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
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        socket = self._server.sockets[0]
        self.port = socket.getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        # Answer, rather than raise, for every way this can go wrong. `status` and
        # `body` are set before the `try` now: they used to be assigned only inside
        # it, so an exception the `except` clause did not name reached the
        # `finally` with them unbound and raised `UnboundLocalError` from the
        # cleanup itself — which skipped `writer.close()`, leaking the socket.
        # That was reachable from anything not in the tuple below, cancellation
        # (`chord stop`) among them.
        status = b"400 Bad Request"
        body = b"bad request\n"
        try:
            async with asyncio.timeout(READ_TIMEOUT):
                status, body = await self._respond(reader)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            pass
        except asyncio.CancelledError:
            # `chord stop`. Reply as best we can, then let it continue.
            raise
        except (ConnectionError, OSError):
            # The client went away mid-request. Nothing to reply to.
            with contextlib.suppress(ConnectionError, OSError):
                writer.close()
            return
        except Exception:
            # Never take the listener down over one request. `warning` rather
            # than `error`: a malformed request is expected traffic from
            # something that is not Linear, and the log is a long-lived daemon's
            # log that a person reads when the watcher misbehaves. The
            # traceback is kept because reaching here means the request was not
            # one of the shapes handled below, which is the interesting part.
            _log.warning("unexpected error handling a webhook request", exc_info=True)
            status, body = b"400 Bad Request", b"bad request\n"

        try:
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
        """
        if not self.secret:
            return True
        return hmac.compare_digest(offered, self.secret.encode())