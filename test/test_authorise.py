"""Who is allowed to trigger a hand-over.

Adding a `Chord` label runs a command on someone's machine. Until `allowed_actors`
existed, that was true for every account in the workspace, which is the same
authority as "can file an issue" — a much lower bar than "can run code here".

These tests pin the three properties that make it a control rather than a
formality: the match is on something the matchee cannot change, an unapproved
issue is skipped *and recorded* so it isn't re-examined forever, and nothing
identifying anybody reaches the harness.
"""

import asyncio
import json
import uuid

import pytest
from conftest import RecordingHarness

from chord import credentials, watcher
from chord.context import render
from chord.linear import Actor, IssuePage, LabelChange
from chord.routing import Router


def uuid_of(n: int) -> str:
    """A made-up but well-formed Linear user id.

    Built rather than written out, so there is one definition of the shape and
    no file full of literals that look like credentials to a secret scanner.
    """
    return str(uuid.UUID(int=n))


ALICE = Actor(id=uuid_of(1), name="Alice")
BOB = Actor(id=uuid_of(2), name="Bob")
# A display name is not an identity: Linear lets anyone rewrite their own, and
# nothing stops two people sharing one. So `allowed_actors` must never be keyed
# on it, and a test that pins that is worth more than a comment.
EVIL = Actor(id=uuid_of(3), name="Alice")


class StubLinear:
    def __init__(self, issues, history=None):
        self._issues = issues
        self._history = history or []
        self.history_asked: list[str] = []

    async def issues_for(self, filter):
        return IssuePage(self._issues, False)

    async def label_history(self, issue_id):
        self.history_asked.append(issue_id)
        return list(self._history)

    async def comments(self, issue_id):
        return []


def issue(n=1, creator=None, labels=("Chord",)):
    return {
        "id": str(n),
        "identifier": f"ENG-{n}",
        "title": "fix login",
        "description": "do the thing",
        "createdAt": "2026-01-01T00:00:00Z",
        "labels": {"nodes": [{"name": name} for name in labels]},
        "creator": creator,
    }


def build(tmp_path, stub, allowed=(), spy=None):
    spy = spy if spy is not None else RecordingHarness("spy")
    router = Router(
        "Chord",
        "spy",
        factory=lambda spec: spy,
        allowed_actors=tuple(allowed),
    )
    return (
        watcher.Watcher(stub, router, 1, tmp_path / "state.json"),
        spy,
    )


def history(*pairs):
    return [
        LabelChange(at=at, labels=list(names), who=who) for at, names, who in pairs
    ]


def run(w, times=1):
    for _ in range(times):
        asyncio.run(w.poll())


# --- the default: permissive, and it says so ---


def test_without_an_allowlist_everyone_is_allowed(tmp_path, capsys):
    """Unset is the state every existing install is in, so it cannot fail closed."""
    spy = RecordingHarness()
    w, spy = build(
        tmp_path,
        StubLinear([issue()], history(("2026-01-01", ("Chord",), EVIL))),
        spy=spy,
    )
    run(w)
    assert len(spy.prompts) == 1


def test_startup_says_the_allowlist_is_off(tmp_path, capsys):
    """Silence about this would read as 'everything is fine'."""
    import asyncio as aio

    spy = RecordingHarness()
    w, _ = build(tmp_path, StubLinear([]), spy=spy)

    async def once():
        task = aio.create_task(w.run())
        await aio.sleep(0.05)
        task.cancel()
        with pytest.raises(aio.CancelledError):
            await task

    aio.run(once())
    out = capsys.readouterr().out
    assert "anyone in the Linear workspace can trigger a run" in out
    assert "allowed_actors" in out


# --- refusing someone who is not allowed ---


def test_an_unapproved_actor_is_skipped_and_never_re_run(tmp_path, capsys):
    """Recorded, like every other hand-over that didn't happen.

    Left unrecorded it would be re-examined on every poll for the life of the
    watcher, which is how a refusal turns into log spam and a slow poll.
    """
    spy = RecordingHarness()
    stub = StubLinear([issue()], history(("2026-01-01", ("Chord",), BOB)))
    w, spy = build(tmp_path, stub, allowed=[ALICE.id], spy=spy)

    run(w, times=3)

    assert spy.prompts == [], "ran work for someone who is not allowed"
    assert w.handed_over == 1, "not recorded, so it would be re-examined forever"
    out = capsys.readouterr().out
    assert "Bob" in out, "the refusal must name who was refused"
    assert "allowed_actors" in out, "and must say what setting to change"


