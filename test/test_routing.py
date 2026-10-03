"""The label grammar and the curation table, which together decide who works.

STRM-201 and STRM-200 both live here: a label that names a harness, and an
issue that carries more than one of them.
"""

import asyncio

import pytest
from conftest import RecordingHarness

from chord import config, harness
from chord.harness import HarnessError
from chord.linear import IssuePage
from chord.routing import DEFAULT_ROUTE, Router, UnknownRoute, route_label, route_of

# --- the grammar ---


@pytest.mark.parametrize(
    "candidate,expected",
    [
        ("Chord", DEFAULT_ROUTE),
        ("Chord/opencode", "opencode"),
        # The whole suffix is the name, so nesting is just a character in it.
        ("Chord/opencode/space-bunny-free", "opencode/space-bunny-free"),
        ("Chord/a/b/c/d", "a/b/c/d"),
        ("Chord/two words", "two words"),
        ("Chord/dots.and-dashes_1", "dots.and-dashes_1"),
    ],
)
def test_a_route_label_names_a_harness(candidate, expected):
    assert route_of("Chord", candidate) == expected


@pytest.mark.parametrize(
    "candidate",
    [
        "Chordle",  # a longer word that merely starts the same
        "Chordle/route",
        "chord",  # Linear label names are case sensitive
        "Something/Chord",
        "ChordX",
        "Chord ",  # trailing space is a different label
        "",
        # Typos rather than routes. Curation rejects these names too, so they
        # can't be curated into matching.
        "Chord/",
        "Chord//x",
        "Chord/x/",
    ],
)
def test_something_that_is_not_a_route_label_is_not_one(candidate):
    assert route_of("Chord", candidate) is None


def test_a_custom_trigger_label_keeps_its_own_routes():
    """`label` is the base, so the grammar follows whatever it is set to."""
    assert route_of("bot", "bot") == DEFAULT_ROUTE
    assert route_of("bot", "bot/typo") == "typo"
    assert route_of("bot", "Chord/typo") is None


def test_route_label_spellings_round_trip():
    for name in ("", "opencode", "opencode/space-bunny-free"):
        assert route_of("Chord", route_label("Chord", name)) == name


# --- the poll filter ---


def test_the_filter_catches_the_bare_label_and_any_route():
    """Both halves matter: a curated route is `<label>/<name>`."""
    assert Router("Chord", "print").filter() == {
        "or": [
            {"labels": {"name": {"eq": "Chord"}}},
            {"labels": {"name": {"startsWith": "Chord/"}}},
        ]
    }


def test_the_filter_prefix_cannot_run_into_unrelated_labels():
    """`startsWith: "Chord"` would match `Chordle` and `Chord-V2`."""
    halves = Router("Chord", "print").filter()["or"]
    assert halves[1] == {"labels": {"name": {"startsWith": "Chord/"}}}


def test_the_filter_does_not_depend_on_how_many_harnesses_are_curated():
    """One request covers every route, so curation doesn't cost round trips."""
    bare = Router("Chord", "print")
    loaded = Router("Chord", "print", {str(n): "cat" for n in range(50)})
    assert bare.filter() == loaded.filter()


# --- reading an issue's labels ---


def labels(*names):
    return {"labels": {"nodes": [{"name": n} for n in names]}}


def test_candidates_find_every_route_on_an_issue():
    router = Router("Chord", "print")
    issue = labels("Chord", "Chord/opencode", "Chord/opencode/tiny")
    assert router.candidates(issue) == ["opencode/tiny", "opencode", DEFAULT_ROUTE]


def test_candidates_are_most_specific_first():
    """The fallback order when Linear's audit trail can't be read."""
    router = Router("Chord", "print")
    assert router.candidates(labels("Chord", "Chord/a")) == ["a", DEFAULT_ROUTE]


def test_candidates_ignore_labels_that_arent_routes():
    router = Router("Chord", "print")
    assert router.candidates(labels("Bug", "Research", "Chordled")) == []


