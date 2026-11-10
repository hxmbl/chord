"""`chord route`, `chord set`, `chord config show|migrate`, `chord labels`, and
the JSON Schema."""

import json
import re
import tomllib
import uuid
from pathlib import Path
from typing import ClassVar

import pytest
from typer.testing import CliRunner

from chord import cli, config
from chord.cli import app

runner = CliRunner()
ME = str(uuid.UUID(int=7))
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def project(tmp_path, monkeypatch):
    folder = tmp_path / "app"
    folder.mkdir()
    monkeypatch.chdir(folder)
    return folder


def project_file(folder: Path) -> Path:
    return folder / ".config" / "chord" / "chord.toml"


def user_file() -> Path:
    return Path.home() / ".config" / "chord" / "chord.toml"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# --- chord route ---


def test_route_add_keeps_comments_and_validates(project):
    path = write(project_file(project), '# mine\nharness = "claude"  # keep\n')

    result = runner.invoke(app, ["route", "add", "big-pickle", "opencode:big-pickle"])

    assert result.exit_code == 0, result.output
    text = path.read_text()
    assert "# mine" in text and "# keep" in text
    assert tomllib.loads(text)["routes"] == {"big-pickle": "opencode:big-pickle"}
    assert "Chord -> big-pickle" in result.output


@pytest.mark.parametrize(
    ("name", "route"), [("a/b", "opencode"), ("fast", "opencode:"), ("x", "nope")]
)
def test_an_invalid_route_writes_nothing(project, name, route):
    path = write(project_file(project), 'harness = "claude"\n')
    before = path.read_text()

    result = runner.invoke(app, ["route", "add", name, route])

    assert result.exit_code == 1
    assert path.read_text() == before
    assert not list(path.parent.glob(".chord-*")), "a temp file was left behind"


def test_route_add_user_writes_the_user_file(project):
    result = runner.invoke(app, ["route", "add", "--user", "c", "claude"])
    assert result.exit_code == 0, result.output
    assert tomllib.loads(user_file().read_text())["routes"] == {"c": "claude"}
    assert not project_file(project).exists()


def test_route_rm_and_ls(project):
    write(
        project_file(project),
        '[routes]\na = "opencode"\nb = "codex"\n[harnesses."x/y"]\ncommand = ["x"]\n',
    )
    listed = runner.invoke(app, ["route", "ls"])
    assert "Chord -> a  ->  opencode" in listed.output
    assert "Chord -> x -> y" in listed.output

    assert runner.invoke(app, ["route", "rm", "a"]).exit_code == 0
    assert runner.invoke(app, ["route", "rm", "x/y"]).exit_code == 0
    assert runner.invoke(app, ["route", "rm", "nope"]).exit_code == 1
    raw = tomllib.loads(project_file(project).read_text())
    assert raw["routes"] == {"b": "codex"}
    assert raw.get("harnesses", {}) == {}


# --- chord set ---


def test_set_puts_a_new_top_level_key_above_the_first_table(project):
    path = write(
        project_file(project),
        '# head\nlabel = "Chord"\n\n# the routes\n[routes]\na = "opencode"\n',
    )
    result = runner.invoke(app, ["set", "harness", "codex:gpt-5"])

    assert result.exit_code == 0, result.output
    raw = tomllib.loads(path.read_text())
    assert raw["harness"] == "codex:gpt-5"
    assert raw["routes"] == {"a": "opencode"}, "the key landed inside [routes]"
    assert "# the routes" in path.read_text()


def test_set_reads_lists_and_numbers(project):
    write(user_file(), f'linear_user_id = "{ME}"\n')
    assert (
        runner.invoke(app, ["set", "allowed_actors", "me, sam@example.com"]).exit_code
        == 0
    )
    assert runner.invoke(app, ["set", "--user", "interval", "30"]).exit_code == 0
    assert (
        runner.invoke(app, ["set", "--user", "notify.received", "false"]).exit_code == 0
    )

    assert tomllib.loads(project_file(project).read_text())["allowed_actors"] == [
        "me",
        "sam@example.com",
    ]
    user = tomllib.loads(user_file().read_text())
    assert user["interval"] == 30
    assert user["notify"] == {"received": False}


