"""Chord's settings, read from a chord.toml in this directory or an ancestor.

Everything here has a default that works, so a fresh checkout runs without a
config file. The file exists for the handful of choices that are genuinely
yours: which label triggers a run, what should do the work, and what the extra
named harnesses are.
"""

import json
import re
import shlex
import tomllib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from chord.paths import (
    config_path,
    project_config_path,
    project_target,
    repo_config_path,
    user_config_path,
)

DEFAULT_LABEL = "Chord"
DEFAULT_HARNESS = "opencode"
DEFAULT_INTERVAL = 60
DEFAULT_WEBHOOK_PORT = 23842

# Linear asks politely and rate-limits per requester, so there is no reason to
# ask more often than this. It also stops a fat-fingered `interval = 0` from
# turning into a denial of service against Linear.
MIN_INTERVAL = 5

# How a route label spells the harness it wants: `<label> -> <harness name>`,
# with each `/` in the name written as another arrow. Linear allows neither `/`
# nor `:` in a label name, so the arrow stands for the `/` and reads as the
# parent -> child relationship it is: the harness curated as
# `opencode/big-pickle` is selected by `Chord -> opencode -> big-pickle`. A `:`
# is an ordinary character in both, because model names have them
# (`qwen3.4:4b`), and so are `.` and uppercase.
#
# `SEPARATOR` is how Chord prints the arrow. `ARROW` is what it reads, with any
# whitespace either side, so `Chord->opencode` routes too.
#
# This lives here rather than in routing.py so that config can check `label`
# and harness names without importing the module that goes on to interpret
# them.
SEPARATOR = " -> "
ARROW = "->"

# What separates the parts of a harness name: `opencode/big-pickle`. chord.toml
# keys always use it, and each one becomes an arrow in the label.
NAME_SEPARATOR = "/"

# The separator route labels used before the arrow, still read so a
# `Chord/<name>` label already in somebody's workspace keeps routing: the whole
# suffix is the name, so `Chord/opencode/tiny` is `opencode/tiny`. Never
# printed: everything Chord tells a person spells routes with `SEPARATOR`.
LEGACY_SEPARATOR = "/"


# The harnesses Chord knows by name, which a route string can name. Kept here
# rather than read from `harness.PRESETS` because harness.py imports this
# module; a test keeps the two in step.
KNOWN_HARNESSES = ("opencode", "claude", "codex", "cursor", "t3", "print")

# What a route string looks like, for error messages.
ROUTE_SHAPE = (
    'harness[:model][@branch], like "opencode", "opencode:big-pickle" or '
    '"t3:openai/gpt-5@main"'
)


@dataclass(frozen=True)
class RouteSpec:
    """A harness Chord knows, with an optional model and branch.

    Written as one string, `harness[:model][@branch]`: `opencode:big-pickle`,
    `t3:openai/gpt-5@main`. The model may contain `/` and `:` (model ids do);
    the branch is everything after the last `@`.
    """

    harness: str
    model: str | None = None
    branch: str | None = None

    def __str__(self) -> str:
        text = self.harness
        if self.model:
            text += f":{self.model}"
        if self.branch:
            text += f"@{self.branch}"
        return text

    def t3_options(self) -> "T3Options":
        """The T3 Code options a `t3:` route stands for.

        The model is `provider/model`, split at the first `/`; the branch is
        the worktree base T3 starts from.
        """
        provider = model = None
        if self.model:
            provider, _, model = self.model.partition("/")
        return T3Options(provider=provider, model=model, worktree=self.branch)


# A harness is either the name of one Chord knows, the command to run, or a
# route string. The command form is the reason Chord stays small: anything
# that reads a prompt from stdin is a harness, so supporting a new tool needs
# no change in here.
type HarnessSpec = str | list[str] | RouteSpec

# The newest config format this Chord reads. A file may say `version = N`;
# missing means 1. A newer file is refused rather than half-read, because the
# keys a newer Chord added would otherwise be "unknown settings" at best and
# misread at worst.
CONFIG_VERSION = 1

# Settings that belong to the machine, not to a project: how often to poll,
# the local webhook, banners, where checkouts live. They live in the user file.
# Found in a project's or a repository's file they are warned about and
# ignored, which also keeps a webhook secret out of a committed file.
#
# `linear_user_id` is who "me" is. `chord setup` writes it, and only the user's
# own file may say it: a cloned repository naming somebody else as "me" would
# hand them this machine.
MACHINE_KEYS = frozenset(
    {
        "interval",
        "webhook_port",
        "webhook_secret",
        "notify",
        "search_dirs",
        "linear_user_id",
    }
)

# Who "me" is. Stricter than the machine settings: only a user-level file may
# say it, never a start-folder file, even the one standing in for a missing
# user file. That one is often a cloned repository, and honouring its
# `linear_user_id` would let the repository decide who "me" authorises.
IDENTITY_KEYS = frozenset({"linear_user_id"})

# Settings that belong to the watcher as a whole. One poll asks Linear for
# every issue under one trigger label, so a target repository can't change it
# per issue; it is ignored (with a warning) in a repository's file.
WATCHER_KEYS = frozenset({"label"})

# Where a value came from, lowest precedence first.
DEFAULT = "default"
USER = "user"
PROJECT = "project"
REPO = "repo"

