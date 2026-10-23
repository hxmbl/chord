"""Chord's settings, read from a chord.toml in this directory or an ancestor.

Everything here has a default that works, so a fresh checkout runs without a
config file. The file exists for the handful of choices that are genuinely
yours: which label triggers a run, what should do the work, and what the extra
named harnesses are.
"""

import json
import shlex
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias

from chord.paths import config_path

DEFAULT_LABEL = "Chord"
DEFAULT_HARNESS = "opencode"
DEFAULT_INTERVAL = 60
DEFAULT_WEBHOOK_PORT = 23842

# Linear asks politely and rate-limits per requester, so there is no reason to
# ask more often than this. It also stops a fat-fingered `interval = 0` from
# turning into a denial of service against Linear.
MIN_INTERVAL = 5

# How a route label spells the harness it wants: `<label>/<harness name>`. The
# whole suffix is the name, so a curated harness called `opencode/tiny` is
# simply selected by `Chord/opencode/tiny`.
#
# This lives here rather than in routing.py so that config can check `label`
# without importing the module that goes on to interpret it.
SEPARATOR = "/"

# A harness is either the name of one Chord knows, or the command to run. The
# command form is the reason Chord stays small: anything that reads a prompt
# from stdin is a harness, so supporting a new tool needs no change in here.
HarnessSpec: TypeAlias = str | list[str]

_SETTINGS = frozenset(
    {
        "label",
        "harness",
        "harnesses",
        "t3",
        "interval",
        "webhook_port",
        "webhook_secret",
        "allowed_actors",
        "notify",
    }
)
_HARNESS_KEYS = frozenset({"command"})
_T3_KEYS = frozenset({"provider", "model", "worktree", "branch"})
_NOTIFY_KEYS = frozenset({"received"})

# Whether an issue that reached its harness gets a banner. On by default, as it
# was before delivery was confirmed quietly: the banner is how most people
# learn that work started somewhere they didn't start it.
DEFAULT_NOTIFY_RECEIVED = True


class ConfigError(Exception):
    """chord.toml says something Chord can't act on."""


@dataclass(frozen=True)
class T3Options:
    provider: str | None = None
    model: str | None = None
    worktree: str | None = None
    branch: str | None = None


@dataclass(frozen=True)
class NotifyOptions:
    """Which desktop banners Chord posts.

    Only the good news is optional. An issue that failed, was skipped or was
    refused is not being worked on, and saying so is the reason banners exist,
    so those have no switch.
    """

    received: bool = DEFAULT_NOTIFY_RECEIVED


def spell(spec: HarnessSpec) -> str:
    """How to name a harness in a sentence or a log line."""
    return spec if isinstance(spec, str) else " ".join(spec)


# What `chord init` writes: every setting, at its default, annotated. It holds
# no secrets, so it is safe to commit — which is the point, since the curation
# table is per-project and belongs with the code.
STOCK = """\
# Chord's settings. Every entry is optional — the comments say what each one
# does and what the default is. Commit this file; it holds no secrets.

# The label that triggers a run. Default: "Chord".
label = "Chord"

# What does the work by default: "opencode" (runs `opencode run`, the issue on
# stdin), a preset ("claude", "codex", "cursor"), "print", or a command.
# Default: "opencode".
harness = "opencode"

# Seconds between checks of Linear. Default: 60, floor 5.
interval = 60

# Local webhook listener. Default: 23842; use 0 for an ephemeral test port.
webhook_port = 23842

# Shared secret for the webhook, sent as the X-Chord-Secret header. Default:
# "" — no authentication. See "Webhooks" in the README before setting one.
webhook_secret = ""

# Who may trigger a run. Default: everyone in the workspace — read "Who can
# trigger a run" in the README before leaving it that way. `chord info`
# prints your Linear user id.
# allowed_actors = ["11111111-1111-1111-1111-111111111111"]

# Tables come last — everything after a `[table]` header belongs to it, so the
# settings above must be written before the first one of these.

# Desktop banners. An issue that failed, was skipped or was refused always gets
# one. `received` is the banner for an issue that reached its harness ("ENG-201
# received"), posted once delivery is confirmed; false keeps that quiet, and
# the log and `chord info` still record every delivery. Default: true.
[notify]
received = true

# Extra named harnesses: a `Chord/<name>` label routes to the one called
# `<name>`. `chord harness add` writes these for you.
#
# [harnesses."opencode/big-pickle"]
# command = ["opencode", "run", "--model", "big-pickle"]

# Optional T3 Code CLI settings, used when `harness = "t3"` or a route selects
# the built-in `t3` harness. Provider and model must be set together.
# [t3]
# provider = "provider-instance-id"
# model = "model-slug"
# worktree = "main"
# branch = "chord/issue-branch"
"""


class AlreadyCurated(Exception):
    """The name already has a harness behind it."""


