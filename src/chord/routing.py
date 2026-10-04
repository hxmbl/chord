"""Which harness an issue is routed to, and the Linear label that says so.

An issue is picked up by a label. A bare `Chord` means the default harness from
chord.toml, and `Chord/<name>` means a curated one from `[harnesses.<name>]`.
The whole suffix is the name, so one harness can be called `opencode/tiny` and
reached with `Chord/opencode/tiny` without the grammar having to know anything
about what a harness is.

Keeping the grammar here rather than in config.py is what makes an unknown
route survivable. Chord has to ask Linear for every `Chord/...` label, including
ones nobody curated, because a label nobody has heard of is either a typo or a
route added in Linear before chord.toml caught up — and in both cases the useful
thing to do is say so in the log, not sit silently on the issue. So this module
knows how to *read* a label; whether the harness behind it exists is a separate
question, and `harness_for` is the only place that answers it.
"""

from collections.abc import Callable, Mapping
from typing import NamedTuple

from chord.config import SEPARATOR, HarnessSpec, spell
from chord.harness import Harness, HarnessError, build

# The name that asks for the default harness. Curated names are separate
# namespaces from the `harness` setting: `Chord/foo` always means "the curated
# `foo`", and a bare `Chord` always means "whatever `harness` says", even when
# the two happen to spell the same command.
DEFAULT_ROUTE = ""


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
    """The Linear label that selects `name`."""
    return f"{label}{SEPARATOR}{name}" if name else label


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
    """
    if candidate == label:
        return DEFAULT_ROUTE
    head = label + SEPARATOR
    if not candidate.startswith(head):
        return None
    name = candidate[len(head) :]
    # `Chord//x` and `Chord/x/` are spelling mistakes rather than routes, and
    # curation rejects those names too, so they can't match anything here.
    if not name or name.startswith(SEPARATOR) or name.endswith(SEPARATOR):
        return None
    return name


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
        factory: Callable[[HarnessSpec], Harness] = build,
        allowed_actors: tuple[str, ...] = (),
    ) -> None:
        self.label = label
        self._default = default
        self._specs: dict[str, HarnessSpec] = dict(harnesses or {})
        self._factory = factory
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

        Both halves matter. The exact match catches a bare `Chord`, and the
        prefix catches `Chord/<anything>` — curated or not, so an unknown route
        reaches the log instead of never arriving at all.
        """
        return {
            "or": [
                {"labels": {"name": {"eq": self.label}}},
                {"labels": {"name": {"startsWith": self.label + SEPARATOR}}},
            ]
        }

    def candidates(self, issue: Mapping[str, object]) -> list[str]:
        """Every route this issue carries, most specific first.

        More than one is normal: an issue picked up by `Chord` and then
        narrowed with `Chord/opencode/tiny` carries both, and the watcher asks
        Linear which was added last. The order here is only the fallback for
        when that question can't be answered.
        """
        found: list[str] = []
        labels = issue.get("labels")
        for node in labels.get("nodes", []) if isinstance(labels, dict) else []:
            if not isinstance(node, dict):
                continue
            route = route_of(self.label, str(node.get("name") or ""))
            if route is not None and route not in found:
                found.append(route)
        return sorted(found, key=lambda route: (-len(route), route))

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