def test_a_duplicate_route_label_is_only_one_candidate():
    router = Router("Chord", "print")
    assert router.candidates(labels("Chord/opencode", "Chord/opencode")) == ["opencode"]


def test_candidates_survive_a_label_node_that_is_not_a_dict():
    """Linear is the other end of this, and it is allowed to surprise us."""
    router = Router("Chord", "print")
    issue = {"labels": {"nodes": [None, "opencode", {"name": "Chord/opencode"}]}}
    assert router.candidates(issue) == ["opencode"]


def test_candidates_survive_an_issue_with_no_labels_at_all():
    assert Router("Chord", "print").candidates({}) == []
    assert Router("Chord", "print").candidates({"labels": None}) == []
    assert Router("Chord", "print").candidates({"labels": {"nodes": []}}) == []


# --- resolving a route to a harness ---


def test_the_default_route_runs_the_configured_harness():
    router = Router("Chord", "print")
    assert router.harness_for(DEFAULT_ROUTE) is router.harness_for("")


def test_a_curated_route_runs_its_own_command():
    built: list[str] = []
    router = Router(
        "Chord",
        "print",
        {"opencode/tiny": ["opencode", "run", "--model", "tiny"]},
        factory=lambda spec: built.append(spec) or RecordingHarness(),  # type: ignore[func-returns-value]
    )
    router.harness_for("opencode/tiny")
    assert built == [["opencode", "run", "--model", "tiny"]]


def test_an_uncured_route_is_refused_by_name():
    with pytest.raises(UnknownRoute, match="typo-here"):
        Router("Chord", "print").harness_for("typo-here")


def test_a_curated_name_does_not_shadow_the_default():
    """Two namespaces: `Chord/foo` is the curated `foo`, `Chord` is `harness`."""
    seen = []
    router = Router(
        "Chord",
        "the-default",
        {"opencode": "the-curated"},
        factory=lambda spec: seen.append(spec) or RecordingHarness(),  # type: ignore[func-returns-value]
    )
    router.harness_for(DEFAULT_ROUTE)
    router.harness_for("opencode")
    assert seen == ["the-default", "the-curated"]


def test_a_route_is_built_once():
    """One PATH lookup per route per watcher, not one per issue."""
    built = []

    def factory(spec):
        built.append(spec)
        return RecordingHarness()

    router = Router("Chord", "print", {"x": "cat"}, factory=factory)
    first = router.harness_for("x")
    assert router.harness_for("x") is first
    assert built == ["cat"]


def test_a_broken_curated_command_is_reported_with_its_label():
    """`chord start` needs to say which label led to the bad command."""
    router = Router("Chord", "print", {"nope": "definitely-not-installed-xyz"})
    with pytest.raises(HarnessError, match="Chord/nope"):
        router.validate()


def test_validation_says_nothing_when_every_route_can_run():
    Router("Chord", "print", {"print": "print"}).validate()


def test_routes_lists_the_default_first_then_the_curated_ones_by_name():
    router = Router("Chord", "opencode", {"zebra": "cat", "alpha": ["claude", "-p"]})
    assert [(r.label, r.name, r.spelling) for r in router.routes()] == [
        ("Chord", "", "opencode"),
        ("Chord/alpha", "alpha", "claude -p"),
        ("Chord/zebra", "zebra", "cat"),
    ]


def test_a_router_with_nothing_curated_still_has_one_route():
    assert [r.label for r in Router("Chord", "opencode").routes()] == ["Chord"]


# --- config: the curation table ---


def test_curated_harnesses_are_read(tmp_path):
    path = tmp_path / "chord.toml"
    path.write_text(
        'harness = "print"\n'
        '[harnesses."opencode/tiny"]\ncommand = ["opencode", "run", "--model", "tiny"]\n'
        "[harnesses.claude]\ncommand = 'claude -p'\n"
    )
    cfg = config.load(path)
    assert cfg.harnesses == {
        "opencode/tiny": ["opencode", "run", "--model", "tiny"],
        "claude": ["claude", "-p"],
    }
    assert cfg.harness == "print"


