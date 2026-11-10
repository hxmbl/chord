"""Layered config: defaults, the user file, the start folder's, the repo's.

Per key the nearest file wins, curated harnesses merge by name, and machine
settings only count in the user file (or in the start folder's file when there
is no user file, so a single-file setup keeps working). A target repository's
own file is read per issue, so editing it needs no restart and a broken one
fails only that issue.
"""

import asyncio
import subprocess
import uuid
from pathlib import Path

import pytest
from conftest import RecordingHarness

from chord import config as cfg
from chord import watcher
from chord.linear import Actor, IssuePage, LabelChange
from chord.routing import RepoRouters, Router


def uuid_of(n: int) -> str:
    return str(uuid.UUID(int=n))


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def user_file(home: Path, text: str) -> Path:
    return write(home / ".config" / "chord" / "chord.toml", text)


def project(home: Path, text: str | None = None, name: str = "app") -> Path:
    folder = home / "Projects" / name
    folder.mkdir(parents=True, exist_ok=True)
    if text is not None:
        write(folder / ".config" / "chord" / "chord.toml", text)
    return folder


# --- merging ---


def test_nearest_file_wins_per_key(home):
    user_file(home, 'label = "Robot"\nharness = "claude"\ninterval = 30\n')
    folder = project(home, 'harness = "codex"\n')

    loaded = cfg.load(start=folder)

    assert loaded.harness == "codex"
    assert loaded.label == "Robot"
    assert loaded.interval == 30
    assert loaded.source_of("harness")[0] == cfg.PROJECT
    assert loaded.source_of("label")[0] == cfg.USER
    assert loaded.source_of("webhook_port") == (cfg.DEFAULT, None)
    assert [layer.source for layer in loaded.layers] == [cfg.USER, cfg.PROJECT]


def test_harnesses_merge_by_name(home):
    user_file(
        home,
        '[harnesses."a"]\ncommand = ["a"]\n[harnesses."b"]\ncommand = ["b"]\n',
    )
    folder = project(
        home, '[harnesses."b"]\ncommand = ["bee"]\n[harnesses."c"]\ncommand = ["c"]\n'
    )

    loaded = cfg.load(start=folder)

    assert loaded.harnesses == {"a": ["a"], "b": ["bee"], "c": ["c"]}
    assert loaded.source_of("harnesses.a")[0] == cfg.USER
    assert loaded.source_of("harnesses.b")[0] == cfg.PROJECT


def test_user_file_alone_is_read(home):
    user_file(home, 'harness = "print"\n')
    loaded = cfg.load(start=project(home))
    assert loaded.harness == "print"
    assert loaded.from_file


def test_no_files_is_defaults(home):
    loaded = cfg.load(start=project(home))
    assert not loaded.from_file
    assert loaded.harness == cfg.DEFAULT_HARNESS
    assert loaded.layers == ()


# --- machine settings ---


def test_machine_keys_in_a_project_file_are_ignored_with_a_warning(home):
    user_file(home, "interval = 30\n")
    folder = project(home, 'interval = 5\nwebhook_secret = "s"\n')

    loaded = cfg.load(start=folder)

    assert loaded.interval == 30
    assert loaded.webhook_secret == ""
    assert any("`interval`" in w and "ignored" in w for w in loaded.warnings)
    assert any("`webhook_secret`" in w for w in loaded.warnings)


def test_a_start_folder_file_with_no_user_file_keeps_its_machine_keys(home):
    """Every existing single-file setup: nothing about it may change."""
    folder = project(home, "interval = 7\n[notify]\nreceived = false\n")

    loaded = cfg.load(start=folder)

    assert loaded.interval == 7
    assert loaded.notify.received is False
    assert loaded.warnings == ()


# --- version ---


def test_missing_version_means_1(home):
    folder = project(home, 'harness = "print"\n')
    assert cfg.load(start=folder).version == 1


