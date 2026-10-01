"""Handing a rendered issue to whatever is going to work on it.

Chord ships one harness of its own, `print`, which is how you see what would be
handed over before trusting it with anything. Everything else is a command:
Chord runs it and writes the issue to its standard input. That is the whole
compatibility story, and it is why supporting a new tool means naming it in
chord.toml rather than teaching Chord about it.
"""

import asyncio
import os
import shlex
import shutil
from typing import Protocol

from chord import logging
from chord.config import HarnessSpec

try:
    HARNESS_TIMEOUT = float(os.environ.get("HARNESS_TIMEOUT", str(30 * 60)))
except ValueError:
    HARNESS_TIMEOUT = 30 * 60


class HarnessError(Exception):
    """The harness couldn't be run, or ran and didn't finish."""


class Harness(Protocol):
    name: str

    async def send(self, prompt: str) -> None: ...


class PrintHarness:
    """Writes the issue to Chord's own output, which is the watcher's log.

    A built-in harness: it makes a first run inspectable instead of
    speculative.
    """

    name = "print"

    async def send(self, prompt: str) -> None:
        logging.write(prompt)


class CommandHarness:
    """Runs a command with the issue on its standard input.

    The prompt goes to stdin rather than onto the command line because a long
    description plus a long discussion runs past the platform's limit on
    argument size, and a harness that quietly loses the end of an issue would
    be a miserable thing to debug.
    """

    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.name = " ".join(argv)

    async def send(self, prompt: str) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                *self.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            raise HarnessError(f"`{self.argv[0]}` isn't on your PATH.") from exc
        except OSError as exc:
            raise HarnessError(f"Couldn't run `{self.name}`: {exc.strerror}.") from exc

        try:
            # stderr was merged into stdout above, so everything the harness
            # said arrives in the first slot and the second is always None.
            output, _ = await asyncio.wait_for(
                process.communicate(prompt.encode()), HARNESS_TIMEOUT
            )
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            logging.error(f"Harness `{self.name}` timed out after {HARNESS_TIMEOUT}s.")
            raise HarnessError(
                f"`{self.name}` timed out after {HARNESS_TIMEOUT}s."
            ) from exc
        except asyncio.CancelledError:
            # We're being shut down, most likely by `chord stop`. Don't leave
            # an agent running with nobody watching it.
            process.kill()
            raise

        said = output.decode(errors="replace").strip()
        if said:
            # Whatever the harness says belongs in the log. Without this, a
            # harness that fails quietly looks exactly like one that worked.
            logging.write(said, end="\n")
        if process.returncode:
            raise HarnessError(f"`{self.name}` exited {process.returncode}.")


class OpenCodeHarness(CommandHarness):
    """The default harness: the `opencode` command."""

    name = "opencode"

    def __init__(self) -> None:
        super().__init__(["opencode"])


def build(spec: HarnessSpec) -> Harness:
    """Turn a configured harness into one that can be sent work."""
    if isinstance(spec, list):
        return CommandHarness(spec)
    if spec == PrintHarness.name:
        return PrintHarness()
    if spec == OpenCodeHarness.name:
        return OpenCodeHarness()

    # A bare word we don't recognise is most likely the name of a tool the
    # user meant, so try it as a command before giving up. Naming the command
    # outright in chord.toml is what the documentation suggests; this is the
    # shortcut that makes it work anyway.
    try:
        argv = shlex.split(spec)
    except ValueError as exc:
        raise HarnessError(f"Invalid harness quoting: {exc}.") from exc
    if not argv:
        raise HarnessError("The harness command is empty.")
    found = shutil.which(argv[0])
    if found:
        return CommandHarness([found, *argv[1:]])
    raise HarnessError(
        f"Don't know a harness called `{spec}`. "
        f"Use `{PrintHarness.name}` or `{OpenCodeHarness.name}`, or "
        'name the command to run instead, like ["claude", "-p"].'
    )