def init_file() -> Path:
    """Write the stock chord.toml for this directory. Refuses to overwrite."""
    path, exists = config_path()
    if exists:
        raise ConfigError(f"{path} already exists — this would overwrite it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(STOCK)
    return path


def write_setup(label: str, harness: HarnessSpec, allowed: list[str]) -> Path:
    """Write the stock chord.toml with `chord setup`'s answers filled in.

    The same annotated file `chord init` writes, so everything a person might
    want to change later is there with its comment. Refuses to overwrite.
    """
    path, exists = config_path()
    if exists:
        raise ConfigError(f"{path} already exists — this would overwrite it.")
    text = STOCK.replace('label = "Chord"', f"label = {json.dumps(label)}", 1)
    spelled = (
        json.dumps(harness)
        if isinstance(harness, str)
        else "[" + ", ".join(json.dumps(part) for part in harness) + "]"
    )
    text = text.replace('harness = "opencode"', f"harness = {spelled}", 1)
    if allowed:
        quoted = ", ".join(json.dumps(ident) for ident in allowed)
        text = text.replace(
            '# allowed_actors = ["11111111-1111-1111-1111-111111111111"]',
            f"allowed_actors = [{quoted}]",
            1,
        )
    # Prove it parses and means what was asked before it lands on disk.
    try:
        _build(tomllib.loads(text), path)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"Those answers would make {path} invalid: {exc}.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def curate(name: str, argv: list[str]) -> tuple[Path, bool]:
    """Add `[harnesses."<name>"]` to the project config, writing the stock
    file first if there isn't one. Returns (path, whether the file was new).

    The table is appended, which is safe because TOML tables come last:
    everything after a `[table]` header belongs to that table, so a new one
    at the end of the file cannot disturb what is already there.
    """
    if not usable_harness_name(name):
        raise ConfigError(
            f"{name!r} isn't a usable harness name: a non-empty string with "
            f'no leading, trailing or doubled {SEPARATOR!r}, like "big-pickle" '
            f'or "opencode/tiny".'
        )
    if not argv or not any(part.strip() for part in argv):
        raise ConfigError("The harness command is empty.")

    path, exists = config_path()
    if exists and name in load(path).harnesses:
        raise AlreadyCurated(f"{name!r} is already curated in {path}.")
    text = path.read_text() if exists else STOCK
    quoted = ", ".join(json.dumps(part) for part in argv)
    block = f"\n[harnesses.{json.dumps(name)}]\ncommand = [{quoted}]\n"
    text = text.rstrip("\n") + "\n" + block
    # Prove the result parses before it lands on disk: a name or argument
    # that can't be spelled in TOML is a mistake now, not a config error
    # that stops `chord start` later.
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"Writing {name!r} would make {path} invalid: {exc}.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path, not exists


@dataclass(frozen=True)
class Config:
    label: str
    harness: HarnessSpec
    harnesses: dict[str, HarnessSpec] = field(default_factory=dict)
    t3: T3Options = field(default_factory=T3Options)
    interval: int = DEFAULT_INTERVAL
    webhook_port: int = DEFAULT_WEBHOOK_PORT
    webhook_secret: str = ""
    allowed_actors: tuple[str, ...] = ()
    notify: NotifyOptions = field(default_factory=NotifyOptions)
    path: Path = field(default_factory=lambda: config_path()[0])
    from_file: bool = False

    @property
    def harness_name(self) -> str:
        """How to talk about the default harness in a sentence."""
        return spell(self.harness)

    @property
    def authorises(self) -> bool:
        """Whether Chord checks who asked for the work before running it.

        Empty means no. An unset allowlist is the state every existing install
        is in, and turning it on silently would stop every hand-over the day
        this shipped. So the default is permissive and `chord start` says so
        out loud, rather than the control being useless by default.
        """
        return bool(self.allowed_actors)


def defaults(path: Path | None = None) -> Config:
    return Config(
        label=DEFAULT_LABEL,
        harness=DEFAULT_HARNESS,
        harnesses={},
        t3=T3Options(),
        interval=DEFAULT_INTERVAL,
        webhook_port=DEFAULT_WEBHOOK_PORT,
        webhook_secret="",
        allowed_actors=(),
        notify=NotifyOptions(),
        path=path or config_path()[0],
        from_file=False,
    )


def load(path: Path | None = None) -> Config:
    """Read project config, then user config, or fall back to defaults.

    The file is looked for in the working directory and then upward, so running
    Chord from a subdirectory finds the project's config rather than silently
    reverting to defaults.
    """
    if path is None:
        path, exists = config_path()
    else:
        exists = path.is_file()
    try:
        text = path.read_text()
    except FileNotFoundError:
        return defaults(path)
    except OSError as exc:
        raise ConfigError(f"Couldn't read {path}: {exc.strerror}.") from exc

    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} isn't valid TOML: {exc}.") from exc

    result = _build(raw, path)
    return result if exists else defaults(path)


