"""The poll loop: find routed issues Chord hasn't offered yet, hand each over.

The memory of what has already gone out lives in a small file beside the log
rather than in the process, so restarting Chord doesn't offer the whole
backlog again.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from chord import logging, notify, subscribe
from chord.context import Issue, render
from chord.harness import HarnessError
from chord.linear import LinearClient, LinearError
from chord.routing import DEFAULT_ROUTE, Router, UnknownRoute, route_label
from chord.subscribe import Subscription
from chord.webhook import Webhook

# The most hand-overs to remember, and no smaller than the number of issues
# one poll will read: `linear.MAX_ISSUES_PER_POLL` is the most a single poll
# can offer, so anything below that and a still-labelled issue could be
# forgotten while it is still in view. Dropping older entries can't re-offer an
# issue in practice, because Linear pages newest-first, so an issue that has
# fallen out of the window is also out of the page.
#
# The cap is what keeps the file readable for the person who opens it to reset
# Chord, which is the reason it exists at all.
STATE_CAP = 1000

# How long a single Linear request gets before we treat it as a failed poll.
# Above `linear.TIMEOUT` on purpose, so the request timeout is what fires when
# Linear is merely slow.
POLL_TIMEOUT = 60


class StateError(Exception):
    """The record of what Chord has already handed over is unreadable."""


def _key(issue_id: object) -> str:
    """One spelling of an issue id, so the check against the state file and the
    entry written to it always agree.

    Linear sends ids as strings, but a mismatch here is silent and expensive:
    Chord would offer every issue a second time. Going through str() at both
    ends makes that impossible rather than merely unlikely.
    """
    return str(issue_id)


def _label(issue: Issue) -> str:
    """How to name an issue in the log, when Linear may not have given us a
    title or an identifier."""
    return str(issue.get("identifier") or issue.get("id") or "(unidentified issue)")


def log(message: str) -> None:
    """One line of the watcher's log, which is what `chord watch` follows.

    Plain lines with a timestamp, because this file is the thing someone reads
    at a puzzling moment, and it is read with ordinary tools. Everything goes
    through `one_line` here rather than at each call site: issue titles, comment
    bodies and Linear's error text all reach this function, and none of them
    should be able to break the line or draw on the reader's terminal.
    """
    logging.info(message)


def _spacer() -> None:
    """A blank line, so a rendered issue doesn't run straight into the log line
    that follows it and leave the reader unsure which is which."""
    logging.write("\n")


class Watcher:
    def __init__(
        self,
        linear: LinearClient,
        router: Router,
        interval: int,
        state_path: Path,
        subscription: Subscription | None = None,
        webhook: Webhook | None = None,
    ) -> None:
        self._linear = linear
        self._router = router
        self._interval = interval
        self._path = state_path
        self._seen = read_state(state_path)
        self._last_problem: str | None = None
        self._subscription = subscription
        self._webhook = webhook

    @property
    def handed_over(self) -> int:
        """How many issues this Chord has offered a harness."""
        return len(self._seen)

    async def run(self) -> None:
        """Poll until cancelled.

        The poll is the loop. A live connection, when one is available, only
        shortens the wait between polls: Linear says something moved, so the
        next poll happens now rather than when the interval runs out. Losing the
        connection costs responsiveness and nothing else, which is why the
        interval wait below is the fallback rather than the other way round.
        """
        log(f"Watching Linear for issues labelled {self._router.default_name!r}")
        routes = self._router.routes()
        curated = [route for route in routes if route.name]
        if curated:
            log("  or any label naming a curated harness:")
            for route in curated:
                log(f"    {route.label} -> {route.spelling}")
        else:
            log("  (no harness is curated, so every issue goes to the default)")
        log(f"Handing each one to {self._router.harness_for(DEFAULT_ROUTE).name}.")
        if self._webhook is not None:
            try:
                await self._webhook.start()
                log(f"Webhook listening at {self._webhook.url}.")
            except OSError as exc:
                self._webhook = None
                self._problem(f"webhook unavailable ({exc}); polling remains enabled")
        if self._subscription is not None and subscribe.available():
            await self._subscription.start()
        try:
            while True:
                await self.poll()
                await self._wait_for_event()
        finally:
            if self._webhook is not None:
                await self._webhook.stop()
            if self._subscription is not None:
                await self._subscription.stop()

    async def _wait_for_event(self) -> None:
        sources = [
            source
            for source in (self._webhook, self._subscription)
            if source is not None
        ]
        if not sources:
            await asyncio.sleep(self._interval)
            return
        tasks = [asyncio.create_task(source.wait(self._interval)) for source in sources]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def note_subscription_problem(self, message: str) -> None:
        """A line about the live connection, through the same once-only rule.

        The watcher runs for days, so a socket that can't be held open must not
        fill the log with the same complaint every interval.
        """
        self._problem(
            f"live updates unavailable ({message}); polling every {self._interval}s"
        )

    async def poll(self) -> None:
        """One pass over Linear. Never raises: a watcher that dies on a bad
        moment is worse than one that says what went wrong and carries on."""
        try:
            page = await asyncio.wait_for(
                self._linear.issues_for(self._router.filter()), POLL_TIMEOUT
            )
        except asyncio.CancelledError:
            raise
        except LinearError as exc:
            self._problem(str(exc))
            return
        except TimeoutError:
            self._problem(f"Linear didn't answer within {POLL_TIMEOUT}s.")
            return
        except Exception as exc:
            # A bug in the paging loop or the HTTP layer is not something a
            # person can fix by waiting, but it is still not worth losing the
            # watcher over: report it and try again on the next tick.
            self._problem(f"couldn't read Linear: {type(exc).__name__}: {exc}")
            return

        # Said out loud on purpose. The alternative — quietly reading the first
        # N issues — is how a backlog ends up with issues that simply never
        # arrive, and nobody can tell why.
        #
        # Reported before `_last_problem` is cleared, and cleared only when
        # there is nothing to report, so a label that is permanently over the
        # cap says so once rather than every poll for the life of the watcher.
        if page.truncated:
            self._problem(
                f"more issues carry {self._router.default_name!r} than one poll "
                "reads; raise MAX_ISSUES_PER_POLL in chord/linear.py to see them all"
            )
        else:
            self._last_problem = None

        # Oldest first, so a backlog works its way through in the order it was
        # written rather than the order it was last touched.
        for issue in sorted(page.issues, key=lambda i: i.get("createdAt") or ""):
            if _key(issue.get("id")) not in self._seen:
                try:
                    await self._hand_over(issue)
                except asyncio.CancelledError:
                    # The one exception that must not be swallowed: this is how
                    # `chord stop` reaches the watcher.
                    raise
                except Exception as exc:
                    # A single malformed issue, or a bug in this loop, costs one
                    # issue rather than the watcher. Anything reaching here is a
                    # bug rather than an expected condition, so it is reported
                    # as one and the rest of the backlog still gets its turn.
                    _spacer()
                    self._problem(
                        f"internal error handing over {_label(issue)}: "
                        f"{type(exc).__name__}: {exc}"
                    )

    async def _hand_over(self, issue: Issue) -> None:
        identifier = _label(issue)
        log(f"{identifier} {issue.get('title') or ''}".rstrip())

        # An issue with no id can't be de-duplicated or recorded, so handing it
        # over would offer it again on every poll forever. Say so and move on;
        # the queue behind it is more useful than this one entry.
        issue_id = issue.get("id")
        if not issue_id:
            _spacer()
            log("  skipped: no id from Linear, so Chord can't record it.")
            return

        chosen = await self._route_for(issue, identifier, issue_id)
        if chosen is None:
            # No route label at all. `_route_for` has already said so, and there
            # is no harness to complain about, so the default runs it.
            chosen = DEFAULT_ROUTE

        try:
            harness = self._router.harness_for(chosen)
        except UnknownRoute as exc:
            # Not run on the default instead. Doing the work with a harness
            # nobody asked for — on the wrong arguments, in the wrong place —
            # is a worse surprise than not doing it, and the person who
            # mislabelled the issue is the one who can fix it.
            #
            # Recorded, like every other hand-over that didn't happen, so one
            # bad route isn't retried on every poll forever. Which is why the
            # second line exists: removing the label won't bring the issue
            # back, and a message that doesn't say so sends someone looking for
            # a fix that isn't one. Both lines are kept short because the log
            # flattens to one line and cuts at 200 characters, which is exactly
            # where the useful half of this would otherwise land.
            _spacer()
            self._problem(
                f"{identifier} skipped: {route_label(self._router.label, exc.name)!r} "
                f"names a harness chord.toml doesn't define."
            )
            self._problem(
                f"  to retry it: add [harnesses.\"{exc.name}\"] to chord.toml, then "
                f"drop {_key(issue_id)} from {self._path.name}."
            )
            self._remember(issue_id)
            return
        except HarnessError as exc:
            # A curated route that `chord start` accepted and can't run now —
            # most likely a command that has since left the PATH. Same class of
            # event as an unknown label and recorded for the same reason: left
            # unrecorded it would be retried on every poll for the life of the
            # watcher, and the log is where the reason already is.
            _spacer()
            self._problem(
                f"{identifier} didn't finish: "
                f"{route_label(self._router.label, chosen)} {exc}"
            )
            self._remember(issue_id)
            return

        if chosen != DEFAULT_ROUTE:
            # Only for a narrowed route. `run()` already said what the default
            # is, and repeating it on every issue would be noise.
            log(f"  route      {route_label(self._router.label, chosen)}")

        comments: list[dict[str, Any]] = []
        try:
            comments = await self._linear.comments(issue_id)
        except LinearError as exc:
            # The issue is still worth handing over. It just arrives without
            # the conversation around it, and the log says so.
            self._problem(f"no discussion for {identifier}: {exc}")

        await notify.send("Chord started", f"Working on {identifier}")
        try:
            await harness.send(render({**issue, "comments": comments}))
        except HarnessError as exc:
            # Recorded as handed over even though it wasn't, on purpose. A
            # harness that is simply missing would otherwise be retried on
            # every poll forever and nothing after it in the queue would ever
            # run. The failure is in the log, which is where a person looks.
            _spacer()
            self._problem(f"{identifier} didn't finish: {exc}")
            self._remember(issue_id)
            await notify.send("Chord done", f"{identifier} did not finish: {exc}")
            return

        _spacer()
        log("  handed over.")
        self._remember(issue_id)
        await notify.send("Chord done", f"{identifier} finished")

    async def _route_for(
        self, issue: Issue, identifier: str, issue_id: str
    ) -> str | None:
        """Which route this issue is routed to, or None if it carries none.

        Choosing the route and building the harness are kept apart on purpose.
        Picking a route can only fail by asking Linear a question; building one
        can fail because a command isn't installed, and that belongs with the
        other "didn't finish" reporting rather than inside a routing decision.
        """
        candidates = self._router.candidates(issue)
        if not candidates:
            # The filter matched this issue on a route label, so this shouldn't
            # happen. Reported rather than guessed at, because guessing means
            # running the work somewhere nobody asked for.
            self._problem(
                f"{identifier} has no {self._router.default_name!r} label on it, "
                "so Chord can't tell what it was routed to. Using the default."
            )
            return None
        if len(candidates) == 1:
            return candidates[0]
        return await self._newest_route(issue_id, identifier, candidates)

    async def _newest_route(
        self, issue_id: str, identifier: str, candidates: list[str]
    ) -> str:
        """Which of several route labels was added to the issue most recently.

        Linear sends an issue's labels as an unordered set, so an issue carrying
        both `Chord` and `Chord/opencode` genuinely does not say which one wins
        from the issue alone. Linear's own audit trail does say, which is what
        this reads. When it can't be read the most specific label wins, which is
        the better guess: someone narrows `Chord` to `Chord/opencode/tiny`
        because they want that one.
        """
        try:
            added = await self._linear.label_history(issue_id)
        except LinearError as exc:
            self._problem(
                f"couldn't read the label history for {identifier} ({exc}); "
                f"using the most specific of {len(candidates)} route labels"
            )
            return candidates[0]

        names = {route_label(self._router.label, route): route for route in candidates}
        for label in added:
            route = names.get(label)
            if route is not None:
                return route
        return candidates[0]

    def _remember(self, issue_id: object) -> None:
        self._seen[_key(issue_id)] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # Keep the newest entries; dicts hold insertion order, so the tail is
        # the most recent.
        trimmed = dict(list(self._seen.items())[-STATE_CAP:])
        self._seen = trimmed
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            body = json.dumps({"handed_over": trimmed}, indent=2) + "\n"
            pending = self._path.with_suffix(self._path.suffix + ".tmp")
            pending.write_text(body)
            pending.replace(self._path)
        except OSError as exc:
            # Losing this costs a duplicate hand-over after a restart, which
            # is a much smaller problem than a watcher that stops working.
            self._problem(f"couldn't record {issue_id} in {self._path}: {exc.strerror}")

    def _problem(self, message: str) -> None:
        """Report a problem, but only the first time in a row.

        A watcher runs for days. Linear being briefly unreachable would
        otherwise fill the log with the same line until nobody wants to read
        the log again.
        """
        if message == self._last_problem:
            return
        self._last_problem = message
        log(f"  {message}")


def read_state(path: Path) -> dict[str, str]:
    """Issue id -> when it was handed over, as an earlier run left it.

    Public because `chord info` reports on it, and because a file that has been
    left corrupt deserves a message that names the way out.
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}  # First run. Nothing has been offered yet.
    except OSError as exc:
        raise StateError(f"Couldn't read {path}: {exc.strerror}.") from exc

    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise StateError(
            f"{path} isn't valid JSON. Delete it to let Chord start over and "
            "offer those issues again."
        ) from exc

    handed_over = raw.get("handed_over") if isinstance(raw, dict) else None
    if not isinstance(handed_over, dict):
        raise StateError(
            f"{path} doesn't look like a Chord state file. Delete it to start over."
        )
    return {str(k): str(v) for k, v in handed_over.items()}