def test_a_refusal_says_how_to_retry_it(tmp_path, capsys):
    """Removing the label won't bring the issue back, so the log has to say so."""
    spy = RecordingHarness()
    w, _ = build(
        tmp_path,
        StubLinear([issue()], history(("2026-01-01", ("Chord",), BOB))),
        allowed=[ALICE.id],
        spy=spy,
    )
    run(w)
    out = capsys.readouterr().out
    assert "chord.toml" in out
    assert "state.json" in out, "the recovery path is the thing people look for"


def test_an_approved_actor_is_handed_over(tmp_path, capsys):
    spy = RecordingHarness()
    stub = StubLinear([issue()], history(("2026-01-01", ("Chord",), ALICE)))
    w, spy = build(tmp_path, stub, allowed=[ALICE.id], spy=spy)
    run(w)
    assert len(spy.prompts) == 1
    assert "Alice" in capsys.readouterr().out


def test_a_shared_display_name_is_not_enough(tmp_path):
    """The reason matching is on the id and not the name.

    `EVIL` is a different account that has set its Linear display name to
    "Alice". Matching on the name would hand this one the work.
    """
    spy = RecordingHarness()
    stub = StubLinear([issue()], history(("2026-01-01", ("Chord",), EVIL)))
    w, spy = build(tmp_path, stub, allowed=[ALICE.id], spy=spy)
    run(w)
    assert spy.prompts == [], "a display name was treated as an identity"


# --- attribution: who, and where the answer came from ---


def test_the_creator_stands_in_when_the_labeller_is_unknown(tmp_path, capsys):
    """The label-add has fallen off Linear's history page.

    The creator is a documented approximation rather than the real answer, so
    the log has to say which of the two was used.
    """
    spy = RecordingHarness()
    stub = StubLinear(
        [issue(creator={"id": ALICE.id, "name": "Alice"})],
        history(),  # readable, but mentions no route label
    )
    w, spy = build(tmp_path, stub, allowed=[ALICE.id], spy=spy)
    run(w)
    assert len(spy.prompts) == 1
    assert "created the issue" in capsys.readouterr().out


def test_the_creator_fallback_is_still_refused_when_not_allowed(tmp_path):
    spy = RecordingHarness()
    stub = StubLinear(
        [issue(creator={"id": BOB.id, "name": "Mallory"})],
        history(),
    )
    w, spy = build(tmp_path, stub, allowed=[ALICE.id], spy=spy)
    run(w)
    assert spy.prompts == []
    assert w.handed_over == 1


def test_an_unattributable_issue_is_refused(tmp_path, capsys):
    """No actor on the trail and no creator: nobody to check, so nobody gets run."""
    spy = RecordingHarness()
    stub = StubLinear([issue(creator=None)], history())
    w, spy = build(tmp_path, stub, allowed=[ALICE.id], spy=spy)
    run(w)
    assert spy.prompts == []
    assert w.handed_over == 1, "a refused issue must not be retried every poll"
    assert "allow" in capsys.readouterr().out.lower()


def test_a_label_added_by_an_integration_is_refused(tmp_path, capsys):
    """`actor` is null when a bot applied the label; `botActor` says which one.

    An integration applying `Chord` is code execution triggered by an automated
    rule, which is exactly the thing an allowlist exists to make deliberate. It
    is refused, and the log says it was a bot rather than "we couldn't tell".
    """
    spy = RecordingHarness()
    bot = Actor(id="33333333-3333-3333-3333-333333333333", name="linear-slack")
    stub = StubLinear([issue()], history(("2026-01-01", ("Chord",), bot)))
    w, spy = build(tmp_path, stub, allowed=[ALICE.id], spy=spy)
    run(w)
    assert spy.prompts == []
    assert "linear-slack" in capsys.readouterr().out


def test_an_unreadable_trail_falls_back_rather_than_failing_the_poll(tmp_path, capsys):
    from chord.linear import LinearError

    class Broken(StubLinear):
        async def label_history(self, issue_id):
            raise LinearError("nope")

    spy = RecordingHarness()
    stub = Broken([issue(creator={"id": ALICE.id, "name": "Alice"})])
    w, spy = build(tmp_path, stub, allowed=[ALICE.id], spy=spy)
    run(w)
    assert len(spy.prompts) == 1, "an unreadable trail must not stop the work"
    assert "creator" in capsys.readouterr().out