def _build(raw: dict, path: Path) -> Config:
    base = defaults(path)

    # A misspelled setting would otherwise be ignored, and the symptom is
    # Chord doing something other than what the file says. Better to say so.
    unknown = set(raw) - _SETTINGS
    if unknown:
        names = ", ".join(f"`{name}`" for name in sorted(unknown))
        raise ConfigError(f"{path} has settings Chord doesn't know: {names}.")

    label = raw.get("label", base.label)
    if not isinstance(label, str) or not label.strip():
        raise ConfigError(f"`label` in {path} has to be a non-empty string.")
    label = label.strip()
    if label.endswith(SEPARATOR):
        # `Chord/` as the trigger would make every route `Chord//<name>`.
        raise ConfigError(
            f"`label` in {path} ends with {SEPARATOR!r}, which would double the "
            f"separator in every route label. Write it as {label.rstrip(SEPARATOR)!r}."
        )

    interval = raw.get("interval", base.interval)
    if not isinstance(interval, int) or isinstance(interval, bool):
        raise ConfigError(f"`interval` in {path} has to be a whole number of seconds.")
    if interval < MIN_INTERVAL:
        raise ConfigError(
            f"`interval` in {path} is {interval}s. The floor is {MIN_INTERVAL}s."
        )

    webhook_port = raw.get("webhook_port", base.webhook_port)
    if (
        not isinstance(webhook_port, int)
        or isinstance(webhook_port, bool)
        or not 0 <= webhook_port <= 65535
    ):
        raise ConfigError(
            f"`webhook_port` in {path} has to be an integer from 0 to 65535."
        )

    webhook_secret = raw.get("webhook_secret", base.webhook_secret)
    if not isinstance(webhook_secret, str):
        raise ConfigError(f"`webhook_secret` in {path} has to be a string.")
    if webhook_secret and webhook_secret.strip() != webhook_secret:
        # HTTP strips optional whitespace around a header value, so a sender
        # cannot transmit a secret with a leading or trailing space — the value
        # that arrives will not match the one configured here. Accepting it
        # produces a setup that looks right and answers every request with 401,
        # which is the worst of the three outcomes: not a clear failure at the
        # point of the mistake, but a silent one at the point of use.
        raise ConfigError(
            f"`webhook_secret` in {path} has leading or trailing whitespace, "
            "which cannot be sent in an HTTP header. Remove it."
        )

    return Config(
        label=label,
        harness=_spec(raw.get("harness", base.harness), path, "`harness`"),
        harnesses=_harnesses(raw.get("harnesses", {}), path),
        t3=_t3_options(raw.get("t3", {}), path),
        interval=interval,
        webhook_port=webhook_port,
        webhook_secret=webhook_secret,
        allowed_actors=_allowed_actors(raw.get("allowed_actors", []), path),
        notify=_notify_options(raw.get("notify", {}), path),
        path=path,
        from_file=True,
    )


def _t3_options(value: object, path: Path) -> T3Options:
    if not isinstance(value, dict):
        raise ConfigError(f"`t3` in {path} has to be a table.")
    extra = set(value) - _T3_KEYS
    if extra:
        names = ", ".join(f"`{key}`" for key in sorted(extra))
        raise ConfigError(f"`t3` in {path} has settings Chord doesn't know: {names}.")

    options: dict[str, str | None] = {}
    for key in _T3_KEYS:
        entry = value.get(key)
        if entry is not None and (not isinstance(entry, str) or not entry.strip()):
            raise ConfigError(f"`t3.{key}` in {path} has to be a non-empty string.")
        options[key] = entry.strip() if isinstance(entry, str) else None

    if bool(options["provider"]) != bool(options["model"]):
        raise ConfigError(
            f"`t3.provider` and `t3.model` in {path} must be set together."
        )
    if options["branch"] and not options["worktree"]:
        raise ConfigError(f"`t3.branch` in {path} requires `t3.worktree`.")
    return T3Options(**options)


def _notify_options(value: object, path: Path) -> NotifyOptions:
    """The `[notify]` table. Strict like `[t3]`: a misspelled key would
    otherwise leave a banner on that somebody believes they turned off."""
    if not isinstance(value, dict):
        raise ConfigError(f"`notify` in {path} has to be a table.")
    extra = set(value) - _NOTIFY_KEYS
    if extra:
        names = ", ".join(f"`{key}`" for key in sorted(extra))
        raise ConfigError(
            f"`notify` in {path} has settings Chord doesn't know: {names}."
        )
    received = value.get("received", DEFAULT_NOTIFY_RECEIVED)
    if not isinstance(received, bool):
        raise ConfigError(
            f"`notify.received` in {path} has to be true or false, unquoted."
        )
    return NotifyOptions(received=received)


