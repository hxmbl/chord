"""Chord's command line.

Six things a person can do: connect Chord to Linear, look at what it's set up
to do, start watching, follow the log, stop watching, and renew the token.
Everything else happens in the background.
"""

import asyncio
import signal
import subprocess
import time
from typing import NoReturn

import typer

from chord import credentials, daemon, subscribe, watcher
from chord.config import Config, ConfigError, load
from chord.harness import HarnessError
from chord.linear import Linear
from chord.routing import Router
from chord.watcher import StateError
from chord.webhook import Webhook

app = typer.Typer(
    help="Hand Linear issues to a coding harness.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def setup():
    """Connect Chord to your Linear account."""
    code, message = asyncio.run(credentials.authenticate(store=True))
    if code:
        _fail(message)
    typer.echo("Connected to Linear.")
    typer.echo("Next: `chord start` to begin watching.")


@app.command()
def refresh():
    """Renew the stored Linear token. This never happens on its own."""
    code, content = asyncio.run(credentials.refresh())
    if code:
        _fail(content)

    expires_at = content.get("expires_at") if isinstance(content, dict) else None
    if expires_at:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(expires_at))
        typer.echo(f"Renewed. The new token is good until {stamp}.")
    else:
        typer.echo("Renewed.")


@app.command()
def start():
    """Start watching Linear in the background."""
    config = _config()
    # Everything that can be wrong is checked here, before anything is spawned,
    # so the person who typed the command is the one who hears about it rather
    # than a log file they haven't been pointed at yet.
    _token()
    _check_routes(_router(config))
    try:
        watcher.read_state(daemon.STATE_FILE)
    except StateError as exc:
        _fail(str(exc))

    try:
        pid = daemon.start()
    except daemon.AlreadyRunning as exc:
        _fail(f"Already watching (pid {exc.pid}). `chord stop` to stop it first.")
    except daemon.StartFailed as exc:
        _fail(str(exc))

    if not config.from_file:
        typer.echo(
            f"No {config.path} here, so those are Chord's defaults. Write that "
            "file to change them; the README has the annotated version."
        )

    typer.echo(f"Watching in the background (pid {pid}).")
    typer.echo("`chord watch` follows the log. `chord info` shows the state.")


@app.command()
def stop():
    """Stop watching."""
    try:
        pid = daemon.stop()
    except daemon.NotOurs as exc:
        _fail(str(exc))
    except daemon.StopFailed as exc:
        _fail(str(exc))

    if pid is None:
        typer.echo("Nothing was watching.")
        return
    typer.echo(f"Stopped (pid {pid}).")


@app.command()
def watch():
    """Follow the watcher's log, as it happens."""
    if not daemon.LOG_FILE.exists():
        if daemon.running_pid() is None:
            _fail("Chord hasn't been started yet. `chord start` first.")
        _fail(
            f"{daemon.LOG_FILE} has gone missing. Try `chord stop` then `chord start`."
        )

    try:
        subprocess.run(["tail", "-f", str(daemon.LOG_FILE)], check=False)
    except KeyboardInterrupt:
        pass  # Ctrl+C is how you leave a log follower; it isn't a failure.
    except FileNotFoundError:
        _fail("Couldn't run `tail`, which `chord watch` uses to follow the log.")


