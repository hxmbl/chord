"""Route strings, `[routes]`, and who "me" and an email are in allowed_actors."""

import json
import uuid
from pathlib import Path

import pytest

from chord import config as cfg
from chord import harness
from chord.config import RouteSpec, T3Options, parse_route
from chord.routing import route_label, router_for

ME = str(uuid.UUID(int=7))
ALICE = str(uuid.UUID(int=8))


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def user_file(text: str) -> Path:
    return write(Path.home() / ".config" / "chord" / "chord.toml", text)


# --- route strings ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("opencode", RouteSpec("opencode")),
        ("opencode:big-pickle", RouteSpec("opencode", "big-pickle")),
        ("t3:openai/gpt-5@main", RouteSpec("t3", "openai/gpt-5", "main")),
        ("opencode:ollama/qwen3:4b", RouteSpec("opencode", "ollama/qwen3:4b")),
        ("claude:opus", RouteSpec("claude", "opus")),
        ("t3@main", RouteSpec("t3", None, "main")),
    ],
)
def test_route_strings_parse(text, expected):
    assert parse_route(text) == expected
    assert str(parse_route(text)) == text


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("", "is empty"),
        ("opencode:", "nothing after ':'"),
        ("opencode@", "nothing after '@'"),
        (":big-pickle", "names no harness"),
        ("nope:x", "doesn't know"),
        ("open code", "contains spaces"),
        ("claude@main", "only t3"),
        ("t3:gpt-5", "provider"),
        ("print:x", "doesn't take"),
        ("opencode:a@b@c", "more than one '@'"),
    ],
)
def test_malformed_route_strings_say_what_and_the_shape(text, why):
    with pytest.raises(cfg.ConfigError) as caught:
        parse_route(text, "`routes.x`")
    message = str(caught.value)
    assert why in message
    assert "harness[:model][@branch]" in message


def test_known_harnesses_cover_every_preset():
    assert set(harness.PRESETS) <= set(cfg.KNOWN_HARNESSES)


# --- [routes] ---


def test_routes_load_and_route_by_name(tmp_path):
    path = write(
        tmp_path / "chord.toml",
        '[routes]\nbig-pickle = "opencode:big-pickle"\nclaude = "claude"\n',
    )
    loaded = cfg.load(path)

    assert loaded.routes == {
        "big-pickle": RouteSpec("opencode", "big-pickle"),
        "claude": RouteSpec("claude"),
    }
    router = router_for(loaded)
    assert [r.label for r in router.routes()][1:] == [
        "Chord -> big-pickle",
        "Chord -> claude",
    ]
    assert route_label("Chord", "big-pickle") == "Chord -> big-pickle"


@pytest.mark.parametrize("name", ["a/b", "a:b", "a->b"])
def test_route_names_cannot_hold_label_separators(tmp_path, name):
    path = write(tmp_path / "chord.toml", f'[routes]\n"{name}" = "opencode"\n')
    with pytest.raises(cfg.ConfigError, match="isn't a usable route name"):
        cfg.load(path)


def test_a_bad_route_string_names_the_route(tmp_path):
    path = write(tmp_path / "chord.toml", '[routes]\nfast = "opencode:"\n')
    with pytest.raises(cfg.ConfigError, match="`routes.fast`"):
        cfg.load(path)


def test_routes_merge_by_name_across_layers(tmp_path, monkeypatch):
    user_file('[routes]\na = "opencode"\nb = "claude"\n')
    start = tmp_path / "start"
    write(start / ".config" / "chord" / "chord.toml", '[routes]\nb = "codex"\n')
    loaded = cfg.load(start=start)
    assert loaded.routes == {"a": RouteSpec("opencode"), "b": RouteSpec("codex")}


def test_harness_can_be_a_route_string_or_take_a_model(tmp_path):
    one = write(tmp_path / "one.toml", 'harness = "opencode:big-pickle"\n')
    two = write(tmp_path / "two.toml", 'harness = "claude"\nmodel = "opus"\n')
    three = write(tmp_path / "three.toml", 'harness = "run.sh host:1"\n')
    assert cfg.load(one).harness == RouteSpec("opencode", "big-pickle")
    assert cfg.load(two).harness == RouteSpec("claude", "opus")
    assert cfg.load(three).harness == ["run.sh", "host:1"]


def test_a_model_needs_a_known_harness(tmp_path):
    path = write(tmp_path / "chord.toml", 'harness = ["my-agent"]\nmodel = "x"\n')
    with pytest.raises(cfg.ConfigError, match="`model`"):
        cfg.load(path)


def test_routes_build_with_their_model(monkeypatch):
    monkeypatch.setattr(harness.shutil, "which", lambda name: f"/bin/{name}")

    claude = harness.build(RouteSpec("claude", "opus"))
    codex = harness.build(RouteSpec("codex", "gpt-5"))
    opencode = harness.build(RouteSpec("opencode", "big-pickle"))

    assert claude.argv[-2:] == ["--model", "opus"]
    assert codex.argv[1:4] == ["exec", "--model", "gpt-5"]
    assert codex.argv[-1] == "-", "codex still reads the issue from stdin"
    assert opencode.argv == ["opencode", "run", "--model", "big-pickle"]


def test_a_t3_route_is_its_options(monkeypatch):
    monkeypatch.setattr(harness.shutil, "which", lambda name: f"/bin/{name}")
    built = harness.build(RouteSpec("t3", "openai/gpt-5", "main"))
    assert built.options == T3Options(provider="openai", model="gpt-5", worktree="main")


def test_command_line_shows_what_a_route_runs():
    assert harness.command_line(RouteSpec("opencode", "m")) == "opencode run --model m"


# --- old shapes still load, with a notice ---


