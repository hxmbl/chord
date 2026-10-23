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

from chord.webhook import MAX_HEAD, READ_TIMEOUT, Webhook


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
    assert b"400" in status_of(reply), f"held open with no answer: {status_of(reply)!r}"


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
    assert b"401" in status_of(reply), (
        f"a bad secret was accepted: {status_of(reply)!r}"
    )
    assert b"200" not in status_of(reply)


def test_optional_whitespace_around_the_secret_is_not_part_of_it():
    """HTTP strips it, so a sender that pads is still the right sender.

    Worth pinning because the opposite reading is defensible too, and getting it
    wrong would be a lockout rather than a hole: a tunnel that pretty-prints
    headers would start getting 401s for a secret it is sending correctly.
    """
    for padding in (b" ", b"  ", b"\t"):
        payload = post(
            headers=b"X-Chord-Secret: " + padding + b"s3cret" + padding + b"\r\n"
        )

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
        pytest.param(
            b"POST /elsewhere HTTP/1.1\r\nContent-Length: 0\r\n\r\n",
            b"404",
            id="wrong-path",
        ),
        pytest.param(b"GET\r\n\r\n", b"400", id="truncated-request-line"),
        pytest.param(
            b"POST /webhook HTTP/1.1\r\nContent-Length: abc\r\n\r\n",
            b"400",
            id="length-not-a-number",
        ),
        pytest.param(
            b"POST /webhook HTTP/1.1\r\nContent-Length: -3\r\n\r\n",
            b"400",
            id="negative-length",
        ),
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


# --- malformed requests are answered, not logged with a traceback ---


@pytest.mark.parametrize(
    "payload,why",
    [
        pytest.param(b"\x00\x01\x02\r\n\r\n", "no spaces at all", id="no-tokens"),
        pytest.param(b"GET\r\n\r\n", "one token", id="one-token"),
        pytest.param(
            b"POST /webhook HTTP/1.1\r\nContent-Length: -1\r\n\r\n",
            "a negative length",
            id="negative-length",
        ),
        pytest.param(
            b"POST /webhook HTTP/1.1\r\nContent-Length: 1e3\r\n\r\n",
            "a length that is not an integer",
            id="float-length",
        ),
    ],
)
def test_a_malformed_request_is_answered_without_a_traceback(payload, why, caplog):
    """These reached the catch-all, which logged a traceback for ordinary garbage.

    A public socket gets malformed requests from scanners. Logging a stack trace
    for each one fills a daemon's log with noise that has nothing to do with the
    watcher, and buries the lines somebody is looking for.

    So the shapes that can be recognised are recognised and answered, and the
    catch-all is left for genuinely unforeseen cases.
    """

    async def scenario():
        hook = Webhook(port=free_port(), secret="")
        await hook.start()
        try:
            return await send(hook.port, payload, wait=READ_TIMEOUT + 5)
        finally:
            await hook.stop()

    reply = asyncio.run(scenario())
    assert b"400" in status_of(reply), f"{why}: {status_of(reply)!r}"
    assert not caplog.records, [r.getMessage() for r in caplog.records]


def test_a_normal_garbage_request_is_still_unlogged(caplog):
    """And the ordinary malformed shapes never reach the catch-all either."""

    async def scenario():
        hook = Webhook(port=free_port(), secret="")
        await hook.start()
        try:
            for payload in (
                b"\x00\x01\x02\r\n\r\n",
                b"GET\r\n\r\n",
                b"POST /webhook HTTP/1.1\r\nContent-Length: -1\r\n\r\n",
                b"POST /webhook HTTP/1.1\r\nContent-Length: abc\r\n\r\n",
            ):
                await send(hook.port, payload, wait=READ_TIMEOUT + 5)
        finally:
            await hook.stop()

    asyncio.run(scenario())
    assert not caplog.records, [r.getMessage() for r in caplog.records]


def test_the_request_line_needs_exactly_three_parts():
    """A request line that is only a method is a bad request, not a crash."""

    async def scenario():
        hook = Webhook(port=free_port(), secret="")
        await hook.start()
        try:
            return await send(hook.port, b"POST\r\n\r\n", wait=READ_TIMEOUT + 5)
        finally:
            await hook.stop()

    assert b"400" in status_of(asyncio.run(scenario()))


def test_a_well_formed_request_still_answers_200():
    """So the tolerance above has not broken the happy path."""

    async def scenario():
        hook = Webhook(port=free_port(), secret="")
        await hook.start()
        try:
            return await send(hook.port, post())
        finally:
            await hook.stop()

    assert b"200 OK" in status_of(asyncio.run(scenario()))


# --- the report the instrumentation reads, and the header bound ---


class FakeWriter:
    """The half of a `StreamWriter` `_handle` touches, so it can be called bare."""

    def __init__(self):
        self.closed = False
        self.written = b""

    def write(self, data):
        self.written += data

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