def test_an_unreadable_trail_does_not_escape_as_an_internal_error(tmp_path, capsys):
    """Anything but LinearError used to escape, which meant never delivered."""

    class Broken(StubLinear):
        async def label_history(self, issue_id):
            raise KeyError("data")

    spy = RecordingHarness()
    w, spy = build(
        tmp_path,
        Broken([issue(creator={"id": ALICE.id, "name": "Alice"})]),
        allowed=[ALICE.id],
        spy=spy,
    )
    run(w)
    assert len(spy.prompts) == 1
    assert "internal error" not in capsys.readouterr().out


# --- cost ---


def test_history_is_not_read_when_there_is_no_allowlist_and_no_choice(tmp_path):
    """The optimisation that keeps the default path's round trips down."""
    spy = RecordingHarness()
    stub = StubLinear([issue()])
    w, spy = build(tmp_path, stub, spy=spy)
    run(w)
    assert stub.history_asked == []
    assert len(spy.prompts) == 1


def test_history_is_read_for_an_unambiguous_issue_when_it_matters(tmp_path):
    """With an allowlist the actor is needed even when the route is not in doubt."""
    spy = RecordingHarness()
    stub = StubLinear([issue()], history(("2026-01-01", ("Chord",), ALICE)))
    w, spy = build(tmp_path, stub, allowed=[ALICE.id], spy=spy)
    run(w)
    assert stub.history_asked == ["1"]
    assert len(spy.prompts) == 1


# --- what must never leave this machine ---


def test_no_email_or_user_id_reaches_the_harness(tmp_path):
    """The prompt goes to an agent. It has no business carrying your roster.

    Chord is read-only against Linear and writes nothing back, so the harness is
    the only place this data could leak out of — and a harness is a third-party
    CLI that may be pointed at a hosted model.
    """
    spy = RecordingHarness()
    stub = StubLinear(
        [issue(creator={"id": ALICE.id, "name": "Alice", "email": "alice@example.com"})],
        history(("2026-01-01", ("Chord",), ALICE)),
    )
    w, spy = build(tmp_path, stub, allowed=[ALICE.id], spy=spy)
    run(w)
    prompt = spy.prompts[0]
    assert "example.com" not in prompt
    assert ALICE.id not in prompt
    assert BOB.id not in prompt


def test_render_ignores_creator_entirely():
    """Belt and braces: the renderer never had a creator field to leak."""
    out = render({"id": "1", "identifier": "E-1",
                  "creator": {"id": ALICE.id, "email": "alice@example.com"}})
    assert "example.com" not in out
    assert ALICE.id not in out


# --- the setting itself ---


def test_the_allowlist_is_a_list_of_user_ids_not_names_or_emails(tmp_path):
    """Rejected at load, so a typo is one line rather than a silent refusal."""
    from chord import config

    for value, because in [
        ('allowed_actors = ["alice@example.com"]', "email"),
        ('allowed_actors = ["Alice"]', "display name"),
        (f'allowed_actors = "{uuid_of(1)}"', "has to be a list"),
        ('allowed_actors = [42]', "isn't one"),
    ]:
        path = tmp_path / "chord.toml"
        path.write_text(value)
        with pytest.raises(config.ConfigError) as caught:
            config.load(path)
        assert because in str(caught.value), f"{value}: {caught.value}"
        assert "chord info" in str(caught.value), "the error must say how to find yours"


def test_the_allowlist_normalises_and_dedupes(tmp_path):
    from chord import config

    path = tmp_path / "chord.toml"
    path.write_text(
        'allowed_actors = [\n'
        f'  "{uuid_of(2).upper()}",\n'
        f'  "{uuid_of(1)}",\n'
        f'  "{uuid_of(2)}",\n'
        "]\n"
    )
    assert config.load(path).allowed_actors == (uuid_of(1), uuid_of(2))


def test_no_allowlist_means_permissive_and_says_so(tmp_path):
    from chord import config

    cfg = config.load(tmp_path / "nothing.toml")
    assert cfg.allowed_actors == ()
    assert cfg.authorises is False


