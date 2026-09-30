"""A tiny local HTTP webhook receiver.

Linear (or a forwarding service such as a tunnel) POSTs an event here. The
payload is intentionally only a wake-up signal: the watcher re-reads Linear,
which keeps label filtering and deduplication in one place and makes malformed
or duplicated webhook payloads harmless.
"""

import asyncio
import contextlib


class Webhook:
    def __init__(
        self, host: str = "127.0.0.1", port: int = 23842, path: str = "/webhook"
    ) -> None:
        self.host = host
        self.port = port
        self.path = path
        self._server: asyncio.AbstractServer | None = None
        self._event = asyncio.Event()
        self._generation = 0
        self._consumed = 0

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}{self.path}"

    @property
    def listening(self) -> bool:
        return self._server is not None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        socket = self._server.sockets[0]
        self.port = socket.getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def wait(self, timeout: float) -> bool:
        if not self.listening and self._generation == self._consumed:
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
                self._generation += 1
                self._event.set()
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
