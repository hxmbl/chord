"""Chord's build number.

Every push to master whose CI goes fully green is tagged with the next integer,
``v1``, ``v2`` and so on; a red push gets none. The version reported is, in
order: ``CHORD_BUILD`` from the environment, the ``vN`` tag on the commit of
the checkout Chord is running from, the number stamped in at install time
(``chord/_build.py``, written by ``hatch_build.py``), or ``dev``.
"""

from __future__ import annotations

import importlib
import os
import re
import subprocess
from pathlib import Path

BUILD_TAG = re.compile(r"^v[0-9]+$")


def _git(root: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def from_git(root: Path) -> str | None:
    """``vN`` when the checkout at ``root`` is on a numbered commit, otherwise
    ``dev (<short hash>)``; None when ``root`` is not in a git checkout."""
    commit = _git(root, "rev-parse", "--short=7", "HEAD")
    if not commit:
        return None
    tags = _git(root, "tag", "--points-at", "HEAD") or ""
    numbered = [tag for tag in tags.splitlines() if BUILD_TAG.match(tag)]
    if numbered:
        return max(numbered, key=lambda tag: int(tag[1:]))
    return f"dev ({commit})"


def _checkout() -> Path | None:
    """The checkout this package is running from, if it is one.

    Only when the package really is that checkout's ``src/chord``: an install
    inside some other repository (a venv in a project) must not report that
    repository's tags.
    """
    package = Path(__file__).resolve().parent
    top = _git(package, "rev-parse", "--show-toplevel")
    if top and (Path(top) / "src" / "chord").resolve() == package:
        return Path(top)
    return None


def build() -> str:
    env = os.environ.get("CHORD_BUILD", "").strip()
    if env:
        return env
    root = _checkout()
    if root is not None:
        live = from_git(root)
        if live:
            return live
    try:
        stamped = importlib.import_module("chord._build").BUILD
    except (ImportError, AttributeError):
        stamped = None
    return stamped or "dev"