@app.command()
def info():
    """Show what Chord is set up to do."""
    config = _config()
    pid = daemon.running_pid()
    try:
        count = len(watcher.read_state(daemon.STATE_FILE))
    except StateError as exc:
        count = None
        state_note = str(exc)
    else:
        state_note = None

    typer.echo("Chord")
    typer.echo(f"  daemon     {_daemon_line(pid)}")
    typer.echo(
        f"  watching   issues labelled {config.label!r} "
        f"or {config.label}/<harness>"
    )
    for line in _routes(config):
        typer.echo(line)
    typer.echo(f"  interval   {config.interval}s")
    webhook_line = f"http://127.0.0.1:{config.webhook_port}/webhook"
    if config.webhook_secret:
        # The secret is Chord's, to tell the user. It is already in a file they
        # wrote and there is nothing here that does not go in their tunnel config
        # too, so hiding it helps nobody and makes the setup harder to follow.
        webhook_line += "  (requires X-Chord-Secret)"
    typer.echo(f"  webhook    {webhook_line}")
    typer.echo(f"  linear     {_linear_line()}")
    typer.echo(f"  config     {_config_line(config)}")
    if count is not None:
        typer.echo(f"  handed     {_plural(count, 'issue')} so far")
    if state_note:
        typer.echo(f"  note       {state_note}")
    for line in _authorisation(config):
        typer.echo(line)

    if not config.from_file:
        typer.echo("")
        typer.echo(
            f"No {config.path} here, so those are Chord's defaults. Write that "
            "file to change them; the README has the annotated version."
        )

    if pid is None:
        typer.echo("")
        typer.echo("Not watching. `chord start` to begin.")


@app.command(name=daemon.SERVE_COMMAND, hidden=True)
def serve():
    """Internal. The detached watcher's own entry point."""
    asyncio.run(_watch())


async def _watch() -> None:
    config = _config()
    token = _token()
    router = _router(config)
    try:
        # `chord start` did this too, but config can have changed since, and a
        # watcher that dies on its first issue is worse than one that says why
        # it is stopping.
        router.validate()
    except HarnessError as exc:
        watcher.log(f"Can't run every route: {exc}")
        raise SystemExit(1) from None

    # Webhooks are the fast path. The interval poll remains the correctness
    # backup for dropped, delayed, or misconfigured deliveries.
    webhook = Webhook(port=config.webhook_port, secret=config.webhook_secret)
    subscription = None
    if subscribe.available():
        subscription = subscribe.Subscription(token)

    try:
        runner = watcher.Watcher(
            linear=Linear(token),
            router=router,
            interval=config.interval,
            state_path=daemon.STATE_FILE,
            subscription=subscription,
            webhook=webhook,
        )
    except StateError as exc:
        watcher.log(str(exc))
        raise SystemExit(1) from None

    if subscription is not None:
        # Route socket trouble through the watcher's once-only logging, so a
        # long outage is one line rather than one line per interval.
        subscription.report_problems_to(runner.note_subscription_problem)

    # SIGTERM cancels the watcher rather than killing the process outright, so
    # a harness part-way through an issue is stopped through the same path that
    # tidies up after it. The log keeps the reason, which `chord stop` alone
    # can't know.
    task = asyncio.create_task(runner.run())
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        watcher.log("Stopping.")
    finally:
        daemon.release()


def _config() -> Config:
    try:
        return load()
    except ConfigError as exc:
        _fail(str(exc))


def _router(config: Config) -> Router:
    return Router(
        config.label,
        config.harness,
        config.harnesses,
        allowed_actors=config.allowed_actors,
    )


def _check_routes(router: Router) -> None:
    """Build every route now, so a broken one is reported by `chord start`.

    A route whose command isn't installed would otherwise be discovered the
    first time an issue happens to carry that label — possibly overnight, with
    no indication of which config file is wrong. Curating a harness you can't
    run is a mistake worth catching while the person is still looking.
    """
    try:
        router.validate()
    except HarnessError as exc:
        _fail(str(exc))


def _routes(config: Config) -> list[str]:
    """The label grammar as it stands, one line per route.

    A curated harness is a command, and commands are long. One route per line
    with the labels in a column keeps a dozen of them readable, which is the
    whole point of listing them: `chord info` is where you check what a label
    in Linear will actually do.
    """
    routes = _router(config).routes()
    width = max(len(route.label) for route in routes)
    lead = "  routes     "
    return [
        f"{lead if index == 0 else ' ' * len(lead)}"
        f"{route.label.ljust(width)}  {route.spelling}"
        for index, route in enumerate(routes)
    ]


