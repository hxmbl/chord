"""Read-only probe: does the routing Chord ships actually work against Linear?

Every finding from the bug review that mattered is fixed and tested, but the
queries are the one thing nobody can check without a live token. This asks
Linear directly, changes nothing, and writes nothing.

Run it with:  uv run python tools/probe_linear.py [label]
"""

import asyncio
import sys

import httpx

from chord.credentials import load
from chord.linear import (
    COMMENTS,
    GRAPHQL_ENDPOINT,
    ISSUES_BY_ROUTE,
    LABEL_HISTORY,
    Linear,
)
from chord.routing import Router

# Deliberately a *different* filter shape from the one Chord ships, so a pass
# here can't be explained by the production query being accidentally right.
EXPLORE = """
query Explore($label: String!) {
  issues(
    filter: {
      labels: { some: { name: { eq: $label } } }
    }
    first: 5
  ) {
    nodes { identifier title labels { nodes { name } } }
  }
}
"""

# What a plain, unfiltered read looks like, for comparison.
UNFILTERED = """
query Recent { issues(first: 5) { nodes { identifier labels { nodes { name } } } } }
"""


def header(text: str) -> None:
    print(f"\n{text}\n{'-' * len(text)}")


async def ask(query: str, variables: dict, token: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.post(
            GRAPHQL_ENDPOINT,
            json={"query": query, "variables": variables},
            headers={"Authorization": f"Bearer {token}"},
        )
    print(f"  HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        print("  (not JSON)")
        return {}
    for error in body.get("errors") or []:
        # Linear's own message text; this is not a Chord secret.
        print(f"  GraphQL error: {error.get('message')}")
    return body.get("data") or {}


def show(nodes: list[dict]) -> None:
    for node in nodes:
        names = [n["name"] for n in (node.get("labels") or {}).get("nodes", [])]
        print(f"     {node.get('identifier'):10} labels={names}")


async def main(label: str) -> int:
    code, content = load()
    if code:
        print(f"No usable token: {content}")
        return 1
    token = content["access_token"]
    router = Router(label, "print")

    header("viewer (is the token live?)")
    data = await ask("{ viewer { id email name } }", {}, token)
    if not data.get("viewer"):
        print("  -> token not accepted")
        return 1
    viewer = data["viewer"]
    print(f"  -> {viewer.get('name')} <{viewer.get('email')}>")

    header(f"issues for the route filter on {label!r} — Chord's shipped query")
    print(f"  filter: {router.filter()}")
    data = await ask(ISSUES_BY_ROUTE, {"filter": router.filter(), "first": 5}, token)
    nodes = (data.get("issues") or {}).get("nodes")
    if nodes is None:
        print("  -> FILTER REJECTED. This is the shape to fix.")
        return 1
    print(f"  -> accepted. {len(nodes)} issue(s) matched")
    show(nodes)

    if nodes:
        names = {
            n["name"]
            for node in nodes
            for n in (node.get("labels") or {}).get("nodes", [])
        }
        matched = {label, *(f"{label}/{c}" for c in router.names)}
        stray = sorted(n for n in names if n.startswith(f"{label}/") and n not in matched)
        if stray:
            print(f"  !! matched issues carry unrouted labels: {stray}")
        elif not (names & matched):
            print("  !! no matched issue carries a route label — filter is not filtering")
        else:
            print("  -> every matched label is a route Chord would act on")

    header(f"does a label that exists but has no issues behave? ({label!r} + '-nope')")
    await ask(
        ISSUES_BY_ROUTE,
        {"filter": {"labels": {"name": {"eq": f"{label}-nope"}}}, "first": 5},
        token,
    )

    header("alternative filter shape: labels: { some: { name: { eq } } }")
    alt = await ask(EXPLORE, {"label": label}, token)
    alt_nodes = (alt.get("issues") or {}).get("nodes")
    print("  -> accepted" if alt_nodes is not None else "  -> rejected")
    if alt_nodes:
        show(alt_nodes)

    header("unfiltered, for comparison")
    recent = await ask(UNFILTERED, {}, token)
    show((recent.get("issues") or {}).get("nodes", [])[:5])

    header("comments query (the second round trip)")
    if nodes:
        issue_id = nodes[0].get("id")
        data = await ask(COMMENTS, {"id": issue_id, "first": 50}, token)
        comments = ((data.get("issue") or {}).get("comments") or {}).get("nodes")
        if comments is None:
            print("  -> REJECTED")
        else:
            print(
                f"  -> accepted. {len(comments)} comment(s) on {nodes[0].get('identifier')}"
            )
    else:
        print("  skipped: no issues to comment on")

    header("label history (how 'newest route label' is decided)")
    if nodes:
        history = await ask(LABEL_HISTORY, {"id": nodes[0].get("id"), "first": 50}, token)
        entries = ((history.get("issue") or {}).get("history") or {}).get("nodes")
        if entries is None:
            print("  -> REJECTED")
        else:
            print(f"  -> accepted. {len(entries)} entr(ies), newest first:")
            for entry in entries:
                added = [a["name"] for a in (entry.get("addedLabels") or [])]
                print(f"       {entry.get('createdAt')} added={added}")
            issue_labels = [
                n["name"] for n in (nodes[0].get("labels") or {}).get("nodes", [])
            ]
            print(f"  issue carries {issue_labels}")
            print(f"  candidates     {router.candidates(nodes[0])}")
    else:
        print("  skipped: no issues to read history from")

    header("Linear class, end to end")
    client = Linear(token)
    page = await client.issues_for(router.filter())
    print(f"  -> Linear.issues_for(...) returned {len(page.issues)}")
    for issue in page.issues[:5]:
        print(f"     {issue.get('identifier')} {issue.get('title')}")
    for issue in page.issues[:1]:
        order = await client.label_history(issue.get("id"))
        print(f"  -> label_history({issue.get('identifier')}) = {order}")

    return 0


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "chord"
    raise SystemExit(asyncio.run(main(which)))