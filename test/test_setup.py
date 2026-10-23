"""`chord setup` walks a person from nothing to ready, and is safe to re-run."""

import tomllib

import pytest
from dotenv import dotenv_values
from typer.testing import CliRunner

from chord import cli, credentials, harness
from chord import setup as guided
from chord.cli import app

runner = CliRunner()


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(guided, "interactive", lambda: True)
    monkeypatch.setattr(guided, "_keep_out_of_git", lambda path, say: None)
    return tmp_path


@pytest.fixture
def signs_in(monkeypatch, keyring_backend):
    """A browser round trip that succeeds as one Linear user, storing the token."""
    calls = []

    async def fake(store):
        calls.append(store)
        creds = {
            "access_token": "a",
            "refresh_token": "r",
            "expires_at": 9e12,
            "viewer_id": "2b43e68a-b603-49e9-ad29-18a65b4dc59f",
            "viewer_name": "hx",
        }
        return credentials._store(creds)

    monkeypatch.setattr(credentials, "authenticate", fake)
    return calls


def tools(monkeypatch, *found):
    monkeypatch.setattr(
        harness.shutil, "which", lambda name: f"/bin/{name}" if name in found else None
    )


def config(project):
    return tomllib.loads((project / ".config" / "chord" / "chord.toml").read_text())


def test_enter_all_the_way_through_gives_a_working_setup(
    project, signs_in, monkeypatch
):
    tools(monkeypatch, "opencode")
    result = runner.invoke(app, ["setup"], input="id-1\nsecret-1\n\n")

    assert result.exit_code == 0, result.output
    env = dotenv_values(project / ".env")
    assert env["LINEAR_CLIENT_ID"] == "id-1"
    assert env["LINEAR_CLIENT_SECRET"] == "secret-1"
    assert "secret-1" not in result.output, "the secret was echoed"
    assert signs_in == [True]
    cfg = config(project)
    assert cfg["label"] == "Chord"
    assert cfg["harness"] == "opencode"
    assert cfg["allowed_actors"] == ["2b43e68a-b603-49e9-ad29-18a65b4dc59f"], (
        "the person signing in wasn't allowed"
    )
    assert "Create the label 'Chord' in Linear" in result.output
    assert "Run `chord start`" in result.output
    assert cfg["notify"] == {"received": True}, "a delivery banner is the default"
    assert "`received = false` under [notify]" in result.output


def test_the_tool_is_chosen_from_the_ones_installed(project, signs_in, monkeypatch):
    tools(monkeypatch, "opencode", "claude")
    (project / ".env").write_text('LINEAR_CLIENT_ID="i"\nLINEAR_CLIENT_SECRET="s"\n')
    result = runner.invoke(app, ["setup"], input="Build\n2\n")

    assert result.exit_code == 0, result.output
    cfg = config(project)
    assert cfg["label"] == "Build"
    assert cfg["harness"] == "claude", "the preset is written by name"
    assert "found in" in result.output, "asked for an app .env already had"


def test_running_it_again_skips_every_finished_step(project, signs_in, monkeypatch):
    tools(monkeypatch, "opencode")
    runner.invoke(app, ["setup"], input="id-1\nsecret-1\n\n")
    before = (project / ".config" / "chord" / "chord.toml").read_text()

    # "n" to signing in again is the only question left.
    result = runner.invoke(app, ["setup"], input="n\n")

    assert result.exit_code == 0, result.output
    assert signs_in == [True], "signed in again without being asked to"
    assert (project / ".config" / "chord" / "chord.toml").read_text() == before
    assert "Signed in as hx" in result.output


def test_an_existing_config_is_never_overwritten(project, signs_in, monkeypatch):
    tools(monkeypatch, "opencode")
    (project / ".env").write_text('LINEAR_CLIENT_ID="i"\nLINEAR_CLIENT_SECRET="s"\n')
    path = project / ".config" / "chord" / "chord.toml"
    path.parent.mkdir(parents=True)
    path.write_text('label = "Mine"\n')

    result = runner.invoke(app, ["setup"])

    assert result.exit_code == 0, result.output
    assert path.read_text() == 'label = "Mine"\n'


def test_a_failed_sign_in_is_one_line(project, monkeypatch, keyring_backend):
    (project / ".env").write_text('LINEAR_CLIENT_ID="i"\nLINEAR_CLIENT_SECRET="s"\n')

    async def refused(store):
        return credentials.FAILED, "Linear said no: access_denied"

    monkeypatch.setattr(credentials, "authenticate", refused)
    result = runner.invoke(app, ["setup"])

    assert result.exit_code == 1
    assert "Linear said no: access_denied" in result.output
    assert "Traceback" not in result.output


def test_without_a_terminal_it_only_signs_in(tmp_path, monkeypatch, signs_in):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(guided, "interactive", lambda: False)
    result = runner.invoke(app, ["setup"])

    assert result.exit_code == 0, result.output
    assert signs_in == [True]
    assert not (tmp_path / ".config").exists(), "wrote config nobody answered for"


def test_start_sets_chord_up_first_when_it_isnt(project, signs_in, monkeypatch):
    tools(monkeypatch, "opencode")
    started = []
    monkeypatch.setattr(cli.daemon, "start", lambda: started.append(1) or 4242)
    monkeypatch.setattr(cli, "_check_routes", lambda router: None)

    result = runner.invoke(app, ["start"], input="id-1\nsecret-1\n\n")

    assert "isn't set up yet" in result.output
    assert signs_in == [True]
    assert config(project)["allowed_actors"] == ["2b43e68a-b603-49e9-ad29-18a65b4dc59f"]
    assert started, result.output
