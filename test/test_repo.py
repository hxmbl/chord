"""Which repository a Linear issue gets worked on, and when it refuses.

Temporary repositories throughout. No Linear credentials, no agent.
"""

import subprocess

import pytest

from chord.repo import (
    FROM_DESCRIPTION,
    FROM_PROJECT,
    FROM_ROOT,
    ResolutionError,
    resolve,
)


def repo_at(path):
    """A directory at `path` that `git` will work in."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    return path


def plain_at(path):
    """A directory at `path` that isn't a repository."""
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- The project title -------------------------------------------------------


def test_the_project_root_is_its_own_repository(tmp_path):
    """Running Chord inside `chord` and asking for project `chord` works.

    The commonest case by some distance, and it needs no nested copy: the
    repository is already the directory Chord was started in.
    """
    start = repo_at(tmp_path / "chord")

    found = resolve({"project": {"name": "chord"}}, start)

    assert found.path == start
    assert found.source == FROM_PROJECT


def test_a_directory_inside_the_root_is_found(tmp_path):
    """A workspace holding several checkouts resolves to the matching one."""
    start = repo_at(tmp_path / "workspace")
    other = repo_at(start / "billing")

    found = resolve({"project": {"name": "billing"}}, start)

    assert found.path == other
    assert found.source == FROM_PROJECT


def test_the_search_does_not_leave_the_project_root(tmp_path):
    """A project named after a directory further up is not that directory.

    This is the regression that made the whole idea untrustworthy. The search
    used to walk every ancestor to `/`, so a Linear project called `home` or
    `Users` matched `$HOME` or `/Users` and put the agent somewhere nobody asked
    for — the precise failure this module exists to prevent.

    The names are the ones this machine actually has, which is the point: they
    are ordinary project names that happen to collide with the filesystem.
    """
    start = repo_at(tmp_path / "workspace")

    for name in ("home", "Users", "tmp", "private", "var", "etc"):
        found = resolve({"project": {"name": name}}, start)
        assert found.source == FROM_ROOT, f"{name!r} reached outside the project root"
        assert found.path == start


def test_a_nested_directory_of_the_same_name_is_not_found(tmp_path):
    """The search is two candidates deep, not a walk.

    A match buried several levels down is a coincidence, not a decision anybody
    made, and acting on one is how the search became unpredictable in the first
    place.
    """
    start = repo_at(tmp_path / "workspace")
    repo_at(start / "src" / "components" / "billing")

    found = resolve({"project": {"name": "billing"}}, start)

    assert found.source == FROM_ROOT


# --- The explicit line ------------------------------------------------------


def test_a_repository_line_in_the_description_is_used(tmp_path):
    """The fallback for a repository that isn't a child of the project root."""
    start = repo_at(tmp_path / "workspace")
    elsewhere = repo_at(tmp_path / "elsewhere" / "billing")

    issue = {"project": None, "description": f'[Chord_Working_Path: "{elsewhere}"]'}

    found = resolve(issue, start)

    assert found.path == elsewhere
    assert found.source == FROM_DESCRIPTION


def test_a_relative_repository_line_is_read_from_the_project_root(tmp_path):
    """A description stays portable when the path is relative to where Chord runs."""
    start = repo_at(tmp_path / "workspace")
    repo_at(start / "billing")

    issue = {"project": None, "description": '[Chord_Working_Path: "billing"]'}

    found = resolve(issue, start)

    assert found.path == (start / "billing").resolve()


def test_a_named_repository_that_is_not_a_repository_is_an_error(tmp_path):
    """A location somebody wrote down is not quietly worked around.

    The whole point of refusing here: the issue said where the work happens,
    and doing it somewhere else would be worse than not doing it.
    """
    start = repo_at(tmp_path / "workspace")
    elsewhere = tmp_path / "elsewhere" / "billing"
    plain_at(elsewhere)

    issue = {"project": None, "description": f'[Chord_Working_Path: "{elsewhere}"]'}

    with pytest.raises(ResolutionError, match="isn't a Git repository"):
        resolve(issue, start)


