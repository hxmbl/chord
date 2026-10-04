"""The poll loop: find routed issues Chord hasn't offered yet, hand each over.

The memory of what has already gone out lives in a small file beside the log
rather than in the process, so restarting Chord doesn't offer the whole
backlog again.
"""

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

from chord import daemon, logging, notify, subscribe
from chord.context import Issue, render
from chord.harness import HarnessError
from chord.linear import MAX_HISTORY, Actor, LinearClient, LinearError, Unauthorised
from chord.routing import DEFAULT_ROUTE, Router, UnknownRoute, route_label
from chord.subscribe import Subscription
from chord.text import one_line
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


def _usable_issues(issues: object) -> tuple[list[Issue], int]:
    """The issues in a page that are records, and how many were not.

    `linear._paged` already drops these, so under normal operation nothing is
    dropped here. It is checked again anyway because `LinearClient` is a
    Protocol — anything implementing it can hand over whatever it likes — and
    because the cost of being wrong here is the whole watcher. Cheap
    insurance, at the one place where it decides what runs.
    """
    if not isinstance(issues, list):
        return [], 0 if issues is None else 1
    good = [issue for issue in issues if isinstance(issue, dict)]
    return good, len(issues) - len(good)


def _order(issue: Issue) -> str:
    """Sort key for oldest-first.

    Takes the stamp off a value that may be any shape at all. It used to be
    `issue.get("createdAt") or ""` evaluated in the `sorted()` call, which is
    outside the per-issue handler — so one issue missing a field, or carrying
    something other than a dict, raised straight out of `poll()`, out of
    `run()`, and killed the daemon with the traceback in a log nobody was
    watching. `poll()` documents that it never raises; this is why that has to
    be true of the sort key too.
    """
    if not isinstance(issue, dict):
        return ""
    return str(issue.get("createdAt") or "")


def _label(issue: Issue) -> str:
    """How to name an issue in the log, when Linear may not have given us a
    title or an identifier."""
    return str(issue.get("identifier") or issue.get("id") or "(unidentified issue)")


def _name(node: object) -> str:
    """A person's or a bot's name for the log, never for a prompt.

    Linear may not have sent one, and `allowed_actors` is matched on the id, so
    this is presentation only. Falling back to the id keeps a refusal actionable
    when the name is missing, which is the case where the reader most needs it.
    """
    if isinstance(node, dict):
        name = str(node.get("name") or "").strip()
        if name:
            return name
        ident = str(node.get("id") or "").strip()
        if ident:
            return f"user id {ident}"
    return "someone Linear didn't name"


# Where a routing decision came from. Kept as values rather than booleans so the
# log can say which of several fallbacks actually applied, instead of every
# failure looking the same.
UNAMBIGUOUS = "one route label, no choice to make"
FROM_HISTORY = "the newest matching label in Linear's audit trail"
OFF_THE_PAGE = "no route label on Linear's audit page"
UNREADABLE = "the audit trail could not be read"
NO_LABEL = "no route label on the issue"


