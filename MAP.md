# Chord Codebase Map

## Overview
Chord is a Python tool that watches Linear for issues carrying one of its route labels and hands each one to the coding harness that label names (opencode, the Claude Code / Codex / Cursor presets, or any command-line tool).

## Entry Point
**`src/chord/cli.py`** - The command-line interface
- Commands: `setup`, `init`, `harness add`, `refresh`, `start`, `stop`, `watch`,
  `info`, `service install|uninstall|status|logs|start|stop|restart`, `help`
  (`help` forwards to the command you name; `_serve` is hidden)
- With a service installed, `start`/`stop`/`watch` go through it (one watcher
  per project); `chord setup` ends by offering `chord service install`
  (default no); `chord info` has a `service` line (file check only, offline)
- `chord start` runs the guided setup first when Chord isn't signed in
- Uses Typer for CLI parsing
- Spawns a background daemon for the watcher process
- All commands are thin wrappers that validate state before calling other modules
- `chord info` stays offline: daemon state, the labels Chord will act on,
  which notification route is in use, and whether a delivery gets a banner
  (`received   ... ([notify] received = true)`), without touching the network
- `chord start` refuses to spawn while any route can't start
  (`Router.problems()`: build errors plus a PATH lookup), listing all of them;
  `chord info` marks the same routes `! can't run:`