def test_no_curated_harnesses_by_default(tmp_path):
    assert config.load(tmp_path / "absent.toml").harnesses == {}


@pytest.mark.parametrize(
    "body,message",
    [
        ("[harnesses.a]\nnope = 1\n", "doesn't know"),
        ("[harnesses.a]\n", "missing `command`"),
        ('[harnesses.a]\ncommand = "x"\nextra = 2\n', "doesn't know"),
        ("[harnesses]\na = 1\n", "has to be a table"),
        ("[harnesses.a]\ncommand = 42\n", "command"),
        ('[harnesses.""]\ncommand = "x"\n', "non-empty"),
        ('[harnesses."/lead"]\ncommand = "x"\n', "non-empty"),
        ('[harnesses."trail/"]\ncommand = "x"\n', "non-empty"),
        ('[harnesses."a//b"]\ncommand = "x"\n', "non-empty"),
        ('[harnesses." pad "]\ncommand = "x"\n', "non-empty"),
    ],
)
def test_a_bad_curation_entry_names_the_problem(tmp_path, body, message):
    path = tmp_path / "chord.toml"
    path.write_text(body)
    with pytest.raises(config.ConfigError, match=message):
        config.load(path)


def test_a_curated_harness_without_arguments_is_a_bare_name(tmp_path):
    path = tmp_path / "chord.toml"
    path.write_text('[harnesses.claude]\ncommand = "claude"\n')
    assert config.load(path).harnesses == {"claude": "claude"}


def test_a_trigger_label_must_not_end_in_the_separator(tmp_path):
    """`label = "Chord/"` would make every route `Chord//<name>`."""
    path = tmp_path / "chord.toml"
    path.write_text('label = "Chord/"\n')
    with pytest.raises(config.ConfigError, match="double the separator"):
        config.load(path)


def test_a_trigger_label_may_contain_the_separator(tmp_path):
    """`label = "team/Chord"` routes under `team/Chord/<name>`, which is fine."""
    path = tmp_path / "chord.toml"
    path.write_text('label = "team/Chord"\n')
    cfg = config.load(path)
    assert cfg.label == "team/Chord"
    assert [
        r.label for r in Router(cfg.label, cfg.harness, cfg.harnesses).routes()
    ] == ["team/Chord"]


# --- the watcher picks a route ---


class StubLinear:
    def __init__(self, issues, history=None, history_error=None):
        self._issues = issues
        self._history = history or []
        self._error = history_error
        self.history_asked: list[str] = []

    async def issues_for(self, filter):
        return IssuePage(self._issues, False)

    async def label_history(self, issue_id):
        self.history_asked.append(issue_id)
        if self._error:
            raise self._error
        return list(self._history)

    async def comments(self, issue_id):
        return []


def issue(n, *label_names):
    return {
        "id": str(n),
        "identifier": f"ENG-{n}",
        "title": f"Issue {n}",
        "description": f"do {n}",
        "createdAt": f"2026-01-0{n}T00:00:00Z",
        "labels": {"nodes": [{"name": name} for name in label_names]},
    }


def build(tmp_path, linear_stub, spy, harnesses):
    from chord import watcher

    return watcher.Watcher(
        linear_stub,
        Router("Chord", "print", harnesses, factory=lambda spec: spy),
        1,
        tmp_path / "state.json",
    )


CURATED = {
    "opencode": ["opencode"],
    "opencode/space-bunny-free": ["opencode", "run", "--model", "space-bunny-free"],
}


def test_the_newest_route_label_is_the_one_that_runs(tmp_path, capsys):
    """The example from the issue: `Chord/opencode/space-bunny-free`."""
    spy = RecordingHarness()
    stub = StubLinear(
        [issue(1, "Chord", "Chord/opencode", "Chord/opencode/space-bunny-free")],
        history=["Chord/opencode/space-bunny-free", "Chord/opencode", "Chord"],
    )
    asyncio.run(build(tmp_path, stub, spy, CURATED).poll())

    assert len(spy.prompts) == 1
    out = capsys.readouterr().out
    assert "Chord/opencode/space-bunny-free" in out
    # The default's label is in the history but must not be what ran.
    assert spy.prompts[0].count("do 1") == 1


