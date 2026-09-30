"""Where Chord's project and user files live.

Project configuration lives in `.config/chord/chord.toml`. Legacy
`chord.toml` files remain readable so upgrading does not silently reset a
project. User-wide configuration is supported from `~/.chord/chord.toml`.
"""

from pathlib import Path

CONFIG_NAME = "chord.toml"
PROJECT_CONFIG = Path(".config") / "chord" / CONFIG_NAME
ENV_NAME = ".env"


def find_upwards(name: str, start: Path | None = None) -> Path:
    """The nearest `name` at or above `start`, else the path it would have had.

    Returning the would-be path even when nothing is there lets callers report
    the file they expected, which is more use than a bare `None`.
    """
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return here / name


def config_path(start: Path | None = None) -> tuple[Path, bool]:
    """Return the preferred project config, then user config, then legacy path."""
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / PROJECT_CONFIG
        if candidate.is_file():
            return candidate, True

    user = Path.home() / ".chord" / CONFIG_NAME
    if user.is_file():
        return user, True

    legacy = find_upwards(CONFIG_NAME, start)
    if legacy != here / CONFIG_NAME or legacy.is_file():
        return legacy, legacy.is_file()
    return here / PROJECT_CONFIG, False


def project_root(start: Path | None = None) -> Path:
    """The directory a project is rooted at.

    The nearest directory holding project config, or the working directory when
    there isn't one. Daemon state hangs off this so that two checkouts each
    keep their own watcher rather than fighting over one pid file.
    """
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        if (directory / PROJECT_CONFIG).is_file() or (
            directory / CONFIG_NAME
        ).is_file():
            return directory
    return here
