"""The webhook is an unauthenticated HTTP listener on a local port.

Three bugs, found by probing rather than reading, and all of them in the same
place: `_handle`'s `finally` block.

**`chord stop` could not finish.** `reader.readexactly(length)` had no timeout.
A client only had to declare a `Content-Length` and then go quiet, and the
handler sat in that read forever. `stop()` awaits `wait_closed()`, which waits
for the handlers, so `stop()` never returned. Confirmed against the pre-fix
code: still hanging when killed after 6 seconds. The user has to SIGKILL. Same
shape as the harness timeout in bde40f2 — an unbounded read on a path that
`chord stop` depends on.

**The cleanup itself raised, leaking the socket.** `status` and `body` were
assigned only inside the `try`, then used in the `finally`. Anything the
`except` clause did not name reached the `finally` with them unbound, so
`UnboundLocalError` came out of the cleanup and `writer.close()` never ran. The
loop's own exception handler is where this showed up, which is easy to miss: it
does not fail the test that caused it.

**No authentication.** The payload is only a wake-up signal, so the worst an
unauthorised caller can do is make the watcher re-read Linear — spending Linear
rate limit on a machine that already holds a valid token, repeatedly, for free.
That is why `webhook_secret` exists, and the check happens before the body is
read so an unauthorised caller cannot make us wait on bytes it may not send.
"""

import asyncio
import socket

import pytest

