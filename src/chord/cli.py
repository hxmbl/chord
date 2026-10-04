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
import typer.core

from chord import credentials, daemon, notify, subscribe, watcher
from chord.config import SEPARATOR, Config, ConfigError, load
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
    # The grammar, not the routing: this is every label the poll can match,
    # which is a wider set than the lines below, and the difference is the
    # point. `Router.filter()` asks Linear for the bare label and anything
    # starting with the prefix, so a `Chord/...` label nobody curated still
    # arrives — to be reported and skipped, never run on the default harness.
    typer.echo(
        f"  watching   issues labelled {config.label!r} "
        f"or anything starting {config.label}{SEPARATOR}"
    )
    for line in _labels(config):
        typer.echo(line)
    typer.echo(f"  interval   {config.interval}s")
    webhook_line = f"http://127.0.0.1:{config.webhook_port}/webhook"
    if config.webhook_secret:
        # The secret is Chord's, to tell the user. It is already in a file they
        # wrote and there is nothing here that does not go in their tunnel config
        # too, so hiding it helps nobody and makes the setup harder to follow.
        webhook_line += "  (requires X-Chord-Secret)"
    typer.echo(f"  webhook    {webhook_line}")
    for line in _notify():
        typer.echo(line)
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


@app.command(
    name="help",
    # Typer builds this one; the help it shows is authored there, not here.
    add_help_option=False,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def help_command(ctx: typer.Context, args: list[str] | None = None):
    """Show this message and exit.

    A spelling of `--help` for people who type `chord help` and mean it.
    Printed from the same place `--help` is, so the two cannot drift apart as
    commands are added.

    With a command name, forwards to that command's help — `chord help start`
    is what people type when they have forgotten the flags, and rejecting it
    would be a worse answer than `chord start --help`.
    """
    parent = ctx.parent
    # The parent context is the same one `--help` is answered from, and its
    # command is the group holding every subcommand. Reaching the answer through
    # the live context rather than rebuilding one is what keeps the two in step.
    assert parent is not None and isinstance(parent.command, typer.core.TyperGroup), (
        "help was invoked without the top-level command group"
    )
    group = parent.command

    # With `allow_extra_args`, anything after the command name lands in
    # `ctx.args` — the function's own `args` parameter is only populated when
    # the annotation makes it a real parameter, which a bare `list[str]` with a
    # default does not reliably do across typer versions. The context is the
    # stable place to read them from.
    names = list(ctx.args)

    # `chord help --help` is the same request as `chord help`, and treating the
    # flag as a command name would report a missing command called `--help`.
    # `chord help -h` too.
    if not names or names[0] in ("--help", "-h"):
        typer.echo(parent.get_help())
        return

    command = group.commands.get(names[0])
    # Anything starting with `_` is an internal command, hidden from `chord
    # --help` and from this error message. Reachable by name would be a way to
    # get at something the interface deliberately does not advertise.
    if command is None or names[0].startswith("_"):
        visible = sorted(n for n in group.commands if not n.startswith("_"))
        typer.echo(
            f"No command named {names[0]!r}. These are the ones: {', '.join(visible)}.",
            err=True,
        )
        raise typer.Exit(2)

    # A context of its own, parented to the top-level one, so the usage line
    # reads `chord start [OPTIONS]` — the same as `chord start --help` rather
    # than the bare `chord [OPTIONS]` that reusing the parent would produce.
    child = typer.Context(
        command=command,
        info_name=names[0],
        parent=parent,
    )
    typer.echo(command.get_help(child))


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
            # An access token lasts 24 hours and this daemon runs for days, so
            # the token read at startup goes stale. Re-reading it when Linear
            # refuses one is what lets a `chord refresh` heal a running daemon
            # instead of needing a restart nobody remembers to do.
            renew=lambda: _try_token(),
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


def _labels(config: Config) -> list[str]:
    """The labels Chord runs work for, one per line, and what each one starts.

    This is the list somebody has to create in Linear by hand — Chord never
    creates a label — so it is printed as the list it is: exact strings, in a
    column, next to the command each one starts. `chord info` is where you check
    what a label in Linear will actually do, and it is also the only place the
    set can be read *before* it matters.

    The closing lines are not decoration. Adding one of these labels is what
    makes this machine run a harness, so the labels are a trigger surface rather
    than a naming convention, and somebody auditing their workspace wants to know
    both halves: what does run something, and what happens to everything else.
    The answer is that it stops the issue — never a run on the default harness,
    which is the mistake a widened grammar would otherwise make. The prefix is
    named by the `watching` line above rather than repeated here, so the two
    cannot drift apart and the note wraps to the same width whatever the label
    is called.

    A curated harness is a command, and commands are long, so one label per line
    keeps a dozen of them readable.
    """
    routes = _router(config).routes()
    width = max(len(route.label) for route in routes)
    lead = "  labels     "
    lines = [
        f"{lead if index == 0 else ' ' * len(lead)}"
        f"{route.label.ljust(width)}  {route.spelling}"
        for index, route in enumerate(routes)
    ]
    return lines + [
        "             Add these in Linear by hand — Chord never creates a",
        "             label. Adding one is what makes this machine run a",
        "             harness, so this list is the whole trigger surface.",
        "             Anything else under that prefix is reported and",
        "             skipped, never run on the default harness.",
    ]


def _notify() -> list[str]:
    """Which notifier a banner would go through on this machine.

    Asked rather than assumed, because the failure is invisible by
    construction: a notification that reaches no daemon raises nothing, so a
    watcher can hand over a hundred issues and never once have said that it had
    been trying to say something.

    Two lines rather than one, because the osascript case carries a workaround
    and the workaround is a single `brew install`: the symptom — banners
    labelled Script Editor — reads as somebody else's software rather than as a
    missing dependency, and is worth naming the fix for.
    """
    backend = notify.backend()
    if backend == "terminal-notifier":
        return ["  notify     terminal-notifier (Notification Centre)"]
    if backend == "osascript":
        return [
            "  notify     osascript (Notification Centre, attributed to",
            "             Script Editor — `brew install terminal-notifier`",
            "             gives the banners their own name instead)",
        ]
    if backend == "desktop_notifier":
        return ["  notify     desktop_notifier (the session daemon)"]
    return [
        "  notify     nothing, so no notifications are sent.",
        "             Nothing else here can report that, so watch `chord watch`.",
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


def _try_token() -> str | None:
    """The stored token if there is a usable one, else None.

    The difference from `_token()` is that this does not stop the process. It is
    called from inside the running daemon, where exiting would be a worse
    answer than carrying on and saying what is wrong — the watcher falls back
    to reporting, and reports once rather than every poll.
    """
    code, content = credentials.load()
    if code or not isinstance(content, dict):
        return None
    token = content.get("access_token")
    return token if isinstance(token, str) and token else None


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