- `chord info` shows the newest five hand-overs from `state.json`'s
  `deliveries`, newest first ("ENG-1 delivered to claude at 23:59, still
  running"), in local time, and next to a harness known by name what it runs
  (`opencode  (opencode run)`, via `harness.command_line`)
- `chord info` prints the accepted labels as a list, because Chord never
  creates a label and the set is kept in step by hand — adding one is what makes
  the machine run a harness

## Core Modules

### Guided setup
**`src/chord/setup.py`** - `chord setup`
- Three steps, each skipped when done: Linear OAuth app credentials into `.env`,
  sign-in (`credentials.authenticate`), project config via `write_setup()`
- Offers the coding tools found on PATH (`KNOWN_TOOLS`: `opencode` plus every
  preset, detected with `harness.missing`) and writes the pick by name; adds the signed-in
  user to `allowed_actors`; never overwrites a config or touches Linear
- Without a terminal it only signs in, as the old `chord setup` did

### Configuration
**`src/chord/config.py`** - Settings management
- Reads `chord.toml` from project directory (searches upward)
- Falls back to user config `~/.chord/chord.toml`
- Falls back to defaults if no config exists
- Settings: `label`, `harness`, `harnesses`, `interval`, `webhook_port`,
  `allowed_actors`, `t3`, `notify`
- `[notify] received` (bool, default true) switches the "received" banner on
  confirmed delivery; strict like `[t3]` (unknown keys and non-bools refused).
  The stock file writes it uncommented, at its default, before the tables that
  `curate()` appends after it
- `harnesses` is the curation table: `<name>` to a command
- `allowed_actors` is a list of Linear **user ids**; ids and not names because
  Linear lets anyone rewrite their own display name
- All settings have defaults, so Chord works without a config file
- `init_file()` writes the stock annotated config; `write_setup()` writes the
  same file with setup's answers filled in; `curate()` appends a
  `[harnesses."<name>"]` table (appending is safe because TOML tables come
  last), proving the result parses before it lands on disk

### Routing
**`src/chord/routing.py`** - Which harness an issue goes to
- Label grammar: bare `<label>` is the default, `<label> -> <name>` is a
  curated harness; each further arrow is a `/` in the name, a parent -> child
  relationship (Linear allows neither `/` nor `:` in labels):
  `Chord -> opencode -> big-pickle` is `[harnesses."opencode/big-pickle"]`
- Arrows are read with any whitespace around them (`Chord->opencode` works) and
  the parts trimmed; `:`, `.` and uppercase are ordinary characters (model
  names); matching is exact and case-sensitive, curated names only
- `route_label()` prints each `/` as ` -> `; the legacy `<label>/<name>`
  spelling is still read (same name, same route) and in the filter, but never
  printed
- `config.usable_harness_name()` refuses names containing `->` and empty or
  space-padded parts, so every curated name round-trips through its label
- `label_on()` reports an unknown route as the label actually on the issue
- `filter()` builds one Linear query covering every route, curated or not
- `candidates()` reads an issue's route labels, most specific first
- `harness_for()` resolves a name to a built harness, memoized
- `validate()` builds every route so the watcher can refuse a broken one
- `problems()` is wider: every route that can't start, from a build error or a
  command missing from PATH, without memoizing or logging a PERF line — what
  `chord start` and `chord info` use
- The watched set is wider than the routable set, and `chord info` says so: the
  labels are the trigger surface, and `chord info` lists the ones that run work

### The Watcher Loop
**`src/chord/watcher.py`** - Main polling logic
- Polls Linear for routed issues at configured interval
- Picks a route per issue; with several route labels, asks Linear's audit
  trail which was added most recently, falling back to the most specific
- Checks who added that label against `allowed_actors` when one is configured
- Tracks which issues have been handed over in `state.json`
- Processes issues oldest-first (by `createdAt`)
- Hands each issue to the harness its labels select
- A failed hand-over (missing command, non-zero exit, timeout) is recorded and
  logged with a `to retry it: drop <id> from state.json` line, like every other
  recorded skip; a held T3 dispatch is the one exception
- Resolves the repository after authorisation, and hands it to the harness as
  its working directory (`repo.py`)
- Announces an issue that didn't go to plan with one banner — failed,
  skipped, or refused. A hand-over that works posts `received` at the moment
  delivery is confirmed (from `on_delivered`, as a task run alongside the
  harness; for T3 once it is accepted) when `[notify] received` is on, the
  default (`Watcher(notify_received=...)`). A later `failed` waits for it, so
  it lands second and replaces it in the issue's group
- Confirms delivery quietly: the harness calls `on_delivered` once it has the
  issue, the watcher logs `delivered to <harness>.` and writes a `deliveries`
  entry to `state.json` straight away, then updates it when the run ends
  (`delivered` → `finished` / `failed` / `stopped`; `not delivered` when the
  harness never got it, with `it never reached <harness>.` in the log;
  `accepted` for T3; `unconfirmed` for a held T3 dispatch). A harness that
  can't confirm (T3, test fakes) counts as delivered when `send` returns
- `deliveries` is trimmed with `handed_over`, and read leniently: it is a
  record for `chord info`, never consulted on whether to run anything
- Never raises: logs problems and continues
- Integrates with webhooks and subscriptions for faster response

### Repository Resolution
**`src/chord/repo.py`** - Which repository a Linear issue is worked on in
- Linear project name, matched against the project root and a directory of that
  name directly inside it — deliberately bounded, because an unbounded ancestor
  walk matched `$HOME` for a project called `home`
- A `[Chord_Working_Path:]` line in the issue description, the fallback for a repository
  that is not a child of the project root; the name is matched in any case, the
  path must be single- or double-quoted, `~` is expanded, relative paths read
  from the project root
- Falls back to the project root when the issue names nothing, because most
  issues carry no Linear project; every hand-over logs which rule was used
- A location that is named but is not a Git repository is an error, not a
  reason to try somewhere else — and such an issue is not recorded as handed
  over, so it is retried once the directory is there
- Detects repositories by asking Git for its top level rather than looking for a
  `.git` entry, which is a file in a linked worktree and a directory elsewhere

### Linear API Client
**`src/chord/linear.py`** - GraphQL API interactions
- Reads issues matching a route filter with pagination (up to 1000 per poll)
- Reads an issue's label history as `LabelChange` records, newest first, each
  carrying the `Actor` (person or integration) who added those labels
- Fetches comments for each issue (up to 200 comments)
- Uses `httpx` for async HTTP requests
- Never writes to Linear (read-only)
- Handles GraphQL errors and reports them cleanly

### Login service
**`src/chord/service.py`** - `chord service`
- systemd user unit (`~/.config/systemd/user/chord-<daemon.PROJECT>.service`)
  on Linux, LaunchAgent (`io.hxmbl.chord.<daemon.PROJECT>`, logs in
  `~/Library/Logs/chord/`) on macOS; `kind()` picks, `current()` describes
  this project's, `None`/`Unsupported` elsewhere (and when systemd has no user
  session)
- Runs `<sys.executable> -m chord.cli _serve --managed`; `--managed` makes the
  watcher write its own pid file via `daemon.claim()`, and exit 0 if a
  `chord start` watcher already holds the project