def test_history_decides_which_of_several_labels_is_newest(tmp_path, capsys):
    """Not "most specific": the newest label added, whatever it happens to be."""
    spy = RecordingHarness()
    stub = StubLinear(
        [issue(1, "Chord", "Chord/opencode")], history=["Chord/opencode", "Chord"]
    )
    asyncio.run(build(tmp_path, stub, spy, CURATED).poll())

    out = capsys.readouterr().out
    assert "route Chord/opencode" in out
    assert "route Chord\n" not in out, "the bare trigger label won a narrowed issue"


def test_a_bare_trigger_label_loses_to_a_narrower_older_one(tmp_path, capsys):
    """Narrowing is the normal direction of travel, so it must be respected."""
    spy = RecordingHarness()
    stub = StubLinear(
        [issue(1, "Chord", "Chord/opencode")],
        # `Chord` added after `Chord/opencode`: the issue was handed back.
        history=["Chord", "Chord/opencode"],
    )
    asyncio.run(build(tmp_path, stub, spy, CURATED).poll())

    out = capsys.readouterr().out
    assert "route      Chord/opencode" not in out
    assert len(spy.prompts) == 1, "the issue should still have been worked on"


def test_history_is_only_read_when_there_is_a_choice(tmp_path):
    """A round trip per issue is a real cost for a decision nobody is making."""
    spy = RecordingHarness()
    stub = StubLinear([issue(1, "Chord"), issue(2, "Chord/opencode")])
    asyncio.run(build(tmp_path, stub, spy, CURATED).poll())

    assert stub.history_asked == [], "asked Linear about an unambiguous issue"
    assert len(spy.prompts) == 2


def test_an_unreadable_history_falls_back_to_the_most_specific(tmp_path, capsys):
    from chord.linear import LinearError

    spy = RecordingHarness()
    stub = StubLinear(
        [issue(1, "Chord", "Chord/opencode")], history_error=LinearError("gone")
    )
    asyncio.run(build(tmp_path, stub, spy, CURATED).poll())

    out = capsys.readouterr().out
    assert "label history" in out, "the fallback was silent"
    assert "Chord/opencode" in out
    assert len(spy.prompts) == 1, "an unreadable history stopped the work"


def test_history_that_mentions_no_route_falls_back_too(tmp_path, capsys):
    """A route applied long enough ago to be off the history page."""
    spy = RecordingHarness()
    stub = StubLinear(
        [issue(1, "Chord", "Chord/opencode")], history=["Bug", "Research"]
    )
    asyncio.run(build(tmp_path, stub, spy, CURATED).poll())

    assert len(spy.prompts) == 1
    assert "route Chord/opencode" in capsys.readouterr().out


def test_an_uncured_route_is_skipped_and_says_which_label(tmp_path, capsys):
    """Not run on the default: doing the work wrong is worse than not."""
    spy = RecordingHarness()
    stub = StubLinear(
        [issue(1, "Chord", "Chord/typo-here")], history=["Chord/typo-here"]
    )
    w = build(tmp_path, stub, spy, CURATED)
    asyncio.run(w.poll())

    out = capsys.readouterr().out
    assert spy.prompts == [], "ran the issue on a harness nobody asked for"
    assert "Chord/typo-here" in out
    assert "typo-here" in out
    assert w.handed_over == 1, "the issue behind it will never be reached"


def test_an_uncured_route_says_how_to_retry_it(tmp_path, capsys):
    spy = RecordingHarness()
    stub = StubLinear([issue(1, "Chord/nope")])
    asyncio.run(build(tmp_path, stub, spy, CURATED).poll())
    out = capsys.readouterr().out
    assert "Chord/nope" in out
    # The issue is recorded, so removing the label won't bring it back. Saying
    # so is the difference between a message someone can act on and one that
    # sends them round in circles.
    assert '[harnesses."nope"]' in out
    assert "state.json" in out


