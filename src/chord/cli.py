"""Chord's command line.

Five things a person does: set Chord up, look at what it's set up to do, start
watching, follow the log, and stop watching. Everything else, renewing the
token included, happens in the background. `chord service` hands the watching to
the operating system, so it starts at login and comes back after a crash.
"""

import asyncio
import signal
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import NoReturn

import typer
import typer.core

from chord import credentials, daemon, notify, service, subscribe, watcher
from chord import setup as guided
from chord.config import (
    SEPARATOR,
    AlreadyCurated,
    Config,
    ConfigError,
    curate,
    init_file,
    load,
)
from chord.harness import HarnessError, command_line
from chord.linear import Linear
from chord.paths import project_root
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
    """Get Chord ready: your Linear app, signing in, and this project."""
    if not guided.interactive():
        # No one to answer questions (a script, CI). Sign in with what `.env`
        # already holds, which is all the old `chord setup` did.
        code, message = asyncio.run(credentials.authenticate(store=True))
        if code:
            _fail(f"{message} Run `chord setup` in a terminal to be walked through it.")
        typer.echo("Connected to Linear.")
        return
    try:
        guided.run()
    except guided.SetupError as exc:
        _fail(str(exc))
    except typer.Abort:
        _fail("\nSetup stopped. Run `chord setup` again to pick up where you left off.")
    if _offer_service():
        typer.echo("\nReady, and watching as a service.")
        return
    typer.echo("\nReady. Run `chord start`.")


def _offer_service() -> bool:
    """At the end of setup, offer to run Chord at login. True if it now does.

    A question, never a default: a service runs harnesses on this machine
    whenever somebody labels an issue, which is a decision, not a setup step.
    Not asked where there's no service manager, or when it's already installed.
    """
    svc = service.current()
    if svc is None or service.installed(svc):
        return False
    if not typer.confirm(
        "\nRun Chord in the background whenever you log in? "
        "(`chord service install`; you can do it later)",
        default=False,
    ):
        return False
    _install_service(svc)
    return True


@app.command()
def init():
    """Write the stock `.config/chord/chord.toml` for this directory."""
    try:
        path = init_file()
    except ConfigError as exc:
        _fail(str(exc))
    typer.echo(f"Wrote {path}.")
    typer.echo("Every setting is there at its default, with a comment. Commit it.")


