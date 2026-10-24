"""`chord config`: edit chord.toml in an editor, and never save a broken one."""

import sys

import pytest
from typer.testing import CliRunner

from chord import cli
from chord.cli import app
from chord.config import STOCK

runner = CliRunner()


def editor_writing(tmp_path, *texts):
    """An $EDITOR that replaces the file with each of `texts` in turn."""
    for n, text in enumerate(texts):
        (tmp_path / f"edit{n}.toml").write_text(text)
    script = tmp_path / "editor.py"
    script.write_text(
        "import pathlib, sys\n"
        f"here = pathlib.Path({str(tmp_path)!r})\n"
        "count = here / 'count'\n"
        "n = int(count.read_text()) if count.exists() else 0\n"
        "count.write_text(str(n + 1))\n"
        "pathlib.Path(sys.argv[1]).write_text((here / f'edit{n}.toml').read_text())\n"
    )
    return f"{sys.executable} {script}"


@pytest.fixture
def project(tmp_path, monkeypatch):
    work = tmp_path / "work"
    (work / ".config" / "chord").mkdir(parents=True)
    monkeypatch.chdir(work)
    monkeypatch.setattr(cli.guided, "interactive", lambda: True)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr(cli.service, "current", lambda: None)
    return work / ".config" / "chord" / "chord.toml"


def test_path_prints_the_file(project):
    project.write_text("")
    result = runner.invoke(app, ["config", "--path"])
    assert result.exit_code == 0
    assert result.output.strip() == str(project.resolve())


def test_a_good_edit_is_saved(tmp_path, project, monkeypatch):
    project.write_text('harness = "print"\n')
    monkeypatch.setenv(
        "EDITOR", editor_writing(tmp_path, 'harness = "print"\ninterval = 30\n')
    )

    result = runner.invoke(app, ["config"])

    assert result.exit_code == 0, result.output
    assert "Saved" in result.output
    assert "interval = 30" in project.read_text()


def test_a_broken_edit_is_never_saved(tmp_path, project, monkeypatch):
    project.write_text('harness = "print"\n')
    monkeypatch.setenv("EDITOR", editor_writing(tmp_path, "interval = 1\n"))

    result = runner.invoke(app, ["config"], input="n\n")

    assert result.exit_code == 1
    assert "The floor is" in result.output
    assert project.read_text() == 'harness = "print"\n'


def test_going_back_in_fixes_it(tmp_path, project, monkeypatch):
    project.write_text('harness = "print"\n')
    monkeypatch.setenv(
        "EDITOR",
        editor_writing(tmp_path, "interval = [\n", 'harness = "print"\ninterval = 9\n'),
    )

    result = runner.invoke(app, ["config"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "isn't valid TOML" in result.output
    assert "interval = 9" in project.read_text()


def test_no_change_writes_nothing(tmp_path, project, monkeypatch):
    project.write_text('harness = "print"\n')
    monkeypatch.setenv("EDITOR", editor_writing(tmp_path, 'harness = "print"\n'))

    result = runner.invoke(app, ["config"])

    assert result.exit_code == 0
    assert "No changes." in result.output


def test_with_no_file_it_starts_from_the_stock_one(tmp_path, project, monkeypatch):
    # The stock file is offered, and an unchanged stock file writes nothing.
    monkeypatch.setenv("EDITOR", editor_writing(tmp_path, STOCK))

    result = runner.invoke(app, ["config"])

    assert "No changes." in result.output
    assert not project.exists()


def test_without_a_terminal_it_says_where_the_file_is(project, monkeypatch):
    monkeypatch.setattr(cli.guided, "interactive", lambda: False)
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 1
    assert "needs a terminal" in result.output