_SETTINGS = frozenset(
    {
        "version",
        "label",
        "harness",
        "model",
        "routes",
        "linear_user_id",
        "harnesses",
        "t3",
        "interval",
        "webhook_port",
        "webhook_secret",
        "allowed_actors",
        "notify",
        "search_dirs",
    }
)
_HARNESS_KEYS = frozenset({"command"})
_T3_KEYS = frozenset({"provider", "model", "worktree", "branch"})
_NOTIFY_KEYS = frozenset({"received"})

# Whether an issue that reached its harness gets a banner. On by default, as it
# was before delivery was confirmed quietly: the banner is how most people
# learn that work started somewhere they didn't start it.
DEFAULT_NOTIFY_RECEIVED = True

# Where people usually keep their checkouts. An issue whose Linear project
# isn't a folder where Chord started is looked for directly inside these.
# Ones that don't exist are skipped, so the list can name the usual spots
# without anyone having all of them.
DEFAULT_SEARCH_DIRS: tuple[str, ...] = (
    "~/Projects",
    "~/projects",
    "~/Developer",
    "~/Code",
    "~/code",
    "~/src",
    "~/dev",
    "~/repos",
    "~/git",
)


class ConfigError(Exception):
    """chord.toml says something Chord can't act on."""


@dataclass(frozen=True)
class Layer:
    """One chord.toml as it took part in the effective config.

    `machine` says whether its machine settings count: true for the user file,
    and for a start-folder file when there is no user file (so a setup that has
    only ever had one file keeps working exactly as it did). `linear_user_id`
    is the exception: only a USER layer may set it, whatever `machine` says.
    """

    source: str
    path: Path
    raw: dict
    machine: bool = False


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
    if isinstance(spec, RouteSpec):
        return str(spec)
    return spec if isinstance(spec, str) else " ".join(spec)


def parse_route(text: object, where: str = "route") -> RouteSpec:
    """Read a route string, `harness[:model][@branch]`. Raises `ConfigError`.

    The harness is everything before the first `:` (or `@`), the branch
    everything after the last `@`, and the model what is in between, `/` and
    `:` included, because model ids have both (`openai/gpt-5`, `qwen3:4b`).
    """

    def bad(why: str) -> ConfigError:
        return ConfigError(f"{where} is {text!r}, which {why}. Expected {ROUTE_SHAPE}.")

    if not isinstance(text, str):
        return_type = type(text).__name__
        raise ConfigError(
            f"{where} has to be a string, not a {return_type}. Expected {ROUTE_SHAPE}."
        )
    if not text.strip():
        raise bad("is empty")
    if text != text.strip() or any(ch.isspace() for ch in text):
        raise bad("contains spaces")
    body, at, branch = text.rpartition("@") if "@" in text else (text, "", "")
    if at and not branch:
        raise bad("has nothing after '@'")
    harness, colon, model = body.partition(":")
    if not harness:
        raise bad("names no harness before ':'" if colon else "names no harness")
    if colon and not model:
        raise bad("has nothing after ':'")
    if "@" in model:
        raise bad("has more than one '@'")
    if harness not in KNOWN_HARNESSES:
        known = ", ".join(KNOWN_HARNESSES)
        raise bad(f"names a harness Chord doesn't know ({known})")
    if harness == "print" and model:
        raise bad("gives `print` a model, which it doesn't take")
    if harness == "t3" and model:
        provider, slash, rest = model.partition("/")
        if not (provider and slash and rest):
            raise bad("gives t3 a model without its provider (`t3:provider/model`)")
    if branch and harness != "t3":
        raise bad("gives a branch, which only t3 understands so far")
    return RouteSpec(harness, model or None, branch or None)


_ROUTE_NAME_FORBIDDEN = ("/", ":")


def route_name_problem(name: object) -> str | None:
    """Why `name` can't be a key under `[routes]`, or None if it can.

    The name becomes the `Chord -> <name>` label, and Linear refuses `/` and
    `:` in a label, so neither may appear.
    """
    if not isinstance(name, str) or not name.strip():
        return "it is empty"
    if name != name.strip():
        return "it starts or ends with a space"
    for forbidden in _ROUTE_NAME_FORBIDDEN:
        if forbidden in name:
            return (
                f"it contains '{forbidden}', which a Linear label can't; the name "
                f"becomes the `Chord -> <name>` label"
            )
    if ARROW in name:
        return f"it contains '{ARROW}', which a route label reads as a separator"
    return None


def as_route(spec: HarnessSpec, t3: "T3Options | None" = None) -> RouteSpec | None:
    """The route string an old-style harness is, if it is one.

    For the deprecation notice and `chord config migrate`: a preset, plain
    `t3`, `print`, and `opencode run [--model M]` all have a route spelling.
    Any other command stays a command: that is the escape hatch.
    """
    if isinstance(spec, RouteSpec):
        return spec
    argv = [spec] if isinstance(spec, str) else list(spec)
    if len(argv) == 1 and argv[0] in KNOWN_HARNESSES:
        if argv[0] == "t3":
            t3 = t3 or T3Options()
            if t3.branch:
                return None  # a T3 branch name has no slot in a route string
            model = f"{t3.provider}/{t3.model}" if t3.provider and t3.model else None
            return RouteSpec("t3", model, t3.worktree)
        return RouteSpec(argv[0])
    if argv[:2] == ["opencode", "run"]:
        if len(argv) == 2:
            return RouteSpec("opencode")
        if len(argv) == 4 and argv[2] in ("--model", "-m") and argv[3]:
            return RouteSpec("opencode", argv[3])
    return None


