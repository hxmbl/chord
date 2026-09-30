"""Read-only probe: does the label filter Chord ships actually work?

Every finding from the bug review that mattered is fixed and tested, but the
label filter was the one thing nobody could check without a live token. This
asks Linear directly, changes nothing, and writes nothing.

Run it with:  uv run python tools/probe_linear.py [label]
"""

import asyncio
import sys

import httpx

from chord.credentials import load
from chord.linear import COMMENTS, GRAPHQL_ENDPOINT, ISSUES_BY_LABEL, Linear

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


async def main(label: str) -> int:
    code, content = load()
    if code:
        print(f"No usable token: {content}")
        return 1
    token = content["access_token"]

    header("viewer (is the token live?)")
    data = await ask("{ viewer { id email name } }", {}, token)
    if not data.get("viewer"):
        print("  -> token not accepted")
        return 1
    viewer = data["viewer"]
    print(f"  -> {viewer.get('name')} <{viewer.get('email')}>")

    header(f"issues by label {label!r} — Chord's shipped filter")
    data = await ask(ISSUES_BY_LABEL, {"label": label}, token)
    nodes = (data.get("issues") or {}).get("nodes")
    if nodes is None:
        print("  -> FILTER REJECTED. This is the shape to fix.")
        return 1
    print(f"  -> accepted. {len(nodes)} issue(s) matched")
    for node in nodes[:10]:
        names = [n["name"] for n in (node.get("labels") or {}).get("nodes", [])]
        print(f"     {node.get('identifier'):10} labels={names}")

    if nodes:
        labels = {
            n["name"]
            for node in nodes
            for n in (node.get("labels") or {}).get("nodes", [])
        }
        if label not in labels:
            print(
                f"  !! matched issues do NOT all carry {label!r} — filter is not filtering"
            )
        else:
            print("  -> every matched issue carries the label, as it should")

    header(f"does a label that exists but has no issues behave? ({label!r} + '-nope')")
    await ask(ISSUES_BY_LABEL, {"label": f"{label}-nope"}, token)

    header("alternative filter shape: labels: { some: { name: { eq } } }")
    alt = await ask(EXPLORE, {"label": label}, token)
    alt_nodes = (alt.get("issues") or {}).get("nodes")
    print("  -> accepted" if alt_nodes is not None else "  -> rejected")

    header("unfiltered, for comparison")
    recent = await ask(UNFILTERED, {}, token)
    for node in (recent.get("issues") or {}).get("nodes", [])[:5]:
        names = [n["name"] for n in (node.get("labels") or {}).get("nodes", [])]
        print(f"     {node.get('identifier'):10} labels={names}")

    header("comments query (the second round trip)")
    if nodes:
        issue_id = nodes[0].get("id")
        data = await ask(COMMENTS, {"id": issue_id}, token)
        comments = ((data.get("issue") or {}).get("comments") or {}).get("nodes")
        if comments is None:
            print("  -> REJECTED")
        else:
            print(
                f"  -> accepted. {len(comments)} comment(s) on {nodes[0].get('identifier')}"
            )
    else:
        print("  skipped: no issues to comment on")

    header("Linear class, end to end")
    client = Linear(token)
    found = await client.issues_with_label(label)
    print(f"  -> Linear.issues_with_label({label!r}) returned {len(found)}")
    for issue in found[:5]:
        print(f"     {issue.get('identifier')} {issue.get('title')}")

    return 0


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "chord"
    raise SystemExit(asyncio.run(main(which)))
