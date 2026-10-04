"""An issue must only ever be worked on by the harness its label named.

The bug this file exists for: `Router.filter()` matches every label with the
prefix `Chord/`, but `route_of()` rejected the malformed ones and returned
`None`. An issue labelled `Chord//claude` therefore matched the query, produced
*no* candidates, and fell through `watcher._resolve` to `DEFAULT_ROUTE` — so it
was handed to the default harness and recorded as a success.

The commit that introduced routing said it plainly: "a route naming no curated
harness skips the issue rather than running the work on a harness nobody asked
for." A malformed route was the one case that did exactly that. It is also the
worst possible outcome, because the person who labelled it has no way to tell:
the log said `handed over.`

The fix is to make `route_of` total over what `filter()` matches, so the two
sides of the grammar cannot disagree again.
"""

import asyncio
import contextlib
import io

import pytest

from chord import config, routing, watcher
from chord.linear import IssuePage
from chord.routing import DEFAULT_ROUTE, EMPTY_NAME, Router, route_of

CURATED = {"claude": "spec-claude", "opencode/tiny": "spec-tiny"}


def labelled(*names):
    return {"labels": {"nodes": [{"name": n} for n in names]}}


def run_once(tmp_path, label_names):
    """Poll one issue carrying `label_names`, and report who was asked to run it."""
    ran: list[str] = []

    class Stub:
        async def issues_for(self, filter):
            return IssuePage(
                [
                    {
                        "id": "1",
                        "identifier": "ENG-1",
                        "title": "please run this",
                        "description": "d",
                        "createdAt": "2026-01-01T00:00:00Z",
                        **labelled(*label_names),
                    }
                ],
                False,
            )

        async def label_history(self, issue_id):
            return []

        async def comments(self, issue_id):
            return []

    class Harness:
        def __init__(self, spec):
            self.name = spec

        async def send(self, prompt):
            ran.append(self.name)

    router = Router("Chord", "DEFAULT", CURATED, factory=Harness)
    w = watcher.Watcher(Stub(), router, 1, tmp_path / "state.json")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        asyncio.run(w.poll())
    return ran, w, buf.getvalue()


# --- the wrong-harness case itself ---


@pytest.mark.parametrize(
    "label",
    ["Chord//claude", "Chord/claude/", "Chord/", "Chord//", "Chord///", "Chord//claude//"],
)
def test_a_malformed_route_never_reaches_the_default_harness(tmp_path, label):
    """The regression.

    Before, each of these ran on the default and was recorded as handed over.
    Now each is skipped, and `claude` — which *is* curated — is still refused,
    because the label asked for `/claude` and nothing is curated under that name.
    """
    ran, _watcher, _out = run_once(tmp_path, [label])
    assert "DEFAULT" not in ran, (
        f"{label!r} was handed to the default harness; the issue asked for a "
        f"narrowed route and got run by an agent nobody chose"
    )


@pytest.mark.parametrize(
    "label", ["Chord//claude", "Chord/claude/", "Chord/"]
)
def test_a_malformed_route_is_skipped_and_says_so(tmp_path, label):
    """Not silently dropped either: the person who labelled it can fix it."""
    ran, w, out = run_once(tmp_path, [label])
    assert ran == []
    assert w.handed_over == 1, "recorded, so it is not retried on every poll"
    assert "skipped" in out
    assert label in out, "the log has to name the label that was wrong"
    assert "state.json" in out, "and say how to retry it"


def test_a_malformed_route_beside_a_good_one_does_not_take_over(tmp_path):
    """`Chord/claude` plus a typo: the good label decides, the typo is ignored.

    Specificity ordering puts the longer name first, so the typo would win the
    fallback if the audit trail could not be read. Curation cannot produce
    `/claude`, so the issue is skipped rather than run by the wrong thing.
    """
    ran, _watcher, out = run_once(tmp_path, ["Chord/claude", "Chord//claude"])
    assert "DEFAULT" not in ran
    assert "spec-claude" not in ran or "skipped" in out


# --- the grammar is now total over the filter ---


def test_route_of_never_returns_none_for_something_the_filter_matched():
    """The invariant that closes the whole class.

    Whatever `filter()` can match, `route_of()` must put a name to. Returning
    None is what sent the issue to the default.
    """
    router = Router("Chord", "print")
    prefix = "Chord/"
    for suffix in ["", "/", "//", "x", "/x", "x/", "//x//", " ", "claude"]:
        label = prefix + suffix
        assert route_of("Chord", label) is not None, (
            f"{label!r} matches the poll filter but names no harness"
        )
        assert router.candidates(labelled(label)) != [], (
            f"{label!r} is invisible to routing despite matching the filter"
        )


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Chord/claude", "claude"),
        ("Chord/opencode/tiny", "opencode/tiny"),
        ("Chord/two words", "two words"),
        ("Chord/dots.and-dashes_1", "dots.and-dashes_1"),
        # The malformed ones, verbatim. They name something; nothing is curated
        # under the name, which is what makes them skippable. `Chord/` names
        # nothing at all, and gets EMPTY_NAME — see below.
        ("Chord//claude", "/claude"),
        ("Chord/claude/", "claude/"),
        ("Chord//", "/"),
    ],
)
def test_route_of_is_verbatim_above_the_separator(name, expected):
    assert route_of("Chord", name) == expected