def test_an_allowlist_turns_the_check_on(tmp_path):
    from chord import config

    path = tmp_path / "chord.toml"
    path.write_text(f'allowed_actors = ["{ALICE.id}"]\n')
    cfg = config.load(path)
    assert cfg.authorises is True
    assert Router("Chord", "spy", allowed_actors=cfg.allowed_actors).authorises is True


def test_matching_is_case_insensitive_because_uuids_are_written_in_caps(tmp_path):
    router = Router("Chord", "spy", allowed_actors=(ALICE.id,))
    assert router.authorises_actor(ALICE.id.upper()) is True
    assert router.authorises_actor(ALICE.id) is True


def test_router_refuses_when_it_cannot_tell_who_asked():
    """A permissive fallback here would turn the control off silently."""
    router = Router("Chord", "spy", allowed_actors=(ALICE.id,))
    assert router.authorises_actor(None) is False
    assert router.authorises_actor("") is False
    assert router.authorises_actor(BOB.id) is False


# --- the id has to be obtainable, or the setting is unusable ---


def test_chord_info_prints_your_linear_user_id(stored_token, no_env, monkeypatch, tmp_path):
    """Nobody knows their Linear user id. Linear's UI does not show it.

    So if `chord info` did not print it, an id-keyed allowlist could not be
    written down at all.
    """
    from typer.testing import CliRunner

    from chord import daemon
    from chord.cli import app

    stored_token(viewer_id=ALICE.id, viewer_name="Alice")
    (tmp_path / "chord.toml").write_text(f'allowed_actors = ["{ALICE.id}"]\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(daemon, "PID_FILE", tmp_path / "absent.pid")
    monkeypatch.setattr(daemon, "STATE_FILE", tmp_path / "absent.json")

    result = CliRunner().invoke(app, ["info"])
    assert result.exit_code == 0, result.output
    assert ALICE.id in result.output
    assert "1 Linear user id" in result.output


def test_chord_info_warns_when_the_allowlist_is_off(keyring_backend, no_env, monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from chord import daemon
    from chord.cli import app

    keyring_backend.set_password(
        "chord", "linear", json.dumps({"access_token": "a", "obtained_at": 0})
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(daemon, "PID_FILE", tmp_path / "absent.pid")
    monkeypatch.setattr(daemon, "STATE_FILE", tmp_path / "absent.json")

    result = CliRunner().invoke(app, ["info"])
    assert "anyone in the workspace can trigger a run" in result.output


def test_a_refresh_keeps_the_recorded_user_id(stored_token, no_env, monkeypatch):
    """Otherwise one `chord refresh` would silently wipe it.

    Linear's token response says nothing about who asked, so the field has to be
    carried across the exchange by hand.
    """
    stored_token(viewer_id=ALICE.id, viewer_name="Alice")

    async def fake_exchange(*a, **k):
        return {"access_token": "new", "refresh_token": "new-refresh"}

    monkeypatch.setattr(
        "authlib.integrations.httpx_client.AsyncOAuth2Client.refresh_token",
        fake_exchange,
    )
    monkeypatch.setattr(credentials, "_client_secrets", lambda: ("id", "secret"))

    code, stored = asyncio.run(credentials.refresh())
    assert code == credentials.OK
    assert stored["viewer_id"] == ALICE.id
    assert stored["viewer_name"] == "Alice"


def test_viewer_id_is_none_before_setup_and_never_raises():
    assert credentials.viewer_id({}) is None
    assert credentials.viewer_id("not a dict") is None
    assert credentials.viewer_id({"viewer_id": "  "}) is None


# --- the default route is not special-cased ---


def test_a_narrowed_route_is_authorised_by_who_added_it(tmp_path):
    """`Chord` was added first, `Chord/claude` second, by someone not allowed.

    The newest label is the one that routes, so it is also the one that
    authorises. Checking the bare `Chord` instead would let anyone who could
    label an issue narrow it onto a harness they picked.
    """
    spy = RecordingHarness()
    stub = StubLinear(
        [issue(labels=("Chord", "Chord/claude"))],
        history(
            ("2026-03-01", ("Chord/claude",), BOB),
            ("2026-01-01", ("Chord",), ALICE),
        ),
    )
    w = watcher.Watcher(
        stub,
        Router("Chord", "spy", {"claude": "stub"}, factory=lambda s: spy,
               allowed_actors=(ALICE.id,)),
        1,
        tmp_path / "state.json",
    )
    run(w)
    assert spy.prompts == [], "the newest label's author is the one who counts"
    assert w.handed_over == 1
