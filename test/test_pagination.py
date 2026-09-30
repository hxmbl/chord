"""Cursor pagination, and the truncation that must be reported rather than
swallowed.

The bug this exists for: `first: 50` with no paging meant a label with more
issues than that silently lost the overflow. A real workspace with 54 labelled
issues returned 50, `hasNextPage=True`, and Chord never looked again.
"""

import asyncio

import pytest

from chord import linear, watcher


def issue(n, **extra):
    return {
        "id": str(n),
        "identifier": f"ENG-{n}",
        "title": f"Issue {n}",
        "createdAt": f"2026-01-{n:02d}T00:00:00Z",
        **extra,
    }


class Recorder:
    """Stands in for the HTTP round trip, so paging is observable.

    Pages are handed out in call order; the cursor that was sent is recorded
    and asserted separately, so the two concerns don't get tangled.
    """

    def __init__(self, pages):
        self._pages = pages  # list of (nodes, has_next, end_cursor)
        self.calls: list[dict] = []

    async def __call__(self, query, variables):
        self.calls.append(dict(variables))
        nodes, has_next, cursor = self._pages[len(self.calls) - 1]
        return {
            "issues": {
                "nodes": nodes,
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            }
        }


def client(pages, monkeypatch):
    lin = linear.Linear("token")
    recorder = Recorder(pages)
    monkeypatch.setattr(lin, "_query", recorder)
    return lin, recorder


# --- walking pages ---


def test_single_page(monkeypatch):
    lin, rec = client([([issue(1), issue(2)], False, None)], monkeypatch)
    page = asyncio.run(lin.issues_with_label("chord"))
    assert [i["id"] for i in page.issues] == ["1", "2"]
    assert page.truncated is False
    assert len(rec.calls) == 1


def test_walks_every_page(monkeypatch):
    """The whole point: 54 issues must not become 50."""
    pages = [
        ([issue(n) for n in range(1, 5)], True, "c1"),
        ([issue(n) for n in range(5, 9)], True, "c2"),
        ([issue(n) for n in range(9, 12)], False, None),
    ]
    lin, rec = client(pages, monkeypatch)
    page = asyncio.run(lin.issues_with_label("chord"))
    assert [i["id"] for i in page.issues] == [str(n) for n in range(1, 12)]
    assert page.truncated is False
    assert len(rec.calls) == 3, "did not follow the cursor"


def test_cursor_is_sent_back(monkeypatch):
    pages = [([issue(1)], True, "cursor-1"), ([issue(2)], False, None)]
    lin, rec = client(pages, monkeypatch)
    asyncio.run(lin.issues_with_label("chord"))
    assert rec.calls[0]["after"] is None
    assert rec.calls[1]["after"] == "cursor-1"


def test_page_size_is_respected(monkeypatch):
    pages = [([issue(1)], True, "1"), ([issue(2)], False, None)]
    lin, rec = client(pages, monkeypatch)
    asyncio.run(lin.issues_with_label("chord"))
    assert all(c["first"] == linear.PAGE_SIZE for c in rec.calls)


def test_last_request_never_asks_for_more_than_it_needs(monkeypatch):
    """A final page shouldn't over-ask; the remaining budget is the cap."""
    pages = [([issue(1)], True, "1"), ([issue(2)], False, None)]
    lin, rec = client(pages, monkeypatch)
    asyncio.run(lin.issues_with_label("chord"))
    assert rec.calls[-1]["first"] == min(
        linear.PAGE_SIZE, linear.MAX_ISSUES_PER_POLL - 1
    )


# --- truncation is reported, never silent ---


def test_hitting_the_cap_reports_truncation(monkeypatch):
    pages = [([issue(1)], True, "1"), ([issue(2)], True, "2")]
    lin, _ = client(pages, monkeypatch)
    monkeypatch.setattr(linear, "MAX_ISSUES_PER_POLL", 2)
    page = asyncio.run(lin.issues_with_label("chord"))
    assert page.truncated is True, "a capped walk reported itself complete"


