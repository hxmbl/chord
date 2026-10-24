"""Where Chord's project and user files live.

Project configuration lives in `.config/chord/chord.toml`. Legacy
`chord.toml` files remain readable so upgrading does not silently reset a
project. User-wide configuration is read from `~/.config/chord/chord.toml`
or `~/.chord/chord.toml`.

The home directory is never a project. `~/.config/chord/chord.toml` has the
same shape as a project config sitting in `~`, and treating it as one made
`$HOME` the project root of every directory under it: issues were looked for
in `~` rather than in the checkout Chord was started in. So the walk up for a
project stops below home, and the file in `~/.config/chord` is read as the
user-wide config it is.
"""

from pathlib import Path

CONFIG_NAME = "chord.toml"
PROJECT_CONFIG = Path(".config") / "chord" / CONFIG_NAME
ENV_NAME = ".env"


def _below_home(here: Path) -> list[Path]:
    """`here` and its parents, stopping before the home directory.

    Everything when `here` isn't under home at all (a checkout in `/srv`).
    """
    home = Path.home().resolve()
    found: list[Path] = []
    for directory in (here, *here.parents):
        if directory == home:
            break
        found.append(directory)
    return found


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
    for directory in _below_home(here):
        candidate = directory / PROJECT_CONFIG
        if candidate.is_file():
            return candidate, True

    for user in (Path.home() / PROJECT_CONFIG, Path.home() / ".chord" / CONFIG_NAME):
        if user.is_file():
            return user, True

    legacy = find_upwards(CONFIG_NAME, start)
    if legacy != here / CONFIG_NAME or legacy.is_file():
        return legacy, legacy.is_file()
    return here / PROJECT_CONFIG, False


def project_root(start: Path | None = None) -> Path:
    """The directory a project is rooted at.

    The nearest directory holding project config, or the working directory when
    there isn't one. Never home or above it: a config there is user-wide.
    Daemon state hangs off this so that two checkouts each keep their own
    watcher rather than fighting over one pid file.
    """
    here = (start or Path.cwd()).resolve()
    for directory in _below_home(here):
        if (directory / PROJECT_CONFIG).is_file() or (
            directory / CONFIG_NAME
        ).is_file():
            return directory
    return here