def test_a_named_directory_that_is_not_a_repository_is_an_error(tmp_path):
    """Same when the name came from the project rather than the description."""
    start = repo_at(tmp_path / "workspace")
    plain_at(start / "billing")

    with pytest.raises(ResolutionError, match="isn't a Git repository"):
        resolve({"project": {"name": "billing"}}, start)


def test_a_tilde_in_the_repository_line_is_expanded(tmp_path, monkeypatch):
    """`~` is what a person writes, and pathlib would read it as a directory name.

    HOME is redirected rather than using the real one, so the test doesn't leave
    a repository sitting in the developer's home directory.
    """
    start = repo_at(tmp_path / "workspace")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo_at(home / "billing")

    issue = {"project": None, "description": '[Chord_Working_Path: "~/billing"]'}

    found = resolve(issue, start)

    assert found.path == home / "billing"
    assert found.source == FROM_DESCRIPTION


def test_the_line_name_is_matched_in_any_case(tmp_path):
    """Nobody will remember the capitalisation, so it cannot matter.

    `chord_working_path` is what somebody will actually type, and a line that
    silently doesn't match falls back to the project root — the wrong code,
    quietly, which is the failure this module exists to prevent.
    """
    start = repo_at(tmp_path / "workspace")
    elsewhere = repo_at(tmp_path / "elsewhere" / "billing")

    issue = {"project": None, "description": f'[chord_working_path: "{elsewhere}"]'}

    found = resolve(issue, start)

    assert found.path == elsewhere
    assert found.source == FROM_DESCRIPTION


def test_either_quote_character_is_accepted(tmp_path):
    """Single or double, in any case of the name — both are what a person types."""
    start = repo_at(tmp_path / "workspace")
    repo_at(start / "billing")

    issue = {"project": None, "description": "[CHORD_WORKING_PATH: 'billing']"}

    found = resolve(issue, start)

    assert found.path == (start / "billing").resolve()
    assert found.source == FROM_DESCRIPTION


@pytest.mark.parametrize(
    "description",
    [
        pytest.param(
            "[Chord_Working_Path: billing]",
            id="no quotes",
        ),
        pytest.param(
            '[Chord_Working_Path: "billing',
            id="quote never closed",
        ),
        pytest.param(
            "[Chord_Working_Path: 'billing\"]",
            id="quotes do not match",
        ),
        pytest.param(
            '[Chord_Working_Path: ""]',
            id="empty quotes",
        ),
        pytest.param(
            '[Chord_Working_Path: "billing" here]',
            id="more than the path",
        ),
    ],
)
def test_a_malformed_line_is_an_error_not_a_fallback(tmp_path, description):
    """A location that is present but unreadable is refused, never ignored.

    Treating any of these as "no line" would fall back to the project root and
    work on the wrong code quietly — so the hand-over stops and says what the
    line should have looked like.
    """
    start = repo_at(tmp_path / "workspace")

    with pytest.raises(ResolutionError, match="Chord_Working_Path"):
        resolve({"project": None, "description": description}, start)


# --- Precedence -------------------------------------------------------------


def test_the_project_title_wins_over_the_description(tmp_path):
    """Both present and both valid: the project is the more specific answer."""
    start = repo_at(tmp_path / "workspace")
    named = repo_at(start / "billing")
    other = repo_at(tmp_path / "elsewhere" / "billing")

    issue = {
        "project": {"name": "billing"},
        "description": f'[Chord_Working_Path: "{other}"]',
    }

    found = resolve(issue, start)

    assert found.path == named
    assert found.source == FROM_PROJECT


# --- The fallback -----------------------------------------------------------


def test_an_issue_that_says_nothing_is_worked_on_in_the_project_root(tmp_path):
    """No project, no line, and the hand-over still happens.

    Most Linear issues have neither. Refusing them would stop Chord working
    for anyone who has not reorganised their workspace around directory names,
    so this falls back — and says so, which is what makes it acceptable.
    """
    start = repo_at(tmp_path / "workspace")

    found = resolve({"project": None, "description": "just a bug"}, start)

    assert found.path == start
    assert found.source == FROM_ROOT


