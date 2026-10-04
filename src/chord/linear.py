"""Asking Linear what has been labelled, and what was said about it.

Chord never writes to Linear. Everything here is a read, and the only secret
involved is a token that credentials.py put in the keychain.
"""

import asyncio
from typing import Any, NamedTuple, Protocol

import httpx

from chord import logging
from chord.context import Issue
from chord.text import one_line

# Seconds for a single GraphQL request. `watcher.POLL_TIMEOUT` is the ceiling
# on the whole poll and sits deliberately above this, so a slow query surfaces
# as Linear being slow rather than as an unexplained poll timeout.
TIMEOUT = 30

GRAPHQL_ENDPOINT = "https://api.linear.app/graphql"

# How many issues to ask for per request. Linear accepts this comfortably; it
# rejected 300, so 100 keeps us under the ceiling with room to spare.
PAGE_SIZE = 100

# How many issues one poll will walk before it stops and says so. The cap is
# the point: a label with a huge backlog would otherwise mean a request per
# hundred issues on every tick, and a cap that reports itself is honest where
# a cap that truncates silently is not.
MAX_ISSUES_PER_POLL = 1000

# Same idea for a single issue's discussion. A harness gets the conversation,
# but an issue with a thousand comments is a sign something else is wrong, and
# reading all of it on every hand-over is not worth the tokens.
MAX_COMMENTS = 200

# How far back to look for the label that routed an issue. Only read when an
# issue carries more than one route label, and only ever to answer "which was
# added last". An issue whose most recent label changes are all older than this
# falls back to the most specific label it carries, which is the right guess
# anyway.
MAX_HISTORY = 50

RETRY_MAX_ATTEMPTS = 3
RETRY_BACKOFF_BASE = 1

