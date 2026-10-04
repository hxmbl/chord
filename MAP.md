# Chord Codebase Map

## Overview
Chord is a Python tool that watches Linear for issues carrying one of its route labels and hands each one to the coding harness that label names (opencode, Claude Code, or any command-line tool).

## Entry Point
**`src/chord/cli.py`** - The command-line interface
- Six commands: `setup`, `refresh`, `start`, `stop`, `watch`, `info`
- Uses Typer for CLI parsing
- Spawns a background daemon for the watcher process
- All commands are thin wrappers that validate state before calling other modules

## Core Modules

### Configuration
**`src/chord/config.py`** - Settings management
- Reads `chord.toml` from project directory (searches upward)
- Falls back to user config `~/.chord/chord.toml`
- Falls back to defaults if no config exists
- Settings: `label`, `harness`, `harnesses`, `interval`, `webhook_port`,
  `allowed_actors`
- `harnesses` is the curation table: `<name>` to a command
- `allowed_actors` is a list of Linear **user ids**; ids and not names because
  Linear lets anyone rewrite their own display name
- All settings have defaults, so Chord works without a config file

### Routing
**`src/chord/routing.py`** - Which harness an issue goes to
- Label grammar: bare `<label>` is the default, `<label>/<name>` is a curated harness
- The whole suffix is the name, so `opencode/tiny` is one name, not a hierarchy
- `filter()` builds one Linear query covering every route, curated or not
- `candidates()` reads an issue's route labels, most specific first
- `harness_for()` resolves a name to a built harness, memoized
- `validate()` builds every route so `chord start` can report a broken one

### The Watcher Loop
**`src/chord/watcher.py`** - Main polling logic
- Polls Linear for routed issues at configured interval
- Picks a route per issue; with several route labels, asks Linear's audit
  trail which was added most recently, falling back to the most specific
- Checks who added that label against `allowed_actors` when one is configured
- Tracks which issues have been handed over in `state.json`
- Processes issues oldest-first (by `createdAt`)
- Hands each issue to the harness its labels select
- Never raises: logs problems and continues
- Integrates with webhooks and subscriptions for faster response

**`src/chord/linear.py`** - GraphQL API interactions
- Reads issues matching a route filter with pagination (up to 1000 per poll)
- Reads an issue's label history as `LabelChange` records, newest first, each
  carrying the `Actor` (person or integration) who added those labels
- Fetches comments for each issue (up to 200 comments)
- Uses `httpx` for async HTTP requests
- Never writes to Linear (read-only)
- Handles GraphQL errors and reports them cleanly

### Process Management
**`src/chord/daemon.py`** - Background process handling
- Manages the watcher's lifecycle as a detached process
- State lives in `~/.chord/sessions/<project>/` (one per project)
- Files: `chord.pid`, `chord.log`, `state.json`, `chord.lock`
- Project identified by directory containing `chord.toml`
- Uses file locking to prevent race conditions on start
- Validates process is actually a Chord watcher before stopping

### Linear API Client
### Harness Execution
**`src/chord/harness.py`** - Running external tools
- Built-in: `print` harness (writes to log for debugging/inspection)
- Built-in: `opencode` harness (the default)
- Command harness: runs any command with issue on stdin
- Protocol-based design for extensibility
- Issues sent via stdin to avoid command-line length limits
- Captures stdout/stderr and logs them
- Non-zero exit codes are reported as failures

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
- Token refresh with `chord refresh` (never automatic)
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
- Best-effort notifications via `desktop-notifier`
- Notifies when Chord starts/finishes working on an issue
- Silent if desktop notifications unavailable

**`src/chord/text.py`** - Text sanitization
- Flattens untrusted text to one safe line
- Removes control characters and terminal escapes
- Prevents log injection and terminal redraw attacks
- Called at log boundary, not at each source

**`src/chord/paths.py`** - File path resolution
- Finds config files by searching upward from CWD
- Handles legacy `chord.toml` vs new `.config/chord/chord.toml`
- Identifies project root for daemon state

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
Harness output (logged to chord.log)
```

## State on Disk

**Project state** (`~/.chord/sessions/<project>/`):
- `chord.pid` - Watcher process ID
- `chord.log` - Watcher's log (what `chord watch` follows)
- `state.json` - Issue IDs that have been handed over
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
4. **Never automatic token refresh**: Linear rotates refresh tokens, so this is a user decision
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
- `desktop-notifier` - Desktop notifications

**Optional**:
- `gql[websockets]` - WebSocket subscriptions for live updates

## Testing

Tests are in `test/` directory:
- `test_cli.py` - CLI commands
- `test_config.py` - Configuration loading, including the curation table
- `test_daemon.py` - Process management
- `test_findings.py` - Issue handling
- `test_pagination.py` - Linear pagination
- `test_routing.py` - Label grammar, curation, and which harness runs
- `test_subscribe.py` - WebSocket subscriptions
- `test_watcher.py` - Main polling loop
- `test_webhook.py` - Webhook handling

`tools/probe_linear.py` is a read-only live check: it asks Linear the same
questions Chord does, so a query shape can be verified without a test account.
