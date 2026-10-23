"""Handing a rendered issue to whatever is going to work on it.

Chord ships one harness of its own, `print`, which is how you see what would be
handed over before trusting it with anything. Everything else is a command:
Chord runs it and writes the issue to its standard input. That is the whole
compatibility story, and it is why supporting a new tool means naming it in
chord.toml rather than teaching Chord about it.
"""

import asyncio
import contextlib
import json
import os
import shlex
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any, Protocol

from chord import logging
from chord.config import HarnessSpec, T3Options


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

    async def send(self, prompt: str, cwd: Path | None = None) -> object | None: ...


class T3ClientError(HarnessError):
    """A T3 CLI operation failed, with dispatch ambiguity kept explicit."""

    def __init__(self, message: str, *, uncertain: bool = False) -> None:
        super().__init__(message)
        self.uncertain = uncertain


class T3CodeHarness:
    """Dispatches work to T3 Code through its supported `t3 thread` CLI."""

    name = "t3"

    def __init__(self, options: T3Options | None = None, executable: str = "t3"):
        found = shutil.which(executable)
        if found is None:
            raise HarnessError(
                "`t3` isn't on your PATH. Install T3 Code and make its CLI "
                "available before selecting the `t3` harness."
            )
        self.executable = found
        self.options = options or T3Options()

    async def send(self, prompt: str, cwd: Path | None = None) -> dict[str, str]:
        if cwd is None:
            raise HarnessError(
                "The `t3` harness needs a resolved project directory; no task was sent."
            )
        argv = [
            self.executable,
            "thread",
            "start",
            ".",
            "-",
            "--json",
            "--title",
            "Chord task",
        ]
        if self.options.provider is not None and self.options.model is not None:
            argv.extend(
                ["--provider", self.options.provider, "--model", self.options.model]
            )
        if self.options.worktree is not None:
            argv.extend(["--worktree", self.options.worktree])
        if self.options.branch is not None:
            argv.extend(["--branch", self.options.branch])

        output = await self._run(argv, prompt, cwd)
        result = self._json_object(output.stdout, "start")
        thread_id = result.get("threadId")
        project_id = result.get("projectId")
        run_id = result.get("runId")
        if (
            not isinstance(thread_id, str)
            or not thread_id.strip()
            or not isinstance(project_id, str)
            or not project_id.strip()
            or (run_id is not None and not isinstance(run_id, str))
        ):
            raise T3ClientError(
                "T3 returned malformed start output; dispatch may have been accepted. "
                "Chord has held this issue to prevent a duplicate.",
                uncertain=True,
            )
        return {
            "thread_id": thread_id.strip(),
            "project_id": project_id,
        }

    async def show_thread(self, thread_id: str) -> dict[str, Any]:
        output = await self._run(
            [self.executable, "thread", "show", "--json", thread_id], None, None
        )
        result = self._json_object(output.stdout, "show")
        thread = result.get("thread")
        messages = result.get("messages")
        if (
            not isinstance(thread, dict)
            or thread.get("threadId") != thread_id
            or not isinstance(thread.get("status"), str)
            or not isinstance(thread.get("pendingRequestCount"), int)
            or isinstance(thread.get("pendingRequestCount"), bool)
            or thread["pendingRequestCount"] < 0
            or not isinstance(messages, list)
            or any(
                not isinstance(message, dict)
                or message.get("role") not in ("user", "assistant")
                or not isinstance(message.get("text"), str)
                for message in messages
            )
        ):
            raise T3ClientError(
                "T3 returned malformed thread output. Update the T3 Code CLI and retry."
            )
        return result

    async def send_follow_up(
        self, thread_id: str, message: str, *, wait: bool = False
    ) -> dict[str, Any]:
        argv = [self.executable, "thread", "send", thread_id, "-", "--json"]
        if wait:
            argv.append("--wait")
        output = await self._run(
            argv, message, None, wait_exit_codes={1, 2} if wait else set()
        )
        result = self._json_object(output.stdout, "send")
        if result.get("threadId") != thread_id:
            raise T3ClientError("T3 returned a response for an unexpected thread.")
        if wait:
            if (
                not isinstance(result.get("status"), str)
                or "reply" not in result
                or "error" not in result
                or result.get("reply") is not None
                and not isinstance(result.get("reply"), str)
                or result.get("error") is not None
                and not isinstance(result.get("error"), str)
            ):
                raise T3ClientError("T3 returned malformed wait output.")
            expected = {"completed": 0, "waiting": 2}
            status = result["status"]
            if status not in expected and output.returncode != 1:
                raise T3ClientError("T3 returned an unknown run status.")
            if status in expected and output.returncode != expected[status]:
                raise T3ClientError("T3 returned an inconsistent run status.")
            if status not in {"completed", "waiting"} and output.returncode != 1:
                raise T3ClientError("T3 returned an inconsistent run status.")
        elif not isinstance(result.get("runId"), str) or result.get("delivery") not in (
            "sent",
            "queued",
        ):
            raise T3ClientError("T3 returned malformed send output.")
        return result

    async def _run(
        self,
        argv: list[str],
        stdin: str | None,
        cwd: Path | None,
        *,
        wait_exit_codes: set[int] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE
                if stdin is not None
                else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd) if cwd else None,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise HarnessError(
                "`t3` isn't on your PATH. Install T3 Code and make its CLI available."
            ) from exc
        except OSError as exc:
            raise HarnessError(
                f"Couldn't run the T3 Code CLI: {exc.strerror}."
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(stdin.encode() if stdin is not None else None),
                HARNESS_TIMEOUT,
            )
        except TimeoutError as exc:
            await self._terminate(process)
            raise T3ClientError(
                f"T3 CLI timed out after {HARNESS_TIMEOUT:.0f}s; dispatch may have "
                "been accepted. Chord has held this issue to prevent a duplicate.",
                uncertain=True,
            ) from exc
        except asyncio.CancelledError:
            await self._terminate(process)
            raise

        returncode = process.returncode
        if returncode is None:
            raise T3ClientError("T3 CLI ended without a recorded exit status.")
        result = subprocess.CompletedProcess(argv, returncode, stdout, stderr)
        permitted = wait_exit_codes or set()
        if returncode != 0 and returncode not in permitted:
            detail = stderr.decode(errors="replace").casefold()
            if "no t3 code project matches" in detail:
                raise T3ClientError(
                    "No T3 Code project matches Chord's resolved repository. Add that "
                    "repository as a T3 Code project or configure a valid working path."
                )
            if "no running t3 code server" in detail:
                raise T3ClientError(
                    "No T3 Code server is running. Start T3 Code, then retry."
                )
            if "no longer available" in detail or "thread not found" in detail:
                raise T3ClientError(
                    "The saved T3 thread is unavailable. Confirm it still exists in "
                    "T3 Code and that Chord is using the same T3 environment."
                )
            raise T3ClientError(
                f"T3 CLI exited with status {returncode}; check the T3 Code server "
                "and configured project/provider/worktree. Dispatch may have been "
                "accepted, so Chord has held this issue to prevent a duplicate.",
                uncertain=True,
            )
        return result

    @staticmethod
    def _json_object(output: bytes, command: str) -> dict[str, Any]:
        try:
            result = json.loads(output)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise T3ClientError(
                f"T3 returned malformed JSON from `thread {command}`."
            ) from exc
        if not isinstance(result, dict):
            raise T3ClientError(f"T3 returned malformed JSON from `thread {command}`.")
        return result

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        _signal_group(process, signal.SIGKILL)
        with contextlib.suppress(TimeoutError, ProcessLookupError, PermissionError):
            await asyncio.wait_for(_reap(process), _REAP_TIMEOUT)
        _close_transport(process)


class PrintHarness:
    """Writes the issue to Chord's own output, which is the watcher's log.

    A built-in harness: it makes a first run inspectable instead of
    speculative.
    """

    name = "print"

    async def send(self, prompt: str, cwd: Path | None = None) -> None:
        if cwd:
            logging.write(f"# Working directory: {cwd}\n")
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

    async def send(self, prompt: str, cwd: Path | None = None) -> None:
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
                cwd=str(cwd) if cwd else None,
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


def build(spec: HarnessSpec, t3_options: T3Options | None = None) -> Harness:
    """Turn a configured harness into one that can be sent work."""
    if isinstance(spec, list):
        if spec == [T3CodeHarness.name]:
            return T3CodeHarness(t3_options)
        return CommandHarness(spec)
    if spec == PrintHarness.name:
        return PrintHarness()
    if spec == OpenCodeHarness.name:
        return OpenCodeHarness()
    if spec == T3CodeHarness.name:
        return T3CodeHarness(t3_options)

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
