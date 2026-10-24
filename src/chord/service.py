"""Running the watcher as a login service, so nobody has to type `chord start`.

`chord start` detaches a watcher from the terminal, and that is enough until the
machine restarts or the watcher dies. A service is the same watcher, owned by
the operating system's own service manager instead: started at login, restarted
if it crashes, and its log kept where that system keeps logs.

* **Linux**: a systemd *user* unit in `~/.config/systemd/user/`. No root, no
  system-wide unit: the watcher needs this person's keychain, their harnesses
  and their desktop session bus for banners, and a user unit has all three.
  Output goes to the journal (`journalctl --user -u <unit>`).
* **macOS**: a LaunchAgent in `~/Library/LaunchAgents/`, loaded into the
  `gui/<uid>` domain, which is the one that can read the login keychain and
  post a banner. Output goes to `~/Library/Logs/chord/<project>.log`.

Anything else, or a Linux without a systemd user session (a container, WSL
without systemd), gets `Unsupported` and a pointer to `chord start`.

**One service per project.** The watcher's state already hangs off the project
(`daemon.PROJECT`, the readable-name-plus-hash slug), so the unit and the label
are named from the same slug: two checkouts get two services and never fight
over one.

**What it runs** is the same command `chord start` detaches —
`<python> -m chord.cli _serve` — plus `--managed`, which makes the watcher write
its own pid file. That keeps `chord info`'s daemon line, `chord stop`'s
"is this ours?" check and the start lock all working unchanged, because the
process is indistinguishable from one `chord start` made. The interpreter is
`sys.executable` as it was spelled, not resolved: a virtualenv's `python` is a
symlink, and following it would run the system interpreter without Chord.

**Environment.** A service starts with almost nothing: systemd hands a user unit
its manager's environment, launchd hands an agent a bare `PATH`. Harnesses are
found on `PATH` (`~/.local/bin`, Homebrew, nvm...), so the `PATH` of the shell
that ran `chord service install` is written into the unit, along with the
handful of variables in `CAPTURED`. Nothing else is: an API key in the shell
would end up in a file on disk, and harnesses keep their own sign-ins anyway.

The functions that *write* the unit and the plist are pure, so they can be
tested byte for byte; everything that talks to systemctl or launchctl goes
through `_run`, which a test replaces.
"""

from __future__ import annotations

import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

SYSTEMD = "systemd"
LAUNCHD = "launchd"

# Reverse-DNS prefix for the launchd label; the project slug follows it.
LABEL_PREFIX = "io.hxmbl.chord"

# Variables copied from the installing shell into the service. PATH is the one
# that matters — it is how a harness is found. The rest are what command-line
# tools commonly need and a service manager does not reliably provide, plus
# Chord's own knobs so the service behaves like the `chord start` it replaces.
# Nothing secret-shaped: a value here is written into a file on disk.
CAPTURED: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
    "PYTHON_KEYRING_BACKEND",
    "CHORD_LOG_LEVEL",
    "CHORD_MAX_AGE",
    "HARNESS_TIMEOUT",
)

# How long the service manager waits after SIGTERM before killing. The watcher
# stops a harness that is mid-run on SIGTERM, which is not instant; `chord stop`
# waits 10s, and a service has no one to report a timeout to, so more.
STOP_TIMEOUT = 30

# Seconds between restarts after a crash. Long enough that a watcher failing on
# start (a bad config) does not spin; short enough that a blip is invisible.
RESTART_DELAY = 10

# How many log lines `chord service status` shows.
STATUS_LINES = 8

# The one way this module runs another program. A module attribute so a test can
# record the calls instead of reaching a real systemctl or launchctl.
_run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run


class ServiceError(Exception):
    """A step that couldn't be done. The message says what to do instead."""


class Unsupported(ServiceError):
    """No service manager Chord knows how to use here."""


@dataclass(frozen=True)
class Service:
    """Everything that names and places one project's service."""

    kind: str  # SYSTEMD or LAUNCHD
    name: str  # unit name ("chord-app-1a2b.service") or launchd label
    path: Path  # the unit file or plist
    root: Path  # the project directory: WorkingDirectory
    argv: tuple[str, ...]  # what it runs
    env: dict[str, str] = field(default_factory=dict)
    log: Path | None = None  # launchd only; systemd logs to the journal
    uid: int = 0  # launchd only: the gui/<uid> domain

    @property
    def manager(self) -> str:
        return "systemd user unit" if self.kind == SYSTEMD else "launchd agent"

    @property
    def target(self) -> str:
        """launchd's name for the loaded job."""
        return f"gui/{self.uid}/{self.name}"


