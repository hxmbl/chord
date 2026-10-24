"""Handing a rendered issue to whatever is going to work on it.

Chord ships one harness of its own, `print`, which is how you see what would be
handed over before trusting it with anything. Everything else is a command:
Chord runs it and writes the issue to its standard input. That is the whole
compatibility story, and it is why supporting a new tool means naming it in
chord.toml rather than teaching Chord about it.

The presets (`claude`, `codex`, `cursor`) are that same command harness with
the flags filled in, because each tool's non-interactive spelling is easy to
get subtly wrong — a run that cannot write files looks like it worked.
"""

import asyncio
import contextlib
import json
import os
import shlex
import shutil
import signal
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple, Protocol

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

# The default harness's command line: OpenCode's scripted form, prompt on stdin.
OPENCODE_ARGV = ("opencode", "run")

# Linux refuses a single command-line argument longer than 128 KiB
# (MAX_ARG_STRLEN), whatever the total limit is. Below that, with room to spare,
# for the one preset that has to take the issue as an argument.
MAX_PROMPT_ARGUMENT = 100_000

# How much of a failed harness's last line goes into the failure message.
_REASON_LENGTH = 160

# How long a freshly started harness has to fall over before Chord counts the
# issue as delivered. Writing to a pipe only proves the kernel took the bytes:
# a harness that dies on startup (a bad flag, a missing login) never read them.
# So delivery is "the whole issue went in, stdin was closed cleanly, and the
# harness was still running (or had exited 0) this long after starting". Short,
# because it is a wait on every hand-over, and a harness that exits sooner
# answers sooner.
DELIVERY_GRACE = 1.0