- `systemd_unit()` / `launchd_plist()` are pure (quoting, `%`/`$` escaping,
  plistlib for XML); `capture_environment()` copies `CAPTURED` (PATH, HOME,
  locale, XDG, Chord's knobs; never API keys)
- Every systemctl/launchctl/journalctl call goes through `service._run`;
  conftest makes it fail by default and stubs `current()` to None
- `loginctl enable-linger` is only read (`lingering()`) and suggested, never run

### Process Management
**`src/chord/daemon.py`** - Background process handling
- Manages the watcher's lifecycle as a detached process
- State lives in `~/.chord/sessions/<project>/` (one per project)
- Files: `chord.pid`, `chord.log`, `state.json`, `chord.lock`
- Project identified by directory containing `chord.toml`
- Uses file locking to prevent race conditions on start
- Validates process is actually a Chord watcher before stopping
- `claim()`: a service-run watcher (`_serve --managed`) writes its own pid file
  under the same lock
- A managed watcher that isn't signed in waits (`cli._wait_for_sign_in`: logs
  the reason once, re-reads the keyring every `interval`s, SIGTERM exits 0)
  instead of exiting 1 into a systemd/launchd restart loop; an unmanaged
  `_serve` still exits 1, since `chord start` checked before spawning it

### Harness Execution
**`src/chord/harness.py`** - Running external tools
- Built-in: `print` harness (writes to log for debugging/inspection)
- Built-in: `opencode` harness (the default): `opencode run` with the issue on
  stdin (`OPENCODE_ARGV`); `run` reads a piped prompt when given no message
  argument, and refuses rather than waits on a permission prompt, so no flag is
  added (`--auto` is left to a curated command)
- Built-in: `t3` harness (the official T3 Code CLI; dispatch-only — the thread
  id is reported and dropped, and a pending marker only prevents a duplicate
  send)
- Presets (`PRESETS`): `claude` (`claude -p --permission-mode acceptEdits`),
  `codex` (`codex exec --sandbox workspace-write -`), `cursor`
  (`cursor-agent`/`agent -p --force --trust`), each a `CommandHarness` with the
  flags filled in; reachable as `harness = "claude"` or a one-word
  `command = ["claude"]`, and refused at build time when not installed
- Cursor's print mode rejects stdin, so its preset passes the issue as the last
  argument (`prompt_as_argument`), refusing one over `MAX_PROMPT_ARGUMENT`
  bytes before spawning
- `missing(spec)` answers "can this start here?" from the PATH, for setup,
  start and info; a command given by path isn't checked (it runs in the issue's
  repository)
- Command harness: runs any command with issue on stdin
- Delivery confirmation: stdin is written and closed by hand (`_feed`), not
  `communicate()`, because `communicate()` swallows the broken pipe that means
  "never got it"; output is read alongside so neither pipe can deadlock. Then
  `DELIVERY_GRACE` (1s): still running, or exited 0, means delivered and
  `on_delivered` is called; a non-zero exit inside it, or a harness that hung
  up on its input, means not delivered (the latter is killed). `PrintHarness`
  confirms after writing
- Protocol-based design for extensibility
- Issues sent via stdin to avoid command-line length limits
- Captures stdout/stderr and logs them
- Non-zero exit codes are reported as failures, carrying the harness's last
  output line; a timeout names `HARNESS_TIMEOUT`
- Each harness runs in its own session, so a timeout or `chord stop` kills the
  whole process group rather than leaving the work it started running
- The timeout is validated, not merely parsed: `float()` accepts `-1`, `nan`
  and `inf`, and each of those loses work quietly

### Issue Rendering
**`src/chord/context.py`** - Formatting Linear issues
- Turns Linear issue + comments into a text block
- Wraps untrusted content in `BEGIN`/`END` markers
- Includes: identifier, title, description, discussion, state, priority, labels
- Explicit preamble telling harnesses to treat content as work, not instructions
- Prompt boundary (not security boundary) - makes distinction legible

### Credentials & OAuth
**`src/chord/credentials.py`** - Linear authentication
- OAuth flow with PKCE (code verifier)
- Client credentials from `.env` file
- Tokens stored in OS keychain (never in files Chord writes)
- Automatic token renewal (`renew_if_due`): an hour before expiry and on a refusal, one renewer per machine via `~/.chord/renew.lock`
- Max age enforcement (configurable via `CHORD_MAX_AGE`)
- Local HTTP server on port 23841 for OAuth callback

### Real-time Updates

**`src/chord/subscribe.py`** - WebSocket subscriptions (optional)
- Listens to Linear via GraphQL subscriptions
- Wakes the watcher early when issues change
- Optional dependency (`gql[websockets]`)
- Handles reconnection with exponential backoff
- Not a replacement for polling - just an accelerator