@dataclass(frozen=True)
class Status:
    installed: bool
    running: bool
    pid: int | None = None
    state: str = ""  # the manager's own word for it: "active", "failed", ...


# --- which manager, and the description of this project's service ---


def kind(
    system: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> str | None:
    """SYSTEMD, LAUNCHD, or None when neither applies. Offline: no subprocess."""
    system = system if system is not None else platform.system()
    if system == "Darwin":
        return LAUNCHD if which("launchctl") else None
    if system == "Linux":
        return SYSTEMD if which("systemctl") else None
    return None


def argv(executable: str | None = None) -> tuple[str, ...]:
    """The service's command: the watcher entry point `chord start` uses.

    The interpreter path is kept as spelled — see the module docstring.
    """
    from chord.daemon import MANAGED_FLAG, PROCESS_MODULE, SERVE_COMMAND

    python = os.path.abspath(executable or sys.executable)
    return (python, "-m", PROCESS_MODULE, SERVE_COMMAND, MANAGED_FLAG)


def capture_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The variables in `CAPTURED` that are set, in that order.

    A value with a newline is left out: neither a unit file nor a plist line is
    the place for one, and none of these legitimately has one.
    """
    return {
        key: environ[key]
        for key in CAPTURED
        if environ.get(key) and "\n" not in environ[key] and "\r" not in environ[key]
    }


def describe(
    kind_: str,
    slug: str,
    root: Path,
    *,
    home: Path,
    command: Sequence[str],
    env: Mapping[str, str],
    uid: int = 0,
    config_home: Path | None = None,
) -> Service:
    """The service for project `slug` rooted at `root`. Pure."""
    if kind_ == SYSTEMD:
        name = f"chord-{slug}.service"
        base = config_home if config_home is not None else home / ".config"
        return Service(
            kind=SYSTEMD,
            name=name,
            path=base / "systemd" / "user" / name,
            root=root,
            argv=tuple(command),
            env=dict(env),
        )
    if kind_ == LAUNCHD:
        label = f"{LABEL_PREFIX}.{slug}"
        return Service(
            kind=LAUNCHD,
            name=label,
            path=home / "Library" / "LaunchAgents" / f"{label}.plist",
            root=root,
            argv=tuple(command),
            env=dict(env),
            log=home / "Library" / "Logs" / "chord" / f"{slug}.log",
            uid=uid,
        )
    raise Unsupported(f"Unknown service manager {kind_!r}.")


def current() -> Service | None:
    """This project's service on this machine, or None where there can't be one.

    Reads nothing but the platform and the environment, so `chord info` can
    call it and stay offline.
    """
    found = kind()
    if found is None:
        return None
    from chord import daemon
    from chord.paths import project_root

    xdg = os.environ.get("XDG_CONFIG_HOME")
    return describe(
        found,
        daemon.PROJECT,
        project_root(),
        home=Path.home(),
        command=argv(),
        env=capture_environment(os.environ),
        uid=os.getuid(),
        config_home=Path(xdg) if xdg else None,
    )


def unsupported_message() -> str:
    return (
        "Chord can run as a service with systemd (Linux) or launchd (macOS), and "
        "neither is available here. `chord start` runs it in the background "
        "instead; run it again after a restart."
    )


def installed(service: Service) -> bool:
    return service.path.is_file()


def ephemeral(executable: str) -> bool:
    """Whether the interpreter looks like it won't be there next week.

    `uvx chord` and friends run from a cache that gets cleaned, and a service
    pointing into it breaks the day it is. Worth a warning, not a refusal.
    """
    parts = Path(executable).parts
    if any(part.startswith("archive-v") for part in parts):
        return True
    temp = Path(tempfile.gettempdir()).resolve()
    try:
        Path(executable).resolve().relative_to(temp)
    except ValueError:
        return False
    return True


# --- the files ---


def _systemd_escape(value: str) -> str:
    """`%` is a specifier in every unit setting; `%%` is a literal one."""
    return value.replace("%", "%%")


def _systemd_word(value: str) -> str:
    """One ExecStart argument, quoted so spaces, quotes and `$` stay literal."""
    escaped = (
        _systemd_escape(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "$$")
    )
    return f'"{escaped}"'


def _systemd_env(key: str, value: str) -> str:
    """One `Environment=` assignment, quoted as a whole."""
    body = f"{key}={_systemd_escape(value)}".replace("\\", "\\\\").replace('"', '\\"')
    return f'Environment="{body}"'


def systemd_unit(service: Service) -> str:
    """The unit file for `service`. Pure, so it is tested as text."""
    root = str(service.root)
    lines = [
        "# Written by `chord service install`; `chord service uninstall` removes it.",
        "# Re-run the install to pick up a new PATH or a moved Chord.",
        "[Unit]",
        f"Description=Chord, watching Linear for {_systemd_escape(root)}",
        # Five crashes in five minutes and systemd stops trying, rather than
        # restarting a watcher with a broken config forever.
        "StartLimitIntervalSec=300",
        "StartLimitBurst=5",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={_systemd_escape(root)}",
        "ExecStart=" + " ".join(_systemd_word(arg) for arg in service.argv),
        *(_systemd_env(key, value) for key, value in service.env.items()),
        # Unbuffered, so the journal has each line as it happens.
        'Environment="PYTHONUNBUFFERED=1"',
        "Restart=on-failure",
        f"RestartSec={RESTART_DELAY}",
        # SIGTERM to the watcher only: it stops its own harness, as on
        # `chord stop`. Anything still there after the timeout is killed.
        "KillMode=mixed",
        f"TimeoutStopSec={STOP_TIMEOUT}",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def launchd_plist(service: Service) -> bytes:
    """The LaunchAgent for `service`. Pure; plistlib does the XML escaping."""
    if service.log is None:
        raise ServiceError("A launchd service needs a log path.")
    env = dict(service.env)
    env["PYTHONUNBUFFERED"] = "1"
    body = {
        "Label": service.name,
        "ProgramArguments": list(service.argv),
        "WorkingDirectory": str(service.root),
        "EnvironmentVariables": env,
        "RunAtLoad": True,
        # Restart after a crash, but not after a clean exit: `chord service
        # stop` and `chord stop` end the watcher cleanly and mean it.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": RESTART_DELAY,
        "ExitTimeOut": STOP_TIMEOUT,
        "StandardOutPath": str(service.log),
        "StandardErrorPath": str(service.log),
    }
    return plistlib.dumps(body, sort_keys=False)


def render(service: Service) -> bytes:
    if service.kind == SYSTEMD:
        return systemd_unit(service).encode()
    return launchd_plist(service)


# --- reading what the manager says ---


def parse_systemd_show(output: str) -> Status:
    """`systemctl --user show -p ...` output, as a Status."""
    values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    active = values.get("ActiveState", "")
    sub = values.get("SubState", "")
    loaded = values.get("LoadState", "") not in ("", "not-found")
    try:
        pid = int(values.get("MainPID", "0")) or None
    except ValueError:
        pid = None
    running = active in ("active", "reloading", "activating") and pid is not None
    state = f"{active} ({sub})" if sub and sub != active else active
    return Status(installed=loaded, running=running, pid=pid, state=state)


def parse_launchctl_print(output: str) -> Status:
    """`launchctl print gui/<uid>/<label>` output, as a Status."""
    state = ""
    pid = None
    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith("state = ") and not state:
            state = line.removeprefix("state = ").strip()
        elif line.startswith("pid = ") and pid is None:
            try:
                pid = int(line.removeprefix("pid = ").strip())
            except ValueError:
                pid = None
    return Status(
        installed=True,
        running=state == "running" and pid is not None,
        pid=pid,
        state=state,
    )


# --- talking to the manager ---


def _call(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return _run(list(args), capture_output=True, text=True, timeout=30)
    except FileNotFoundError as exc:
        raise ServiceError(f"Couldn't run `{args[0]}`.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ServiceError(f"`{' '.join(args)}` didn't answer in 30s.") from exc


def _checked(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    done = _call(args)
    if done.returncode != 0:
        said = (done.stderr or done.stdout or "").strip().splitlines()
        why = f": {said[-1]}" if said else ""
        raise ServiceError(f"`{' '.join(args)}` failed (exit {done.returncode}){why}")
    return done


def _systemctl(*args: str) -> list[str]:
    return ["systemctl", "--user", *args]


def check_manager(service: Service) -> None:
    """Fail clearly where the manager exists but can't be used.

    `systemctl` is installed in most containers and on WSL, with no user
    manager behind it; the error it prints there ("Failed to connect to bus")
    explains nothing to somebody who asked for a service.
    """
    if service.kind != SYSTEMD:
        return
    done = _call(_systemctl("show-environment"))
    if done.returncode != 0:
        raise Unsupported(
            "systemd is here but there's no user session to run a service in "
            "(common in containers and WSL). `chord start` runs Chord in the "
            "background instead."
        )


def install(service: Service) -> None:
    """Write the unit or plist and start it. Re-running replaces it."""
    check_manager(service)
    service.path.parent.mkdir(parents=True, exist_ok=True)
    if service.kind == SYSTEMD:
        service.path.write_bytes(render(service))
        _checked(_systemctl("daemon-reload"))
        _checked(_systemctl("enable", service.name))
        # Restart rather than start, so a re-install picks up the new unit.
        _checked(_systemctl("restart", service.name))
        return

    assert service.log is not None
    service.log.parent.mkdir(parents=True, exist_ok=True)
    # A loaded agent keeps its old definition until it is booted out, so a
    # re-install unloads first. Not loaded is the normal case and not an error.
    _call(["launchctl", "bootout", service.target])
    service.path.write_bytes(render(service))
    done = _call(["launchctl", "bootstrap", f"gui/{service.uid}", str(service.path)])
    if done.returncode != 0:
        # Older macOS, or a session bootstrap can't reach (over SSH, say).
        _checked(["launchctl", "load", "-w", str(service.path)])


def uninstall(service: Service) -> bool:
    """Stop the service and remove its file. False if it wasn't installed."""
    if not installed(service):
        return False
    if service.kind == SYSTEMD:
        _call(_systemctl("disable", "--now", service.name))
        service.path.unlink(missing_ok=True)
        _call(_systemctl("daemon-reload"))
        _call(_systemctl("reset-failed", service.name))
        return True

    done = _call(["launchctl", "bootout", service.target])
    if done.returncode != 0:
        _call(["launchctl", "unload", "-w", str(service.path)])
    service.path.unlink(missing_ok=True)
    return True


def status(service: Service) -> Status:
    if not installed(service):
        return Status(installed=False, running=False)
    if service.kind == SYSTEMD:
        done = _call(
            _systemctl(
                "show",
                service.name,
                "-p",
                "LoadState",
                "-p",
                "ActiveState",
                "-p",
                "SubState",
                "-p",
                "MainPID",
            )
        )
        found = parse_systemd_show(done.stdout if done.returncode == 0 else "")
        return Status(True, found.running, found.pid, found.state or "unknown")

    done = _call(["launchctl", "print", service.target])
    if done.returncode != 0:
        return Status(True, False, None, "not loaded")
    return parse_launchctl_print(done.stdout)


def start(service: Service) -> None:
    _require(service)
    if service.kind == SYSTEMD:
        _checked(_systemctl("start", service.name))
    elif status(service).state == "not loaded":
        _checked(["launchctl", "bootstrap", f"gui/{service.uid}", str(service.path)])
    else:
        _checked(["launchctl", "kickstart", service.target])


def stop(service: Service) -> None:
    _require(service)
    if service.kind == SYSTEMD:
        _checked(_systemctl("stop", service.name))
        return
    if status(service).running:
        # SIGTERM is a clean exit for the watcher, and KeepAlive only restarts
        # an unclean one, so this stays stopped until the next login.
        _checked(["launchctl", "kill", "SIGTERM", service.target])


def restart(service: Service) -> None:
    _require(service)
    if service.kind == SYSTEMD:
        _checked(_systemctl("restart", service.name))
    elif status(service).state == "not loaded":
        _checked(["launchctl", "bootstrap", f"gui/{service.uid}", str(service.path)])
    else:
        _checked(["launchctl", "kickstart", "-k", service.target])


def logs_command(service: Service, *, follow: bool, lines: int) -> list[str]:
    """The command that shows (or follows) the service's log. Pure."""
    if service.kind == SYSTEMD:
        command = ["journalctl", "--user", "-u", service.name, "-n", str(lines)]
        command += ["-f"] if follow else ["--no-pager"]
        return command
    assert service.log is not None
    command = ["tail", "-n", str(lines)]
    return command + (["-F", str(service.log)] if follow else [str(service.log)])


def recent_log(service: Service, lines: int = STATUS_LINES) -> list[str]:
    """The newest log lines, for `chord service status`. Empty if unreadable."""
    if service.kind == LAUNCHD and (service.log is None or not service.log.exists()):
        return []
    try:
        done = _call(logs_command(service, follow=False, lines=lines))
    except ServiceError:
        return []
    if done.returncode != 0:
        return []
    return [line for line in done.stdout.splitlines() if line.strip()][-lines:]


def lingering() -> bool | None:
    """Whether systemd keeps this user's services running after logout.

    None when it can't be told. Only ever read: enabling it is a choice about
    the whole account, so Chord names the command and leaves it to the person.
    """
    user = os.environ.get("USER") or os.environ.get("LOGNAME")
    if not user:
        return None
    try:
        done = _call(["loginctl", "show-user", user, "-p", "Linger", "--value"])
    except ServiceError:
        return None
    if done.returncode != 0:
        return None
    answer = done.stdout.strip()
    return answer == "yes" if answer in ("yes", "no") else None


def _require(service: Service) -> None:
    if not installed(service):
        raise ServiceError(
            "The service isn't installed. `chord service install` sets it up."
        )
