"""Handing a rendered issue to whatever is going to work on it.

Chord ships one harness of its own, `print`, which is how you see what would be
handed over before trusting it with anything. Everything else is a command:
Chord runs it and writes the issue to its standard input. That is the whole
compatibility story, and it is why supporting a new tool means naming it in
chord.toml rather than teaching Chord about it.
"""

import asyncio
import contextlib
import os
import shlex
import shutil
import signal
from typing import Protocol

from chord import logging
from chord.config import HarnessSpec


def _harness_timeout() -> float:
    """Seconds a command harness may take, from `HARNESS_TIMEOUT`.

    Validated rather than merely parsed: `float()` happily accepts `-1`, `nan`
    and `inf`, and each of those is a way to lose work quietly. A negative or
    zero value made every harness fail instantly — and a failed hand-over is
    *recorded*, so the whole backlog would be marked done without ever running.
    `inf` silently removed the timeout the README promises. So the value has to
    be a positive, finite number, and anything else falls back to the default
    with a line in the log rather than being obeyed.
    """
    raw = os.environ.get("HARNESS_TIMEOUT")
    if raw is None or not raw.strip():
        return DEFAULT_HARNESS_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        logging.warning(
            f"HARNESS_TIMEOUT={raw!r} isn't a number; using "
            f"{DEFAULT_HARNESS_TIMEOUT:.0f}s."
        )
        return DEFAULT_HARNESS_TIMEOUT
    if not (value > 0) or value == float("inf"):
        logging.warning(
            f"HARNESS_TIMEOUT={raw!r} isn't a positive number of seconds; using "
            f"{DEFAULT_HARNESS_TIMEOUT:.0f}s."
        )
        return DEFAULT_HARNESS_TIMEOUT
    return value


DEFAULT_HARNESS_TIMEOUT = 30 * 60
HARNESS_TIMEOUT = _harness_timeout()

# How long to wait for a killed harness to be reaped and its pipe released. This
# is a bound on tidying up after something already failed, not on the work
# itself, so it is short: the alternative is a watcher that stops working and
# never says why.
_REAP_TIMEOUT = 5.0


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


def _signal_group(process: asyncio.subprocess.Process, sig: int) -> None:
    """Signal the harness's whole process group.

    `start_new_session=True` makes the child a session and group leader, so its
    pid is also its pgid and `killpg` on that number reaches everything it
    started — a test runner's workers, an agent's background server.

    `killpg` takes the pgid **positive**. Passing it negative, as the POSIX
    spelling of `kill(-pgid)` suggests, is a different call and does not do this:
    it signals the caller's own group instead, which is Chord, so the harness
    tree is left running while Chord takes a signal it never expected. That is
    what the negative form did here, and it is why the grandchild outlived the
    group signal in testing on macOS.

    Falls back to the single process when the group is already gone, which is
    the normal case for a harness that exited on its own.
    """
    try:
        os.killpg(process.pid, sig)
        return
    except ProcessLookupError:
        # The group is empty. The process may still exist having called
        # setsid() itself, so try it directly before giving up.
        pass
    except (PermissionError, OSError):
        return

    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(process.pid, sig)


async def _reap(process: asyncio.subprocess.Process) -> None:
    """Wait for a killed harness, even while this task is being cancelled.

    `_terminate` runs on the cancellation path, so the task is already marked
    cancelled and a bare `await` inside it is cancelled again immediately —
    which would abandon the reap and leave a zombie. `shield` protects the
    inner work from that second delivery.
    """
    await asyncio.shield(process.wait())


def _close_transport(process: asyncio.subprocess.Process) -> None:
    """Release the pipe reader so nothing is left waiting on it.

    Only on a process that already failed or was killed: on the success path
    `communicate()` has finished and the transport is closed anyway. Best-effort
    because this reaches into asyncio internals, which can move between
    versions — and it is an optimisation, not a correctness requirement.
    """
    transport = getattr(process, "_transport", None)
    if transport is None:
        return
    with contextlib.suppress(Exception):
        transport.close()


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
                # Its own session, so the whole harness tree can be signalled as
                # one. Without it, `kill()` reaches only the process we spawned,
                # and a harness that leaves anything running keeps its share of
                # the stdout pipe open -- at which point `communicate()` below
                # never returns and the watcher is stuck rather than reporting
                # the timeout it just detected.
                start_new_session=True,
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
            # Logged before any attempt to tidy up, because the tidy-up is
            # exactly what used to fail silently. `communicate()` waits for the
            # pipe to close, and a grandchild holding the write end means it
            # never does; the timeout was detected, and saying so cannot wait on
            # the thing that would hang.
            logging.error(f"Harness `{self.name}` timed out after {HARNESS_TIMEOUT}s.")
            await self._terminate(process)
            raise HarnessError(
                f"`{self.name}` timed out after {HARNESS_TIMEOUT}s."
            ) from exc
        except asyncio.CancelledError:
            # We're being shut down, most likely by `chord stop`. Don't leave
            # an agent running with nobody watching it.
            await self._terminate(process)
            raise

        said = output.decode(errors="replace").strip()
        if said:
            # Whatever the harness says belongs in the log. Without this, a
            # harness that fails quietly looks exactly like one that worked.
            logging.write(said, end="\n")
        if process.returncode:
            raise HarnessError(f"`{self.name}` exited {process.returncode}.")

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        """Stop a harness and everything it started, then let go of its output.

        SIGKILL to the group, because a harness part-way through an issue may
        have children doing the actual work and a polite signal first would
        leave them running against somebody's repository after `chord stop`.

        The drain afterwards is bounded and best-effort. It exists to reap the
        child and release the pipe, not to collect output nobody is going to
        read — a grandchild that ignored the group signal can hold that pipe
        open indefinitely, and waiting for it unbounded is how a watcher used to
        end up wedged inside its own timeout handler with nothing in the log.
        """
        _signal_group(process, signal.SIGKILL)
        # Reaping is best-effort, and the timeout has to cover the wait itself.
        # `wait_for` raising would replace the CancelledError that `chord stop`
        # sent, which turns a clean shutdown into a traceback and leaves the
        # watcher reporting the wrong reason for stopping. It must not escape.
        with contextlib.suppress(TimeoutError, ProcessLookupError, PermissionError):
            await asyncio.wait_for(_reap(process), _REAP_TIMEOUT)
        # Nobody is reading this any more; closing the transport stops the
        # reader task waiting on a pipe that may never reach EOF.
        _close_transport(process)


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