def test_set_notify_sound_goes_to_the_user_file_only(project):
    refused = runner.invoke(app, ["set", "notify.sound", "true"])
    assert refused.exit_code != 0
    assert "machine setting" in refused.output

    assert runner.invoke(app, ["set", "--user", "notify.sound", "true"]).exit_code == 0
    assert tomllib.loads(user_file().read_text())["notify"] == {"sound": True}

    bad = runner.invoke(app, ["set", "--user", "notify.sound", "loud"])
    assert bad.exit_code != 0


def test_set_refuses_machine_keys_without_user_and_bad_values(project):
    machine = runner.invoke(app, ["set", "interval", "30"])
    assert machine.exit_code == 1 and "--user" in machine.output

    floor = runner.invoke(app, ["set", "--user", "interval", "1"])
    assert floor.exit_code == 1 and "floor" in floor.output
    assert not user_file().exists(), "an invalid value reached the disk"

    unknown = runner.invoke(app, ["set", "colour", "red"])
    assert unknown.exit_code == 1 and "no setting `colour`" in unknown.output


# --- chord config show ---


def test_config_show_json_names_each_source(project, tmp_path):
    write(user_file(), 'harness = "claude"\ninterval = 30\n')
    write(project_file(project), 'label = "Robot"\n[routes]\nfast = "opencode:tiny"\n')
    repo = tmp_path / "lib"
    write(project_file(repo), 'harness = "codex"\n')

    shown = json.loads(runner.invoke(app, ["config", "show", "--json"]).output)
    assert shown["settings"]["harness"] == {
        "value": "claude",
        "source": "user",
        "file": str(user_file()),
    }
    assert shown["settings"]["label"]["source"] == "project"
    assert shown["settings"]["webhook_port"]["source"] == "default"
    assert shown["routes"]["fast"]["label"] == "Robot -> fast"
    assert [layer["source"] for layer in shown["layers"]] == ["user", "project"]

    with_repo = runner.invoke(app, ["config", "show", "--json", "--repo", str(repo)])
    assert json.loads(with_repo.output)["settings"]["harness"]["source"] == "repo"

    text = runner.invoke(app, ["config", "show"]).output
    assert re.search(r"harness\s+\"claude\"\s+\(user\)", text)


def test_config_show_hides_the_webhook_secret(project):
    write(user_file(), 'webhook_secret = "hunter2"\n')
    out = runner.invoke(app, ["config", "show", "--json"]).output
    assert "hunter2" not in out


def test_config_path_still_works(project):
    result = runner.invoke(app, ["config", "--path"])
    assert result.exit_code == 0
    assert result.output.strip().endswith("chord.toml")


# --- chord config migrate ---

OLD = """\
# my project
label = "Chord"
harness = "t3"
interval = 30
webhook_secret = "s3cret"

[notify]
received = false

[harnesses."big-pickle"]
command = ["opencode", "run", "--model", "big-pickle"]

[harnesses."opencode/tiny"]
command = ["opencode", "run", "--model", "tiny"]

[harnesses.mine]
command = ["./agent.sh"]

[t3]
provider = "openai"
model = "gpt-5"
worktree = "main"
"""


def test_migrate_shows_a_diff_then_moves_things(project):
    path = write(project_file(project), OLD)

    dry = runner.invoke(app, ["config", "migrate"], input="n\n")
    assert dry.exit_code == 1
    assert '-harness = "t3"' in dry.output
    assert path.read_text() == OLD, "a declined migration wrote something"

    done = runner.invoke(app, ["config", "migrate", "--yes"])
    assert done.exit_code == 0, done.output

    text = path.read_text()
    raw = tomllib.loads(text)
    assert "# my project" in text
    assert raw["version"] == 1
    assert raw["harness"] == "t3:openai/gpt-5@main"
    assert "t3" not in raw
    assert raw["routes"] == {"big-pickle": "opencode:big-pickle"}
    # A name with `/` keeps its label, and a command of your own stays.
    assert set(raw["harnesses"]) == {"opencode/tiny", "mine"}
    assert not set(raw) & config.MACHINE_KEYS

    user = tomllib.loads(user_file().read_text())
    assert user["interval"] == 30
    assert user["webhook_secret"] == "s3cret"
    assert user["notify"] == {"received": False}

    loaded = config.load(start=project)
    assert loaded.interval == 30
    assert loaded.warnings == ()

    again = runner.invoke(app, ["config", "migrate", "--yes"])
    assert "Nothing to migrate." in again.output


