"""Resolving which repository a Linear issue belongs to.

A Linear issue names where the work happens, and this turns that into a
directory the harness is launched in. In order:

1. The project title, matched against the two places it can plausibly be: the
   directory Chord is rooted at, and a directory of that name inside it.
2. An explicit `[Chord_Working_Path: "/path/to/repo"]` line in the issue
   description. The path is quoted, and the name matches in any case, because
   both are typed by hand.
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

import re
import subprocess
from pathlib import Path
from typing import Any, NamedTuple

from chord import logging
from chord.text import one_line

# The line in an issue description that explicitly names a repository.
# Matched in any case, since it is typed by hand; the path it carries must be
# in single or double quotes, so that spaces inside it stay inside it.
# Relative paths resolve from the directory Chord is rooted at.
EXPLICIT_PATH_PREFIX = "[Chord_Working_Path:"

# How the repository was chosen, named in the log line for every hand-over.
FROM_PROJECT = "project title"
FROM_DESCRIPTION = "issue description"
FROM_ROOT = "project root (fallback)"


class ResolutionError(Exception):
    """The repository for this issue was named but can't be used.

    `hint` is what to do about it, logged on its own line after the reason, so
    neither has to be cut short to fit one log line.
    """

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


class Resolution(NamedTuple):
    """Where the work happens, and how that was decided."""

    path: Path
    source: str


# A Markdown backslash escape: a backslash before ASCII punctuation, which
# stands for the punctuation alone.
_MARKDOWN_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")

# What a working-path line looks like, for messages that suggest writing one.
_LINE_EXAMPLE = f'{EXPLICIT_PATH_PREFIX} "~/path/to/repo"]'


def resolve(issue: dict[str, Any], start_dir: Path) -> Resolution:
    """Find the repository for `issue`, falling back to `start_dir`.

    Raises `ResolutionError` when the issue names a repository explicitly and
    that name is wrong, or when the fallback directory itself is unusable.
    """
    project_name = _project_name(issue)
    unmatched: str | None = None
    if project_name:
        found = _find_by_project_name(project_name, start_dir)
        if found is not None and is_git_repository(found):
            return Resolution(found, FROM_PROJECT)
        if found is not None:
            # The issue named a directory and it isn't a repository. Falling
            # through to the root would work on the wrong code quietly, so
            # this is the one case where a named-but-wrong location is fatal.
            raise ResolutionError(
                f"its Linear project '{project_name}' matched {found}, which "
                f"isn't a Git repository.",
                hint=f"Make it one, or add {_LINE_EXAMPLE} to the description.",
            )
        unmatched = project_name

    explicit = _explicit_path(issue.get("description"))
    if explicit is not None:
        path = explicit if explicit.is_absolute() else (start_dir / explicit)
        path = path.resolve()
        if is_git_repository(path):
            return Resolution(path, FROM_DESCRIPTION)
        raise ResolutionError(
            f"its {EXPLICIT_PATH_PREFIX}] line names {path}, which isn't a Git "
            f"repository.",
            hint="Make it one, or point the line somewhere else.",
        )

    # Nothing named a location. Work where Chord was started.
    if is_git_repository(start_dir):
        if unmatched:
            logging.warning(
                f"No folder named '{unmatched}' in {start_dir}, so working in "
                f"{start_dir} itself."
            )
        else:
            logging.warning(
                f"No repository named by the issue, so working in {start_dir}."
            )
        return Resolution(start_dir, FROM_ROOT)

    if unmatched:
        reason = (
            f"no folder named '{unmatched}' (its Linear project) in {start_dir}, "
            f"which isn't a Git repository itself."
        )
    else:
        reason = f"it names no repository, and {start_dir} isn't a Git repository."
    raise ResolutionError(
        reason,
        hint=(
            f"Start Chord in the repository or the folder holding it, or add "
            f"{_LINE_EXAMPLE} to the description."
        ),
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

    The name is matched in any case, preferring an exact match: a Linear
    project is titled `Chord` and its checkout is called `chord`, and on Linux
    those are different names that nobody means differently.
    """
    wanted = project_name.casefold()
    if start_dir.name.casefold() == wanted and start_dir.is_dir():
        return start_dir
    exact = start_dir / project_name
    if exact.is_dir():
        return exact
    try:
        entries = sorted(start_dir.iterdir())
    except OSError:
        return None
    for entry in entries:
        if entry.name.casefold() == wanted and entry.is_dir():
            return entry
    return None


def _explicit_path(description: object) -> Path | None:
    """The path from a `[Chord_Working_Path: "..."]` line, or None without one.

    The path is quoted — single or double — so a path containing a space is
    still one path, and so a line that forgot the quotes is an error rather
    than a silently shortened one. The name itself matches in any case,
    because it is typed by hand and a rule nobody can spell is a rule that
    fires at random.

    Relative, so that a description stays portable between machines. The first
    such line wins; a second one is a contradiction rather than a preference,
    and Linear's own description field has no way to say which was meant.

    Raises `ResolutionError` when the line is there but malformed. Somebody
    wrote down a location: reading it as "no location" would fall back to the
    project root and work on the wrong code quietly, which is the exact
    failure this module exists to prevent.

    Takes `object` because Linear's description is a nullable string field and
    `render` is already hardened against the shapes it turns out to hold. A
    hand-over that stopped here would be the worst place to reintroduce that.
    """
    if not isinstance(description, str):
        return None
    prefix = EXPLICIT_PATH_PREFIX.lower()
    for line in description.splitlines():
        # Linear stores descriptions as Markdown and escapes what its editor
        # would otherwise read as syntax: the line typed as `[Chord_Working_Path:
        # "..."]` comes back as `\[Chord_Working_Path: "..."\]`, and an
        # underscore anywhere in it may come back as `\_`. Read it as typed.
        line = _MARKDOWN_ESCAPE.sub(r"\1", line.strip())
        if not line.lower().startswith(prefix):
            continue
        rest = line[len(EXPLICIT_PATH_PREFIX) :].strip()
        if not rest or rest[0] not in "\"'":
            raise ResolutionError(
                f"The `{EXPLICIT_PATH_PREFIX}` line needs its path in single or "
                f'double quotes, like `{EXPLICIT_PATH_PREFIX} "/path/to/repo"]`.'
            )
        quote, rest = rest[0], rest[1:]
        end = rest.find(quote)
        if end == -1:
            raise ResolutionError(
                f"The `{EXPLICIT_PATH_PREFIX}` line opens a {quote} quote and "
                f"never closes it."
            )
        text, trailing = rest[:end], rest[end + 1 :].strip()
        if trailing not in ("", "]"):
            raise ResolutionError(
                f"The `{EXPLICIT_PATH_PREFIX}` line must hold nothing but the "
                f"quoted path; found {trailing!r} after the closing quote."
            )
        if not text:
            raise ResolutionError(
                f"The `{EXPLICIT_PATH_PREFIX}` line quotes an empty path."
            )
        # `~` first, because it is what a person writes and pathlib
        # treats it as an ordinary directory name.
        return Path(text).expanduser()
    return None


def is_git_repository(path: Path) -> bool:
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