# What `chord init` writes: every setting, at its default, annotated. It holds
# no secrets, so it is safe to commit — which is the point, since the curation
# table is per-project and belongs with the code.
STOCK = """\
# ============================================================================
#  chord.toml: Chord's settings
# ============================================================================
#
# The config format this file is written in. Optional; missing means 1.
version = 1
#
# How Chord works, in one paragraph:
#   Chord watches Linear. When an issue gets the trigger label (`Chord` unless
#   you change it below), Chord hands the issue to a "harness" (a coding agent
#   such as opencode, Claude Code or Codex) and runs it in the issue's
#   repository. The harness gets the issue's title, description and comments
#   on stdin, and does the work.
#
# About this file:
#   - Every setting is optional. Delete a line and Chord uses the default
#     written next to it.
#   - Lines starting with `#` are comments. Remove the `#` to turn a line on.
#   - It holds no secrets (your Linear sign-in lives in the system keychain),
#     so it's safe to commit alongside your code.
#   - `chord config` opens this file and checks it before saving, so a typo
#     can't break Chord. `chord info` shows what Chord made of it.
#   - Chord reads this file when it starts. After changing it, restart:
#     `chord stop` then `chord start`, or `chord service restart`.
#
# Where Chord looks for it (merged per key, nearest wins):
#   1. ~/.config/chord/chord.toml or ~/.chord/chord.toml (your own defaults)
#   2. .config/chord/chord.toml in the directory you run Chord from, or any
#      directory above it
#   3. per issue, .config/chord/chord.toml in the repository the issue runs in
# Machine settings (interval, webhook_*, [notify], search_dirs) count only in
# your own file, or in the start folder's when you have no file of your own.


# ----------------------------------------------------------------------------
#  The trigger label
# ----------------------------------------------------------------------------
#
# Put this label on a Linear issue and Chord picks it up. Chord never creates
# labels, so make this one in Linear yourself (Settings > Labels).
#
# Route labels build on it: `Chord -> codex` sends an issue to the harness
# called `codex` (see "Extra harnesses" below).
#
# Default: "Chord"
label = "Chord"


# ----------------------------------------------------------------------------
#  The default harness
# ----------------------------------------------------------------------------
#
# What works on an issue labelled just `Chord`. One of:
#
#   "opencode"  runs `opencode run`, the issue on stdin
#   "claude"    Claude Code: `claude -p --permission-mode acceptEdits`
#   "codex"     Codex: `codex exec --sandbox workspace-write -`
#   "cursor"    Cursor's CLI: `cursor-agent -p --force --trust`
#   "t3"        T3 Code (configure it in the [t3] section at the bottom)
#   "print"     prints the issue instead of running anything; handy to test
#
# Or any command of your own, as a list of words. It gets the issue on stdin:
#   harness = ["opencode", "run", "--model", "big-pickle"]
#
# Default: "opencode"
harness = "opencode"


# ----------------------------------------------------------------------------
#  How often to check Linear
# ----------------------------------------------------------------------------
#
# Seconds between checks. Chord also hears about changes straight away when
# it can (see the webhook below), so this is mostly a safety net.
#
# Default: 60. Lowest allowed: 5.
interval = 60


# ----------------------------------------------------------------------------
#  Webhook (optional, for instant pickup)
# ----------------------------------------------------------------------------
#
# Chord listens on this local port. Anything that POSTs to it (a Linear
# webhook relayed to this machine, say) wakes Chord to check Linear right
# away. It's only a nudge: Chord still reads Linear itself, and polling works
# without it.
#
# Default: 23842. Use 0 to pick any free port (mainly for tests).
webhook_port = 23842

# A password the sender has to include, in the X-Chord-Secret header.
# Empty means anyone who can reach the port can send to it. Read "Webhooks"
# in the README before setting one. No spaces at either end.
#
# Default: "" (no secret)
webhook_secret = ""


# ----------------------------------------------------------------------------
#  Who can trigger a run
# ----------------------------------------------------------------------------
#
# A label decides whether this machine runs a command, so think about who can
# add one. Unset, ANYONE in your Linear workspace can trigger a run here, and
# Chord warns you about that every time it starts.
#
# List who may trigger runs: "me" (you, as `chord setup` recorded you), emails,
# or Linear user ids. Remove the `#` to turn it on:
#
# allowed_actors = ["me"]


# ----------------------------------------------------------------------------
#  Where to look for repositories
# ----------------------------------------------------------------------------
#
# When an issue's Linear project isn't a folder where you started Chord,
# Chord also looks for a Git repository of that name directly inside these
# folders. Ones that don't exist are skipped. `[]` turns this off.
#
# Default: ["~/Projects", "~/projects", "~/Developer", "~/Code", "~/code",
#           "~/src", "~/dev", "~/repos", "~/git"]
# search_dirs = ["~/Projects"]


# ============================================================================
#  Sections
# ============================================================================
#
# TOML rule: everything under a `[section]` line belongs to that section until
# the next one. So plain settings like the ones above must come BEFORE the
# first `[section]`, never after it.


# ----------------------------------------------------------------------------
#  [notify]: desktop notifications
# ----------------------------------------------------------------------------
#
# Chord always posts a notification when an issue fails, is skipped or is
# refused, because then nothing is happening on it.
#
# received: also notify when an issue reaches its harness ("STRM-12
# received"). false keeps those quiet; the log and `chord info` still record
# every one.
#
# Default: true
[notify]
received = true


# ----------------------------------------------------------------------------
#  [harnesses]: extra harnesses, picked by label
# ----------------------------------------------------------------------------
#
# Name a harness here, then route an issue to it with a label. Linear won't
# allow `/` or `:` in a label, so each `/` in a name becomes ` -> ` in the
# label:
#
#   name in this file          label in Linear
#   codex                      Chord -> codex
#   opencode/big-pickle        Chord -> opencode -> big-pickle
#
# Names are matched exactly, capitals included. Create each label in Linear
# yourself. `chord harness add NAME -- COMMAND...` writes the entry for you
# and prints the label to make. Remove the `#`s to use these examples:
#
# [harnesses."codex"]
# command = ["codex"]
#
# [harnesses."opencode/big-pickle"]
# command = ["opencode", "run", "--model", "big-pickle"]


# ----------------------------------------------------------------------------
#  [t3]: T3 Code settings
# ----------------------------------------------------------------------------
#
# Only used when the harness is "t3". Set provider and model together, or
# neither.
#
# [t3]
# provider = "provider-instance-id"
# model = "model-slug"
# worktree = "main"
# branch = "chord/issue-branch"


# ----------------------------------------------------------------------------
#  Where the work happens (no setting, just a reminder)
# ----------------------------------------------------------------------------
#
# For each issue Chord picks a repository, in this order:
#   1. A folder named after the issue's Linear project, either the folder you
#      started Chord in or one directly inside it. Capitals don't matter.
#   2. A line like this in the issue's description:
#        [Chord_Working_Path: "~/Projects/my-repo"]
#   3. A repository named after the Linear project directly inside one of
#      the `search_dirs` folders (~/Projects and friends, see above). If two
#      match, the issue is skipped until a line like the one in 2 picks one.
#   4. The folder you started Chord in, if it's a Git repository.
# If none of those is a Git repository, the issue is skipped and the log
# says why.
"""