def _allowed_actors(value: object, path: Path) -> tuple[str, ...]:
    """The Linear user ids allowed to trigger a hand-over.

    Matched on user id and nothing else, deliberately. Linear lets anyone
    rewrite their own `displayName` and `name` through the API, and neither is
    unique, so an allowlist keyed on either could be satisfied by someone who
    merely types a name they were not given. `User.email` is not in
    `UserUpdateInput` and is the identity Linear's own OAuth uses, so it would
    do — but it churns when someone's address changes, and a churn here fails
    closed, which reads as Chord being broken. The id is immutable and unique.

    Which means the config holds UUIDs, so `chord info` prints yours: that is
    the only way to write this setting down without a copy-paste from the
    Linear UI.
    """
    if not isinstance(value, list):
        raise ConfigError(
            f"`allowed_actors` in {path} has to be a list of Linear user ids, "
            'even for one of them: `allowed_actors = ["<id>"]`. '
            "`chord info` prints yours."
        )

    seen: dict[str, str] = {}
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise ConfigError(
                f"`allowed_actors` in {path} has to be a list of Linear user ids, "
                f"and {entry!r} isn't one. `chord info` prints yours."
            )
        try:
            # Canonical form, so the same id written two ways is one entry.
            seen.setdefault(str(uuid.UUID(entry.strip())), entry.strip())
        except (ValueError, AttributeError, TypeError):
            raise ConfigError(
                f"`allowed_actors` in {path} holds {entry.strip()!r}, which is not "
                "a Linear user id. Use the id, not an email or a display name: "
                "`chord info` prints yours, and a display name would be something "
                "any workspace member could set to match yours."
            ) from None

    # Sorted so the setting reads the same way twice and `chord info` can
    # compare it against what is stored in the keychain.
    return tuple(sorted(seen))


def _harnesses(value: object, path: Path) -> dict[str, HarnessSpec]:
    """The `[harnesses.<name>]` tables: extra harnesses a route label can name.

    Each one is a command and nothing else, which is the whole point: the label
    chooses the entry, and the entry is an ordinary command like any other.
    """
    if not isinstance(value, dict):
        raise ConfigError(f"`harnesses` in {path} has to be a table of names.")

    result: dict[str, HarnessSpec] = {}
    for name, table in value.items():
        # Named, not just described: with a dozen curated harnesses, "a name is
        # wrong somewhere in this file" is a worse message than the one that
        # says which one.
        where = f"`harnesses.{name}`"
        if not _usable_name(name):
            raise ConfigError(
                f"{where} in {path} has to be a usable harness name: a non-empty "
                f"string with no leading, trailing or doubled {SEPARATOR!r}, "
                f'like "opencode" or "opencode/tiny".'
            )
        if not isinstance(table, dict):
            raise ConfigError(f"{where} in {path} has to be a table with a `command`.")
        extra = set(table) - _HARNESS_KEYS
        if extra:
            names = ", ".join(f"`{key}`" for key in sorted(extra))
            raise ConfigError(
                f"{where} in {path} has settings Chord doesn't know: {names}."
            )
        if "command" not in table:
            raise ConfigError(f"{where} in {path} is missing `command`.")
        result[name] = _spec(table["command"], path, f"{where} `command`")
    return result


def usable_harness_name(name: object) -> bool:
    """Whether `name` can be spelled as the suffix of a route label.

    `Chord//x` and `Chord/x/` are typos rather than routes. Rejecting them in
    config means curation and the label grammar agree about which names exist.

    Public, and used by `routing` as well, because both sides of "is this a
    route?" have to be the same question. When they were not — the filter matched
    any `Chord/...` prefix while the parser rejected the malformed ones — an
    issue labelled `Chord//claude` matched the filter, produced no candidates,
    and fell through to the default harness. The issue asked for one agent and
    got another, silently, which is the one outcome the routing exists to
    prevent.
    """
    if not isinstance(name, str) or not name or name != name.strip():
        return False
    return not (
        name.startswith(SEPARATOR) or name.endswith(SEPARATOR) or SEPARATOR * 2 in name
    )


# Kept as a private alias so the existing call site reads naturally.
_usable_name = usable_harness_name


def _spec(value: object, path: Path, where: str) -> HarnessSpec:
    if isinstance(value, str) and value.strip():
        try:
            argv = shlex.split(value)
        except ValueError as exc:
            raise ConfigError(f"{where} in {path} has invalid quoting: {exc}.") from exc
        return argv if len(argv) > 1 else value.strip()
    if isinstance(value, list) and value and all(isinstance(p, str) for p in value):
        return list(value)
    raise ConfigError(
        f'{where} in {path} has to be a name like "{DEFAULT_HARNESS}", or a '
        'command to run with the issue on stdin, like ["claude", "-p"].'
    )
