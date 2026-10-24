"""`~/.config/chord/chord.toml` is user-wide config, never a project in `~`.

It has the shape of a project config sitting in the home directory, and was
once read as one: every directory under home then had `~` as its project root,
and issues were looked for in `~` instead of the checkout Chord was started in.
"""

from pathlib import Path

import pytest

from chord import paths


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".config" / "chord").mkdir(parents=True)
    (home / ".config" / "chord" / "chord.toml").write_text('harness = "print"\n')
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def test_a_checkout_under_home_is_its_own_root(home):
    checkout = home / "Projects" / "chord"
    checkout.mkdir(parents=True)

    assert paths.project_root(checkout) == checkout.resolve()


def test_the_home_config_is_still_read_as_user_config(home):
    checkout = home / "Projects" / "chord"
    checkout.mkdir(parents=True)

    found, exists = paths.config_path(checkout)

    assert exists
    assert found == home / ".config" / "chord" / "chord.toml"


def test_a_project_config_beats_the_home_one(home):
    checkout = home / "Projects" / "chord"
    (checkout / ".config" / "chord").mkdir(parents=True)
    (checkout / ".config" / "chord" / "chord.toml").write_text("")

    found, _ = paths.config_path(checkout / "src")

    assert found == checkout / ".config" / "chord" / "chord.toml"
    assert paths.project_root(checkout / "src") == checkout.resolve()


def test_starting_in_home_itself_still_roots_there(home):
    assert paths.project_root(home) == home.resolve()