def test_old_harnesses_and_t3_still_load_with_a_notice(tmp_path):
    path = write(
        tmp_path / "chord.toml",
        'harness = "t3"\n'
        '[harnesses."big-pickle"]\ncommand = ["opencode", "run", "--model", "big-pickle"]\n'
        '[harnesses."opencode/tiny"]\ncommand = ["opencode", "run", "--model", "tiny"]\n'
        '[harnesses.mine]\ncommand = ["./agent.sh"]\n'
        '[t3]\nprovider = "openai"\nmodel = "gpt-5"\n',
    )
    loaded = cfg.load(path)

    assert loaded.harnesses["opencode/tiny"] == ["opencode", "run", "--model", "tiny"]
    assert loaded.t3.provider == "openai"
    notices = " ".join(loaded.warnings)
    assert "`[t3]`" in notices and "deprecated" in notices
    assert "`big-pickle`" in notices
    # A command with no route spelling is the escape hatch, not deprecated; a
    # name with `/` can't be a route name, so it isn't nagged about either.
    assert "`mine`" not in notices
    assert "opencode/tiny" not in notices


# --- "me" ---


def test_me_is_the_user_files_id(tmp_path):
    user_file(f'linear_user_id = "{ME}"\nallowed_actors = ["me"]\n')
    loaded = cfg.load(start=tmp_path)
    assert loaded.allowed_actors == (ME,)


def test_me_in_a_project_file_is_the_user_files_id(tmp_path):
    user_file(f'linear_user_id = "{ME}"\n')
    write(tmp_path / ".config" / "chord" / "chord.toml", 'allowed_actors = ["me"]\n')
    assert cfg.load(start=tmp_path).allowed_actors == (ME,)


def test_a_repo_file_cannot_say_who_me_is(tmp_path):
    user_file(f'linear_user_id = "{ME}"\n')
    base = cfg.load(start=tmp_path / "start")
    repo = tmp_path / "repo"
    write(
        repo / ".config" / "chord" / "chord.toml",
        f'linear_user_id = "{ALICE}"\nallowed_actors = ["me"]\n',
    )
    effective = cfg.load_repo(base, repo)
    assert effective.allowed_actors == (ME,)
    assert any("`linear_user_id`" in w for w in effective.warnings)


def test_a_start_folder_file_cannot_say_who_me_is_without_a_user_file(
    tmp_path, stored_token
):
    """The single-file exception honours machine keys, but never identity.

    With no user file the start folder's file stands in for it, and it is
    often a cloned repository. Its `linear_user_id` must not decide who "me"
    authorises; the signed-in user does.
    """
    stored_token(viewer_id=ME)
    start = tmp_path / "start"
    write(
        start / ".config" / "chord" / "chord.toml",
        f'linear_user_id = "{ALICE}"\nallowed_actors = ["me"]\ninterval = 120\n',
    )
    loaded = cfg.load(start=start)
    assert loaded.allowed_actors == (ME,)
    assert loaded.interval == 120  # the other machine keys still count
    assert any("`linear_user_id`" in w for w in loaded.warnings)


def test_a_start_folder_id_alone_does_not_make_me_resolvable(tmp_path):
    start = tmp_path / "start"
    write(
        start / ".config" / "chord" / "chord.toml",
        f'linear_user_id = "{ALICE}"\nallowed_actors = ["me"]\n',
    )
    with pytest.raises(cfg.ConfigError, match="chord setup"):
        cfg.load(start=start)


def test_me_falls_back_to_the_signed_in_user(tmp_path, stored_token):
    stored_token(viewer_id=ME)
    path = write(tmp_path / "chord.toml", 'allowed_actors = ["me"]\n')
    assert cfg.load(path).allowed_actors == (ME,)


def test_me_without_an_id_says_to_run_setup(tmp_path):
    path = write(tmp_path / "chord.toml", 'allowed_actors = ["me"]\n')
    with pytest.raises(cfg.ConfigError, match="chord setup"):
        cfg.load(path)


def test_setup_stores_the_id_in_the_user_file_and_keeps_the_rest():
    path = user_file('# mine\nlabel = "Robot"\n\n[notify]\nreceived = false\n')
    cfg.remember_user_id(ME.upper())
    cfg.remember_user_id(ME)

    text = path.read_text()
    assert text.count("linear_user_id") == 1
    assert "# mine" in text and "received = false" in text
    assert cfg.load(path).linear_user_id == ME


# --- emails ---


def test_emails_resolve_to_ids(tmp_path):
    path = write(
        tmp_path / "chord.toml", f'allowed_actors = ["Alice@Example.com", "{ME}"]\n'
    )
    loaded = cfg.load(path)
    assert loaded.unresolved_emails == ("alice@example.com",)

    asked: list[list[str]] = []

    def lookup(emails):
        asked.append(emails)
        return {"alice@example.com": ALICE}

    resolved = cfg.resolve_emails(loaded, lookup)
    assert resolved.allowed_actors == tuple(sorted((ALICE, ME)))
    assert asked == [["alice@example.com"]]


def test_an_email_linear_does_not_know_is_an_error(tmp_path):
    path = write(tmp_path / "chord.toml", 'allowed_actors = ["bob@example.com"]\n')
    with pytest.raises(cfg.ConfigError, match="bob@example.com"):
        cfg.resolve_emails(cfg.load(path), lambda emails: {})


def test_an_unresolved_email_matches_nobody(tmp_path):
    path = write(tmp_path / "chord.toml", 'allowed_actors = ["bob@example.com"]\n')
    router = router_for(cfg.load(path))
    assert router.authorises
    assert not router.authorises_actor("bob@example.com".upper())
    assert not router.authorises_actor(json.dumps(ALICE))