class AlreadyCurated(Exception):
    """The name already has a harness behind it."""


def check_text(text: str, path: Path) -> None:
    """Raise `ConfigError` if `text` isn't a config Chord would load.

    The same reading `load` does, so `chord config` refuses exactly what
    `chord start` would have refused, at the moment the person is still
    looking at the file.
    """
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"That isn't valid TOML: {exc}.") from None
    _build(raw, path)


def init_file() -> Path:
    """Write the stock chord.toml for this directory. Refuses to overwrite."""
    path = project_target()
    exists = path.is_file()
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
    path = project_target()
    exists = path.is_file()
    if exists:
        raise ConfigError(f"{path} already exists — this would overwrite it.")
    text = STOCK.replace('label = "Chord"', f"label = {json.dumps(label)}", 1)
    if isinstance(harness, RouteSpec):
        spelled = json.dumps(str(harness))
    elif isinstance(harness, str):
        spelled = json.dumps(harness)
    else:
        spelled = "[" + ", ".join(json.dumps(part) for part in harness) + "]"
    text = text.replace('harness = "opencode"', f"harness = {spelled}", 1)
    if allowed:
        quoted = ", ".join(json.dumps(ident) for ident in allowed)
        text = text.replace(
            '# allowed_actors = ["me"]',
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


_USER_ID_LINE = re.compile(r"^linear_user_id\s*=.*$", re.MULTILINE)


def remember_user_id(ident: str) -> Path:
    """Store who "me" is, `linear_user_id`, in the user's own file.

    The user file rather than a project's: on a shared machine each OS user
    gets their own "me", and a committed file never says who it is. Creates
    the file if there isn't one; otherwise replaces the line or adds it at the
    top, where a top-level key is always safe in TOML.
    """
    canonical = str(uuid.UUID(ident))
    path = user_config_path() or Path.home() / ".config" / "chord" / "chord.toml"
    line = f"linear_user_id = {json.dumps(canonical)}"
    text = path.read_text() if path.is_file() else ""
    head, sep, rest = text.partition("\n[")
    if _USER_ID_LINE.search(head):
        text = _USER_ID_LINE.sub(line, head, count=1) + sep + rest
    else:
        comment = '# Who "me" is in allowed_actors. Written by `chord setup`.\n'
        text = comment + line + "\n" + ("\n" + text if text else "")
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"Couldn't add linear_user_id to {path}: {exc}.") from exc
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
            f"{name!r} isn't a usable harness name: {name_problem(name)}."
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
    # `[routes]`: name -> route string. Merged with `harnesses` for routing.
    routes: dict[str, RouteSpec] = field(default_factory=dict)
    model: str | None = None
    linear_user_id: str | None = None
    t3: T3Options = field(default_factory=T3Options)
    interval: int = DEFAULT_INTERVAL
    webhook_port: int = DEFAULT_WEBHOOK_PORT
    webhook_secret: str = ""
    allowed_actors: tuple[str, ...] = ()
    notify: NotifyOptions = field(default_factory=NotifyOptions)
    search_dirs: tuple[Path, ...] = field(
        default_factory=lambda: tuple(Path(p) for p in DEFAULT_SEARCH_DIRS)
    )
    path: Path = field(default_factory=lambda: config_path()[0])
    from_file: bool = False
    version: int = CONFIG_VERSION
    # The files that made this config, lowest precedence first.
    layers: tuple[Layer, ...] = ()
    # Setting -> (source, file) for every value a file set. A curated harness
    # is keyed `harnesses.<name>`. Anything absent is a built-in default.
    sources: dict[str, tuple[str, Path]] = field(default_factory=dict)
    # Things worth saying that don't stop Chord: ignored settings, mostly.
    warnings: tuple[str, ...] = ()

    @property
    def all_routes(self) -> dict[str, HarnessSpec]:
        """Every named route: old `[harnesses]` and `[routes]`, routes winning."""
        return {**self.harnesses, **self.routes}

    @property
    def unresolved_emails(self) -> tuple[str, ...]:
        """Emails in `allowed_actors` that still need a Linear user id."""
        return tuple(actor for actor in self.allowed_actors if "@" in actor)

    def source_of(self, key: str) -> tuple[str, Path | None]:
        """Where `key` came from: a layer's source and file, or the default."""
        found = self.sources.get(key)
        return found if found else (DEFAULT, None)

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
        search_dirs=tuple(Path(p) for p in DEFAULT_SEARCH_DIRS),
        path=path or config_path()[0],
        from_file=False,
    )