def test_version_1_is_accepted(tmp_path):
    path = write(tmp_path / "chord.toml", "version = 1\n")
    assert cfg.load(path).version == 1


def test_a_newer_version_is_a_hard_error(tmp_path):
    path = write(tmp_path / "chord.toml", "version = 2\nfuture = true\n")
    with pytest.raises(cfg.ConfigError, match="config version 2"):
        cfg.load(path)


@pytest.mark.parametrize("value", ["0", '"1"', "true", "1.5"])
def test_a_nonsense_version_is_an_error(tmp_path, value):
    path = write(tmp_path / "chord.toml", f"version = {value}\n")
    with pytest.raises(cfg.ConfigError, match="`version`"):
        cfg.load(path)


# --- the target repository's file ---


def test_repo_file_overrides_per_key(home):
    user_file(home, f'harness = "claude"\nallowed_actors = ["{uuid_of(1)}"]\n')
    base = cfg.load(start=project(home))
    repo = project(
        home, f'harness = "codex"\nallowed_actors = ["{uuid_of(2)}"]\n', "lib"
    )

    effective = cfg.load_repo(base, repo)

    assert effective.harness == "codex"
    assert effective.allowed_actors == (uuid_of(2),)
    assert effective.source_of("harness")[0] == cfg.REPO
    assert effective.layers[-1].source == cfg.REPO


def test_repo_file_cannot_change_machine_or_watcher_keys(home):
    user_file(home, 'label = "Chord"\ninterval = 30\n')
    base = cfg.load(start=project(home))
    repo = project(home, 'label = "Other"\ninterval = 5\n', "lib")

    effective = cfg.load_repo(base, repo)

    assert effective.label == "Chord"
    assert effective.interval == 30
    assert any("`label`" in w for w in effective.warnings)
    assert any("`interval`" in w for w in effective.warnings)


def test_notify_sound_is_a_machine_key(home):
    """A project's or a repository's file can't make this machine beep: only
    the user file sets `[notify] sound`."""
    assert "notify" in cfg.MACHINE_KEYS
    user_file(home, "[notify]\nsound = false\n")
    folder = project(home, "[notify]\nsound = true\n")

    loaded = cfg.load(start=folder)
    assert loaded.notify.sound is False
    assert any("`notify`" in w and "machine setting" in w for w in loaded.warnings)

    repo = project(home, "[notify]\nsound = true\n", "lib")
    effective = cfg.load_repo(loaded, repo)
    assert effective.notify.sound is False
    assert any("`notify`" in w for w in effective.warnings)


def test_notify_sound_from_the_user_file_is_used(home):
    user_file(home, "[notify]\nsound = true\n")
    loaded = cfg.load(start=project(home))
    assert loaded.notify.sound is True
    assert loaded.source_of("notify")[0] == cfg.USER


def test_a_default_notify_in_a_project_file_does_not_warn(home):
    """The value it would have anyway changes nothing, so it is ignored quietly."""
    user_file(home, 'harness = "print"\n')
    folder = project(home, "[notify]\nreceived = true\nsound = false\n")
    loaded = cfg.load(start=folder)
    assert not any("`notify`" in w for w in loaded.warnings)


def test_a_repo_without_a_file_is_the_base(home):
    base = cfg.load(start=project(home))
    assert cfg.load_repo(base, project(home, None, "lib")) is base


def test_the_start_folder_is_not_layered_twice(home):
    folder = project(home, 'harness = "codex"\n')
    base = cfg.load(start=folder)
    assert cfg.load_repo(base, folder) is base


def test_a_broken_repo_file_is_a_config_error(home):
    base = cfg.load(start=project(home))
    repo = project(home, "harness = \n", "lib")
    with pytest.raises(cfg.ConfigError, match="isn't valid TOML"):
        cfg.load_repo(base, repo)