harness_app = typer.Typer(
    help="Curated harnesses in chord.toml.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(harness_app, name="harness")


@harness_app.command("add")
def harness_add(
    name: str = typer.Argument(..., help="What a `Chord/<name>` label routes to."),
    command: list[str] = typer.Argument(
        ..., help="The command to run, with the issue on stdin. Use `--` first."
    ),
):
    """Add `[harnesses."<name>"]` to chord.toml.

    Creates the stock chord.toml if there isn't one. The name is what a
    `Chord/<name>` Linear label selects — you still create that label by
    hand, because Chord never creates one.
    """
    try:
        path, created = curate(name, command)
    except AlreadyCurated as exc:
        _fail(str(exc))
    except ConfigError as exc:
        _fail(str(exc))
    verb = "Wrote" if created else "Added to"
    label = SEPARATOR.join([load(path).label, name])
    typer.echo(f"{verb} {path}.")
    typer.echo(
        f"Create the label {label!r} in Linear by hand — Chord never creates one."
    )
    typer.echo("`chord info` lists every label Chord acts on.")


@app.command()
def refresh():
    """Renew the Linear token now. Chord also does this on its own."""
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
    """Start watching Linear in the background. Sets Chord up first if needed."""
    _signed_in()
    config = _config()
    # Everything that can be wrong is checked here, before anything is spawned,
    # so the person who typed the command is the one who hears about it rather
    # than a log file they haven't been pointed at yet.
    _check_routes(_router(config))
    try:
        watcher.read_state(daemon.STATE_FILE)
    except StateError as exc:
        _fail(str(exc))

    svc = service.current()
    if svc is not None and service.installed(svc):
        # One watcher per project. With a service installed it is the
        # service's to run, so a second, hand-started one can't race it.
        try:
            service.start(svc)
        except service.ServiceError as exc:
            _fail(str(exc))
        typer.echo(f"Started the Chord service ({svc.manager} {svc.name}).")
        typer.echo("`chord watch` follows the log. `chord info` shows the state.")
        return

    try:
        pid = daemon.start()
    except daemon.AlreadyRunning as exc:
        _fail(f"Already watching (pid {exc.pid}). `chord stop` to stop it first.")
    except daemon.StartFailed as exc:
        _fail(str(exc))

    if not config.from_file:
        typer.echo(
            f"No {config.path} here, so those are Chord's defaults. "
            "`chord init` writes that file."
        )

    typer.echo(f"Watching in the background (pid {pid}).")
    typer.echo("`chord watch` follows the log. `chord info` shows the state.")


@app.command()
def stop():
    """Stop watching."""
    svc = service.current()
    if svc is not None and service.installed(svc):
        try:
            service.stop(svc)
        except service.ServiceError as exc:
            _fail(str(exc))
        typer.echo("Stopped the Chord service. It starts again at your next login;")
        typer.echo("`chord service uninstall` stops that too.")
        return
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
    svc = service.current()
    if svc is not None and service.installed(svc):
        _follow(service.logs_command(svc, follow=True, lines=50))
        return
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
    deliveries: list[dict[str, str]] = []
    try:
        count = len(watcher.read_state(daemon.STATE_FILE))
        deliveries = watcher.read_deliveries(daemon.STATE_FILE)
    except StateError as exc:
        count = None
        state_note = str(exc)
    else:
        state_note = None

    typer.echo("Chord")
    typer.echo(f"  daemon     {_daemon_line(pid)}")
    typer.echo(f"  service    {_service_line()}")
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
    typer.echo(f"  received   {_received_line(config)}")
    typer.echo(f"  linear     {_linear_line()}")
    typer.echo(f"  config     {_config_line(config)}")
    if count is not None:
        typer.echo(f"  handed     {_plural(count, 'issue')} so far")
    for line in _deliveries(deliveries, running=pid is not None):
        typer.echo(line)
    if state_note:
        typer.echo(f"  note       {state_note}")
    for line in _authorisation(config):
        typer.echo(line)

    if not config.from_file:
        typer.echo("")
        typer.echo(
            f"No {config.path} here, so those are Chord's defaults. "
            "`chord init` writes that file."
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
def serve(
    managed: bool = typer.Option(False, daemon.MANAGED_FLAG, hidden=True),
):
    """Internal. The detached watcher's own entry point."""
    if managed:
        # Run by systemd or launchd: nobody else will write the pid file.
        try:
            daemon.claim()
        except daemon.AlreadyRunning as exc:
            # A clean exit, so the service manager doesn't restart into the
            # same refusal every ten seconds.
            watcher.log(
                f"Already watching (pid {exc.pid}, from `chord start`), so the "
                "service isn't starting a second watcher. `chord stop`, then "
                "`chord service start`."
            )
            raise SystemExit(0) from None
    asyncio.run(_watch(managed=managed))


service_app = typer.Typer(
    help="Run Chord as a background service that starts at login.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(service_app, name="service")


def _service() -> service.Service:
    svc = service.current()
    if svc is None:
        _fail(service.unsupported_message())
    return svc


@service_app.command("install")
def service_install():
    """Run Chord at login, and restart it if it stops. Re-run to update it."""
    svc = _service()
    # The same checks `chord start` makes, for the same reason: a service that
    # fails on its first start reports to a log nobody is reading yet.
    _signed_in()
    _check_routes(_router(_config()))
    _install_service(svc)


def _install_service(svc: service.Service) -> None:
    was_installed = service.installed(svc)
    if not was_installed:
        # A watcher from `chord start` would stand the service down. Hand over.
        try:
            stopped = daemon.stop()
        except (daemon.NotOurs, daemon.StopFailed) as exc:
            _fail(str(exc))
        if stopped is not None:
            typer.echo(f"Stopped the watcher `chord start` began (pid {stopped}).")
    try:
        service.install(svc)
    except service.ServiceError as exc:
        _fail(str(exc))

    verb = "Updated" if was_installed else "Installed"
    typer.echo(f"{verb} the Chord service for {svc.root}.")
    typer.echo(f"  {svc.manager}  {svc.name}")
    typer.echo(f"  file          {svc.path}")
    if svc.kind == service.SYSTEMD:
        typer.echo(f"  logs          journalctl --user -u {svc.name}")
    else:
        typer.echo(f"  logs          {svc.log}")
    typer.echo("It starts when you log in and restarts if it crashes.")
    typer.echo("`chord service status` checks on it; `chord watch` follows it.")
    if service.ephemeral(svc.argv[0]):
        typer.echo(
            f"Note: Chord is running from {svc.argv[0]}, which looks like a "
            "temporary cache. Install it for good (`uv tool install`, `pipx`) "
            "and run `chord service install` again, or the service will break."
        )
    if svc.kind == service.SYSTEMD and service.lingering() is not True:
        typer.echo(
            "It stops when you log out. To keep it running then, and from boot: "
            "`loginctl enable-linger $USER`."
        )
    typer.echo(
        "The service has the PATH this shell has. Install a new coding tool "
        "somewhere else? Re-run `chord service install`."
    )


@service_app.command("uninstall")
def service_uninstall():
    """Stop the service and remove it. `chord start` still works as before."""
    svc = _service()
    try:
        removed = service.uninstall(svc)
    except service.ServiceError as exc:
        _fail(str(exc))
    if not removed:
        typer.echo("The service wasn't installed.")
        return
    typer.echo(f"Removed {svc.path}. Chord no longer starts at login.")


@service_app.command("status")
def service_status():
    """Whether the service is installed and running, and its newest log lines."""
    svc = _service()
    if not service.installed(svc):
        typer.echo("The Chord service isn't installed for this project.")
        typer.echo("`chord service install` runs Chord at login.")
        return
    try:
        state = service.status(svc)
    except service.ServiceError as exc:
        _fail(str(exc))
    running = f"yes (pid {state.pid})" if state.running else f"no ({state.state})"
    typer.echo("Chord service")
    typer.echo(f"  manager    {svc.manager}")
    typer.echo(f"  name       {svc.name}")
    typer.echo(f"  file       {svc.path}")
    typer.echo(f"  project    {svc.root}")
    typer.echo(f"  running    {running}")
    lines = service.recent_log(svc)
    if lines:
        typer.echo("")
        for line in lines:
            typer.echo(f"  {line}")
    if not state.running:
        typer.echo("")
        typer.echo(
            "`chord service start` starts it; `chord service logs` says why it stopped."
        )


@service_app.command("logs")
def service_logs(
    follow: bool = typer.Option(
        True, "--follow/--no-follow", help="Keep following new lines."
    ),
    lines: int = typer.Option(50, "--lines", "-n", min=1),
):
    """The service's log: journald on Linux, ~/Library/Logs/chord on macOS."""
    svc = _service()
    if not service.installed(svc):
        _fail("The service isn't installed. `chord service install` sets it up.")
    _follow(service.logs_command(svc, follow=follow, lines=lines))


@service_app.command("start")
def service_start():
    """Start the installed service now."""
    _service_action(service.start, "Started")


@service_app.command("stop")
def service_stop():
    """Stop it until the next login (or `chord service start`)."""
    _service_action(service.stop, "Stopped")


@service_app.command("restart")
def service_restart():
    """Restart it, picking up a changed chord.toml."""
    _service_action(service.restart, "Restarted")


def _service_action(action: Callable[[service.Service], None], verb: str) -> None:
    svc = _service()
    try:
        action(svc)
    except service.ServiceError as exc:
        _fail(str(exc))
    typer.echo(f"{verb} the Chord service ({svc.name}).")


def _follow(command: list[str]) -> None:
    """Run a log viewer in the foreground until Ctrl+C."""
    try:
        subprocess.run(command, check=False)
    except KeyboardInterrupt:
        pass
    except FileNotFoundError:
        _fail(f"Couldn't run `{command[0]}`, which shows the service's log.")


def _service_line() -> str:
    """Whether this project has a service. Offline: only the file is checked."""
    svc = service.current()
    if svc is None:
        return "not available here (`chord start` runs it)"
    if service.installed(svc):
        return f"installed, {svc.manager} {svc.name} (`chord service status`)"
    return "not installed (`chord service install` runs Chord at login)"


async def _watch(managed: bool = False) -> None:
    config = _config()
    if managed:
        # Signed out is a state a service waits through, not a crash: see
        # `_wait_for_sign_in`.
        found = await _wait_for_sign_in(config.interval)
        if found is None:
            watcher.log("Stopping.")
            daemon.release()
            return
        token = found
    else:
        # `chord start` checked the sign-in before spawning this, so a missing
        # token here is a real failure, said to the log on the way out.
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
            # refuses one picks up a token another Chord (or `chord setup`)
            # stored, instead of needing a restart nobody remembers to do.
            renew=lambda: _try_token(),
            # And renewing it with Linear before it runs out, so nobody has to.
            freshen=_Freshener(),
            # The directory where Chord was started, used as the base for
            # repository resolution.
            start_dir=project_root(),
            # `[notify] received`: a banner when an issue reaches its harness.
            notify_received=config.notify.received,
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


async def _wait_for_sign_in(every: float) -> str | None:
    """The token, once there is a usable one. None if asked to stop first.

    For a watcher a service manager runs. Exiting on "not signed in" is right
    for `chord start`, which checks before spawning and has someone to tell;
    under systemd or launchd it is a crash, so the manager restarts into the
    same answer every few seconds — systemd gives up after five and leaves the
    service failed, launchd never does. Being signed out is also a state a
    service genuinely passes through: installed before `chord setup`, or a
    token that went past its max age while the machine was off. So it waits,
    says why once (and again only if the reason changes), and looks again every
    `every` seconds, which is the poll interval: the same cadence it would ask
    Linear at. `chord setup` or `chord refresh` storing a token is then all it
    takes, with no restart.

    SIGTERM while waiting ends it cleanly (None), the same as SIGTERM while
    watching, so `chord service stop` is a successful exit launchd leaves be.
    """
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, stopping.set)
    said: str | None = None
    try:
        while True:
            code, content = credentials.load()
            if not code and isinstance(content, dict):
                token = content.get("access_token")
                if isinstance(token, str) and token:
                    if said is not None:
                        watcher.log("Signed in to Linear. Starting to watch.")
                    return token
                content = "No access token in the keychain. Run `chord setup`."
            reason = str(content)
            if reason != said:
                watcher.log(
                    f"Not signed in to Linear: {reason} Waiting, and checking "
                    f"again every {every:g}s; nothing to restart once it's done."
                )
                said = reason
            try:
                await asyncio.wait_for(stopping.wait(), timeout=every)
            except TimeoutError:
                continue
            return None
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


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
        t3_options=config.t3,
    )


def _check_routes(router: Router) -> None:
    """Build every route now, so a broken one is reported by `chord start`.

    A route whose command isn't installed would otherwise be discovered the
    first time an issue happens to carry that label — possibly overnight, with
    no indication of which config file is wrong. Curating a harness you can't
    run is a mistake worth catching while the person is still looking.
    """
    problems = router.problems()
    if not problems:
        return
    lines = [f"{route.label} can't be run: {reason}" for route, reason in problems]
    lines.append(
        "Fix chord.toml or install the tool, then `chord start` again. "
        "`chord setup` offers the coding tools it finds."
    )
    _fail("\n".join(lines))


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
    router = _router(config)
    routes = router.routes()
    # Offline like everything else here: only the PATH is asked. A route that
    # can't start says so on its own line, because the label next to it is one
    # somebody can still put on an issue.
    broken = {route.label: reason for route, reason in router.problems()}
    width = max(len(route.label) for route in routes)
    lead = "  labels     "
    lines: list[str] = []
    for index, route in enumerate(routes):
        # A harness known by name says what it runs, so `opencode` isn't
        # mistaken for the interactive TUI that plain `opencode` starts.
        runs = command_line(route.spelling)
        shown = f"{route.spelling}  ({runs})" if runs else route.spelling
        lines.append(
            f"{lead if index == 0 else ' ' * len(lead)}"
            f"{route.label.ljust(width)}  {shown}"
        )
        if route.label in broken:
            lines.append(
                f"{' ' * len(lead)}{' ' * width}  ! can't run: {broken[route.label]}"
            )
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


def _received_line(config: Config) -> str:
    """Whether an issue that reaches its harness gets a banner.

    Shown with the setting's name, because it is the one banner with a switch
    and somebody wondering why they got (or didn't get) one should be able to
    find it from here.
    """
    if config.notify.received:
        return "banner when an issue reaches its harness ([notify] received = true)"
    return "no banner on delivery ([notify] received = false); log and info only"


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
            lines.append(
                "             which is NOT in allowed_actors — Chord won't run your work"
            )
    return lines


def _stored() -> object:
    code, content = credentials.read()
    return None if code else content


def _signed_in() -> str:
    """The token, renewed first if it's due. Stops the command if there's none.

    Renewing here rather than refusing means a Chord that was stopped for a
    weekend starts again with nothing to type.
    """
    code, content = credentials.read()
    if code and guided.interactive():
        # Not signed in yet. Walk them through it rather than sending them off
        # to run another command first.
        typer.echo("Chord isn't set up yet.\n")
        try:
            guided.run()
        except guided.SetupError as exc:
            _fail(str(exc))
        except typer.Abort:
            _fail("\nSetup stopped. Run `chord setup` to finish it.")
        typer.echo("")
        code, content = credentials.read()
    if code:
        _fail(content)
    code, content = asyncio.run(credentials.renew_if_due())
    # A renewal that fails while the token still works is the watcher's to
    # retry, not a reason to refuse to start.
    if code and credentials.load()[0]:
        _fail(content)
    return _token()


def _token() -> str:
    code, content = credentials.load()
    if code:
        _fail(content)
    if not isinstance(content, dict):  # load() only succeeds with a dict.
        _fail("The keychain entry isn't a set of credentials. Run `chord setup`.")
    return content["access_token"]


class _Freshener:
    """Renews the stored token with Linear when it's due, for the watcher.

    Remembers when the token is next due, so a poll that has nothing to renew
    touches neither the keychain nor Linear. A renewal that fails waits
    `RENEW_RETRY` before the next attempt, forced or not: a refresh token Linear
    has refused stays refused, and asking every poll would only fill its logs.
    """

    def __init__(self) -> None:
        stored = _stored()
        self._due = credentials.renew_at(stored) if isinstance(stored, dict) else 0.0
        self._backoff_until = 0.0

    async def __call__(self, force: bool) -> str | None:
        now = time.time()
        if now < self._backoff_until or (not force and now < self._due):
            return None
        code, content = await credentials.renew_if_due(force=force)
        if code or not isinstance(content, dict):
            self._backoff_until = now + credentials.RENEW_RETRY
            raise RuntimeError(content)
        self._due = credentials.renew_at(content)
        token = content.get("access_token")
        return token if isinstance(token, str) and token else None


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
        return "connected, token past max age (renewed on `chord start`)"

    expires_at = content.get("expires_at")
    if not isinstance(expires_at, int | float) or isinstance(expires_at, bool):
        return "connected"
    left = expires_at - time.time()
    if left <= 0:
        return "connected, token expired (renewed on `chord start`)"
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


# How many recent hand-overs `chord info` lists. The newest is the one people
# look for; a few behind it show whether it is a pattern.
RECENT_DELIVERIES = 5


def _deliveries(entries: list[dict[str, str]], *, running: bool) -> list[str]:
    """The newest hand-overs, newest first: did each reach its harness, and then?

    The quiet half of delivery. A hand-over that works posts no banner, so this
    is where somebody checks that the issue got there — including while the
    harness is still working on it, which is the point of keeping "delivered"
    apart from "finished".
    """
    lines = []
    for entry in reversed(entries[-RECENT_DELIVERIES:]):
        label = "delivery  " if not lines else "          "
        lines.append(f"  {label} {_delivery(entry, running=running)}")
    return lines


def _delivery(entry: dict[str, str], *, running: bool) -> str:
    who = entry.get("identifier", "?")
    harness = entry.get("harness", "?")
    status = entry.get("status", "")
    sent = _clock(entry.get("delivered_at"))
    ended = _clock(entry.get("ended_at"))
    if status == watcher.UNDELIVERED:
        return f"{who} NOT delivered to {harness} ({ended})"
    if status == watcher.UNCONFIRMED:
        return f"{who} unconfirmed at {harness} ({ended}); held to avoid a duplicate"
    head = f"{who} delivered to {harness} at {sent}"
    if status == watcher.DELIVERED:
        # Still "delivered" with no watcher running means it was interrupted
        # without the chance to say so — a crash, not `chord stop`.
        return f"{head}, still running" if running else f"{head}, never finished"
    if status == watcher.FINISHED:
        return f"{head}, finished {ended}"
    if status == watcher.FAILED:
        return f"{head}, failed {ended}"
    if status == watcher.STOPPED:
        return f"{head}, stopped {ended}"
    if status == watcher.ACCEPTED:
        return f"{head}; T3 Code has it now"
    return f"{head} ({status})"


def _clock(stamp: str | None) -> str:
    """A UTC stamp from the state file as this machine's local time.

    Just the time for today, because that is the case people check; the date
    too otherwise, so yesterday's 23:59 doesn't read as tonight's.
    """
    if not stamp:
        return "?"
    try:
        when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return stamp
    local = when.astimezone()
    if local.date() == datetime.now().astimezone().date():
        return local.strftime("%H:%M")
    return local.strftime("%Y-%m-%d %H:%M")


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _fail(message: object) -> NoReturn:
    """Report a problem and stop. Chord keeps its failures to one line, and
    they go to stderr so a script can tell them from the answer."""
    typer.echo(message, err=True)
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