def load(path: Path | None = None, start: Path | None = None) -> Config:
    """The effective config: defaults, then the user file, then the project's.

    With `path`, that one file and nothing else, as `chord config --path`
    reports it and as tests and `chord harness add` read it.

    Without, the layers are merged per key, nearest winning, with curated
    harnesses merged by name:

      1. built-in defaults
      2. the user file, `~/.config/chord/chord.toml` (or `~/.chord/chord.toml`)
      3. the start folder's `.config/chord/chord.toml`, looked for upward from
         the working directory (never at home, which is the user file's)

    A target repository's own file is a fourth layer added per issue, by
    `load_repo`, once Chord knows which repository the issue runs in.
    """
    if path is not None:
        return _load_one(path)

    nearest, _ = config_path(start)
    user = user_config_path()
    project = project_config_path(start)
    layers: list[Layer] = []
    if user is not None:
        layers.append(Layer(USER, user, _read(user), machine=True))
    if project is not None and not _same(project, user):
        # With no user file, the start folder's file is the only one there is,
        # and it has always been where machine settings went. Honouring them
        # keeps every single-file setup working; once a user file exists, the
        # machine settings live there and a project file's are ignored.
        layers.append(Layer(PROJECT, project, _read(project), machine=user is None))
    if not layers:
        # Nothing in either place. A legacy `chord.toml` higher up (above
        # home, say) is still read the way it always was: as the only file.
        legacy, exists = config_path(start)
        if exists:
            layers.append(Layer(USER, legacy, _read(legacy), machine=True))
            nearest = legacy
    return _compose(tuple(layers), nearest)


def load_repo(base: Config, repo: Path) -> Config:
    """`base` with the target repository's own chord.toml on top, if it has one.

    Read on every call, so editing a repository's file takes effect on the
    next issue with no restart. Raises `ConfigError` for a broken file, which
    the watcher turns into that one issue failing.
    """
    candidate = repo_config_path(repo)
    if not candidate.is_file():
        return base
    if any(_same(candidate, layer.path) for layer in base.layers):
        # The repository is the start folder (or the user file's home):
        # already a layer, and reading it twice would only repeat warnings.
        return base
    layers = (*base.layers, Layer(REPO, candidate, _read(candidate)))
    return _compose(layers, base.path)


def _same(one: Path | None, other: Path | None) -> bool:
    if one is None or other is None:
        return False
    try:
        return one.resolve() == other.resolve()
    except OSError:
        return one == other


def _read(path: Path) -> dict:
    try:
        text = path.read_text()
    except OSError as exc:
        raise ConfigError(f"Couldn't read {path}: {exc.strerror}.") from exc
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} isn't valid TOML: {exc}.") from exc


def _load_one(path: Path) -> Config:
    exists = path.is_file()
    try:
        raw = _read(path)
    except ConfigError:
        if not exists:
            return defaults(path)
        raise
    if not exists:  # pragma: no cover - a race with the file disappearing
        return defaults(path)
    return _compose((Layer(USER, path, raw, machine=True),), path)


