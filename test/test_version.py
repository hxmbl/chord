"""`chord --version`: the green-build number, or dev."""

import re
import subprocess

from typer.testing import CliRunner

from chord import version
from chord.cli import app

runner = CliRunner()


def _git(root, *args):
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
    )


def _repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(
        tmp_path,
        "-c", "user.name=t", "-c", "user.email=t@example.com",
        "commit", "-q", "--allow-empty", "-m", "x",
    )
    return tmp_path


def test_version_flag_prints_build_number_or_dev(monkeypatch):
    monkeypatch.delenv("CHORD_BUILD", raising=False)
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert re.fullmatch(r"chord (v\d+|dev( \([0-9a-f]+\))?)\n", result.output)


def test_environment_overrides(monkeypatch):
    monkeypatch.setenv("CHORD_BUILD", "v42")
    result = runner.invoke(app, ["--version"])
    assert result.output == "chord v42\n"


def test_numbered_commit_reports_its_tag(tmp_path):
    root = _repo(tmp_path)
    _git(root, "tag", "v1")
    _git(root, "tag", "v1.1")  # not a build number
    _git(root, "tag", "v3")
    assert version.from_git(root) == "v3"


def test_unnumbered_commit_is_dev_with_hash(tmp_path):
    root = _repo(tmp_path)
    assert re.fullmatch(r"dev \([0-9a-f]{7,}\)", version.from_git(root) or "")


def test_outside_git_is_none(tmp_path):
    assert version.from_git(tmp_path) is None


def test_falls_back_to_dev_without_git_or_stamp(monkeypatch):
    monkeypatch.delenv("CHORD_BUILD", raising=False)
    monkeypatch.setattr(version, "_checkout", lambda: None)
    monkeypatch.setattr(
        version.importlib,
        "import_module",
        lambda name: (_ for _ in ()).throw(ImportError(name)),
    )
    assert version.build() == "dev"
