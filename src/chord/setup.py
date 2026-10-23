"""`chord setup`: from nothing to ready to watch, by answering a few questions.

Three steps, each skipped when it is already done, so running it again is
always safe:

1. The Linear app. Chord signs in through an OAuth app the person owns, so its
   client id and secret go in `.env`. Asked for here rather than hand-written.
2. Signing in. The browser round trip `credentials.authenticate` already does.
3. This project. The trigger label and the coding tool, with the person who
   just signed in as the only one allowed to trigger a run. Written to
   `.config/chord/chord.toml`.

Nothing here writes to Linear, creates a label, or overwrites a config someone
already wrote. Every answer has a default, so pressing Enter all the way
through gives a working setup.
"""

import asyncio
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import typer
from dotenv import dotenv_values, set_key

from chord import credentials
from chord.config import DEFAULT_LABEL, ConfigError, write_setup
from chord.harness import PRESETS, missing
from chord.paths import ENV_NAME, config_path, find_upwards, project_root

REDIRECT_URL = f"http://127.0.0.1:{credentials.CALLBACK_PORT}/callback"

# Coding tools Chord can offer, in the order they are offered. Each is a
# harness Chord knows by name — the default `opencode`, and the presets in
# `harness.PRESETS` — so the config gets `harness = "claude"` and the flags stay
# Chord's to keep right. Only the ones on PATH are offered.
KNOWN_TOOLS: tuple[str, ...] = ("opencode", *PRESETS)


class SetupError(Exception):
    """A step that cannot go on. The message is one line saying what to do."""


def interactive() -> bool:
    """Whether a person is at the terminal to answer questions."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def run(say: Callable[[str], None] = typer.echo) -> None:
    """The whole guided setup. Raises `SetupError` on a step it can't finish."""
    say("Setting up Chord.\n")
    _linear_app(say)
    viewer = _sign_in(say)
    _project(say, viewer)


# --- 1. the Linear app ---


def _env_path() -> Path:
    found = find_upwards(ENV_NAME)
    return found if found.is_file() else project_root() / ENV_NAME


def _linear_app(say: Callable[[str], None]) -> None:
    path = _env_path()
    env = dotenv_values(path) if path.is_file() else {}
    if env.get("LINEAR_CLIENT_ID") and env.get("LINEAR_CLIENT_SECRET"):
        say(f"1. Linear app: found in {path}.")
        return

    say("1. Linear app")
    say("   Chord signs in through an OAuth app you own. In Linear, open")
    say("   Settings > Security & access > OAuth applications > New, and use:")
    say(f"     Redirect URL   {REDIRECT_URL}")
    say("     Permissions    read")
    say("   Linear shows the secret once, so keep the page open.\n")
    client_id = typer.prompt("   Client ID").strip()
    secret = typer.prompt("   Client secret", hide_input=True).strip()
    if not client_id or not secret:
        raise SetupError("Both the client ID and secret are needed.")

    path.touch(mode=0o600, exist_ok=True)
    set_key(str(path), "LINEAR_CLIENT_ID", client_id, quote_mode="always")
    set_key(str(path), "LINEAR_CLIENT_SECRET", secret, quote_mode="always")
    say(f"   Saved to {path}.")
    _keep_out_of_git(path, say)


def _keep_out_of_git(path: Path, say: Callable[[str], None]) -> None:
    """Offer to git-ignore `.env` when it is in a repository that would commit it."""
    try:
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", str(path)],
            cwd=path.parent,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return
    # 0: ignored. 1: not ignored. Anything else: not a repository.
    if ignored.returncode != 1:
        return
    if typer.confirm("   .env isn't git-ignored here. Add it to .gitignore?", True):
        gitignore = path.parent / ".gitignore"
        existing = gitignore.read_text() if gitignore.is_file() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        gitignore.write_text(existing + f"{ENV_NAME}\n")
        say("   Added.")


# --- 2. signing in ---


def _sign_in(say: Callable[[str], None]) -> dict:
    code, content = credentials.read()
    if not code and isinstance(content, dict):
        name = content.get("viewer_name") or "your Linear account"
        if credentials.viewer_id(content) and not typer.confirm(
            f"\n2. Signed in as {name}. Sign in again?", False
        ):
            return content

    say("\n2. Signing in (your browser opens)")
    code, content = asyncio.run(credentials.authenticate(store=True))
    if code or not isinstance(content, dict):
        raise SetupError(str(content))
    say(f"   Signed in as {content.get('viewer_name') or 'you'}.")
    return content


# --- 3. this project ---


def installed_tools() -> list[str]:
    return [name for name in KNOWN_TOOLS if missing(name) is None]


def _choose(tools: list[str], say: Callable[[str], None]) -> str:
    """Ask by number or name until the answer is one of `tools`."""
    while True:
        answer = typer.prompt(
            "   Which coding tool should do the work", default="1"
        ).strip()
        if answer.isdigit() and 1 <= int(answer) <= len(tools):
            return tools[int(answer) - 1]
        if answer in tools:
            return answer
        say(f"   Pick 1 to {len(tools)}.")


def _project(say: Callable[[str], None], viewer: dict) -> None:
    path, exists = config_path()
    if exists:
        say(f"\n3. This project: using {path}.")
        return

    say("\n3. This project")
    label = typer.prompt(
        "   Label that sends an issue to Chord", default=DEFAULT_LABEL
    ).strip()

    tools = installed_tools()
    if not tools:
        say("   No coding tool found on PATH; using opencode (`opencode run`).")
        say("   Install it, or point `harness` in the config at another command.")
        tool = "opencode"
    elif len(tools) == 1:
        tool = tools[0]
        say(f"   Coding tool: {tool}.")
    else:
        for n, name in enumerate(tools, 1):
            say(f"     {n}. {name}")
        tool = _choose(tools, say)

    you = credentials.viewer_id(viewer)
    allowed = [you] if you else []
    try:
        written = write_setup(label, tool, allowed)
    except ConfigError as exc:
        raise SetupError(str(exc)) from exc
    say(f"   Wrote {written}.")
    if allowed:
        say("   Only you can trigger a run. Add teammates to allowed_actors there.")

    say(f"\n   Create the label {label!r} in Linear yourself. Chord never creates")
    say("   labels, so adding it is what lets an issue reach this machine.")