def _kept(layer: Layer) -> tuple[dict, list[str]]:
    """The part of a layer that counts, and a warning for each part that doesn't."""
    ignored: set[str] = set()
    if not layer.machine:
        ignored |= MACHINE_KEYS
    if layer.source != USER:
        ignored |= IDENTITY_KEYS
    if layer.source == REPO:
        ignored |= WATCHER_KEYS
    dropped = sorted(set(layer.raw) & ignored)
    warnings = []
    for key in dropped:
        if _is_default(key, layer.raw[key]):
            # Ignoring a value that is the default anyway changes nothing, so
            # the stock file's own lines don't warn on every start.
            continue
        if key in MACHINE_KEYS:
            why = "is a machine setting; it belongs in ~/.config/chord/chord.toml"
        else:
            why = "applies to the whole watcher, so only the start folder's file can set it"
        warnings.append(f"`{key}` in {layer.path} is ignored: it {why}.")
    return {k: v for k, v in layer.raw.items() if k not in ignored}, warnings


def _is_default(key: str, value: object) -> bool:
    defaults_by_key: dict[str, object] = {
        "interval": DEFAULT_INTERVAL,
        "webhook_port": DEFAULT_WEBHOOK_PORT,
        "webhook_secret": "",
        "search_dirs": list(DEFAULT_SEARCH_DIRS),
        "label": DEFAULT_LABEL,
    }
    if key == "notify":
        return value in ({}, {"received": DEFAULT_NOTIFY_RECEIVED})
    return key in defaults_by_key and value == defaults_by_key[key]


# Tables merged entry by entry across layers, so a project can add a route
# without restating the user's. Every other table is replaced whole by the
# nearer file: `[t3]`'s provider and model only make sense together.
_MERGED_TABLES = frozenset({"harnesses", "routes"})


def _merge(layers: tuple[Layer, ...]) -> dict:
    merged: dict = {}
    for layer in layers:
        kept, _ = _kept(layer)
        for key, value in kept.items():
            if (
                key in _MERGED_TABLES
                and isinstance(value, dict)
                and isinstance(merged.get(key), dict)
            ):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
    return merged


def _sources(layers: tuple[Layer, ...]) -> dict[str, tuple[str, Path]]:
    found: dict[str, tuple[str, Path]] = {}
    for layer in layers:
        kept, _ = _kept(layer)
        for key, value in kept.items():
            if key in _MERGED_TABLES and isinstance(value, dict):
                for name in value:
                    found[f"{key}.{name}"] = (layer.source, layer.path)
            else:
                found[key] = (layer.source, layer.path)
    return found


def _compose(layers: tuple[Layer, ...], path: Path) -> Config:
    if not layers:
        return defaults(path)
    warnings: list[str] = []
    merged = _merge(layers)
    # Who "me" is comes from the user's own file, whichever file says "me".
    me = _user_id(merged.get("linear_user_id"), layers[0].path)
    for layer in layers:
        kept, dropped = _kept(layer)
        warnings.extend(dropped)
        # Each file on its own first, so a mistake is reported against the
        # file that holds it rather than against the merge.
        _build(kept, layer.path, me)
        warnings.extend(_deprecations(kept, layer.path))
    result = _build(merged, layers[-1].path, me)
    return replace(
        result,
        path=path,
        layers=layers,
        sources=_sources(layers),
        warnings=tuple(warnings),
    )


def _build(raw: dict, path: Path, me: str | None = None) -> Config:
    base = defaults(path)

    # A misspelled setting would otherwise be ignored, and the symptom is
    # Chord doing something other than what the file says. Better to say so.
    # The version first: a newer file's settings are "unknown" only because
    # this Chord is older, and saying that is the useful message.
    version = _version(raw.get("version", CONFIG_VERSION), path)

    unknown = set(raw) - _SETTINGS
    if unknown:
        names = ", ".join(f"`{name}`" for name in sorted(unknown))
        raise ConfigError(f"{path} has settings Chord doesn't know: {names}.")

    label = raw.get("label", base.label)
    if not isinstance(label, str) or not label.strip():
        raise ConfigError(f"`label` in {path} has to be a non-empty string.")
    label = label.strip()
    for separator in (ARROW, LEGACY_SEPARATOR):
        if label.endswith(separator):
            # `Chord ->` as the trigger would make every route
            # `Chord -> -> <name>`, and `Chord/` would do the same to the
            # legacy `Chord//<name>`.
            bare = label.removesuffix(separator).rstrip()
            raise ConfigError(
                f"`label` in {path} ends with '{separator}', which would double "
                f"the separator in every route label. Write it as '{bare}'."
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

    user_id = _user_id(raw.get("linear_user_id"), path) or me
    model = raw.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ConfigError(f"`model` in {path} has to be a non-empty string.")
    harness = _spec(raw.get("harness", base.harness), path, "`harness`")
    if model is not None:
        if isinstance(harness, RouteSpec):
            name = harness.harness
        elif isinstance(harness, str) and harness in KNOWN_HARNESSES:
            name = harness
        else:
            raise ConfigError(
                f"`model` in {path} needs `harness` to be one Chord knows "
                f"({', '.join(KNOWN_HARNESSES)}); a command sets its own model."
            )
        branch = harness.branch if isinstance(harness, RouteSpec) else None
        harness = parse_route(
            str(RouteSpec(name, model.strip(), branch)),
            f"`harness` with `model` in {path}",
        )

    return Config(
        label=label,
        harness=harness,
        harnesses=_harnesses(raw.get("harnesses", {}), path),
        routes=_routes(raw.get("routes", {}), path),
        model=model.strip() if isinstance(model, str) else None,
        linear_user_id=user_id,
        t3=_t3_options(raw.get("t3", {}), path),
        interval=interval,
        webhook_port=webhook_port,
        webhook_secret=webhook_secret,
        allowed_actors=_allowed_actors(raw.get("allowed_actors", []), path, user_id),
        notify=_notify_options(raw.get("notify", {}), path),
        search_dirs=_search_dirs(raw.get("search_dirs"), base.search_dirs, path),
        path=path,
        from_file=True,
        version=version,
    )


def _user_id(value: object, path: Path) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value).strip()))
    except ValueError:
        raise ConfigError(
            f"`linear_user_id` in {path} has to be a Linear user id (a UUID). "
            "`chord setup` writes it."
        ) from None


