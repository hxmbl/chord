"""A tiny local HTTP webhook receiver.

Linear (or a forwarding service such as a tunnel) POSTs an event here. The
payload is intentionally only a wake-up signal: the watcher re-reads Linear,
which keeps label filtering and deduplication in one place and makes malformed
or duplicated webhook payloads harmless.
"""

import asyncio
import contextlib

from chord.event_source import EventSource


class Webhook(EventSource):
    def __init__(
        self, host: str = "127.0.0.1", port: int = 23842, path: str = "/webhook"
    ) -> None:
        super().__init__()
        self.host = host
        self.port = port
        self.path = path
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
        try:
            headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            request = headers.split(b"\r\n", 1)[0].decode("latin1")
            method, path, _ = request.split(" ", 2)
            length = 0
            for line in headers.split(b"\r\n")[1:]:
                if line.lower().startswith(b"content-length:"):
                    length = min(int(line.split(b":", 1)[1]), 1_000_000)
            await reader.readexactly(length)
            if method == "POST" and path == self.path:
                self._wake()
                status = b"200 OK"
                body = b"ok\n"
            else:
                status = b"404 Not Found"
                body = b"not found\n"
        except (
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            TimeoutError,
            ValueError,
        ):
            status = b"400 Bad Request"
            body = b"bad request\n"
        finally:
            writer.write(
                b"HTTP/1.1 "
                + status
                + b"\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            with contextlib.suppress(ConnectionError):
                await writer.drain()
            writer.close()
            await writer.wait_closed()