# Told once the harness has the issue, while it may still be working on it.
Delivered = Callable[[], None]


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
    """Dispatches work to T3 Code through its supported `t3 thread` CLI.

    Sending is the whole job: once T3 accepts the thread, the work belongs to
    T3 Code. Chord never reads a thread back, follows up, or waits on a run.
    """

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

    async def _run(
        self,
        argv: list[str],
        stdin: str | None,
        cwd: Path | None,
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
        if returncode != 0:
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

    async def send(
        self,
        prompt: str,
        cwd: Path | None = None,
        *,
        on_delivered: Delivered | None = None,
    ) -> None:
        if cwd:
            logging.write(f"# Working directory: {cwd}\n")
        logging.write(prompt)
        if on_delivered is not None:
            on_delivered()


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


async def _feed(pipe: asyncio.StreamWriter, data: bytes) -> bool:
    """Write all of `data` to a harness's stdin and close it. Whether it took it.

    `communicate()` used to do this, and it swallows a broken pipe on purpose —
    which is exactly the case that matters here: a harness that hung up on its
    input never got the issue. An empty write (the prompt went on the command
    line) can't be refused, so only a real one is checked.
    """
    if data and pipe.is_closing():
        # It hung up before the first byte was written. asyncio notices that
        # on its own and closes the transport *without* an error (nothing was
        # buffered yet), after which `write` drops the data silently and, on
        # some versions (3.12.3, Ubuntu's), `drain` and `wait_closed` return
        # as though it all went through. Checked here, with no await between
        # this and the write, so the answer can't change underneath it.
        with contextlib.suppress(Exception):
            pipe.close()
        return False
    try:
        if data:
            pipe.write(data)
            await pipe.drain()
        pipe.close()
        await pipe.wait_closed()
    except (BrokenPipeError, ConnectionResetError):
        with contextlib.suppress(Exception):
            pipe.close()
        return not data
    return True


class CommandHarness:
    """Runs a command with the issue on its standard input.

    The prompt goes to stdin rather than onto the command line because a long
    description plus a long discussion runs past the platform's limit on
    argument size, and a harness that quietly loses the end of an issue would
    be a miserable thing to debug.
    """

    def __init__(
        self,
        argv: list[str],
        name: str | None = None,
        *,
        prompt_as_argument: bool = False,
    ) -> None:
        self.argv = argv
        self.name = name or " ".join(argv)
        # Only for a tool that refuses a prompt on stdin (Cursor's CLI). The
        # issue then goes on the end of the command line instead, which is
        # bounded, so an issue too long for it is refused before anything runs.
        self.prompt_as_argument = prompt_as_argument

    async def send(
        self,
        prompt: str,
        cwd: Path | None = None,
        *,
        on_delivered: Delivered | None = None,
    ) -> None:
        """Run the harness on `prompt`, telling `on_delivered` once it has it.

        `on_delivered` is called at most once, while the harness may still be
        running: see `DELIVERY_GRACE` for what counts. A harness that never got
        the issue (it closed its input early, or died on startup) raises without
        calling it, so the caller can tell "never arrived" from "arrived, then
        failed".
        """
        argv = self.argv
        stdin = prompt.encode()
        if self.prompt_as_argument:
            if len(stdin) > MAX_PROMPT_ARGUMENT:
                raise HarnessError(
                    f"The issue is {len(stdin)} bytes, too long for `{self.name}`, "
                    f"which takes its prompt on the command line (limit "
                    f"{MAX_PROMPT_ARGUMENT}). Shorten the discussion or route it "
                    "to a harness that reads stdin."
                )
            argv = [*self.argv, prompt]
            stdin = b""
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                # Its own session, so the whole harness tree can be signalled as
                # one. Without it, `kill()` reaches only the process we spawned,
                # and a harness that leaves anything running keeps its share of
                # the stdout pipe open -- at which point reading its output
                # never finishes and the watcher is stuck rather than reporting
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
            # said arrives on the one pipe.
            output = await asyncio.wait_for(
                self._drive(process, stdin, on_delivered), HARNESS_TIMEOUT
            )
        except TimeoutError as exc:
            # Logged before any attempt to tidy up, because the tidy-up is
            # exactly what used to fail silently. Reading the output waits for
            # the pipe to close, and a grandchild holding the write end means it
            # never does; the timeout was detected, and saying so cannot wait on
            # the thing that would hang.
            logging.error(
                f"Harness `{self.name}` timed out after {HARNESS_TIMEOUT:.0f}s."
            )
            await self._terminate(process)
            raise HarnessError(
                f"`{self.name}` timed out after {HARNESS_TIMEOUT:.0f}s and was "
                "stopped. Set HARNESS_TIMEOUT (seconds) if it needs longer."
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
            # The last thing it said is usually the reason, and the banner is
            # the only place a person reads it without opening the log.
            last = said.splitlines()[-1].strip() if said else ""
            if len(last) > _REASON_LENGTH:
                last = last[: _REASON_LENGTH - 1] + "…"
            reason = f": {last}" if last else " without saying why"
            raise HarnessError(
                f"`{self.name}` exited {process.returncode}{reason}. "
                "Its full output is in the log."
            )

    async def _drive(
        self,
        process: asyncio.subprocess.Process,
        stdin: bytes,
        on_delivered: Delivered | None,
    ) -> bytes:
        """Feed the harness, confirm it took the issue, and collect its output.

        The output is read from the start, alongside the write, so a harness
        that prints a lot before it has read its whole input can't deadlock
        against a full pipe in each direction.
        """
        assert process.stdin is not None and process.stdout is not None
        reader = asyncio.ensure_future(process.stdout.read())
        exited = asyncio.ensure_future(process.wait())
        try:
            accepted = await _feed(process.stdin, stdin)
            await asyncio.wait({exited}, timeout=DELIVERY_GRACE)
            if not accepted and (not exited.done() or process.returncode == 0):
                # It hung up on its input and either kept going or exited
                # cleanly: whatever it is doing, it isn't working on this issue.
                # A non-zero exit falls through to the usual failure instead,
                # which carries the reason it gave.
                await self._terminate(process)
                raise HarnessError(
                    f"`{self.name}` closed its input before reading the whole "
                    "issue, so it never got it. Check it reads the prompt on stdin."
                )
            if accepted and not process.returncode and on_delivered is not None:
                on_delivered()
            output = await reader
            await exited
            return output
        finally:
            for task in (reader, exited):
                if not task.done():
                    task.cancel()

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
    """The default harness: OpenCode's non-interactive `opencode run`.

    Plain `opencode` starts the terminal UI, which waits for a person nobody
    is there to be. `opencode run` is the scripted form: with no message
    argument and stdin not a terminal, it reads the whole prompt from stdin
    (`opencode run [message..]`, https://opencode.ai/docs/cli/#run). No flag is
    needed to keep it from prompting — a permission that would ask is refused
    rather than waited on, and OpenCode's defaults already allow edits and
    shell commands in the directory it starts in. `--auto` would approve the
    rest too, which is more than Chord should decide for anyone; add it in a
    curated `command` if your OpenCode config asks for things.
    """

    name = "opencode"

    def __init__(self) -> None:
        super().__init__(list(OPENCODE_ARGV), self.name)


class Preset(NamedTuple):
    """A coding agent Chord knows how to run without being told the command.

    `executables` are tried in order and the first on the PATH runs, because a
    tool can be installed under more than one name. `args` are the tool's own
    non-interactive flags, checked against its documentation, with the least
    permission that still lets it edit the checkout it is started in.
    """

    executables: tuple[str, ...]
    args: tuple[str, ...]
    install: str
    prompt_as_argument: bool = False


# Name to preset. A preset is written by name (`harness = "claude"`, or
# `command = ["claude"]` under `[harnesses.<name>]`), and runs as an ordinary
# command harness: issue in, output to the log, non-zero exit is a failure.
PRESETS: dict[str, Preset] = {
    # Claude Code. `-p` reads the prompt from stdin; `acceptEdits` lets it write
    # files, which a `-p` run otherwise has refused, without approving every
    # shell command.
    "claude": Preset(
        ("claude",),
        ("-p", "--permission-mode", "acceptEdits"),
        "Claude Code (https://code.claude.com)",
    ),
    # Codex. `exec -` takes the whole prompt from stdin; `exec` is read-only
    # unless the sandbox allows writing to the workspace.
    "codex": Preset(
        ("codex",),
        ("exec", "--sandbox", "workspace-write", "-"),
        "the Codex CLI (https://developers.openai.com/codex/cli)",
    ),
    # Cursor's CLI, installed as `cursor-agent` and, newer, `agent`. Print mode
    # wants the prompt as an argument and rejects one on stdin. `--force` is what
    # lets print mode apply changes rather than propose them; `--trust` skips
    # the workspace-trust prompt nobody is there to answer.
    "cursor": Preset(
        ("cursor-agent", "agent"),
        ("-p", "--force", "--trust"),
        "the Cursor CLI (https://cursor.com/docs/cli/installation)",
        prompt_as_argument=True,
    ),
}


def preset_executable(name: str) -> str | None:
    """Where the preset `name` is installed, or None if it isn't."""
    for executable in PRESETS[name].executables:
        found = shutil.which(executable)
        if found:
            return found
    return None


def _preset(name: str) -> CommandHarness:
    preset = PRESETS[name]
    found = preset_executable(name)
    if found is None:
        raise HarnessError(
            f"`{preset.executables[0]}` isn't on your PATH. Install "
            f"{preset.install}, or pick another harness."
        )
    return CommandHarness(
        [found, *preset.args],
        name,
        prompt_as_argument=preset.prompt_as_argument,
    )


def command_line(spec: HarnessSpec) -> str | None:
    """What a harness known by name actually runs, for `chord info` to show.

    None for anything spelled out as a command already, since its spelling is
    its command line. Read from the same tables `build` uses, without touching
    the PATH, so the line cannot drift from what runs.
    """
    if isinstance(spec, list):
        if len(spec) != 1:
            return None
        spec = spec[0]
    if spec == OpenCodeHarness.name:
        return " ".join(OPENCODE_ARGV)
    if spec in PRESETS:
        preset = PRESETS[spec]
        line = " ".join([preset.executables[0], *preset.args])
        return f"{line} <issue>" if preset.prompt_as_argument else line
    return None


def missing(spec: HarnessSpec) -> str | None:
    """Why `spec` can't be started on this machine, or None if it can.

    The check `chord start`, `chord info` and `chord setup` share. Building a
    harness already refuses a bare name, a preset or `t3` that isn't installed;
    this also covers what building leaves to the first issue — an explicit
    command, and the default `opencode` — so a harness that can't start is
    found while somebody is looking rather than overnight, when the issue it
    was handed is recorded as failed.

    A command given by path (`./scripts/on-issue.sh`) is not checked: it runs in
    the issue's repository, which isn't known until an issue arrives.
    """
    if isinstance(spec, list):
        if len(spec) == 1 and spec[0] in PRESETS:
            spec = spec[0]
        elif spec == [T3CodeHarness.name]:
            spec = T3CodeHarness.name
        else:
            return _not_on_path(spec[0])
    if spec == PrintHarness.name:
        return None
    if spec in PRESETS:
        if preset_executable(spec) is None:
            return (
                f"`{PRESETS[spec].executables[0]}` isn't on your PATH. Install "
                f"{PRESETS[spec].install}."
            )
        return None
    if spec in (OpenCodeHarness.name, T3CodeHarness.name):
        return _not_on_path(spec)
    try:
        argv = shlex.split(spec)
    except ValueError as exc:
        return f"Invalid harness quoting: {exc}."
    return _not_on_path(argv[0]) if argv else "The harness command is empty."


def _not_on_path(command: str) -> str | None:
    if os.sep in command or (os.altsep and os.altsep in command):
        return None
    return None if shutil.which(command) else f"`{command}` isn't on your PATH."


def build(spec: HarnessSpec, t3_options: T3Options | None = None) -> Harness:
    """Turn a configured harness into one that can be sent work."""
    if isinstance(spec, list):
        if spec == [T3CodeHarness.name]:
            return T3CodeHarness(t3_options)
        # `command = ["claude"]` under a curated table means the preset, the
        # same way `["t3"]` means the T3 integration.
        if len(spec) == 1 and spec[0] in PRESETS:
            return _preset(spec[0])
        return CommandHarness(spec)
    if spec == PrintHarness.name:
        return PrintHarness()
    if spec == OpenCodeHarness.name:
        return OpenCodeHarness()
    if spec == T3CodeHarness.name:
        return T3CodeHarness(t3_options)
    if spec in PRESETS:
        return _preset(spec)

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
        f"Use `{PrintHarness.name}`, `{OpenCodeHarness.name}`, "
        f"{', '.join(f'`{name}`' for name in PRESETS)} or `{T3CodeHarness.name}`, or "
        'name the command to run instead, like ["claude", "-p"].'
    )