def _authorisation(config: Config) -> list[str]:
    """The allowlist, and yours, because the setting is a list of ids.

    Nobody knows their own Linear user id — Linear's UI does not show it — so a
    config that is a list of them would be unwritable if `chord info` were not
    the place that hands it over. It also says whether Chord is currently
    checking at all, because an empty allowlist is the permissive default and
    silence about that would read as "everything is fine".
    """
    if not config.authorises:
        return [
            "  allowed    anyone in the workspace can trigger a run.",
            "             Set allowed_actors in chord.toml to restrict that;",
            "             `chord info` prints your Linear user id.",
        ]

    yours = credentials.viewer_id(_stored())
    if not config.authorises:
        return [
            "  allowed    anyone in the workspace can trigger a run.",
            "             Set allowed_actors in chord.toml to restrict that;",
            f"             your Linear user id is {yours or '(run `chord setup` first)'}.",
        ]

    lines = [f"  allowed    {len(config.allowed_actors)} Linear user id(s)"]
    if yours:
        # Printed whether or not it is in the list. The list is ids, Linear does
        # not show you yours, and `chord info` is the only place that can.
        lines.append(f"             yours: {yours}")
        if yours not in config.allowed_actors:
            lines.append("             which is NOT in allowed_actors — Chord won't run your work")
    return lines


def _stored() -> object:
    code, content = credentials.read()
    return None if code else content


def _token() -> str:
    code, content = credentials.load()
    if code:
        _fail(content)
    if not isinstance(content, dict):  # load() only succeeds with a dict.
        _fail("The keychain entry isn't a set of credentials. Run `chord setup`.")
    return content["access_token"]


def _linear_line() -> str:
    """Credential status, from the keychain only. `chord info` stays offline so
    it's quick and can't fail for reasons that have nothing to do with what
    you're asking.

    This reads rather than loads, so a token that is present but past our max
    age reads as connected-and-stale instead of as not connected at all. Those
    are different problems with different next steps, and the one thing a
    person running `chord info` is trying to tell apart.
    """
    code, content = credentials.read()
    if code:
        return f"not connected ({content})"
    if not isinstance(content, dict):
        return "not connected (the keychain entry is in an unexpected shape)"

    age = time.time() - (credentials.obtained_at(content) or time.time())
    if age > credentials.max_age():
        return "connected, but the token is past max age (`chord refresh`)"

    expires_at = content.get("expires_at")
    if not isinstance(expires_at, int | float) or isinstance(expires_at, bool):
        return "connected"
    left = expires_at - time.time()
    if left <= 0:
        return "connected, but the token has expired (`chord refresh`)"
    return f"connected, token good for {_remaining(left)}"


def _daemon_line(pid: int | None) -> str:
    if pid is None:
        return "not running"
    up = _uptime(daemon.started_at())
    return f"running (pid {pid}{up})"


def _config_line(config: Config) -> str:
    if not config.from_file:
        return f"{config.path} (defaults, file not created)"
    return str(config.path)


def _uptime(started: float | None) -> str:
    if started is None:
        return ""
    return f", up {_remaining(time.time() - started)}"


def _remaining(seconds: float) -> str:
    """A rough span, in words. Exact to the second would be false precision:
    nothing here is scheduled to the second."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{max(seconds, 0)}s"
    if seconds < 60 * 60:
        return f"{seconds // 60}m"
    if seconds < 60 * 60 * 24:
        return f"{seconds // (60 * 60)}h {seconds % (60 * 60) // 60}m"
    return f"{seconds // (60 * 60 * 24)}d {seconds % (60 * 60 * 24) // (60 * 60)}h"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _fail(message: object) -> NoReturn:
    """Report a problem and stop. Chord keeps its failures to one line, and
    they go to stderr so a script can tell them from the answer."""
    typer.echo(message, err=True)
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
