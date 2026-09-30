import asyncio

from chord.webhook import Webhook


def test_webhook_wakes_on_post():
    class Reader:
        async def readuntil(self, _separator):
            return b"POST /webhook HTTP/1.1\r\nContent-Length: 2\r\n\r\n"

        async def readexactly(self, _length):
            return b"{}"

    class Writer:
        def __init__(self):
            self.output = b""

        def write(self, body):
            self.output += body

        async def drain(self): ...
        def close(self): ...
        async def wait_closed(self): ...

    async def scenario():
        webhook = Webhook(port=0)
        writer = Writer()
        await webhook._handle(Reader(), writer)
        return await webhook.wait(0.1), writer.output

    woke, response = asyncio.run(scenario())
    assert woke is True
    assert b"200 OK" in response


def test_webhook_rejects_other_paths():
    class Reader:
        async def readuntil(self, _separator):
            return b"POST /wrong HTTP/1.1\r\nContent-Length: 0\r\n\r\n"

        async def readexactly(self, _length):
            return b""

    class Writer:
        def __init__(self):
            self.output = b""

        def write(self, body):
            self.output += body

        async def drain(self): ...
        def close(self): ...
        async def wait_closed(self): ...

    async def scenario():
        webhook = Webhook(port=0)
        writer = Writer()
        await webhook._handle(Reader(), writer)
        return writer.output, await webhook.wait(0.01)

    response, woke = asyncio.run(scenario())
    assert b"404 Not Found" in response
    assert woke is False