class Routed(NamedTuple):
    """One issue's routing decision, and who made it.

    `who` is the actor of the history entry that added the routing label, which
    is the act that caused the run — so it is the right thing to check an
    allowlist against. None means "not knowable", which callers must treat as
    "ask nobody" rather than "ask anyone".
    """

    route: str
    who: Actor | None
    source: str


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
        renew: Callable[[], str | None] | None = None,
    ) -> None:
        self._linear = linear
        self._router = router
        self._interval = interval
        self._path = state_path
        self._seen = read_state(state_path)
        self._last_problem: str | None = None
        self._subscription = subscription
        self._webhook = webhook
        # How to re-read the token. Supplied rather than reached for, because the
        # watcher has no business reading a keychain, and because a test needs
        # to be able to say what the next token is.
        self._renew = renew
        # The last token this process adopted, so a keychain read that hands
        # back the same dead string is recognised as not-a-renewal.
        self._adopted: str | None = None

    @property
    def handed_over(self) -> int:
        """How many issues this Chord has offered a harness."""
        return len(self._seen)

    def _renew_token(self) -> bool:
        """Re-read the token and hand it to the client. Whether it changed.

        Returns False when there is nothing to adopt — no `renew` supplied, the
        keychain has no usable token, or it holds the one that just failed.

        That last case is the one that matters. `renew` is a plain keychain
        read, so a person who has not run `chord refresh` gets back the same
        dead string every time. Treating that as a renewal meant asking Linear
        again with the same credential, on every poll, forever — one wasted
        round trip per interval for as long as the daemon runs. Comparing
        against what we last adopted is what turns "nobody has refreshed this"
        into one attempt instead of an unbounded number of them.
        """
        if self._renew is None:
            return False
        try:
            token = self._renew()
        except Exception as exc:  # a keychain that won't open is not fatal
            self._problem(f"couldn't re-read the stored token: {one_line(str(exc))}")
            return False
        if not token or token == self._adopted:
            return False
        use = getattr(self._linear, "use_token", None)
        if use is None:
            return False
        try:
            use(token)
        except Exception as exc:
            self._problem(f"couldn't adopt the new token: {one_line(str(exc))}")
            return False
        self._adopted = token
        return True

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
        if not self._router.authorises:
            # Said once, in the log, at startup, because it is the difference
            # between "a label decides where the work goes" and "a label decides
            # whether your machine runs a command", and the default is the
            # permissive one. Silence would read as "everything is fine".
            self._problem(
                "anyone in the Linear workspace can trigger a run; set allowed_actors "
                "in chord.toml to restrict that (`chord info` prints your user id)"
            )
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
        moment is worse than one that says what went wrong and carries on.

        The promise covers the whole body, including the paging loop and the
        sort, rather than just the request. A daemon has no supervisor and no
        restart: when it exits, Chord stops watching until somebody notices and
        runs `chord start` again, and the only trace is a traceback in a log
        file. So the guard belongs here, at the outermost edge, and the
        per-issue handler inside it is there to keep one bad issue from costing
        the rest of the backlog.
        """
        try:
            await self._poll()
        except asyncio.CancelledError:
            # `chord stop` reaches the watcher this way. Swallowing it here would
            # make the watcher unstoppable.
            raise
        except Exception as exc:
            self._problem(
                f"couldn't read Linear: {type(exc).__name__}: {one_line(str(exc))}"
            )

    async def _poll(self) -> None:
        try:
            page = await asyncio.wait_for(
                self._linear.issues_for(self._router.filter()), POLL_TIMEOUT
            )
        except asyncio.CancelledError:
            raise
        except Unauthorised as exc:
            # The one failure that a retry on its own cannot fix: the token this
            # process was started with is dead, and will stay dead until somebody
            # runs `chord refresh`. Re-read it and try once more, so the common
            # case — a person refreshed the token and left the daemon running —
            # heals without anybody noticing there was anything to heal.
            #
            # Once per poll at most, and only when the token actually changed. A
            # keychain read is not free and this runs every interval forever.
            if not self._renew_token():
                self._problem(
                    f"{exc} Run `chord refresh`, then this Chord picks it up."
                )
                return
            try:
                page = await asyncio.wait_for(
                    self._linear.issues_for(self._router.filter()), POLL_TIMEOUT
                )
            except (LinearError, TimeoutError) as retry:
                self._problem(
                    f"still couldn't read Linear after refreshing: {one_line(str(retry))}"
                )
                return
        except LinearError as exc:
            self._problem(str(exc))
            return
        except TimeoutError:
            self._problem(f"Linear didn't answer within {POLL_TIMEOUT}s.")
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

        usable, dropped = _usable_issues(page.issues)
        if dropped:
            # Said rather than swallowed: an issue that disappears without a word
            # is indistinguishable from one that never existed.
            self._problem(f"skipped {dropped} issue(s) Linear returned unreadable")

        # Oldest first, so a backlog works its way through in the order it was
        # written rather than the order it was last touched.
        for issue in sorted(usable, key=_order):
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
                    #
                    # Recorded, because an issue that is never recorded is
                    # retried on every poll for the life of the watcher. That
                    # turned a bug in the middle of the hand-over into an issue
                    # that silently never got worked on, re-logged once a minute
                    # for weeks, with the work invisible from the outside. An
                    # unrecoverable hand-over is the same class of outcome as a
                    # recorded failure: both mean "Chord will not do this one",
                    # and both should stop costing a round trip every interval.
                    #
                    # The two lines exist because recording it is invisible
                    # otherwise. Removing the id from the state file is how
                    # somebody retries it by hand.
                    _spacer()
                    self._problem(
                        f"internal error handing over {_label(issue)}: "
                        f"{type(exc).__name__}: {one_line(str(exc))}"
                    )
                    _spacer()
                    self._problem(
                        f"  this issue was recorded as handed over without being "
                        f"worked on. To retry it: drop {_key(issue.get('id'))} from "
                        f"{self._path.name}."
                    )
                    self._remember(issue.get("id"))

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

        chosen = await self._resolve(issue, identifier, issue_id)

        if not await self._authorise(issue, identifier, issue_id, chosen):
            return

        try:
            harness = self._router.harness_for(chosen.route)
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
                f"{route_label(self._router.label, chosen.route)} {exc}"
            )
            self._remember(issue_id)
            return

        if chosen.route != DEFAULT_ROUTE:
            # Only for a narrowed route. `run()` already said what the default
            # is, and repeating it on every issue would be noise.
            log(f"  route      {route_label(self._router.label, chosen.route)}")

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
        except BaseException:
            # `CancelledError` is meant to travel: it is how `chord stop` reaches
            # the watcher, and swallowing it here would make the watcher
            # unstoppable. Every other `BaseException` — `KeyboardInterrupt`, a
            # library raising something outside `Exception`, a `SystemExit` from
            # a harness — leaves the issue unrecorded, which means the same work
            # is handed to the agent again on the next poll, and the one after
            # that, for as long as the process lives.
            #
            # Recording it and letting the exception on its way closes that. It
            # does not change the at-least-once contract: the agent may still
            # have done the work before the exception, and re-running it is the
            # documented behaviour rather than a bug. The alternative, an
            # in-progress marker written before the run, would trade this
            # duplicate for the possibility of never running the work at all,
            # which is the worse of the two.
            self._remember(issue_id)
            raise

        _spacer()
        log("  handed over.")
        self._remember(issue_id)
        await notify.send("Chord done", f"{identifier} finished")

    async def _resolve(
        self, issue: Issue, identifier: str, issue_id: Any
    ) -> Routed:
        """Which harness this issue is routed to, and who routed it.

        Both questions come out of one read of Linear's audit trail, because
        both are answered by the same entry: the one that added the routing
        label decides where the work goes, and the actor of that entry is the
        person whose name is on the decision. Reading it twice would cost two
        round trips to learn two halves of one fact.

        `who` is None whenever the answer is not knowable — the entry has
        fallen off the audit page, Linear sent neither an actor nor a bot, or
        the read failed. Callers treat that as "ask nobody", never as "ask
        anyone": a permissive fallback here would turn the allowlist off
        silently on exactly the issues it is hardest to reason about.
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
            return Routed(DEFAULT_ROUTE, None, NO_LABEL)

        # One round trip is spent on the routing choice when there is one, and
        # unconditionally when there is an allowlist, because then the actor is
        # needed even for an issue whose route is not in doubt.
        ambiguous = len(candidates) > 1
        if not ambiguous and not self._router.authorises:
            return Routed(candidates[0], None, UNAMBIGUOUS)

        try:
            changes = await self._linear.label_history(str(issue_id))
        except Exception as exc:
            # Attribution and routing degrade differently, and both are said out
            # loud: a routing guess runs the work somewhere that may not be what
            # was asked for, so it is reported; attribution falling back to the
            # issue's creator is a documented approximation, so it is reported
            # too rather than being invisible.
            if ambiguous:
                self._problem(
                    f"couldn't read the label history for {identifier} "
                    f"({one_line(str(exc))}); using the most specific of "
                    f"{len(candidates)} route labels"
                )
            else:
                self._problem(
                    f"couldn't read who added the routing label for {identifier} "
                    f"({one_line(str(exc))}); falling back to the issue's creator"
                )
            return Routed(candidates[0], None, UNREADABLE)

        names = {route_label(self._router.label, route): route for route in candidates}
        for change in changes:
            for name in change.labels:
                route = names.get(name)
                if route is not None:
                    if ambiguous and change.who is None:
                        self._problem(
                            f"couldn't tell who added {name!r} to {identifier}; "
                            "using the most specific route label it carries"
                        )
                    return Routed(route, change.who, FROM_HISTORY)

        # The trail was readable and simply doesn't mention any of this issue's
        # route labels: the label was applied long enough ago to be off the page.
        if ambiguous:
            self._problem(
                f"{identifier}'s label history doesn't mention any of its "
                f"{len(candidates)} route labels; using the most specific"
            )
        return Routed(candidates[0], None, OFF_THE_PAGE)

    async def _authorise(
        self, issue: Issue, identifier: str, issue_id: Any, routed: Routed
    ) -> bool:
        """Whether the person who routed this issue may have it run.

        The act that causes code to run is adding the routing label, so that is
        what is checked against `allowed_actors`. When that cannot be attributed
        — the entry is off the audit page, or an integration applied the label —
        the issue's creator stands in, and the log names which of the two was
        used. A fallback that was invisible would not be a fallback you could
        reason about at the moment it mattered.

        Refusals are recorded, like every other hand-over that did not happen,
        so one unapproved issue is not re-examined on every poll for the life of
        the watcher. Which is why the second line exists: removing the label
        won't bring the issue back, and a message that doesn't say so sends
        someone looking for a fix that isn't one.
        """
        if not self._router.authorises:
            return True

        actor_id = routed.who.id if routed.who else None
        asked = f"added {self._router.default_name!r}" if routed.who else "created the issue"
        if routed.who is None:
            creator = issue.get("creator")
            actor_id = str(creator.get("id")) if isinstance(creator, dict) else None
            actor_id = actor_id or None
            asked = "created the issue"

        if actor_id and self._router.authorises_actor(actor_id):
            # Named, because which of the two attributions was used is the
            # difference between a check and an approximation.
            who = routed.who.name if routed.who else _name(issue.get("creator"))
            log(f"  allowed     {who}, who {asked}")
            return True

        if routed.who is not None:
            subject = f"{routed.who.name} {asked}"
        elif actor_id:
            subject = f"{_name(issue.get('creator'))} {asked}"
        else:
            subject = (
                f"nobody Chord can identify {asked} (the routing label's history "
                f"is off Linear's {MAX_HISTORY}-entry page, and the issue has no creator)"
            )

        _spacer()
        self._problem(f"{identifier} skipped: {subject}, and that is not in allowed_actors.")
        _spacer()
        self._problem(
            f"  to allow them: add their Linear user id to allowed_actors in chord.toml "
            f"(see `chord info` for yours), then drop {_key(issue_id)} from {self._path.name}."
        )
        self._remember(issue_id)
        return False

    def _remember(self, issue_id: object) -> None:
        self._seen[_key(issue_id)] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # Keep the newest entries; dicts hold insertion order, so the tail is
        # the most recent.
        trimmed = dict(list(self._seen.items())[-STATE_CAP:])
        self._seen = trimmed
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            body = json.dumps({"handed_over": trimmed}, indent=2) + "\n"
            daemon._write_private(self._path, body)
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