def test_an_uncured_route_line_is_not_truncated(tmp_path, capsys):
    """`one_line` cuts at 200 characters, which is where the fix would land."""
    spy = RecordingHarness()
    stub = StubLinear([issue(1, "Chord/some-reasonably-long-route-name")])
    asyncio.run(build(tmp_path, stub, spy, CURATED).poll())
    out = capsys.readouterr().out
    assert "state.json" in out
    assert "…" not in out, "the message was cut short"


def test_a_bad_route_does_not_wedge_the_queue_behind_it(tmp_path):
    spy = RecordingHarness()
    stub = StubLinear([issue(1, "Chord/nope"), issue(2, "Chord")])
    asyncio.run(build(tmp_path, stub, spy, CURATED).poll())
    assert len(spy.prompts) == 1
    assert "do 2" in spy.prompts[0]


def test_a_curated_route_that_stops_being_runnable_is_recorded(tmp_path, capsys):
    """A command that was on the PATH when Chord started and isn't any more.

    Building a harness raises `HarnessError`, which is the same class of event
    as an unknown label. Letting it escape would leave the issue unrecorded, so
    every poll would retry it and log an internal error for the life of the
    watcher.
    """
    from chord import watcher as w
    from chord.harness import HarnessError

    built = []

    def factory(spec):
        if spec == "print":
            built.append(spec)
            return RecordingHarness()
        raise HarnessError("`opencode` isn't on your PATH.")

    stub = StubLinear([issue(1, "Chord/opencode")])
    runner = w.Watcher(
        stub,
        Router("Chord", "print", CURATED, factory=factory),
        1,
        tmp_path / "state.json",
    )
    asyncio.run(runner.poll())

    out = capsys.readouterr().out
    assert built == [], "the default should not have been asked to do the work"
    assert "didn't finish" in out, "reported as something other than a failure"
    assert "Chord/opencode" in out, "the route label was not named"
    assert runner.handed_over == 1, "an unrecorded route is retried on every poll"


def test_a_curated_route_that_cannot_run_does_not_wedge_the_queue(tmp_path):
    from chord import watcher as w
    from chord.harness import HarnessError

    spy = RecordingHarness()

    def factory(spec):
        if spec == "print":
            return spy
        raise HarnessError("`opencode` isn't on your PATH.")

    stub = StubLinear([issue(1, "Chord/opencode"), issue(2, "Chord")])
    runner = w.Watcher(
        stub,
        Router("Chord", "print", CURATED, factory=factory),
        1,
        tmp_path / "state.json",
    )
    asyncio.run(runner.poll())

    assert len(spy.prompts) == 1
    assert "do 2" in spy.prompts[0]


def test_an_issue_with_no_route_label_says_so_and_uses_the_default(tmp_path, capsys):
    """The filter should have matched on one; if it didn't, don't guess quietly."""
    spy = RecordingHarness()
    stub = StubLinear([issue(1, "Bug")])
    asyncio.run(build(tmp_path, stub, spy, CURATED).poll())

    out = capsys.readouterr().out
    assert len(spy.prompts) == 1
    assert "no 'Chord' label" in out


def test_the_default_route_is_not_logged_on_every_issue(tmp_path, capsys):
    """`run()` already said what the default is. Repeating it is noise."""
    spy = RecordingHarness()
    stub = StubLinear([issue(1, "Chord")])
    asyncio.run(build(tmp_path, stub, spy, CURATED).poll())
    assert "route " not in capsys.readouterr().out