def test_an_unmatched_project_title_also_falls_back(tmp_path):
    """A project that matches no directory is not a reason to stop working.

    The project is still worth reporting — `resolve` only knows the directory,
    so the log line is where the reader learns which name went unmatched — but
    it is not an error, because the issue is otherwise perfectly runnable.
    """
    start = repo_at(tmp_path / "workspace")

    found = resolve({"project": {"name": "billing"}, "description": ""}, start)

    assert found.path == start
    assert found.source == FROM_ROOT


def test_the_fallback_is_refused_when_it_is_not_a_repository(tmp_path):
    """With nowhere to fall back to, this is a genuine failure.

    Both answers unusable, and running the harness anywhere else would be the
    silent wrong-repository problem the module was written to stop.
    """
    start = plain_at(tmp_path / "workspace")

    with pytest.raises(ResolutionError, match="isn't a Git repository"):
        resolve({"project": None, "description": ""}, start)


# --- Git detection ----------------------------------------------------------


def test_a_linked_worktree_counts_as_a_repository(tmp_path):
    """`.git` is a file in a worktree, so the check asks Git rather than stat."""
    main = repo_at(tmp_path / "main")
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(linked), "-b", "other"],
        cwd=main,
        check=True,
        capture_output=True,
    )

    assert (linked / ".git").is_file(), "expected the linked form, not a directory"

    found = resolve(
        {"project": None, "description": f'[Chord_Working_Path: "{linked}"]'}, main
    )

    assert found.path == linked.resolve()
    assert found.source == FROM_DESCRIPTION


def test_a_directory_that_only_looks_like_a_repository_is_not_one(tmp_path):
    """An empty `.git` directory is not a repository, and git agrees."""
    start = repo_at(tmp_path / "workspace")
    fake = plain_at(tmp_path / "fake")
    (fake / ".git").mkdir()

    issue = {"project": None, "description": f'[Chord_Working_Path: "{fake}"]'}

    with pytest.raises(ResolutionError, match="isn't a Git repository"):
        resolve(issue, start)


def test_a_path_that_does_not_exist_is_not_a_repository(tmp_path):
    """The message names the path, so a typo is findable."""
    start = repo_at(tmp_path / "workspace")

    issue = {
        "project": None,
        "description": f'[Chord_Working_Path: "{tmp_path / "nope"}"]',
    }

    with pytest.raises(ResolutionError, match="nope"):
        resolve(issue, start)


# --- Unexpected payloads ----------------------------------------------------


@pytest.mark.parametrize("description", [42, None, [], {"a": 1}, True])
def test_a_description_that_is_not_text_is_simply_no_repository(tmp_path, description):
    """Linear's description is a nullable string field, and it is not always one.

    `render` is hardened against exactly these shapes, because an issue whose
    payload is odd still has work in it. Reading a repository out of it cannot
    be the place that stops the hand-over, so anything that isn't text is
    treated as no line at all and the issue falls back like any other.
    """
    start = repo_at(tmp_path / "workspace")

    found = resolve({"project": None, "description": description}, start)

    assert found.source == FROM_ROOT
    assert found.path == start


# --- What a person sees when it goes wrong -----------------------------------


def test_the_project_title_matches_a_folder_in_any_case(tmp_path):
    """Linear project `Chord`, checkout `chord`: the same thing on any OS."""
    start = plain_at(tmp_path / "home")
    checkout = repo_at(start / "chord")

    found = resolve({"project": {"name": "Chord"}}, start)

    assert found.path == checkout
    assert found.source == FROM_PROJECT


def test_an_exact_match_beats_another_case(tmp_path):
    start = plain_at(tmp_path / "home")
    repo_at(start / "chord")
    exact = repo_at(start / "Chord")
    if (start / "chord").samefile(exact):
        pytest.skip("case-insensitive filesystem")

    assert resolve({"project": {"name": "Chord"}}, start).path == exact


def test_nowhere_to_run_names_the_project_and_says_what_to_do(tmp_path):
    """One reason and one fix, each short enough not to be cut off."""
    start = plain_at(tmp_path / "home")

    with pytest.raises(ResolutionError) as caught:
        resolve({"project": {"name": "Chord"}}, start)

    reason = str(caught.value)
    assert "no folder named 'Chord'" in reason
    assert f"in {start}, which isn't a Git repository itself" in reason
    assert "Chord_Working_Path" in caught.value.hint
    assert "Start Chord in the repository" in caught.value.hint
