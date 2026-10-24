"""Which harness an issue is routed to, and the Linear label that says so.

An issue is picked up by a label. A bare `Chord` means the default harness from
chord.toml, and `Chord -> <name>` means a curated one from `[harnesses.<name>]`.
Linear allows neither `/` nor `:` in a label, so each arrow after the first
stands for a `/` in the harness name, a parent -> child relationship:
`Chord -> opencode -> big-pickle` is the harness curated as
`opencode/big-pickle`. Arrows are read with any whitespace around them and the
parts between them trimmed, so `Chord->opencode->big-pickle` is the same route.
Any other character is the name's own, `:` included, so
`Chord -> opencode -> Qwen3.4:8B` is `opencode/Qwen3.4:8B`. Matching is exact
and case-sensitive against curated names, so the grammar never has to know
anything about what a harness is.

The older spelling, `Chord/<name>`, is still read, so route labels created
before the arrow keep working: `Chord/t3` and `Chord -> t3` are both `t3`, and
the whole legacy suffix is the name (`Chord/opencode/tiny` is `opencode/tiny`).
Only the arrow is ever printed, as ` -> `.

Keeping the grammar here rather than in config.py is what makes an unknown
route survivable. Chord has to ask Linear for every `Chord -> ...` label,
including ones nobody curated, because a label nobody has heard of is either a
typo or a route added in Linear before chord.toml caught up — and in both cases
the useful thing to do is say so in the log, not sit silently on the issue. So
this module knows how to *read* a label; whether the harness behind it exists is
a separate question, and `harness_for` is the only place that answers it.
"""

import re
from collections.abc import Callable, Mapping
from typing import NamedTuple

from chord.config import (
    ARROW,
    LEGACY_SEPARATOR,
    NAME_SEPARATOR,
    SEPARATOR,
    HarnessSpec,
    T3Options,
    spell,
)
from chord.harness import Harness, HarnessError, build, missing
from chord.performance import instrumented

# The name that asks for the default harness. Curated names are separate
# namespaces from the `harness` setting: `Chord -> foo` always means "the curated
# `foo`", and a bare `Chord` always means "whatever `harness` says", even when
# the two happen to spell the same command.
DEFAULT_ROUTE = ""

# What `<label> ->` (or the legacy `<label>/`) with nothing after it names. Not
# `""`, because that is `DEFAULT_ROUTE` — so returning the empty suffix would
# make a label that named no harness indistinguishable from the bare trigger
# label, and it would run the work on the default harness. The arrow itself,
# because no name read from a label keeps one (each becomes `/`) and no curated
# name may contain one, so it lands in the same skip-and-report path as any
# other unknown route.
EMPTY_NAME = ARROW

# An arrow and the whitespace around it, for splitting a label into parts.
_ARROW = re.compile(r"\s*" + re.escape(ARROW) + r"\s*")


class UnknownRoute(HarnessError):
    """A route label named a harness that isn't curated.

    A `HarnessError` because from the watcher's point of view this is the same
    class of event as a harness that isn't installed: something about the route
    can't be run, and the queue behind it still deserves its turn.
    """

    def __init__(self, name: str) -> None:
        super().__init__(f"no harness named {name!r}")
        self.name = name


def route_label(label: str, name: str) -> str:
    """The Linear label that selects `name`, in the current spelling.

    Every `/` in the name is written as ` -> `, because Linear refuses `/` in a
    label: `opencode/big-pickle` is `Chord -> opencode -> big-pickle`.
    """
    if not name:
        return label
    return SEPARATOR.join([label, *name.split(NAME_SEPARATOR)])


class Route(NamedTuple):
    """One label, the harness it names, and how that harness is spelled.

    Carried as one record because the three are always wanted together:
    `chord info` prints the label and the spelling, and validation builds the
    name.
    """

    label: str
    name: str
    spelling: str


