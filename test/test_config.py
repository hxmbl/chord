"""Config parsing and credential freshness, including the paths each of them
resolves through."""

import time

import pytest

from chord import config, credentials

# --- config ---


def test_defaults_run_with_no_file(tmp_path):
    cfg = config.load(tmp_path / "nothing.toml")
    assert cfg.label == "Chord"
    assert cfg.harness == "opencode"  # default built-in harness
    assert cfg.interval == 60
    assert cfg.from_file is False


def test_reads_a_config(tmp_path):
    path = tmp_path / "chord.toml"
    path.write_text('label = "ready"\nharness = ["claude", "-p"]\ninterval = 15\n')
    cfg = config.load(path)
    assert (cfg.label, cfg.harness, cfg.interval) == ("ready", ["claude", "-p"], 15)
    assert cfg.from_file is True


def test_project_config_uses_hidden_config_directory(tmp_path, monkeypatch):
    path = tmp_path / ".config" / "chord" / "chord.toml"
    path.parent.mkdir(parents=True)
    path.write_text('label = "Ready"\n')
    monkeypatch.chdir(tmp_path)
    cfg = config.load()
    assert cfg.path == path
    assert cfg.label == "Ready"


def test_unknown_setting_is_rejected(tmp_path):
    """A misspelling would otherwise be silently ignored."""
    path = tmp_path / "chord.toml"
    path.write_text("labl = 'typo'\n")
    with pytest.raises(config.ConfigError, match="doesn't know"):
        config.load(path)


def test_interval_floor_is_enforced(tmp_path):
    path = tmp_path / "chord.toml"
    path.write_text("interval = 1\n")
    with pytest.raises(config.ConfigError, match="floor"):
        config.load(path)


def test_interval_must_be_a_whole_number(tmp_path):
    path = tmp_path / "chord.toml"
    path.write_text("interval = 1.5\n")
    with pytest.raises(config.ConfigError, match="whole number"):
        config.load(path)


def test_bool_is_not_an_interval(tmp_path):
    """`isinstance(True, int)` is True in Python."""
    path = tmp_path / "chord.toml"
    path.write_text("interval = true\n")
    with pytest.raises(config.ConfigError):
        config.load(path)


def test_empty_label_is_rejected(tmp_path):
    path = tmp_path / "chord.toml"
    path.write_text('label = "   "\n')
    with pytest.raises(config.ConfigError, match="non-empty"):
        config.load(path)


def test_harness_must_be_a_name_or_command(tmp_path):
    path = tmp_path / "chord.toml"
    path.write_text("harness = 42\n")
    with pytest.raises(config.ConfigError, match="harness"):
        config.load(path)


def test_nested_array_harness_is_rejected(tmp_path):
    path = tmp_path / "chord.toml"
    path.write_text('harness = ["claude", ["nested"]]\n')
    with pytest.raises(config.ConfigError):
        config.load(path)


def test_harness_name_is_human_readable(tmp_path):
    path = tmp_path / "chord.toml"
    path.write_text('harness = ["claude", "-p"]\n')
    assert config.load(path).harness_name == "claude -p"


# --- credential freshness ---


def test_load_accepts_a_fresh_token(stored_token, no_env):
    stored_token()
    code, content = credentials.load()
    assert code == credentials.OK
    assert content["access_token"] == "access"


def test_load_rejects_a_stale_token(stored_token, no_env):
    stored_token(obtained_at=time.time() - 48 * 3600)
    assert credentials.load()[0] == credentials.FAILED


def test_read_ignores_age(stored_token, no_env):
    """`read` is the gate-free half, which is what `refresh` needs."""
    stored_token(obtained_at=time.time() - 48 * 3600)
    code, content = credentials.read()
    assert code == credentials.OK
    assert content["refresh_token"] == "refresh"


def test_max_age_comes_from_a_parent_env(tmp_path, monkeypatch):
    """The CWD-relative `.env` was why a subdirectory saw different settings."""
    (tmp_path / ".env").write_text("CHORD_MAX_AGE=60\n")
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    assert credentials.max_age() == 60


def test_max_age_falls_back_to_the_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert credentials.max_age() == credentials.DEFAULT_MAX_AGE


def test_max_age_ignores_a_nonsense_value(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("CHORD_MAX_AGE=not-a-number\n")
    monkeypatch.chdir(tmp_path)
    assert credentials.max_age() == credentials.DEFAULT_MAX_AGE


def test_obtained_at_falls_back_to_expiry(tmp_path):
    """Tokens stored before we kept a stamp still know when they expire."""
    expires = time.time()
    assert (
        credentials.obtained_at({"expires_at": expires})
        == expires - credentials.LINEAR_TTL
    )


def test_obtained_at_is_none_without_anything(tmp_path):
    assert credentials.obtained_at({}) is None


def test_corrupt_keychain_entry_is_a_message(stored_token, keyring_backend, no_env):
    keyring_backend.set_password("chord", "linear", "not json")
    code, content = credentials.load()
    assert code == credentials.FAILED
    assert "isn't JSON" in str(content)


def test_missing_keychain_entry_is_a_message(no_env):
    code, content = credentials.read()
    assert code == credentials.FAILED
    assert "chord setup" in str(content)