def _routes(value: object, path: Path) -> dict[str, RouteSpec]:
    """`[routes]`: `name = "harness[:model][@branch]"`, one line per route."""
    if not isinstance(value, dict):
        raise ConfigError(
            f"`routes` in {path} has to be a table, like `[routes]` then "
            f'`big-pickle = "opencode:big-pickle"`.'
        )
    result: dict[str, RouteSpec] = {}
    for name, text in value.items():
        problem = route_name_problem(name)
        if problem:
            raise ConfigError(
                f"`routes.{name}` in {path} isn't a usable route name: {problem}."
            )
        result[name] = parse_route(text, f"`routes.{name}` in {path}")
    return result


def _deprecations(raw: dict, path: Path) -> list[str]:
    """Notices for the old shapes, which still load."""
    found: list[str] = []
    if "t3" in raw:
        found.append(
            f"`[t3]` in {path} is deprecated: write it as a route string, like "
            '`harness = "t3:provider/model@worktree"`. `chord config migrate` '
            "does it for you."
        )
    harnesses = raw.get("harnesses")
    if isinstance(harnesses, dict):
        movable = [
            name
            for name, table in harnesses.items()
            if isinstance(table, dict)
            and route_name_problem(name) is None
            and isinstance(table.get("command"), str | list)
            and as_route(table["command"], None) is not None
            and table.get("command") not in (["t3"], "t3")
        ]
        if movable:
            names = ", ".join(f"`{name}`" for name in movable)
            found.append(
                f"`[harnesses]` entries {names} in {path} are deprecated: write "
                'them under `[routes]`, like `big-pickle = "opencode:big-pickle"`. '
                "`chord config migrate` does it for you."
            )
    return found


def _keychain_user_id() -> str | None:
    """The Linear user id recorded with the stored token, if any."""
    from chord import credentials  # here, not at the top: avoids an import cycle

    code, content = credentials.read()
    return None if code else credentials.viewer_id(content)


def _version(value: object, path: Path) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ConfigError(
            f"`version` in {path} has to be a whole number from 1, like `version = 1`."
        )
    if value > CONFIG_VERSION:
        raise ConfigError(
            f"{path} is written for config version {value}, and this Chord reads "
            f"up to version {CONFIG_VERSION}. Update Chord to a release that "
            f"supports config version {value}, or set `version = {CONFIG_VERSION}`."
        )
    return value


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


def _search_dirs(
    value: object, default: tuple[Path, ...], path: Path
) -> tuple[Path, ...]:
    """The folders to look in for a repository named after a Linear project.

    Absolute or starting with `~`. A relative path would mean something
    different depending on where Chord was started, which is the kind of
    setting that works on one machine and quietly misses on the next.
    """
    if value is None:
        return default
    if not isinstance(value, list):
        raise ConfigError(
            f"`search_dirs` in {path} has to be a list of folders, like "
            f'`search_dirs = ["~/Projects"]`. `[]` turns it off.'
        )
    folders: list[Path] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise ConfigError(
                f"`search_dirs` in {path} has to be a list of folders, and "
                f"{entry!r} isn't one."
            )
        text = entry.strip()
        if not (text.startswith("~") or Path(text).is_absolute()):
            raise ConfigError(
                f"`search_dirs` in {path} needs absolute paths or ones starting "
                f"with `~`; {text!r} is relative."
            )
        folder = Path(text)
        if folder not in folders:
            folders.append(folder)
    return tuple(folders)


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


ME = "me"