def route_of(label: str, candidate: str) -> str | None:
    """The harness `candidate` asks for, or None if it isn't a Chord label.

    `""` for the bare trigger label, which asks for the default harness.

    Total over what `filter()` matches. The filter accepts any label starting
    with `<label>->`, `<label> ->` or the legacy `<label>/`, so this must
    return a name for every one of those: a label that matched the query and
    then produced no route here fell through to the default harness and was
    worked on by an agent nobody asked for. `Chord -> -> x`, `Chord -> x ->`
    and `Chord//x` are spelling mistakes, but they are *somebody's* spelling
    mistake and the answer is to tell them, not to run their work elsewhere.

    The name comes back with each arrow read as `/` and each part trimmed,
    which is how `Router` keys the curated harnesses.

    Whether the name is one `harness_for` can build is a separate question, and
    the one that answers it is `config.usable_harness_name` — the same predicate
    curation applies, so the two sides of the grammar cannot drift apart again.
    """
    if candidate == label:
        return DEFAULT_ROUTE
    if candidate.startswith(label):
        rest = candidate[len(label) :]
        arrow = _ARROW.match(rest)
        if arrow:
            # The parts between arrows, trimmed and joined with `/`:
            # `Chord -> -> x` gives "/x" and `Chord -> x ->` gives "x/", and
            # neither can be curated, so each names a harness that does not
            # exist.
            #
            # The empty suffix needs care, because "" *is* `DEFAULT_ROUTE` —
            # the bare `Chord` label. So `Chord ->` and `Chord` would resolve
            # to the same route, and a label that named nothing would run work
            # on the default harness. That is the whole bug, one character
            # narrower. An empty suffix gets a name that is empty enough to be
            # unusable and visible enough to log.
            suffix = rest[arrow.end() :].strip()
            if not suffix:
                return EMPTY_NAME
            return NAME_SEPARATOR.join(_ARROW.split(suffix))
    head = label + LEGACY_SEPARATOR
    if candidate.startswith(head):
        # The legacy suffix, verbatim: `Chord/t3` gives "t3", the same name as
        # `Chord -> t3`, so an old label keeps its old meaning, and `Chord//x`
        # gives "/x", which can't be curated.
        name = candidate[len(head) :]
        return name if name else EMPTY_NAME
    return None