**`src/chord/webhook.py`** - HTTP webhook receiver
- Tiny HTTP server on port 23842
- Accepts POSTs from Linear (or via tunnel)
- Payload is just a wake-up signal
- Watcher re-reads Linear after webhook trigger
- Faster than polling, but polling is the correctness backup

### Utilities

**`src/chord/notify.py`** - Desktop notifications
- **At most one banner per issue** — `failed`, `skipped`, `refused` always;
  `received` ("ENG-1 received" / "Delivered to claude.") on confirmed delivery
  unless `[notify] received = false`. Delivery is also recorded in the log,
  `state.json` and `chord info` either way. (b201600 removed `received`; it
  came back behind the setting, now on delivery rather than at hand-over start)
- Four verbs (`received`, `failed`, `skipped`, `refused`) rather than one
  free-form `send`, so the title cannot contradict the body
- macOS asks the daemon by a route that needs no app bundle, because Chord is a
  bare unsigned `python3` and `UNUserNotificationCenter` is unreachable from one:
  `terminal-notifier` if installed, otherwise `osascript`
- Linux and Windows keep using `desktop-notifier`, which works from a script there
- Text goes over as arguments, never concatenated into a script
- One group per issue, so a later outcome replaces an earlier one rather than
  stacking under it
- Bounded, killed rather than abandoned, and every failure silent
- `backend()` reports the route for `chord info`, which is the only way to find
  out a notification was going nowhere

**`src/chord/text.py`** - Text sanitization
- Flattens untrusted text to one safe line
- Removes control characters and terminal escapes
- Prevents log injection and terminal redraw attacks
- Called at log boundary, not at each source

**`src/chord/logging.py`** - Log output
- Dependency-free, `CHORD_LOG_LEVEL`-controlled, one timestamped line per event
- Everything goes through `text.one_line`, so a message cannot forge a line
- `write()` passes harness output through unchanged

**`src/chord/event_source.py`** - Shared live-event plumbing
- The base class behind `Webhook` and `Subscription`: one place that knows how to
  wait for a wake-up and how to count one
- A generation counter rather than a bare event, so an event that arrives while
  the watcher is mid-poll is not lost

**`src/chord/paths.py`** - File path resolution
- Finds config files by searching upward from CWD
- Handles legacy `chord.toml` vs new `.config/chord/chord.toml`
- Identifies project root for daemon state; never home or above, so
  `~/.config/chord/chord.toml` is user-wide config, not a project in `~`

**`src/chord/performance.py`** - Performance instrumentation
- An `instrumented` decorator that logs one structured `PERF` line per call at
  DEBUG (`CHORD_LOG_LEVEL=debug`): file, function, duration in ms, success/failure
- Measurements exist to inform a future rewrite decision, so only log lines —
  no histograms or dashboards
- A bool return value is the outcome, because `poll` and the webhook handler
  answer failures rather than raise them; `CancelledError` logs as a clean
  stop, because that is what `chord stop` does

## Data Flow

```
Linear (issue carrying a route label)
    ↓
linear.py (GraphQL fetch: one query for every route)
    ↓
routing.py (which label is newest, and what does it name)
    ↓
linear.py (label history, only when there is a choice to make)
    ↓
context.py (render to text)
    ↓
harness.py (send to external tool via stdin)
    ↓
watcher.py (delivered: log line + state.json, "received" banner if on)
    ↓
Harness output (logged to chord.log)
```

## State on Disk

**Project state** (`~/.chord/sessions/<project>/`):
- `chord.pid` - Watcher process ID
- `chord.log` - Watcher's log (what `chord watch` follows)
- `state.json` - Issue IDs that have been handed over (`handed_over`), held
  T3 dispatches (`t3_pending`), and what became of each recent hand-over
  (`deliveries`: identifier, harness, status, `delivered_at`, `ended_at`)
- `chord.lock` - File lock for start serialization

**Configuration** (searched upward from CWD):
- `.config/chord/chord.toml` (preferred)
- `~/.chord/chord.toml` (user-wide)
- `chord.toml` (legacy, root-level)
- `.env` (Linear client credentials)

## Key Design Decisions

1. **Read-only Linear**: Chord never writes back, so Linear stays the source of truth
13. **Trigger authority is explicit, and permissive by default**: `allowed_actors`
    exists and is off unless configured; `chord start` says which state it is in
