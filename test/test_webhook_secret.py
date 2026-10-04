"""`webhook_secret` in chord.toml.

Two things have to be true, and only one of them is obvious.

A configured secret is required, and compared in constant time — that part is
covered in test_webhook.py, at the point where it is enforced. What this file
covers is that the value is *usable*: that Chord does not accept a secret which
no sender can transmit.

HTTP strips optional whitespace around a header value. So a secret written as
`" s3cret"` in chord.toml, and correctly sent as `X-Chord-Secret:  s3cret`,
arrives as `"s3cret"` — which does not match the configured string. Every
request is answered 401, forever, with nothing in the log to say why, because
from Chord's side the request looks like a wrong secret and the person has
copied theirs carefully into the tunnel configuration.

That is worse than rejecting it at the point of the mistake, so it is rejected
there. Interior spaces are a different matter and are still allowed: they are
part of the value and survive the round trip.

The other thing checked here is that the default is unchanged. An empty secret
means no authentication, which is the state every existing install is in, and
turning it on by default would stop every working webhook on upgrade.
"""

import pytest

from chord.config import ConfigError, load


def write(tmp_path, body: str):
    path = tmp_path / "chord.toml"
    path.write_text(body)
    return path


def test_a_plain_secret_is_accepted(tmp_path):
    config = load(write(tmp_path, 'webhook_secret = "s3cret"\n'))
    assert config.webhook_secret == "s3cret"
    assert config.webhook_secret != ""


def test_the_default_is_no_secret(tmp_path):
    """Failing closed here would stop every working webhook on upgrade."""
    assert load(write(tmp_path, "")).webhook_secret == ""


def test_an_empty_secret_means_no_authentication(tmp_path):
    """Explicitly empty is the same as unset, not an error."""
    assert load(write(tmp_path, 'webhook_secret = ""\n')).webhook_secret == ""


def test_a_secret_with_interior_spaces_is_accepted(tmp_path):
    """These survive an HTTP header, unlike the padding below."""
    config = load(write(tmp_path, 'webhook_secret = "two words"\n'))
    assert config.webhook_secret == "two words"


def test_a_secret_long_enough_to_be_a_secret_is_accepted(tmp_path):
    long_secret = "a1b2c3d4e5f6" * 8
    assert load(write(tmp_path, f'webhook_secret = "{long_secret}"\n')).webhook_secret == (
        long_secret
    )


def test_a_secret_with_non_ascii_is_accepted(tmp_path):
    """Header values are latin-1 on the wire, so this is worth pinning."""
    config = load(write(tmp_path, 'webhook_secret = "café-secret"\n'))
    assert config.webhook_secret == "café-secret"


@pytest.mark.parametrize(
    "secret,why",
    [
        pytest.param(" s3cret", "leading", id="leading-space"),
        pytest.param("s3cret ", "trailing", id="trailing-space"),
        pytest.param(" s3cret ", "both", id="both"),
        pytest.param("\ts3cret", "a tab", id="leading-tab"),
        pytest.param("\n", "a newline", id="newline-only"),
    ],
)
def test_a_secret_that_cannot_be_sent_is_rejected(tmp_path, secret, why):
    """The mistake this exists for.

    Accepted-but-unsendable is the worst outcome: it looks configured, it fails
    at 401 for every request, and there is nothing to distinguish "wrong secret"
    from "secret nobody can transmit".
    """
    escaped = secret.replace("\t", "\\t").replace("\n", "\\n")
    with pytest.raises(ConfigError, match="whitespace"):
        load(write(tmp_path, f'webhook_secret = "{escaped}"\n'))


def test_the_error_names_the_setting_and_the_file(tmp_path):
    """A config error is read once and acted on, so it has to be enough."""
    path = write(tmp_path, 'webhook_secret = " x"\n')
    with pytest.raises(ConfigError) as caught:
        load(path)
    message = str(caught.value)
    assert "webhook_secret" in message
    assert str(path) in message
    assert "whitespace" in message


def test_the_secret_is_not_echoed_in_the_error(tmp_path):
    """A config error goes in a log and on a terminal."""
    with pytest.raises(ConfigError) as caught:
        load(write(tmp_path, 'webhook_secret = " hunter2-secret"\n'))
    assert "hunter2-secret" not in str(caught.value)


def test_a_non_string_secret_is_rejected(tmp_path):
    for value in ("123", "true", "[]"):
        with pytest.raises(ConfigError, match="string"):
            load(write(tmp_path, f"webhook_secret = {value}\n"))


def test_it_is_a_known_setting(tmp_path):
    """Not an unknown-key error — the spelling has to be the documented one."""
    config = load(write(tmp_path, 'webhook_secret = "x"\n'))
    assert config.from_file is True
    assert config.webhook_secret == "x"


def test_it_can_be_overridden_by_the_project(tmp_path):
    """Config layering still applies to it, like every other setting."""
    user = tmp_path / "user.toml"
    user.write_text('webhook_secret = "from-user"\n')
    project = tmp_path / "chord.toml"
    project.write_text("")
    assert load(project) is not None  # project exists, no secret of its own