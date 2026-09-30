"""Chord's settings, read from a chord.toml in this directory or an ancestor.

Everything here has a default that works, so a fresh checkout runs without a
config file. The file exists for the handful of choices that are genuinely
yours: which label triggers a run, and what should do the work.
"""

import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from chord.paths import config_path

DEFAULT_LABEL = "Chord"
DEFAULT_HARNESS = "print"
DEFAULT_INTERVAL = 60
DEFAULT_WEBHOOK_PORT = 23842

# Linear asks politely and rate-limits per requester, so there is no reason to
# ask more often than this. It also stops a fat-fingered `interval = 0` from
# turning into a denial of service against Linear.
MIN_INTERVAL = 5

# A harness is either the name of one Chord knows, or the command to run. The
# command form is the reason Chord stays small: anything that reads a prompt
# from stdin is a harness, so supporting a new tool needs no change in here.
HarnessSpec: TypeAlias = str | list[str]


class ConfigError(Exception):
    """chord.toml says something Chord can't act on."""


@dataclass(frozen=True)
class Config:
    label: str
    harness: HarnessSpec
    interval: int
    webhook_port: int
    path: Path
    from_file: bool

    @property
    def harness_name(self) -> str:
        """How to talk about the harness in a sentence."""
        if isinstance(self.harness, str):
            return self.harness
        return " ".join(self.harness)


def defaults(path: Path | None = None) -> Config:
    return Config(
        label=DEFAULT_LABEL,
        harness=DEFAULT_HARNESS,
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
    unknown = set(raw) - {"label", "harness", "interval", "webhook_port"}
    if unknown:
        names = ", ".join(f"`{name}`" for name in sorted(unknown))
        raise ConfigError(f"{path} has settings Chord doesn't know: {names}.")

    label = raw.get("label", base.label)
    if not isinstance(label, str) or not label.strip():
        raise ConfigError(f"`label` in {path} has to be a non-empty string.")

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
        label=label.strip(),
        harness=_harness(raw.get("harness", base.harness), path),
        interval=interval,
        webhook_port=webhook_port,
        path=path,
        from_file=True,
    )


def _harness(value: object, path: Path) -> HarnessSpec:
    if isinstance(value, str) and value.strip():
        try:
            argv = shlex.split(value)
        except ValueError as exc:
            raise ConfigError(
                f"`harness` in {path} has invalid quoting: {exc}."
            ) from exc
        return argv if len(argv) > 1 else value.strip()
    if isinstance(value, list) and value and all(isinstance(p, str) for p in value):
        return list(value)
    raise ConfigError(
        f'`harness` in {path} has to be a name like "{DEFAULT_HARNESS}", or a '
        'command to run with the issue on stdin, like ["claude", "-p"].'
    )
