# Overnight Log Summary

## What Was Found
- `src/setup.py` (now `src/credentials.py`) was already solid OAuth PKCE implementation with good security — **left untouched**
- `src/main.py` had working `setup`/`refresh` commands, but `start`/`stop`/`info` were stubs
- Everything else below is new implementation

## What Was Built
Six new modules completing the pipeline:
- `config.py` — `chord.toml` with defaults/validation
- `linear.py` — GraphQL reads (never writes)
- `context.py` — issue + discussion → harness input
- `harness.py` — runs commands with issue on stdin
- `watcher.py` — poll loop + dedupe tracking
- `daemon.py` — background process, pid, log

Pipeline now works: `Linear → Chord → Context → harness`

## Key Design Decisions

**Zero-config defaults:** Works without `chord.toml` (label: `chord`, harness: `print`, interval: 60s)

**Harness is a command, not a plugin:** Anything that reads stdin works. Config change, not release. `print` is the only built-in.

**Issue on stdin, not argv:** Tested with 400KB prompts — would exceed argument limits.

**Bare harness names work:** `harness = "claude"` uses PATH if not built-in.

**One-time handover, tracked on disk:** `~/.chord/state.json` (capped 500 entries). Restart doesn't re-offer backlog. Delete file to reset.

**Failed handovers are recorded anyway:** Missing/errored harness is logged and marked done — prevents one broken harness from wedging the queue forever.

**Resilient daemon:** Network errors, rate limits, expired tokens log once and retry next tick. `chord refresh` picked up automatically by running watcher.

**Pre-flight validation:** `start` checks config, credentials, harness, state before spawning — errors go to user, not buried in log.

**State outside repo:** `~/.chord/<project>/` holds pid, log, state. `chord.toml` (no secrets) lives in repo.

**Python 3.11 minimum:** Uses `tomllib`, builtin `TimeoutError`, `X | Y` unions — no need for 3.13.

**`watch` uses `tail -f`:** POSIX tool people know. Daemon uses `start_new_session=True` to avoid inheriting event loops/sockets.

## Bugs Found (by running code)

1. **Harness read wrong end of `communicate()`:** Merged stderr into stdout, unpacked backwards as `(output, None)`. Every command harness crashed on `None.decode()`. Fixed.

2. **Dedupe failed across restarts:** State file stringified keys but issue IDs weren't normalized, so restarts could re-offer issues. Both ends now use `str()` consistently.

## Unfinished

- **No tests:** `test/` directory doesn't exist (pyproject.toml points at it). Deliberate per instructions.
- **Label trigger only:** Assignment/agent-session triggers untouched (mutually exclusive ordering).
- **No pagination:** `first: 50` on issues/comments. Past 50 labelled issues, rest invisible.
- **No harness timeout:** Hanging harness hangs watcher. `chord stop` waits 10s then reports failure rather than SIGKILL.
- **POSIX-only:** `_serve` uses `os.kill`, signal handlers, `tail -f`. Windows needs different approach.

## Immediate Action Items

⚠️ **`test_secret.txt` is staged as deleted but contains a real key** — staged content is `API_KEY=<value>`. Committing as-is will commit that key. Decide before next commit; add to `.gitignore` if throwaway.

⚠️ **`test/` directory doesn't exist** — `pytest` will error until created.

## What I Didn't Verify

No live Linear round trip — label filter shape is from Linear docs but untested with real token. First `chord setup` + `chord start` is the real test.

No tests/linters/type checkers run. Drove CLI and watcher with Linear stubbed out, which is how the bugs surfaced.