def reader_for(payload: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    return reader


def test_the_handler_reports_a_served_request_as_a_success():
    async def scenario():
        hook = Webhook(secret="")
        return await hook._handle(reader_for(post()), FakeWriter())

    assert asyncio.run(scenario()) is True


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(post(headers=b"X-Chord-Secret: wrong\r\n"), id="401"),
        pytest.param(post(method=b"GET"), id="404"),
        pytest.param(
            b"POST /webhook HTTP/1.1\r\nContent-Length: abc\r\n\r\n", id="400"
        ),
    ],
)
def test_a_refused_request_is_reported_as_a_failure(payload):
    """The method answers failures rather than raising them.

    "Did not raise" would report every 400, 401 and 404 as a success, which is
    a measurement pointing the wrong way.
    """

    async def scenario():
        hook = Webhook(secret="s3cret")
        return await hook._handle(reader_for(payload), FakeWriter())

    assert asyncio.run(scenario()) is False


def test_a_cancelled_request_still_closes_its_socket():
    """`chord stop` cancels a handler mid-request; the socket must still close.

    Cancellation used to re-raise straight past the write and the close, leaking
    the connection — and `stop()` waits for the handlers, so it never finished.
    """

    async def scenario():
        hook = Webhook(secret="")

        async def never(reader):
            await asyncio.sleep(3600)

        hook._respond = never
        writer = FakeWriter()
        task = asyncio.create_task(hook._handle(asyncio.StreamReader(), writer))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return writer

    assert asyncio.run(scenario()).closed, "a cancelled handler leaked its socket"


def test_a_refused_secret_says_so_in_the_log(caplog):
    """Otherwise a lockout is invisible: every delivery 401s, live updates fall
    back to polling, and nothing distinguishes a wrong secret from one that
    cannot be transmitted at all."""

    async def scenario():
        hook = Webhook(port=free_port(), secret="s3cret")
        await hook.start()
        try:
            return await send(hook.port, post(headers=b"X-Chord-Secret: wrong\r\n"))
        finally:
            await hook.stop()

    reply = asyncio.run(scenario())
    assert b"401" in status_of(reply)
    messages = [record.getMessage() for record in caplog.records]
    assert any("wrong secret" in message for message in messages), messages
    assert all("s3cret" not in message for message in messages), "the secret was echoed"


# --- a secret has to survive the wire, in either encoding ---


def test_a_non_ascii_secret_sent_as_latin1_is_accepted():
    """HTTP header values are latin-1 on the wire.

    Matching only the UTF-8 form meant a secret configured with an accent and
    sent the standard way was refused forever, and a lockout looks exactly like
    a wrong secret.
    """

    async def scenario():
        hook = Webhook(port=free_port(), secret="café")
        await hook.start()
        try:
            return await send(hook.port, post(headers=b"X-Chord-Secret: caf\xe9\r\n"))
        finally:
            await hook.stop()

    assert b"200 OK" in status_of(asyncio.run(scenario()))


def test_a_non_ascii_secret_sent_as_utf8_is_accepted():
    """A modern client may encode the header that way; refusing it is a lockout."""

    async def scenario():
        hook = Webhook(port=free_port(), secret="café")
        await hook.start()
        try:
            # `.encode()` is UTF-8, the wire form a modern client produces.
            header = b"X-Chord-Secret: " + "café".encode() + b"\r\n"
            return await send(hook.port, post(headers=header))
        finally:
            await hook.stop()

    assert b"200 OK" in status_of(asyncio.run(scenario()))


def test_a_non_ascii_secret_still_refuses_the_wrong_one():
    async def scenario():
        hook = Webhook(port=free_port(), secret="café")
        await hook.start()
        try:
            return await send(hook.port, post(headers=b"X-Chord-Secret: cafe\r\n"))
        finally:
            await hook.stop()

    assert b"401" in status_of(asyncio.run(scenario()))


def test_a_secret_no_client_can_send_has_no_wire_form():
    """A lone surrogate cannot be encoded as a header value, so nothing matches."""
    from chord.webhook import _wire_forms

    assert _wire_forms("\ud800") == []


def test_a_header_block_over_the_limit_is_refused_not_buffered():
    """`MAX_HEAD` is the bound, and the asyncio default it was lost under is 64KiB.

    A header block between `MAX_HEAD` and that default used to be read in full
    and answered 200, so the bound was dead. It is what stops one request from
    making the listener hold an arbitrary amount before the secret is checked at
    all.
    """
    padding = b"X-Pad: " + b"y" * (MAX_HEAD + 4096) + b"\r\n"

    async def scenario():
        hook = Webhook(port=free_port(), secret="")
        await hook.start()
        try:
            return await send(hook.port, post(headers=padding), wait=READ_TIMEOUT + 5)
        finally:
            await hook.stop()

    assert b"400" in status_of(asyncio.run(scenario()))