def test_migrate_keeps_a_t3_branch(project):
    path = write(
        project_file(project),
        'harness = "t3"\n[t3]\nworktree = "main"\nbranch = "chord/x"\n',
    )
    runner.invoke(app, ["config", "migrate", "--yes"])
    raw = tomllib.loads(path.read_text())
    assert raw["t3"]["branch"] == "chord/x"
    assert raw["harness"] == "t3"


# --- chord labels ---


class FakeLinear:
    created: ClassVar[list[str]] = []

    def __init__(self, token):
        pass

    async def label_names(self, prefix):
        return {"Chord", "Chord -> fast"}

    async def create_label(self, name):
        FakeLinear.created.append(name)


@pytest.fixture
def linear(monkeypatch):
    FakeLinear.created = []
    monkeypatch.setattr(cli, "Linear", FakeLinear)
    monkeypatch.setattr(cli, "_token", lambda: "token")
    return FakeLinear


def test_labels_is_a_dry_run_by_default(project, linear):
    write(project_file(project), '[routes]\nfast = "opencode"\nslow = "claude"\n')
    result = runner.invoke(app, ["labels"])

    assert result.exit_code == 0, result.output
    assert re.search(r"Chord -> fast\s+present", result.output)
    assert re.search(r"Chord -> slow\s+missing", result.output)
    assert linear.created == []


def test_labels_create_makes_only_the_missing_ones(project, linear):
    write(project_file(project), '[routes]\nfast = "opencode"\nslow = "claude"\n')
    result = runner.invoke(app, ["labels", "--create"])
    assert result.exit_code == 0, result.output
    assert linear.created == ["Chord -> slow"]


# --- the schema ---

jsonschema = pytest.importorskip("jsonschema")


def schema():
    return json.loads((ROOT / "schema" / "chord.v1.json").read_text())


def examples() -> list[tuple[str, str]]:
    """The stock file, with its examples on, and every TOML block in the README."""
    stock = config.STOCK
    on = stock.replace('# model = "big-pickle"', 'model = "big-pickle"').replace(
        '# allowed_actors = ["me"]', 'allowed_actors = ["me"]'
    )
    found = [("STOCK", stock), ("STOCK, examples on", on)]
    readme = (ROOT / "README.md").read_text()
    for index, block in enumerate(re.findall(r"```toml\n(.*?)```", readme, re.DOTALL)):
        found.append((f"README toml block {index + 1}", block))
    return found


@pytest.mark.parametrize(("where", "text"), examples())
def test_every_documented_example_matches_the_schema(where, text):
    jsonschema.validate(tomllib.loads(text), schema())


@pytest.mark.parametrize(
    "text",
    [
        "version = 2\n",
        "interval = 1\n",
        '[routes]\n"a/b" = "opencode"\n',
        '[routes]\nfast = "opencode:"\n',
        '[routes]\nfast = "nope"\n',
        'allowed_actors = ["Alice"]\n',
        "colour = 1\n",
    ],
)
def test_the_schema_refuses_what_chord_refuses(text, tmp_path):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(tomllib.loads(text), schema())
    path = write(tmp_path / "chord.toml", text)
    with pytest.raises(config.ConfigError):
        config.load(path)


def test_migrate_drops_a_project_id_instead_of_adopting_it(project):
    """Moving it would launder a cloned repository's "me" into the user file."""
    alice = "00000000-0000-0000-0000-000000000008"
    path = write(project_file(project), f'linear_user_id = "{alice}"\n')

    done = runner.invoke(app, ["config", "migrate", "--yes"])
    assert done.exit_code == 0, done.output

    assert "linear_user_id" not in tomllib.loads(path.read_text())
    user = user_file()
    if user.is_file():
        assert "linear_user_id" not in tomllib.loads(user.read_text())