2. **State in process-external file**: Restarting doesn't re-offer entire backlog
3. **Polling is the source of truth**: Webhooks/subscriptions are just accelerators
4. **Automatic token renewal, one renewer at a time**: Linear rotates refresh tokens, so renewals are serialised per machine and stored before use; `chord refresh` renews on demand
5. **Issue on stdin**: Avoids command-line length limits for long issues
6. **Prompt boundaries**: BEGIN/END markers make it clear what's untrusted content
7. **One-line logging**: All untrusted text flattened to prevent log injection
8. **Project-scoped state**: Multiple checkouts can each watch their own Linear project
9. **Routing is a label, not a config switch**: One poll covers every route, so
   curation costs nothing per issue
10. **"Newest route label" is read, not inferred**: Linear sends an issue's labels
    unordered, so the audit trail is the only honest answer
11. **An unknown route stops the issue**: Running work on a harness nobody chose
    is worse than not running it
12. **The prompt doesn't know about routing**: One rendered issue, whichever
    route it took

## Dependencies

**Core**:
- `typer` - CLI
- `rich` - Terminal output
- `authlib` - OAuth
- `httpx` - HTTP client
- `keyring` - Secure credential storage
- `python-dotenv` - Environment variables
- `desktop-notifier` - Desktop notifications on Linux and Windows only; on
  macOS its backend cannot reach Notification Centre from a script (see
  `notify.py`), so that platform goes through `terminal-notifier` or
  `osascript` instead

**Optional**:
- `gql[websockets]` - WebSocket subscriptions for live updates
- `terminal-notifier` (Homebrew, macOS) - Optional. Gives the banners
  terminal-notifier's name instead of Script Editor's. Nothing breaks without
  it; `chord info` reports which route is in use

## Testing

Tests are in `test/` directory:
- `test_at_least_once.py` - The at-least-once contract, and the margin around it
- `test_auth_expiry.py` - Token refresh across a long-running daemon
- `test_auto_renew.py` - The token renewing itself, so nobody runs `chord refresh`
- `test_authorise.py` - The `allowed_actors` allowlist and attribution
- `test_boundaries.py` - Linear answers that are not what the query asked for
- `test_cli.py` - CLI commands, including the `chord info` label showcase
- `test_config.py` - Configuration loading, including the curation table and
  `[notify]` validation
- `test_daemon.py` - Process management
- `test_service.py` - `chord service`: unit/plist text, escaping, env capture,
  systemctl/launchctl calls (recorded, never real), CLI hand-over, the
  signed-out managed watcher waiting instead of exiting
- `tools/service_e2e.sh` + `.github/workflows/service-e2e.yml` - the same
  commands against real systemd --user (ubuntu) and launchd (macOS) in CI
- `test_delivery.py` - The path from a Linear issue to a harness, its one
  banner, and `notify_received=False` keeping a working hand-over quiet
- `test_delivered.py` - Delivery confirmation: what counts as delivered, what
  the watcher records, what `chord info` shows, and the `received` banner going
  out while the harness is still running
- `test_findings.py` - Issue handling
- `test_harness.py` - Running an external command without letting it run away
- `test_presets.py` - The opencode default and the Claude Code / Codex / Cursor presets against stand-in
  executables, the PATH check in start/info/setup, and what a failure says
- `test_log_safety.py` - What untrusted text is allowed to do to the log
- `test_notify.py` - Notification shape, platform routing, injection safety, and
  the one-banner-per-issue rule
- `test_pagination.py` - Linear pagination
- `test_performance.py` - The instrumentation, measured by what it writes down
- `test_permissions.py` - What Chord writes to disk, and who can read it
- `test_process_identity.py` - `chord stop` signalling only Chord's own processes
- `test_repo.py` - Which repository an issue gets worked on, and when it refuses
- `test_routing.py` - Label grammar, curation, and which harness runs
- `test_stale_pid.py` - A stale pid file not wedging `chord start`
- `test_subscribe.py` - WebSocket subscriptions
- `test_t3.py` - The T3 Code CLI integration, with a local executable standing in
- `test_watcher.py` - Main polling loop
- `test_webhook.py` - Webhook handling
- `test_webhook_secret.py` - The webhook shared secret
- `test_wrong_harness.py` - An issue only ever worked on by the harness its label named

`tools/probe_linear.py` is a read-only live check: it asks Linear the same
questions Chord does, so a query shape can be verified without a test account.