# The route label is the whole of the trigger, so the filter happens at Linear.
# `routing.Router.filter()` builds it: a bare label or any `<label>/...` suffix,
# in one request, so the number of curated harnesses doesn't change how often
# Chord talks to Linear.
#
# `after` walks the connection: the first page alone used to be the whole
# result, which silently hid every issue past page one.
ISSUES_BY_ROUTE = """
query IssuesByRoute($filter: IssueFilter!, $first: Int!, $after: String) {
  issues(filter: $filter, first: $first, after: $after) {
    nodes {
      id
      identifier
      title
      description
      url
      priority
      createdAt
      state { name }
      labels { nodes { name } }
      creator { id name }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

# Which of an issue's labels was added last, read from Linear's own audit trail.
# This is what makes "use the newest matching label" a fact rather than a guess:
# `labels { nodes { name } }` comes back as an unordered set with no timestamps,
# so an issue carrying both `Chord` and `Chord/opencode/tiny` cannot be resolved
# from the issue alone. `addedLabels` says who put which label on and when.
#
# `actor` is who did it, which is what `allowed_actors` is checked against. It is
# null when an integration applied the label, and `botActor` says which one, so
# both are asked for: a label nobody's fingers were on is a real case, not a
# hypothetical, and it has to be reportable rather than indistinguishable from
# "we could not find out".
LABEL_HISTORY = """
query LabelHistory($id: String!, $first: Int!) {
  issue(id: $id) {
    history(first: $first) {
      nodes {
        createdAt
        addedLabels { name }
        actor { id name }
        botActor { id name }
      }
    }
  }
}
"""

# Comments are a second round trip on purpose. A labelled backlog can be long,
# and pulling every discussion on every poll would be a lot of reading to find
# the one issue that is new.
COMMENTS = """
query IssueComments($id: String!, $first: Int!, $after: String) {
  issue(id: $id) {
    comments(first: $first, after: $after) {
      nodes {
        body
        createdAt
        user { name }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


class LinearError(Exception):
    """Linear didn't give us what we asked for."""


class Actor(NamedTuple):
    """Whoever applied a label: a person, or an integration acting for one.

    `id` is what `allowed_actors` is matched against, because it is the only
    field on a Linear user that a user cannot rewrite themselves — `name` and
    `displayName` are both in `UserUpdateInput`, and neither is unique.

    `name` is here for the log. It is never rendered into a prompt: an issue
    goes to an agent, and an agent has no business carrying a list of your
    colleagues' names or addresses.
    """

    id: str
    name: str


class LabelChange(NamedTuple):
    """One entry of an issue's audit trail.

    Newest first, because that is the order a caller wants to scan: the first
    route label found is the one asked for most recently, and `who` says who
    asked. Kept as one record per history entry rather than flattened into a
    list of names, because the actor belongs to the *event*, so a flattened
    list would lose exactly the attribution the allowlist needs.
    """

    at: str
    labels: list[str]
    who: Actor | None


def flatten(changes: list[LabelChange]) -> list[str]:
    """Label names across `changes`, newest first, each name once.

    The first time a name appears is its most recent addition, so that is the
    one kept: a label added, removed and added again is worth one look, at the
    time it was last put on.
    """
    seen: set[str] = set()
    order: list[str] = []
    for change in changes:
        for name in change.labels:
            if name and name not in seen:
                seen.add(name)
                order.append(name)
    return order


def _actor(entry: dict[str, Any]) -> Actor | None:
    """Who applied the labels in one history entry, if the answer is knowable.

    Three cases, and the third is the reason this is not one lookup:
    a person did it, an integration did it, or Linear sent neither. An
    integration shows up as `actor: null` with `botActor` set, so without the
    second field a bot-applied label is indistinguishable from an unreadable
    one — and both mean "no person authorised this", which the caller has to be
    able to say out loud rather than guess at.
    """
    for field_name in ("actor", "botActor"):
        node = entry.get(field_name)
        if isinstance(node, dict) and node.get("id"):
            name = str(node.get("name") or "").strip()
            return Actor(
                id=str(node["id"]),
                # An integration's `name` is its slug; this reads better in a log
                # and never reaches a prompt either way.
                name=name or "an integration",
            )
    return None


class IssuePage(NamedTuple):
    """One poll's worth of labelled issues.

    `truncated` says the walk stopped at `MAX_ISSUES_PER_POLL` with more to
    come. The caller reports it rather than pretending the list is complete,
    which is the difference between a limit a person can reason about and a
    limit that eats their issues.
    """

    issues: list[Issue]
    truncated: bool


class LinearClient(Protocol):
    async def issues_for(self, filter: dict[str, Any]) -> IssuePage: ...

    async def label_history(self, issue_id: str) -> list[LabelChange]: ...

    async def comments(self, issue_id: str) -> list[dict[str, Any]]: ...


class Linear:
    def __init__(self, access_token: str) -> None:
        # The token lives only in this header. Nothing here logs a request, and
        # the failures below report status codes rather than bodies so a token
        # can't ride along into the log.
        self._headers = {"Authorization": f"Bearer {access_token}"}

    async def issues_for(self, filter: dict[str, Any]) -> IssuePage:
        nodes, truncated = await self._paged(
            ISSUES_BY_ROUTE, "issues", {"filter": filter}, MAX_ISSUES_PER_POLL
        )
        return IssuePage(nodes, truncated)

    async def label_history(self, issue_id: str) -> list[LabelChange]:
        """One record per audit-trail entry, newest first.

        Newest first because that is the order a caller wants to scan, and
        because Linear's own order for `history` was verified against a live
        workspace rather than assumed — it is newest-first and `first: N` takes
        the newest N. It is still sorted here, because the answer decides which
        harness runs and who is allowed to ask, and a silent reordering would be
        a silent misrouting. ISO 8601 in UTC sorts correctly as text, which is
        what every Linear timestamp is.
        """
        data = await self._query(LABEL_HISTORY, {"id": issue_id, "first": MAX_HISTORY})
        entries = ((data.get("issue") or {}).get("history") or {}).get("nodes") or []

        # Sorted here rather than trusted, because the answer decides which
        # harness runs and who is allowed to ask, and a silent reordering would
        # be a silent misrouting. ISO 8601 in UTC sorts correctly as text,
        # which is what every Linear timestamp is.
        ordered = sorted(
            (entry for entry in entries if isinstance(entry, dict)),
            key=lambda entry: str(entry.get("createdAt") or ""),
            reverse=True,
        )

        changes: list[LabelChange] = []
        for entry in ordered:
            labels = [
                str(label.get("name") or "")
                for label in entry.get("addedLabels") or []
                if isinstance(label, dict)
            ]
            changes.append(
                LabelChange(
                    at=str(entry.get("createdAt") or ""),
                    labels=[name for name in labels if name],
                    who=_actor(entry),
                )
            )
        return changes

    async def comments(self, issue_id: str) -> list[dict[str, Any]]:
        nodes, _ = await self._paged(
            COMMENTS, "issue", {"id": issue_id}, MAX_COMMENTS, inner="comments"
        )
        return nodes

    async def _paged(
        self,
        query: str,
        field: str,
        variables: dict[str, Any],
        limit: int,
        inner: str | None = None,
    ) -> tuple[list[Any], bool]:
        """Walk a Relay connection to the end, or to `limit`, whichever is first.

        Returns the nodes and whether it stopped short. `inner` names a nested
        connection, for the case where the top-level field is an object
        wrapping one (`issue { comments { ... } }`).
        """
        nodes: list[Any] = []
        after: str | None = None

        while len(nodes) < limit:
            data = await self._query(
                query,
                {
                    **variables,
                    "first": min(PAGE_SIZE, limit - len(nodes)),
                    "after": after,
                },
            )
            connection = data.get(field) or {}
            if inner:
                connection = connection.get(inner) or {}

            nodes.extend(connection.get("nodes") or [])

            page = connection.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return nodes, False
            after = page.get("endCursor")
            if not after:
                # Says there's more but won't say where. Stop rather than ask
                # for the same page forever.
                return nodes, True

        # Out of budget with Linear still offering more.
        return nodes, True

    async def _query(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(RETRY_MAX_ATTEMPTS):
            try:
                async with httpx.AsyncClient(timeout=TIMEOUT) as http:
                    response = await http.post(
                        GRAPHQL_ENDPOINT,
                        json={"query": query, "variables": variables},
                        headers=self._headers,
                    )
            except (httpx.HTTPError, TimeoutError) as exc:
                if attempt + 1 == RETRY_MAX_ATTEMPTS:
                    raise LinearError(
                        f"Couldn't reach Linear ({type(exc).__name__})."
                    ) from exc
                delay = RETRY_BACKOFF_BASE * 2**attempt
                logging.warning(
                    f"Linear request failed ({type(exc).__name__}); "
                    f"retrying in {delay}s (attempt {attempt + 2}/{RETRY_MAX_ATTEMPTS})."
                )
                await asyncio.sleep(delay)
                continue

            if 500 <= response.status_code < 600:
                if attempt + 1 == RETRY_MAX_ATTEMPTS:
                    raise LinearError(f"Linear returned HTTP {response.status_code}.")
                delay = RETRY_BACKOFF_BASE * 2**attempt
                logging.warning(
                    f"Linear returned HTTP {response.status_code}; "
                    f"retrying in {delay}s (attempt {attempt + 2}/{RETRY_MAX_ATTEMPTS})."
                )
                await asyncio.sleep(delay)
                continue
            break

        if response.status_code in (401, 403):
            # The status and nothing more: the body can carry workspace detail,
            # and a rejected token is one `chord refresh` away from fine.
            raise LinearError(
                f"Linear rejected the token (HTTP {response.status_code}). "
                "Run `chord refresh`."
            )
        if response.status_code != 200:
            raise LinearError(f"Linear returned HTTP {response.status_code}.")

        try:
            body = response.json()
        except ValueError as exc:
            raise LinearError("Linear sent back something that isn't JSON.") from exc

        # GraphQL answers 200 and puts failures in an `errors` list, so the
        # status code alone doesn't tell us the query worked. The first message
        # is nearly always the one that explains it.
        errors = body.get("errors")
        if errors:
            detail = errors[0].get("message") or "no detail given"
            raise LinearError(f"Linear turned down the query: {one_line(detail)}")

        data = body.get("data")
        if not isinstance(data, dict):
            raise LinearError("Linear sent back no data.")
        return data
