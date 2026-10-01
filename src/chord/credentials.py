"""Linear OAuth: connecting, storing the token in the keychain, renewing it.

The client secret is read from `.env` and never logged, never returned, and
never leaves this module except into the OAuth client that needs it.
"""

import asyncio
import json
import re
import secrets
import time
import warnings
from typing import TypeAlias

import typer
from authlib.deprecate import AuthlibDeprecationWarning

from chord.linear import GRAPHQL_ENDPOINT
from chord.paths import find_upwards

AUTHORIZATION_ENDPOINT = "https://linear.app/oauth/authorize"
TOKEN_ENDPOINT = "https://api.linear.app/oauth/token"
VIEWER_QUERY = "{ viewer { id email name } }"

KEYRING_SERVICE = "chord"
KEYRING_ACCOUNT = "linear"

CALLBACK_PORT = 23841
CALLBACK_TIMEOUT = 120

# The OAuth calls are short, bounded exchanges with Linear's own servers, and
# deliberately much tighter than a poll: during `chord setup` a person is
# sitting in front of a browser waiting on the result. `chord.linear.TIMEOUT`
# is the read timeout, and `chord.watcher.POLL_TIMEOUT` the ceiling on a poll.
AUTH_TIMEOUT = 10

# The only query params we ever accept from the callback.
_CALLBACK_PARAMS = ("code", "state", "error", "error_description")

OK = 0
FAILED = 1

# Linear's access tokens are good for 24 hours and its app settings offer no
# way to ask for less, so nothing we do here shortens a leaked token's life at
# Linear. What a lower ceiling buys is churn: a dev build with a small
# CHORD_MAX_AGE stops handing out a token it got hours ago and makes you
# re-authorize, so a token copied off a dev machine goes stale from our side on
# a short clock. See `max_age`.
DEFAULT_MAX_AGE = 24 * 60 * 60
LINEAR_TTL = 24 * 60 * 60

# Every step answers with (exit code, content): on failure the content is the
# error message, on success it is whatever that step produced.
Outcome: TypeAlias = tuple[int, str | dict]

warnings.filterwarnings(
    "ignore",
    message="The httpx module is deprecated.*",
    category=AuthlibDeprecationWarning,
)


def _oauth_clients():
    """Load authlib without surfacing its expected legacy fallback warning."""
    try:
        import httpx2  # type: ignore[reportMissingImports]  # noqa: F401
    except ImportError:
        # Authlib emits this warning when it falls back to httpx. Keep the
        # fallback for existing installations, but don't make every test and
        # command noisy when httpx2 is not installed yet.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore", message="The httpx module is deprecated.*"
            )
            from authlib.integrations.httpx_client import (
                AsyncOAuth2Client,
                OAuthError,
            )
    else:
        from authlib.integrations.httpx_client import AsyncOAuth2Client, OAuthError
    return AsyncOAuth2Client, OAuthError


def _printable(value: str) -> str:
    """Neuter anything attacker-controlled before it reaches the terminal."""
    return re.sub(r"[^\w .:\-]", "?", value)[:100]


def _client_secrets() -> str | tuple[str, str]:
    """The client id and secret from `.env`, or a message saying what's wrong.

    Returns a string on failure and the pair on success, rather than an
    `(exit code, content)` pair, because a pair of strings is not one of the
    things `Outcome` carries and every caller has to unpack it anyway.

    Both keys are read together so that neither call site can end up with one
    guarded by a friendly message and the other raising a bare KeyError at the
    person, which is what `refresh` used to do with the secret.
    """
    from dotenv import dotenv_values

    path = find_upwards(".env")
    env = dotenv_values(path)
    wanted = ("LINEAR_CLIENT_ID", "LINEAR_CLIENT_SECRET")
    missing = [name for name in wanted if not env.get(name)]
    if missing:
        return f"{', '.join(missing)} missing from {path}."
    return (env[wanted[0]] or "", env[wanted[1]] or "")


def max_age() -> int:
    """Seconds we're willing to keep handing out the same token."""
    from dotenv import dotenv_values

    configured = dotenv_values(find_upwards(".env")).get("CHORD_MAX_AGE")
    try:
        return int(configured) if configured else DEFAULT_MAX_AGE
    except ValueError:
        return DEFAULT_MAX_AGE


def obtained_at(credentials: dict) -> float | None:
    """When we got this token, preferring our own stamp over Linear's arithmetic."""
    stamp = credentials.get("obtained_at")
    if isinstance(stamp, int | float):
        return float(stamp)

    # Tokens stored before we kept a stamp still know their expiry, and the TTL
    # is fixed, so back out the issue time from it.
    expires_at = credentials.get("expires_at")
    if isinstance(expires_at, int | float):
        return float(expires_at) - LINEAR_TTL
    return None


def _store(credentials: dict) -> Outcome:
    """Store credentials in keychain. Use keyring here."""
    import keyring
    from keyring.errors import KeyringError

    # Never fall back to a plaintext file backend.
    backends = getattr(keyring.get_keyring(), "backends", [keyring.get_keyring()])
    if any(type(b).__module__.startswith("keyrings.alt") for b in backends):
        return FAILED, "Refusing to use an insecure keyring backend."

    try:
        # Stamp it so `load` can tell how long we've been sitting on this.
        stored = {**credentials, "obtained_at": time.time()}
        keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, json.dumps(stored))
    except KeyringError as exc:
        return FAILED, f"Couldn't store credentials: {type(exc).__name__}."

    return OK, stored


