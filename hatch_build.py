"""Stamps Chord's build number into the package at build time.

`uv tool install git+https://github.com/hxmbl/chord` builds from a checkout and
then runs without one, so the number is read from git here and written to
``src/chord/_build.py`` (gitignored), which `chord --version` reads. An sdist
carries the stamp so a wheel built from it keeps the number. See
``src/chord/version.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class StampBuild(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if version == "editable":
            # An editable install runs from the checkout and asks git live.
            return
        root = Path(self.root)
        # Loaded by path: the chord package itself isn't importable mid-build.
        spec = importlib.util.spec_from_file_location(
            "_chord_version", root / "src" / "chord" / "version.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        stamp = root / "src" / "chord" / "_build.py"
        build = module.from_git(root) if (root / ".git").exists() else None
        if build:
            stamp.write_text(f"BUILD = {build!r}\n")
        if stamp.is_file():
            sdist = self.target_name == "sdist"
            target = "src/chord/_build.py" if sdist else "chord/_build.py"
            build_data["force_include"][str(stamp)] = target