def test_the_bare_label_still_asks_for_the_default():
    assert route_of("Chord", "Chord") == DEFAULT_ROUTE


def test_an_empty_suffix_names_nothing_rather_than_the_default():
    """`Chord/` with nothing after it, which Linear will happily let you create.

    It cannot return `""`, because that *is* `DEFAULT_ROUTE` — the bare `Chord`
    label — and the two would be indistinguishable, so a label naming no harness
    would run the work on the default one. That is the bug, one character
    narrower than the others. So it gets its own name, which is not curatable
    and therefore skips.
    """
    assert route_of("Chord", "Chord/") == routing.EMPTY_NAME
    assert routing.EMPTY_NAME != DEFAULT_ROUTE
    assert not config.usable_harness_name(routing.EMPTY_NAME)


@pytest.mark.parametrize(
    "name", ["Chordle", "Chordle/route", "chord", "Something/Chord", "ChordX", "Chord ", ""]
)
def test_something_that_is_not_a_route_label_is_still_not_one(name):
    """Unchanged: a label that is not a route at all is not a route."""
    assert route_of("Chord", name) is None


# --- the two sides of the grammar share one predicate ---


@pytest.mark.parametrize("name", [EMPTY_NAME, "/x", "x/", "/", "//", "a//b"])
def test_a_typo_name_is_never_curatable(name):
    """So it can never match a curated harness, and is skipped rather than run."""
    assert not config.usable_harness_name(name)


@pytest.mark.parametrize("name", ["claude", "opencode/tiny", "a b", "x.y-z_1"])
def test_a_real_name_is_curatable(name):
    assert config.usable_harness_name(name)


def test_config_and_routing_agree_about_the_grammar():
    """One predicate, so they cannot drift apart again.

    The bug was a *disagreement*: config accepted only well-formed names while
    the parser rejected the malformed ones, and the gap between the two is
    exactly where work reached the wrong harness.
    """
    for name in ["x", "/x", "x/", "//", "a//b", "claude", "a/b"]:
        route = route_of("Chord", f"Chord/{name}")
        if route is not None and route != DEFAULT_ROUTE:
            # Verbatim above the separator, so a typo names a harness that does
            # not exist rather than erasing itself and falling through to the
            # default.
            assert route == name, f"{name!r} became {route!r}"


def test_cannot_curate_a_name_the_grammar_produces_from_a_typo(tmp_path):
    """So `Chord//claude` has nothing it could match, by construction."""
    path = tmp_path / "chord.toml"
    path.write_text('[harnesses."/claude"]\ncommand = "cat"\n')
    with pytest.raises(config.ConfigError):
        config.load(path)


# --- the documented behaviour this restores ---


def test_a_well_formed_uncured_route_is_still_skipped(tmp_path):
    """The case the design already handled, and still does."""
    ran, _watcher, out = run_once(tmp_path, ["Chord/nope"])
    assert ran == []
    assert "skipped" in out
    assert "Chord/nope" in out
    assert '[harnesses."nope"]' in out, "the fix must be spelled out"


def test_a_well_formed_curated_route_still_runs(tmp_path):
    """And the happy path is untouched by any of this."""
    ran, w, out = run_once(tmp_path, ["Chord/claude"])
    assert ran == ["spec-claude"]
    assert w.handed_over == 1
    assert "skipped" not in out


def test_the_default_still_runs_for_the_bare_label(tmp_path):
    ran, _watcher, out = run_once(tmp_path, ["Chord"])
    assert ran == ["DEFAULT"]
    assert "skipped" not in out


def test_a_non_route_label_is_not_a_route(tmp_path):
    """Unrelated labels are still ignored entirely."""
    ran, _watcher, out = run_once(tmp_path, ["Bug", "Research"])
    assert ran == ["DEFAULT"], "the filter matched it, so the default runs it"
    assert "skipped" not in out


def test_candidates_ordering_is_unaffected(tmp_path):
    """Most specific first, which is the fallback when history is unreadable."""
    router = Router("Chord", "print", CURATED)
    assert router.candidates(
        labelled("Chord", "Chord/opencode", "Chord/opencode/tiny")
    ) == ["opencode/tiny", "opencode", DEFAULT_ROUTE]


def test_a_label_matching_the_filter_prefix_always_appears(tmp_path):
    """Including one that is only a prefix of a real route."""
    router = Router("Chord", "print", CURATED)
    found = router.candidates(labelled("Chord/opencode/tiny/"))
    assert found == ["opencode/tiny/"], "a trailing slash must not erase the route"