def test_a_newer_repo_file_is_refused(home):
    base = cfg.load(start=project(home))
    repo = project(home, "version = 9\n", "lib")
    with pytest.raises(cfg.ConfigError, match="version 9"):
        cfg.load_repo(base, repo)


def test_repo_routers_reread_an_edited_file(home):
    base = cfg.load(start=project(home))
    repo = project(home, 'harness = "print"\n', "lib")
    warned: list[str] = []
    routers = RepoRouters(base, Router("Chord", base.harness), warn=warned.append)

    assert routers.config_for(repo).harness == "print"
    first = routers(repo)
    assert routers(repo) is first  # unchanged file, same router

    write(
        repo / ".config" / "chord" / "chord.toml", 'harness = "codex"\ninterval = 9\n'
    )
    assert routers.config_for(repo).harness == "codex"
    assert any("`interval`" in w for w in warned)


# --- the watcher, per issue ---


class StubLinear:
    def __init__(self, issues, history=()):
        self._issues = issues
        self._history = list(history)

    async def issues_for(self, filter):
        return IssuePage(self._issues, False)

    async def label_history(self, issue_id):
        return list(self._history)

    async def comments(self, issue_id):
        return []


def issue(n, project_name):
    return {
        "id": str(n),
        "identifier": f"ENG-{n}",
        "title": "fix",
        "description": "",
        "createdAt": "2026-01-01",
        "labels": {"nodes": [{"name": "Chord"}]},
        "project": {"name": project_name},
        "creator": {"id": uuid_of(5), "name": "Eve"},
    }


def git_repo(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(folder)], check=True)
    return folder


def test_allowed_actors_is_checked_against_the_target_repo(home, tmp_path):
    start = git_repo(tmp_path / "start")
    open_repo = git_repo(start / "open")
    locked = git_repo(start / "locked")
    write(
        locked / ".config" / "chord" / "chord.toml",
        f'allowed_actors = ["{uuid_of(1)}"]\n',
    )

    base = cfg.load(start=start)
    spy = RecordingHarness()
    make = lambda c: Router(
        c.label,
        c.harness,
        c.harnesses,
        factory=lambda spec: spy,
        allowed_actors=c.allowed_actors,
    )
    eve = Actor(id=uuid_of(5), name="Eve")
    stub = StubLinear(
        [issue(1, "open"), issue(2, "locked")],
        [LabelChange(at="2026-01-01", labels=["Chord"], who=eve)],
    )
    w = watcher.Watcher(
        stub,
        make(base),
        1,
        tmp_path / "state.json",
        start_dir=start,
        search_dirs=(),
        repo_router=RepoRouters(base, make(base), make=make),
    )
    asyncio.run(w.poll())

    assert len(spy.prompts) == 1
    assert spy.working_directories == [open_repo]


def test_a_broken_repo_file_fails_only_that_issue(home, tmp_path, capsys):
    start = git_repo(tmp_path / "start")
    good = git_repo(start / "good")
    bad = git_repo(start / "bad")
    write(bad / ".config" / "chord" / "chord.toml", "harness = [\n")

    base = cfg.load(start=start)
    spy = RecordingHarness()
    make = lambda c: Router(c.label, c.harness, factory=lambda spec: spy)
    w = watcher.Watcher(
        StubLinear([issue(1, "bad"), issue(2, "good")]),
        make(base),
        1,
        tmp_path / "state.json",
        start_dir=start,
        search_dirs=(),
        repo_router=RepoRouters(base, make(base), make=make),
    )
    asyncio.run(w.poll())

    assert spy.working_directories == [good]
    out = capsys.readouterr().out
    assert "ENG-1 skipped: the chord.toml in" in out
    assert str(bad) in out
    # Not recorded, so fixing the file brings it back with no restart.
    write(bad / ".config" / "chord" / "chord.toml", 'harness = "print"\n')
    asyncio.run(w.poll())
    assert bad in spy.working_directories
