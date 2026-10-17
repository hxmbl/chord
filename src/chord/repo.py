"""Resolving which repository a Linear issue belongs to.

A Linear issue names where the work happens, and this turns that into a
directory the harness is launched in. In order:

1. The project title, matched against the two places it can plausibly be: the
   directory Chord is rooted at, and a directory of that name inside it.
2. An explicit `Repository: /path/to/repo` line in the issue description.
3. Chord's own project root, as a fallback — loudly.

The fallback is the part that matters. Plenty of issues carry no project and no
explicit path, and refusing to hand those over would stop Chord working for
everyone who has not reorganised their Linear workspace around directory names.
So an issue that says nothing about where it lives is worked on in the project
root, which is where Chord was started, and the log says so on every hand-over.

What the fallback never does is guess. If a project title or an explicit path
*does* point somewhere that isn't a usable repository, that is an error rather
than a reason to try somewhere else: somebody wrote down a location, and
silently working in a different one is the failure this module exists to
prevent.

The search never leaves the project root. An earlier version walked every
ancestor up to `/`, which meant a Linear project called `home` or `Users`
resolved to `$HOME` or `/Users` and put the agent somewhere nobody asked for.
"""

import subprocess
from pathlib import Path
from typing import Any, NamedTuple

from chord import logging
from chord.text import one_line

# The line in an issue description that explicitly names a repository.
# Relative paths resolve from the directory Chord is rooted at.
EXPLICIT_PATH_PREFIX = "Repository:"

# How the repository was chosen, named in the log line for every hand-over.
FROM_PROJECT = "project title"
FROM_DESCRIPTION = "issue description"
FROM_ROOT = "project root (fallback)"


class ResolutionError(Exception):
    """The repository for this issue was named but can't be used."""


class Resolution(NamedTuple):
    """Where the work happens, and how that was decided."""

    path: Path
    source: str


def resolve(issue: dict[str, Any], start_dir: Path) -> Resolution:
    """Find the repository for `issue`, falling back to `start_dir`.

    Raises `ResolutionError` when the issue names a repository explicitly and
    that name is wrong, or when the fallback directory itself is unusable.
    """
    project_name = _project_name(issue)
    if project_name:
        found = _find_by_project_name(project_name, start_dir)
        if found is not None and _is_git_repository(found):
            return Resolution(found, FROM_PROJECT)
        if found is not None:
            # The issue named a directory and it isn't a repository. Falling
            # through to the root would work on the wrong code quietly, so
            # this is the one case where a named-but-wrong location is fatal.
            raise ResolutionError(
                f"The project '{project_name}' matched {found}, which isn't a Git "
                f"repository. Either make it one, or rename the directory to "
                f"'{project_name}', or put an explicit "
                f"`{EXPLICIT_PATH_PREFIX} /absolute/path` line in the description."
            )
        logging.info(
            f"No directory named '{project_name}' in {start_dir} or directly inside it."
        )

    explicit = _explicit_path(issue.get("description"))
    if explicit is not None:
        path = explicit if explicit.is_absolute() else (start_dir / explicit)
        path = path.resolve()
        if _is_git_repository(path):
            return Resolution(path, FROM_DESCRIPTION)
        raise ResolutionError(
            f"The `{EXPLICIT_PATH_PREFIX}` line in the description names {path}, "
            f"which isn't a Git repository. Either make it one, or point the line "
            f"somewhere else."
        )

    # Nothing named a location. Work where Chord was started.
    if _is_git_repository(start_dir):
        logging.warning(
            f"No repository named by the issue: falling back to {start_dir}. Add a "
            f"Linear project matching a directory name, or a "
            f"`{EXPLICIT_PATH_PREFIX} /absolute/path` line to the description."
        )
        return Resolution(start_dir, FROM_ROOT)

    raise ResolutionError(
        f"Nothing named a repository for this issue, and {start_dir} isn't a Git "
        f"repository either, so there's nowhere to run a harness. Start Chord "
        f"inside the repository, or put an explicit "
        f"`{EXPLICIT_PATH_PREFIX} /absolute/path` line in the description."
    )


def _project_name(issue: dict[str, Any]) -> str | None:
    """The project name from the issue, if it has one."""
    project = issue.get("project")
    if isinstance(project, dict):
        name = project.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _find_by_project_name(project_name: str, start_dir: Path) -> Path | None:
    """The directory called `project_name` at `start_dir`, or inside it.

    Two candidates, not a search. The project root itself counts, because that
    is where Chord already is — running Chord inside `chord` and asking for
    project `chord` should not need a nested copy. Otherwise a subdirectory of
    the root, for a workspace that keeps several checkouts side by side.

    Deliberately not recursive and deliberately not up the tree. Both were
    ways of matching a directory nobody meant, and a resolution that can reach
    `/` is not one anybody can reason about.
    """
    if start_dir.name == project_name and start_dir.is_dir():
        return start_dir
    candidate = start_dir / project_name
    return candidate if candidate.is_dir() else None


def _explicit_path(description: object) -> Path | None:
    """The path from a `Repository:` line, or None when there isn't one.

    Relative, so that a description stays portable between machines. The first
    such line wins; a second one is a contradiction rather than a preference,
    and Linear's own description field has no way to say which was meant.

    Takes `object` because Linear's description is a nullable string field and
    `render` is already hardened against the shapes it turns out to hold. A
    hand-over that stopped here would be the worst place to reintroduce that.
    """
    if not isinstance(description, str):
        return None
    for line in description.splitlines():
        line = line.strip()
        if line.startswith(EXPLICIT_PATH_PREFIX):
            text = line[len(EXPLICIT_PATH_PREFIX) :].strip()
            if text:
                # `~` first, because it is what a person writes and pathlib
                # treats it as an ordinary directory name. A description is
                # typed by hand, so a path that needs quoting is a path that
                # won't be written.
                return Path(text).expanduser()
    return None


def _is_git_repository(path: Path) -> bool:
    """Whether `path` is itself the root of a Git repository.

    Asked of Git rather than inferred from a `.git` entry, because that entry
    is a file in a linked worktree and a directory in a normal clone, and
    Chord wants either.

    Compared against the reported top level rather than merely asking whether
    Git recognises the directory, because Git walks up: an ordinary folder
    inside a repository is "inside a work tree" too, and treating one as a
    repository would launch the harness in a subdirectory of whatever
    checkout happens to contain it. Resolving to the top level is the question
    that has the same answer for a clone, a worktree, and a bare mistake.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            capture_output=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        # No git, or no permission to run it. Neither is this path's fault, so
        # say which rather than blaming the directory.
        logging.warning(f"Couldn't check {path} for a Git repository: {one_line(exc)}")
        return False
    if result.returncode != 0:
        return False
    top = result.stdout.strip().decode(errors="replace")
    if not top:
        # A bare repository, which has no top level to hand back.
        return False
    return Path(top).resolve() == path.resolve()