class Router:
    """The label grammar and the curation table, joined up.

    Pure bookkeeping: it reads labels and hands back specs. Building the thing
    that actually runs is `harness.build`, reached through `harness_for`, and
    memoized so a route pays for one PATH lookup per watcher rather than one per
    issue.
    """

    def __init__(
        self,
        label: str,
        default: HarnessSpec,
        harnesses: Mapping[str, HarnessSpec] | None = None,
        factory: Callable[[HarnessSpec], Harness] | None = None,
        allowed_actors: tuple[str, ...] = (),
        t3_options: T3Options | None = None,
    ) -> None:
        self.label = label
        self._default = default
        self._specs: dict[str, HarnessSpec] = dict(harnesses or {})
        self._factory = factory or (lambda spec: build(spec, t3_options))
        self._built: dict[str, Harness] = {}
        self._allowed = frozenset(actor.strip().lower() for actor in allowed_actors)

    @property
    def authorises(self) -> bool:
        """Whether Chord checks who asked before running the work.

        The watcher reads this to decide whether a hand-over has to ask Linear
        who applied the routing label. With nothing allowed, nobody is asked and
        nobody is refused.
        """
        return bool(self._allowed)

    def authorises_actor(self, actor_id: str | None) -> bool:
        """Whether `actor_id` may trigger a hand-over.

        An id nobody can supply means the answer was not knowable — an
        integration applied the label, or the event has fallen off the audit
        page. Both are refused, because a control that guesses in the
        permissive direction is not a control.
        """
        if not self._allowed:
            return True
        return bool(actor_id) and actor_id.strip().lower() in self._allowed

    @property
    def names(self) -> list[str]:
        """The curated harness names, in the order a person would read them."""
        return sorted(self._specs)

    @property
    def default_name(self) -> str:
        return route_label(self.label, DEFAULT_ROUTE)

    def filter(self) -> dict:
        """The Linear issue filter that finds every route in one query.

        Every part matters. The exact match catches a bare `Chord`, and the
        arrow prefixes catch `Chord -> <anything>` and `Chord-><anything>` —
        curated or not, so an unknown route reaches the log instead of never
        arriving at all. Whitespace after the arrow needs no prefix of its own,
        because the prefix ends at the arrow. The legacy prefix, `Chord/`, keeps
        route labels made before the arrow arriving.

        Exact prefixes rather than "starts with `Chord`": a label like
        `Chordless` is somebody else's, and an issue fetched for it would carry
        no route and be skipped as malformed.
        """
        return {
            "or": [
                {"labels": {"name": {"eq": self.label}}},
                {"labels": {"name": {"startsWith": f"{self.label} {ARROW}"}}},
                {"labels": {"name": {"startsWith": self.label + ARROW}}},
                {"labels": {"name": {"startsWith": self.label + LEGACY_SEPARATOR}}},
            ]
        }

    def candidates(self, issue: Mapping[str, object]) -> list[str]:
        """Every route this issue carries, most specific first.

        More than one is normal: an issue picked up by `Chord` and then
        narrowed with `Chord -> opencode -> tiny` carries both, and the watcher asks
        Linear which was added last. The order here is only the fallback for
        when that question can't be answered.
        """
        found: list[str] = []
        labels = issue.get("labels")
        # `or []` rather than a default: a present-but-null `nodes` is not
        # missing, so `.get(key, [])` would hand back `None` and the loop would
        # raise. Linear sends `nodes: null` for an issue with no labels, and a
        # raise here is recorded as handed over — the work is silently lost.
        nodes = (labels.get("nodes") or []) if isinstance(labels, dict) else []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            route = route_of(self.label, str(node.get("name") or ""))
            if route is not None and route not in found:
                found.append(route)
        return sorted(found, key=lambda route: (-len(route), route))

    def label_on(self, issue: Mapping[str, object], name: str) -> str:
        """The label on `issue` that asked for `name`, spelled as it is there.

        For telling somebody about a route that can't run: `Chord//claude` has
        to be reported as the label they can find on the issue, not as the
        `Chord ->  -> claude` it would be in the current spelling. Falls back to
        the current spelling when no label on the issue names the route.
        """
        labels = issue.get("labels")
        nodes = (labels.get("nodes") or []) if isinstance(labels, dict) else []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            candidate = str(node.get("name") or "")
            if route_of(self.label, candidate) == name:
                return candidate
        return route_label(self.label, name)

    @instrumented
    def harness_for(self, name: str) -> Harness:
        """The harness a route runs on, built once and remembered."""
        if name in self._built:
            return self._built[name]
        if name == DEFAULT_ROUTE:
            spec = self._default
        elif name in self._specs:
            spec = self._specs[name]
        else:
            raise UnknownRoute(name)
        self._built[name] = self._factory(spec)
        return self._built[name]

    def routes(self) -> list[Route]:
        """Every route, the default first and the curated ones by name."""
        return [Route(self.default_name, DEFAULT_ROUTE, spell(self._default))] + [
            Route(route_label(self.label, name), name, spell(self._specs[name]))
            for name in self.names
        ]

    def validate(self) -> None:
        """Build every route, so a broken one is reported before it is needed.

        Raises the `HarnessError` that `harness_for` would raise, with the
        route's label in front of it — a curated harness naming a command that
        isn't installed is a mistake in a file, and `chord start` is the last
        moment the person who made it is still looking.
        """
        for route in self.routes():
            try:
                self.harness_for(route.name)
            except HarnessError as exc:
                raise HarnessError(f"{route.label} can't be run: {exc}") from None

    def problems(self) -> list[tuple[Route, str]]:
        """Every route that can't be started on this machine, and why.

        Wider than `validate`, which only builds: building leaves an explicit
        command and the default `opencode` to be found missing by the first
        issue that needs them, and that issue is recorded as failed. This asks
        the PATH as well, for every route rather than stopping at the first,
        so `chord start` and `chord info` can list everything wrong at once.
        """
        found: list[tuple[Route, str]] = []
        for route in self.routes():
            spec = (
                self._default
                if route.name == DEFAULT_ROUTE
                else self._specs[route.name]
            )
            # The factory directly rather than `harness_for`: this is a
            # question, not a hand-over, so it neither memoizes nor writes a
            # PERF line into `chord info`.
            try:
                self._factory(spec)
            except HarnessError as exc:
                found.append((route, str(exc)))
                continue
            reason = missing(spec)
            if reason:
                found.append((route, reason))
        return found
