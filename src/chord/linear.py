"""Asking Linear what has been labelled, and what was said about it.

Chord never writes to Linear. Everything here is a read, and the only secret
involved is a token that credentials.py put in the keychain.
"""

from typing import Any, NamedTuple, Protocol

import httpx

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

# The label is the whole of the trigger, so the filter happens at Linear.
# `after` walks the connection: the first page alone used to be the whole
# result, which silently hid every issue past page one.
ISSUES_BY_LABEL = """
query IssuesByLabel($label: String!, $first: Int!, $after: String) {
  issues(filter: { labels: { name: { eq: $label } } }, first: $first, after: $after) {
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
    }
    pageInfo { hasNextPage endCursor }
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
    async def issues_with_label(self, label: str) -> IssuePage: ...

    async def comments(self, issue_id: str) -> list[dict[str, Any]]: ...


class Linear:
    def __init__(self, access_token: str) -> None:
        # The token lives only in this header. Nothing here logs a request, and
        # the failures below report status codes rather than bodies so a token
        # can't ride along into the log.
        self._headers = {"Authorization": f"Bearer {access_token}"}

    async def issues_with_label(self, label: str) -> IssuePage:
        nodes, truncated = await self._paged(
            ISSUES_BY_LABEL, "issues", {"label": label}, MAX_ISSUES_PER_POLL
        )
        return IssuePage(nodes, truncated)

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
        async with httpx.AsyncClient(timeout=TIMEOUT) as http:
            try:
                response = await http.post(
                    GRAPHQL_ENDPOINT,
                    json={"query": query, "variables": variables},
                    headers=self._headers,
                )
            except httpx.HTTPError as exc:
                raise LinearError(
                    f"Couldn't reach Linear ({type(exc).__name__})."
                ) from exc

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