def test_startup_says_which_routes_are_available(tmp_path, capsys):
    from chord import watcher

    spy = RecordingHarness()
    stub = StubLinear([])
    w = watcher.Watcher(
        stub,
        Router("Chord", "opencode", CURATED, factory=lambda s: spy),
        60,
        tmp_path / "state.json",
    )

    async def once():
        task = asyncio.create_task(w.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(once())
    out = capsys.readouterr().out
    assert "issues labelled 'Chord'" in out
    assert "Chord/opencode -> opencode" in out
    assert "Chord/opencode/space-bunny-free -> opencode run --model" in out


def test_startup_admits_when_nothing_is_curated(tmp_path, capsys):
    from chord import watcher

    spy = RecordingHarness()
    w = watcher.Watcher(
        StubLinear([]),
        Router("Chord", "opencode", factory=lambda s: spy),
        60,
        tmp_path / "state.json",
    )

    async def once():
        task = asyncio.create_task(w.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(once())
    assert "no harness is curated" in capsys.readouterr().out


# --- Linear.label_history ---


class Client:
    """Stands in for the HTTP round trip so the shape of Linear's answer,
    rather than the request, is what these tests look at."""

    def __init__(self, payload):
        self._payload = payload
        self.calls: list[dict] = []

    async def _query(self, query, variables):
        self.calls.append(dict(variables))
        return self._payload

    issues_for = None


def history_client(payload):
    from chord.linear import Linear

    client = Linear("token")
    stub = Client(payload)
    client._query = stub._query  # type: ignore[method-assign]
    client.calls = stub.calls  # type: ignore[attr-defined]
    return client


def test_label_history_is_newest_first():
    """The order decides which route runs, so it is sorted here not trusted."""
    client = history_client(
        {
            "issue": {
                "history": {
                    "nodes": [
                        {
                            "createdAt": "2026-01-01T00:00:00Z",
                            "addedLabels": [{"name": "Old"}],
                        },
                        {
                            "createdAt": "2026-03-01T00:00:00Z",
                            "addedLabels": [{"name": "New"}],
                        },
                        {
                            "createdAt": "2026-02-01T00:00:00Z",
                            "addedLabels": [{"name": "Mid"}],
                        },
                    ]
                }
            }
        }
    )
    assert asyncio.run(client.label_history("1")) == ["New", "Mid", "Old"]


def test_label_history_drops_a_label_that_came_back_around():
    """Added, removed, added again: one answer is enough."""
    client = history_client(
        {
            "issue": {
                "history": {
                    "nodes": [
                        {
                            "createdAt": "2026-03-01T00:00:00Z",
                            "addedLabels": [{"name": "A"}],
                        },
                        {
                            "createdAt": "2026-01-01T00:00:00Z",
                            "addedLabels": [{"name": "A"}],
                        },
                    ]
                }
            }
        }
    )
    assert asyncio.run(client.label_history("1")) == ["A"]


def test_label_history_survives_entries_that_added_nothing():
    """Linear returns `addedLabels: null` on most history entries."""
    client = history_client(
        {
            "issue": {
                "history": {
                    "nodes": [
                        {"createdAt": "2026-03-01T00:00:00Z", "addedLabels": None},
                        {
                            "createdAt": "2026-01-01T00:00:00Z",
                            "addedLabels": [{"name": "A"}],
                        },
                    ]
                }
            }
        }
    )
    assert asyncio.run(client.label_history("1")) == ["A"]


def test_label_history_of_an_issue_with_no_history():
    client = history_client({"issue": {"history": {"nodes": []}}})
    assert asyncio.run(client.label_history("1")) == []


def test_label_history_of_a_missing_issue():
    client = history_client({"issue": None})
    assert asyncio.run(client.label_history("gone")) == []


def test_label_history_asks_for_a_bounded_window():
    from chord.linear import MAX_HISTORY

    client = history_client({"issue": {"history": {"nodes": []}}})
    asyncio.run(client.label_history("1"))
    assert client.calls[0]["first"] == MAX_HISTORY
    assert client.calls[0]["id"] == "1"


def test_the_history_query_asks_for_what_it_needs():
    from chord.linear import LABEL_HISTORY

    assert "addedLabels" in LABEL_HISTORY
    assert "createdAt" in LABEL_HISTORY


# --- the printed harness still works, unchanged ---


def test_the_print_harness_is_still_reachable_by_name():
    assert isinstance(harness.build("print"), harness.PrintHarness)