from chord.webhook import READ_TIMEOUT, Webhook


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def send(port: int, payload: bytes, wait: float = 3.0) -> bytes:
    """Send raw bytes and return the reply, or b"" if none came."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(payload)
        await writer.drain()
        try:
            return await asyncio.wait_for(reader.read(400), timeout=wait)
        except TimeoutError:
            return b""
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


def status_of(reply: bytes) -> bytes:
    return reply.split(b"\r\n", 1)[0] if reply else b"<no reply>"


def post(body: bytes = b"{}", headers: bytes = b"", method: bytes = b"POST") -> bytes:
    return (
        method
        + b" /webhook HTTP/1.1\r\nHost: x\r\n"
        + headers
        + b"Content-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    )


# --- stop() must be able to finish ---


def test_stop_finishes_while_a_body_is_outstanding():
    """The regression. A stalled reader used to make `chord stop` hang forever.

    Bounded, so against the pre-fix code this is a failure and not a hang that
    takes the suite with it. A hang *is* the bug, so it has to be observable.
    """

    async def scenario():
        hook = Webhook(port=free_port(), secret="")
        await hook.start()
        _, writer = await asyncio.open_connection("127.0.0.1", hook.port)
        writer.write(b"POST /webhook HTTP/1.1\r\nContent-Length: 999999\r\n\r\npart")
        await writer.drain()
        await asyncio.sleep(0.2)
        try:
            await asyncio.wait_for(hook.stop(), timeout=READ_TIMEOUT + 5)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "stall",
    [
        pytest.param(b"Content-Length: 999999\r\n\r\npart", id="body-truncated"),
        pytest.param(b"", id="no-body-no-terminator"),
        pytest.param(b"X: " + b"y" * 200_000 + b"\r\n", id="headers-endless"),
    ],
)
def test_a_stalled_request_is_answered_rather_than_held(stall):
    """Every read is bounded, so no client can pin a handler open.

    The reply is a 400 rather than silence: a sender that never gets an answer
    does not know whether to retry, whereas one told "bad request" can stop.
    """
    payload = b"POST /webhook HTTP/1.1\r\n" + stall

    async def scenario():
        hook = Webhook(port=free_port(), secret="")
        await hook.start()
        try:
            return await send(hook.port, payload, wait=READ_TIMEOUT + 5)
        finally:
            await hook.stop()

    reply = asyncio.run(scenario())
    assert b"400" in status_of(reply), (
        f"held open with no answer: {status_of(reply)!r}"
    )


def test_a_normal_webhook_is_unaffected_by_the_bounds():
    async def scenario():
        hook = Webhook(port=free_port(), secret="")
        await hook.start()
        try:
            return await send(hook.port, post())
        finally:
            await hook.stop()

    assert b"200 OK" in status_of(asyncio.run(scenario()))


def test_a_body_of_exactly_the_limit_is_accepted():
    """The cap is a cap on what we will *reserve*, not a rejection."""

    async def scenario():
        hook = Webhook(port=free_port(), secret="")
        await hook.start()
        try:
            return await send(hook.port, post(b"x" * 1_000_000))
        finally:
            await hook.stop()

    assert b"200 OK" in status_of(asyncio.run(scenario()))


def test_an_oversized_body_is_truncated_not_refused():
    """A client claiming more than the cap still gets its wake-up.

    Truncating to the cap and reading that much is the safe reading: refusing
    outright would mean every sender with a slightly wrong `Content-Length` gets
    no wake-up at all, which is the failure mode that actually loses work.
    """

    async def scenario():
        hook = Webhook(port=free_port(), secret="")
        await hook.start()
        try:
            return await send(hook.port, post(b"x" * 10))
        finally:
            await hook.stop()

    assert b"200 OK" in status_of(asyncio.run(scenario()))


# --- the shared secret ---


def test_the_right_secret_is_accepted():
    async def scenario():
        hook = Webhook(port=free_port(), secret="s3cret")
        await hook.start()
        try:
            return await send(hook.port, post(headers=b"X-Chord-Secret: s3cret\r\n"))
        finally:
            await hook.stop()

    assert b"200 OK" in status_of(asyncio.run(scenario()))


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(b"", id="no-header"),
        pytest.param(b"X-Chord-Secret: wrong\r\n", id="wrong"),
        pytest.param(b"X-Chord-Secret: s3cre\r\n", id="prefix"),
        pytest.param(b"X-Chord-Secret: s3crets\r\n", id="longer"),
        pytest.param(b"X-Chord-Secret: S3CRET\r\n", id="wrong-case"),
        pytest.param(b"X-Chord-Secret:  s3cretx\r\n", id="trailing-junk"),
    ],
)
def test_a_wrong_or_missing_secret_is_refused(headers):
    async def scenario():
        hook = Webhook(port=free_port(), secret="s3cret")
        await hook.start()
        try:
            return await send(hook.port, post(headers=headers))
        finally:
            await hook.stop()

    reply = asyncio.run(scenario())
    assert b"401" in status_of(reply), f"a bad secret was accepted: {status_of(reply)!r}"
    assert b"200" not in status_of(reply)


def test_optional_whitespace_around_the_secret_is_not_part_of_it():
    """HTTP strips it, so a sender that pads is still the right sender.

    Worth pinning because the opposite reading is defensible too, and getting it
    wrong would be a lockout rather than a hole: a tunnel that pretty-prints
    headers would start getting 401s for a secret it is sending correctly.
    """
    for padding in (b" ", b"  ", b"\t"):
        payload = post(headers=b"X-Chord-Secret: " + padding + b"s3cret" + padding + b"\r\n")

        async def scenario(payload=payload):
            hook = Webhook(port=free_port(), secret="s3cret")
            await hook.start()
            try:
                return await send(hook.port, payload)
            finally:
                await hook.stop()

        assert b"200 OK" in status_of(asyncio.run(scenario())), (
            f"padding {padding!r} was treated as part of the secret"
        )


def test_the_header_name_is_matched_without_regard_to_case():
    """HTTP header names are case-insensitive, and the sender is not Chord."""

    async def scenario():
        hook = Webhook(port=free_port(), secret="s3cret")
        await hook.start()
        try:
            return await send(hook.port, post(headers=b"x-chord-secret: s3cret\r\n"))
        finally:
            await hook.stop()

    assert b"200 OK" in status_of(asyncio.run(scenario()))


def test_no_secret_configured_means_no_authentication():
    """The default is unchanged: adding a secret must not break existing setups.

    Failing closed here would stop every working webhook on upgrade, and a
    loopback listener is not the boundary worth spending an upgrade on. The
    secret is opt-in and `chord info` says whether it is on.
    """

    async def scenario():
        hook = Webhook(port=free_port(), secret="")
        await hook.start()
        try:
            return await send(hook.port, post())
        finally:
            await hook.stop()

    assert b"200 OK" in status_of(asyncio.run(scenario()))


def test_a_refused_request_does_not_wake_the_watcher():
    """Otherwise the secret buys nothing: the work is already scheduled."""

    async def scenario():
        hook = Webhook(port=free_port(), secret="s3cret")
        woke = []
        hook._wake = lambda: woke.append(True)
        await hook.start()
        try:
            await send(hook.port, post(headers=b"X-Chord-Secret: wrong\r\n"))
            await asyncio.sleep(0.1)
            return woke
        finally:
            await hook.stop()

    assert asyncio.run(scenario()) == []


def test_the_secret_is_checked_before_the_body_is_read():
    """So an unauthorised caller cannot make us wait on bytes it may not send.

    Without this ordering the 401 still comes back, but only after the sender
    finishes a body it had no business sending — which is a free way to occupy
    a handler.
    """
    payload = (
        b"POST /webhook HTTP/1.1\r\nX-Chord-Secret: wrong\r\n"
        b"Content-Length: 999999\r\n\r\npart"
    )

    async def scenario():
        hook = Webhook(port=free_port(), secret="s3cret")
        await hook.start()
        try:
            # A short wait: if the body were awaited first this would be empty.
            return await send(hook.port, payload, wait=READ_TIMEOUT / 4)
        finally:
            await hook.stop()

    reply = asyncio.run(scenario())
    assert b"401" in status_of(reply), (
        f"waited for the body before refusing: {status_of(reply)!r}"
    )


def test_the_secret_is_never_echoed():
    """A 401 that quotes the expected secret hands over half the problem."""

    async def scenario():
        hook = Webhook(port=free_port(), secret="s3cret")
        await hook.start()
        try:
            return await send(hook.port, post(headers=b"X-Chord-Secret: wrong\r\n"))
        finally:
            await hook.stop()

    assert b"s3cret" not in asyncio.run(scenario())


# --- the malformed-request surface, unchanged in shape ---


@pytest.mark.parametrize(
    "payload,expected",
    [
        pytest.param(post(method=b"GET"), b"404", id="wrong-method"),
        pytest.param(b"POST /elsewhere HTTP/1.1\r\nContent-Length: 0\r\n\r\n", b"404", id="wrong-path"),
        pytest.param(b"GET\r\n\r\n", b"400", id="truncated-request-line"),
        pytest.param(b"POST /webhook HTTP/1.1\r\nContent-Length: abc\r\n\r\n", b"400", id="length-not-a-number"),
        pytest.param(b"POST /webhook HTTP/1.1\r\nContent-Length: -3\r\n\r\n", b"400", id="negative-length"),
        pytest.param(
            b"POST /webhook HTTP/1.1\r\nContent-Length: 5\r\n\r\nab",
            b"400",
            id="body-shorter-than-declared",
        ),
    ],
)
def test_malformed_requests_still_get_a_tidy_answer(payload, expected):
    async def scenario():
        hook = Webhook(port=free_port(), secret="")
        await hook.start()
        try:
            # A truncated body is indistinguishable from a slow one, so it is
            # answered when the read gives up rather than immediately.
            return await send(hook.port, payload, wait=READ_TIMEOUT + 5)
        finally:
            await hook.stop()

    assert expected in status_of(asyncio.run(scenario()))


def test_the_listener_survives_a_client_that_vanishes_mid_request():
    """A reset connection is normal traffic, not an error worth surviving loudly."""

    async def scenario():
        hook = Webhook(port=free_port(), secret="")
        await hook.start()
        try:
            for _ in range(20):
                _, writer = await asyncio.open_connection("127.0.0.1", hook.port)
                writer.write(b"POST /webhook HTTP/1.1\r\nContent-Length: 100\r\n\r\n")
                await writer.drain()
                # Drop it without finishing the body, hard.
                writer.transport.abort()
                await asyncio.sleep(0.01)
            # Still serving.
            return await send(hook.port, post())
        finally:
            await hook.stop()

    assert b"200 OK" in status_of(asyncio.run(scenario()))


def test_a_burst_of_bad_requests_does_not_exhaust_the_listener():
    """One bad request must not cost the next good one its connection."""
    bad = b"POST /webhook HTTP/1.1\r\nContent-Length: 999999\r\n\r\nx"

    async def scenario():
        hook = Webhook(port=free_port(), secret="")
        await hook.start()
        held = []
        try:
            for _ in range(40):
                reader, writer = await asyncio.open_connection("127.0.0.1", hook.port)
                writer.write(bad)
                await writer.drain()
                held.append((reader, writer))
            return await send(hook.port, post())
        finally:
            for _, writer in held:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass
            await hook.stop()

    assert b"200 OK" in status_of(asyncio.run(scenario()))


def test_stopping_twice_is_harmless():
    """`chord stop` on a daemon that already stopped is a normal thing to do."""

    async def scenario():
        hook = Webhook(port=free_port(), secret="")
        await hook.start()
        await hook.stop()
        await hook.stop()

    asyncio.run(scenario())