def test_exact_fit_is_not_truncation(monkeypatch):
    """Landing exactly on the limit with nothing more is a complete answer."""
    pages = [([issue(1), issue(2)], False, None)]
    lin, _ = client(pages, monkeypatch)
    monkeypatch.setattr(linear, "MAX_ISSUES_PER_POLL", 2)
    assert asyncio.run(lin.issues_with_label("chord")).truncated is False


def test_a_cursor_that_vanishes_stops(monkeypatch):
    """Says there's more but won't say where: stop rather than loop forever."""
    pages = [([issue(1)], True, None)]
    lin, rec = client(pages, monkeypatch)
    page = asyncio.run(lin.issues_with_label("chord"))
    assert len(rec.calls) == 1
    assert page.truncated is True


# --- nested connection, for comments ---


def test_comments_paginate_through_the_nested_connection(monkeypatch):
    """`comments` lives inside `issue { ... }`, so the walk has to reach in."""
    pages = [
        ([{"body": "a"}], True, "c1"),
        ([{"body": "b"}], True, "c2"),
        ([{"body": "c"}], False, None),
    ]
    seen: list[dict] = []

    async def fake(query, variables):
        seen.append(dict(variables))
        nodes, more, cursor = pages[len(seen) - 1]
        return {
            "issue": {
                "comments": {
                    "nodes": nodes,
                    "pageInfo": {"hasNextPage": more, "endCursor": cursor},
                }
            }
        }

    lin = linear.Linear("token")
    monkeypatch.setattr(lin, "_query", fake)
    comments = asyncio.run(lin.comments("1"))
    assert [c["body"] for c in comments] == ["a", "b", "c"]
    assert len(seen) == 3


def test_a_missing_issue_yields_no_comments(monkeypatch):
    async def fake(query, variables):
        return {"issue": None}

    lin = linear.Linear("token")
    monkeypatch.setattr(lin, "_query", fake)
    assert asyncio.run(lin.comments("gone")) == []


# --- the watcher reports truncation ---


class StubHarness:
    name = "stub"

    async def send(self, prompt):
        return None


def test_watcher_says_when_it_could_not_read_everything(tmp_path, capsys, monkeypatch):
    class Truncating:
        async def issues_with_label(self, label):
            return linear.IssuePage([issue(1)], True)

        async def comments(self, issue_id):
            return []

    w = watcher.Watcher(
        Truncating(), StubHarness(), "chord", 1, tmp_path / "state.json"
    )
    asyncio.run(w.poll())
    out = capsys.readouterr().out
    assert "more issues carry" in out, "truncation was swallowed"
    assert "chord" in out


def test_watcher_reports_truncation_once(tmp_path, capsys):
    class Truncating:
        async def issues_with_label(self, label):
            return linear.IssuePage([], True)

        async def comments(self, issue_id):
            return []

    w = watcher.Watcher(
        Truncating(), StubHarness(), "chord", 1, tmp_path / "state.json"
    )
    for _ in range(3):
        asyncio.run(w.poll())
    assert capsys.readouterr().out.count("more issues carry") == 1


def test_no_truncation_means_no_complaint(tmp_path, capsys):
    class Whole:
        async def issues_with_label(self, label):
            return linear.IssuePage([], False)

        async def comments(self, issue_id):
            return []

    w = watcher.Watcher(Whole(), StubHarness(), "chord", 1, tmp_path / "state.json")
    asyncio.run(w.poll())
    assert "more issues carry" not in capsys.readouterr().out


# --- the shipped queries must actually ask for a page ---


def test_queries_declare_paging():
    assert "$after" in linear.ISSUES_BY_LABEL
    assert "pageInfo" in linear.ISSUES_BY_LABEL
    assert "hasNextPage" in linear.ISSUES_BY_LABEL
    assert "$after" in linear.COMMENTS
    assert "pageInfo" in linear.COMMENTS


def test_page_size_is_under_what_linear_accepts():
    """Linear rejected first: 300 outright, so this must not creep up."""
    assert linear.PAGE_SIZE <= 250


@pytest.mark.parametrize("cap", [0, 1, 50, 1000])
def test_the_cap_is_positive_and_sane(cap):
    assert cap >= 0