def read() -> Outcome:
    """Whatever is in the keychain, with no opinion on how old it is.

    The gate-free half of `load`, and the reason it is a separate function:
    `refresh` exists to renew a token that is past its max age, so it must be
    able to read one. Routing it through the freshness check made `chord
    refresh` answer "Run `chord refresh`" and lock the only way out.
    """
    import keyring

    try:
        raw = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    except Exception as exc:  # Keyring backends raise all sorts of things.
        return FAILED, f"Couldn't read the keychain: {type(exc).__name__}."

    if not raw:
        return FAILED, "Not set up yet. Run `chord setup`."

    try:
        credentials = json.loads(raw)
    except ValueError:
        return FAILED, "The keychain entry isn't JSON. Run `chord setup` again."

    if not isinstance(credentials, dict) or not credentials.get("access_token"):
        return FAILED, "No access token in the keychain. Run `chord setup` again."

    return OK, credentials


def load() -> Outcome:
    """Read the stored credentials back out of the keychain, if they're fresh.

    A pure read: no network, no refresh. A token that's past our max age comes
    back as a failure, because renewing it is your call, not ours.
    """
    code, content = read()
    if code:
        return code, content
    if not isinstance(content, dict):  # read() only succeeds with a dict.
        return (
            FAILED,
            "The keychain entry isn't a set of credentials. Run `chord setup`.",
        )

    stamp = obtained_at(content)
    if stamp is None:
        return FAILED, "Can't tell how old that token is. Run `chord refresh`."

    age = time.time() - stamp
    if age > max_age():
        mins = round(age / 60)
        return FAILED, f"Token is {mins}m old, past max age. Run `chord refresh`."

    return OK, content


async def refresh() -> Outcome:
    """Trade the stored refresh token for a new pair, and put the new pair away.

    Never automatic, and never silent: Linear rotates the refresh token on
    every exchange, so the old one dies the moment this succeeds. We always
    re-store, otherwise you'd be left holding a dead pair and a live access
    token with nothing to renew it.
    """
    # `read`, not `load`: a token past its max age is precisely the one that
    # needs renewing, so applying the freshness gate here would make this
    # command refuse to do the only thing it exists to do.
    code, content = read()
    if code:
        return code, content

    if not isinstance(content, dict):
        return FAILED, "Stored credentials are in an unexpected shape."
    credentials = content

    refresh_token = credentials.get("refresh_token")
    if not refresh_token:
        return FAILED, "No refresh token stored. Run `chord setup` again."

    pair = _client_secrets()
    if isinstance(pair, str):
        return FAILED, pair
    client_id, client_secret = pair

    # Authlib prefers its httpx2 compatibility module. Older environments may
    # not have httpx2, so the helper retains the httpx fallback.
    AsyncOAuth2Client, OAuthError = _oauth_clients()

    # authlib's AsyncOAuth2Client supports `async with` at runtime (it
    # inherits it from httpx.AsyncClient) but doesn't declare __aenter__ /
    # __aexit__ in its annotations, so pyright reports an error here. The
    # stubs are incomplete, not the code.
    async with AsyncOAuth2Client(  # type: ignore[reportGeneralTypeIssues]
        client_id=client_id,
        client_secret=client_secret,
        token=credentials,
        timeout=AUTH_TIMEOUT,
    ) as client:
        try:
            # The old token comes along so fields Linear omits survive the swap.
            token = await client.refresh_token(
                TOKEN_ENDPOINT, refresh_token=refresh_token
            )
        except OAuthError as exc:
            return FAILED, f"Linear refused the refresh: {_printable(str(exc))}"

    rotated = dict(token)
    code, content = _store(rotated)
    if code:
        return code, content

    return OK, content


