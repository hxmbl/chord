"""Chord's settings, read from a chord.toml in this directory or an ancestor.

Everything here has a default that works, so a fresh checkout runs without a
config file. The file exists for the handful of choices that are genuinely
yours: which label triggers a run, what should do the work, and what the extra
named harnesses are.
"""

import shlex
import tomllib
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

_SETTINGS = frozenset({"label", "harness", "harnesses", "interval", "webhook_port"})
_HARNESS_KEYS = frozenset({"command"})


class ConfigError(Exception):
    """chord.toml says something Chord can't act on."""


def spell(spec: HarnessSpec) -> str:
    """How to name a harness in a sentence or a log line."""
    return spec if isinstance(spec, str) else " ".join(spec)


@dataclass(frozen=True)
class Config:
    label: str
    harness: HarnessSpec
    harnesses: dict[str, HarnessSpec] = field(default_factory=dict)
    interval: int = DEFAULT_INTERVAL
    webhook_port: int = DEFAULT_WEBHOOK_PORT
    path: Path = field(default_factory=lambda: config_path()[0])
    from_file: bool = False

    @property
    def harness_name(self) -> str:
        """How to talk about the default harness in a sentence."""
        return spell(self.harness)


def defaults(path: Path | None = None) -> Config:
    return Config(
        label=DEFAULT_LABEL,
        harness=DEFAULT_HARNESS,
        harnesses={},
        interval=DEFAULT_INTERVAL,
        webhook_port=DEFAULT_WEBHOOK_PORT,
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

    return Config(
        label=label,
        harness=_spec(raw.get("harness", base.harness), path, "`harness`"),
        harnesses=_harnesses(raw.get("harnesses", {}), path),
        interval=interval,
        webhook_port=webhook_port,
        path=path,
        from_file=True,
    )


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


def _usable_name(name: object) -> bool:
    """Whether `name` can be spelled as the suffix of a route label.

    `Chord//x` and `Chord/x/` are typos rather than routes. Rejecting them here
    means curation and the label grammar agree, so a name that config accepts
    is a name a label can actually select.
    """
    if not isinstance(name, str) or not name or name != name.strip():
        return False
    return not (
        name.startswith(SEPARATOR) or name.endswith(SEPARATOR) or SEPARATOR * 2 in name
    )


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