# Good enough to tell an email from a typo; Linear is the judge of the rest.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _allowed_actors(
    value: object, path: Path, me: str | None = None
) -> tuple[str, ...]:
    """The Linear user ids allowed to trigger a hand-over.

    Matched on user id and nothing else, deliberately. Linear lets anyone
    rewrite their own `displayName` and `name` through the API, and neither is
    unique, so an allowlist keyed on either could be satisfied by someone who
    merely types a name they were not given. The id is immutable and unique.

    The file may say it three ways, and all three become ids:
      - a UUID, as before;
      - "me", the id `chord setup` stored as `linear_user_id` in the user
        file (else the one recorded with the stored token);
      - an email, kept lowercased here and resolved to an id with Linear when
        the watcher starts (`resolve_emails`). Until then it matches nobody,
        so an email Linear doesn't know fails closed.
    """
    if not isinstance(value, list):
        raise ConfigError(
            f"`allowed_actors` in {path} has to be a list, even for one person: "
            '`allowed_actors = ["me"]`. Use "me", emails or Linear user ids.'
        )

    seen: dict[str, str] = {}
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise ConfigError(
                f'`allowed_actors` in {path} has to be a list of "me", emails or '
                f"Linear user ids, and {entry!r} isn't one."
            )
        text = entry.strip()
        if text.lower() == ME:
            ident = me or _keychain_user_id()
            if not ident:
                raise ConfigError(
                    f'`allowed_actors` in {path} says "me", but Chord doesn\'t know '
                    "your Linear user id yet. Run `chord setup`, which stores it."
                )
            seen.setdefault(str(uuid.UUID(ident)), text)
            continue
        if "@" in text:
            if not _EMAIL.match(text):
                raise ConfigError(
                    f"`allowed_actors` in {path} holds {text!r}, which isn't an "
                    'email. Use "me", an email or a Linear user id.'
                )
            seen.setdefault(text.lower(), text)
            continue
        try:
            # Canonical form, so the same id written two ways is one entry.
            seen.setdefault(str(uuid.UUID(text)), text)
        except (ValueError, AttributeError, TypeError):
            raise ConfigError(
                f'`allowed_actors` in {path} holds {text!r}, which is not "me", an '
                "email or a Linear user id. Not a display name: any workspace "
                "member could set theirs to match yours."
            ) from None

    # Sorted so the setting reads the same way twice and `chord info` can
    # compare it against what is stored in the keychain.
    return tuple(sorted(seen))


def resolve_emails(
    config: Config, lookup: "Callable[[list[str]], dict[str, str]]"
) -> Config:
    """`config` with each email in `allowed_actors` replaced by its user id.

    `lookup` asks Linear: emails in, `{email: id}` out for the ones it knows.
    An email Linear doesn't know is an error, naming it, rather than a person
    silently not allowed.
    """
    emails = list(config.unresolved_emails)
    if not emails:
        return config
    found = {email.lower(): ident for email, ident in lookup(emails).items()}
    unknown = [email for email in emails if email not in found]
    if unknown:
        names = ", ".join(unknown)
        raise ConfigError(
            f"`allowed_actors` names {names}, and Linear has no user with that "
            "email in this workspace."
        )
    ids = {str(uuid.UUID(found[a])) if "@" in a else a for a in config.allowed_actors}
    return replace(config, allowed_actors=tuple(sorted(ids)))


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
                f"{where} in {path} isn't a usable harness name: {name_problem(name)}."
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


def name_problem(name: object) -> str:
    """Why `name` isn't a usable harness name, in words, for the error.

    The arrow gets its own sentence because it is the mistake somebody makes
    after reading a label: `opencode->tiny` looks like the label's spelling, and
    the fix is to write the `/` the label stands for.
    """
    if isinstance(name, str) and ARROW in name:
        return (
            f"harness names can't contain '{ARROW}', which a route label reads "
            f"as '{NAME_SEPARATOR}'. Write the parts with '{NAME_SEPARATOR}', "
            f'like "opencode/big-pickle"'
        )
    return (
        f"a non-empty string with no '{ARROW}', no leading or trailing space, "
        f"and no empty or space-padded part between '{NAME_SEPARATOR}'s, like "
        f'"big-pickle" or "opencode/tiny"'
    )


def usable_harness_name(name: object) -> bool:
    """Whether `name` can be spelled as the suffix of a route label.

    Every `/` in a name is an arrow in its label, and a label's arrows are read
    with any whitespace around them and the parts trimmed. So a name is usable
    when it survives that round trip unchanged: no `->` of its own (it would
    read as another `/`), and no part that is empty (`/x`, `x/`, `x//y` are
    `Chord -> -> x` and friends, typos rather than routes) or padded with spaces
    (`x / y` would come back as `x/y`). Anything else goes, `:`, `.` and
    uppercase included, because model names contain them.

    Public, and used by `routing` as well, because both sides of "is this a
    route?" have to be the same question. When they were not — the filter matched
    any `Chord/...` prefix while the parser rejected the malformed ones — an
    issue labelled `Chord//claude` matched the filter, produced no candidates,
    and fell through to the default harness. The issue asked for one agent and
    got another, silently, which is the one outcome the routing exists to
    prevent.
    """
    if not isinstance(name, str) or not name or ARROW in name:
        return False
    return all(part and part == part.strip() for part in name.split(NAME_SEPARATOR))


# Kept as a private alias so the existing call site reads naturally.
_usable_name = usable_harness_name


def _looks_like_route(value: str) -> bool:
    """Whether a `harness` string is a route string rather than a command.

    One word, naming a harness Chord knows, followed by `:` or `@`. A command
    with a colon in it (`./run.sh host:1`) has a space, or doesn't start with a
    known name, so it stays a command.
    """
    text = value.strip()
    if not text or any(ch.isspace() for ch in text):
        return False
    head = re.split(r"[:@]", text, maxsplit=1)[0]
    return head in KNOWN_HARNESSES and head != text


def _spec(value: object, path: Path, where: str) -> HarnessSpec:
    if isinstance(value, str) and _looks_like_route(value):
        return parse_route(value.strip(), f"{where} in {path}")
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