async def authenticate(store: bool) -> Outcome:
    """Authenticate via OAuth and get the key."""

    async def _obtain() -> Outcome:
        """Obtain credentials."""

        import threading
        import webbrowser
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from urllib.parse import parse_qs, urlencode, urlparse

        # The handler lives on a server thread, so the callback hops back to us
        # over a plain threading event instead of an asyncio future.
        received = threading.Event()
        callback: dict[str, str] = {}
        # Filled in once authlib has minted the state, before the server starts
        # serving. The handler uses it to reject anything that isn't our flow.
        expected_state: dict[str, str] = {}

        class _CallbackHandler(BaseHTTPRequestHandler):
            timeout = 5  # a stalled connection can't wedge the single-threaded server

            def _reply(self, status: int, body: bytes = b"") -> None:
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'none'")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                """Handle the OAuth callback."""
                # Only accept the exact host we bound to (DNS rebinding guard).
                if self.headers.get("Host") != f"127.0.0.1:{CALLBACK_PORT}":
                    self._reply(400)
                    return

                if urlparse(self.path).path.rstrip("/") != "/callback":
                    self._reply(404)
                    return

                # First valid callback wins; nothing can overwrite it afterwards.
                if received.is_set():
                    self._reply(409)
                    return

                query = parse_qs(urlparse(self.path).query)
                params = {k: query[k][0] for k in _CALLBACK_PARAMS if k in query}

                # Stray or forged hits get bounced without ending the wait, so
                # a local poke can't kill the login attempt.
                expected = expected_state.get("value")
                if (
                    not expected
                    or not secrets.compare_digest(
                        params.get("state", "").encode(), expected.encode()
                    )
                    or not ("code" in params or "error" in params)
                ):
                    self._reply(400)
                    return

                callback.update(params)
                self._reply(200, b"<p>Chord is connected. Close this window.</p>")
                received.set()

            def log_message(self, format: str, *args) -> None:
                """Don't spam stdout with HTTP logs."""

        # Assign a port for OAuth
        try:
            server = HTTPServer(("127.0.0.1", CALLBACK_PORT), _CallbackHandler)
        except OSError as exc:
            return FAILED, f"Port {CALLBACK_PORT} is taken: {exc.strerror}."

        port = server.server_address[1]
        redirect_uri = f"http://127.0.0.1:{port}/callback"

        pair = _client_secrets()
        if isinstance(pair, str):
            return FAILED, pair
        client_id, client_secret = pair

        from authlib.common.security import generate_token
        # See the matching import in refresh().
        AsyncOAuth2Client, OAuthError = _oauth_clients()

        # authlib's AsyncOAuth2Client supports `async with` at runtime (it
        # inherits it from httpx.AsyncClient) but doesn't declare __aenter__ /
        # __aexit__ in its annotations, so pyright reports an error here. The
        # stubs are incomplete, not the code.
        async with AsyncOAuth2Client(  # type: ignore[reportGeneralTypeIssues]
            client_id=client_id,
            client_secret=client_secret,
            scope="read",
            redirect_uri=redirect_uri,
            code_challenge_method="S256",
            timeout=AUTH_TIMEOUT,
        ) as client:
            # The verifier has to outlive the round trip through the browser,
            # so it is generated up front and handed back at token time.
            code_verifier = generate_token(48)
            uri, state = client.create_authorization_url(
                AUTHORIZATION_ENDPOINT, code_verifier=code_verifier
            )
            expected_state["value"] = state
            typer.echo(f"Grant access here:\n{uri}")
            webbrowser.open(uri)

            # serve_forever blocks, so keep it off the event loop.
            serving = asyncio.create_task(asyncio.to_thread(server.serve_forever))
            try:
                granted = await asyncio.to_thread(received.wait, CALLBACK_TIMEOUT)
            finally:
                server.shutdown()
                await serving
                server.server_close()

            if not granted:
                return FAILED, f"Timed out after {CALLBACK_TIMEOUT}s."

            if "error" in callback:
                return FAILED, f"Linear said no: {_printable(callback['error'])}"

            authorization_response = f"{redirect_uri}?{urlencode(callback)}"
            # fetch_token replays the callback through the state check, so a
            # forged or stale redirect dies here rather than downstream.
            try:
                token = await client.fetch_token(
                    TOKEN_ENDPOINT,
                    authorization_response=authorization_response,
                    state=state,
                    code_verifier=code_verifier,
                )
            except OAuthError as exc:
                return FAILED, f"Linear refused the code: {_printable(str(exc))}"

        return OK, dict(token)

    async def _confirm(credentials: dict) -> Outcome:
        """Verify the credentials are valid."""
        import httpx

        access_token = credentials.get("access_token")
        if not access_token:
            return FAILED, "No access token in the credentials."

        try:
            async with httpx.AsyncClient(timeout=AUTH_TIMEOUT) as http:
                response = await http.post(
                    GRAPHQL_ENDPOINT,
                    json={"query": VIEWER_QUERY},
                    headers={"Authorization": f"Bearer {access_token}"},
                )
        except httpx.HTTPError as exc:
            return FAILED, f"Couldn't reach Linear: {type(exc).__name__}."

        if response.status_code != 200:
            return FAILED, f"Linear rejected the token: HTTP {response.status_code}."

        try:
            data = response.json().get("data") or {}
        except ValueError:
            return FAILED, "Linear sent back something that isn't JSON."

        if not data.get("viewer"):
            return FAILED, "No viewer came back for that token."

        return OK, "Credentials validated."

    code, content = await _obtain()
    if code:
        return code, content

    if not isinstance(content, dict):
        return FAILED, "Obtained credentials in an unexpected shape."
    credentials = content

    code, content = await _confirm(credentials)
    if code:
        return code, content

    if store:
        code, content = _store(credentials)
        if code:
            return code, content
        return OK, content
    typer.echo("Not storing credentials. Bye.")

    return OK, credentials


if __name__ == "__main__":
    typer.echo("Running obtain and throw away authenticate for testing.")
    code, content = asyncio.run(authenticate(False))
    if code:
        typer.echo(content, err=True)
    else:
        typer.echo("Authenticated. Throwing the credentials away.")
    raise SystemExit(code)
