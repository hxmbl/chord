# Chord

> Connect Linear together with the tools you use to build.

Chord watches Linear for issues carrying one label, and hands each one to a
coding harness of your choosing.

```
Linear  →  Chord  →  Context  →  chosen harness
```

Chord is the layer that isn't Cursor. Put the `chord` label on an issue, and
whatever you normally build with — Claude Code, Codex, a script of your own —
is handed the issue and gets to work on it.

It never writes back to Linear. Linear stays the source of truth; Chord is the
trigger.

---

## Requirements

- [uv](https://docs.astral.sh/uv/)
- Python 3.11 or newer
- A Linear OAuth application

Chord asks Linear for the `read` scope and nothing else. It never writes to
Linear.

Chord starts a local webhook listener for fast issue notifications. Polling is
kept as the reliable backup, so a dropped webhook cannot lose work.

## Setup

### 1. Create a Linear OAuth application

In Linear: **Settings → Security & access → OAuth applications → New OAuth
application**.

- **Redirect URL**: `http://127.0.0.1:23841/callback`
- **Permissions**: `read`

Linear shows the client secret once, at creation.

### 2. Put the credentials in `.env`

```bash
LINEAR_CLIENT_ID="your client id"
LINEAR_CLIENT_SECRET="your client secret"
```

`.env` is already git-ignored, and the token itself never goes near it — see
[Credentials](#credentials) below.

### 3. Connect

```bash
uv sync
uv run chord setup
```

This opens a browser, asks Linear to confirm, and puts the resulting token in
your keychain. `chord setup` is the only step that needs a person.

### 4. Start watching

```bash
uv run chord start
```

Put the `chord` label on an issue. Within a minute it appears in the log:

```bash
uv run chord watch
```

That is the whole loop, and it works with no config file at all.

---

## Commands

| Command            | What it does                                            |
| ------------------ | ------------------------------------------------------- |
| `chord setup`      | Connect Chord to Linear. Once.                          |
| `chord start`      | Watch in the background.                                |
| `chord stop`       | Stop watching.                                          |
| `chord watch`      | Follow the log, live.                                   |
| `chord info`       | Show the connection, the settings, and the state.       |
| `chord refresh`    | Renew the token. You'll need this about once a day.     |

`chord info` is the one to reach for when something isn't happening.

---

## chord.toml

Chord runs with no config file. Write `.config/chord/chord.toml` in your
project when you want to change the harness, label, or webhook port. A user
config at `~/.chord/chord.toml` is also supported; the project config wins.

```toml
# The label that triggers a run. Default: "Chord".
label = "Chord"

# What does the work. Default: "print".
harness = "print"

# Seconds between checks of Linear. Default: 60, floor 5.
interval = 60

# Local webhook listener. Default: 23842; use 0 for an ephemeral test port.
webhook_port = 23842
```

Commit this file. It holds no secrets. Legacy root-level `chord.toml` files are
still read for compatibility.

Chord looks for `chord.toml` in the directory you run it from and then upward,
so `chord start` works the same from a subdirectory. The nearest one wins. That
directory is also what identifies the project to the rest of Chord — see
[State on disk](#state-on-disk).

## Live updates

The watcher listens for HTTP `POST` events at `/webhook` and immediately
re-checks the configured label. Point a Linear webhook or a public tunnel at
the URL printed in the log. The webhook payload is only a wake-up signal;
Chord fetches the issue and discussion itself, so duplicate or malformed event
payloads are harmless. Polling continues at `interval` as the backup.

Linear's subscription filters can narrow by assignee, project, state, parent
and team, but not by label, so every issue event in the workspace arrives and
the label is checked afterwards. A spurious event costs one poll that finds
nothing.

Set `interval` as high as you like when webhooks are working — it becomes the
cadence for catching anything missed during a delivery outage, not the delay
before you hear about a new issue.

## Harnesses

`harness` is either a name Chord knows or a command to run.

**`print`** is the default. It writes the issue to Chord's log and is a real
harness, not a placeholder — it makes the first run inspectable.

Anything else is run as a command, with the issue **on standard input**:

```toml
harness = ["claude", "-p"]
harness = ["./scripts/on-issue.sh"]
harness = ["my-agent", "--prompt", "-"]
```

A bare name works too, if it's on your `PATH`:

```toml
harness = "claude"
```

That is the whole compatibility layer. Chord doesn't need to know what a
harness is, so pointing it at a tool Chord has never heard of is a config
change, not a release.

Two things follow from the issue arriving on stdin:

- It works regardless of how long the issue is. A description plus a
  discussion can run past the operating system's limit on command-line
  arguments, and stdin has no such limit.
- Your harness has to read stdin. Most agent CLIs take a prompt this way; if
  yours wants a file, wrap it: `["sh", "-c", "cat > /tmp/issue.md; my-agent --file /tmp/issue.md"]`

Whatever your harness prints goes into Chord's log, and a non-zero exit is
reported as a failure.

---

## How it works

Once a minute, Chord asks Linear for issues carrying the label. For each one it
hasn't seen, it fetches the discussion, renders the two together, and hands
that to the harness.

**A poll reads the whole label, not just its first page.** Chord follows
Linear's cursor up to 1000 issues, so a label with a long history is fully
visible. If a label somehow exceeds that, the log says so rather than quietly
reading a prefix and leaving you to wonder why a few issues never arrive.

**Each issue is handed over once.** The record lives in the project's state
file rather than in the process, so restarting Chord doesn't offer your whole
backlog again. To start over, stop Chord and delete that file. Chord remembers
the most recent 1000 hand-overs, matching the most a single poll can offer.

**A backlog works oldest first**, so issues are picked up in the order they
were written.

**Chord keeps running when Linear doesn't.** An unreachable Linear, a rate
limit, or a token that needs renewing is written to the log once and retried on
the next tick; the watcher doesn't die. Run `chord refresh` and a running
watcher picks the new token up on its next poll.

**A failed hand-over is recorded anyway.** If your harness is missing or
errors, the log says so and Chord moves to the next issue. Retrying forever
would mean one broken harness blocked everything behind it.

**The issue is delimited, not just concatenated.** Everything a person wrote in
Linear goes to the harness between explicit `BEGIN`/`END` markers, under a line
saying it is the work to be done and not instructions about how to behave. An
issue body is written by whoever filed it, and a harness is often an agent that
does what the text tells it to. This is a prompt boundary, not a security
boundary: it makes the distinction legible, and nothing more.

## State on disk

Everything Chord writes lives under `~/.chord/sessions`, in a directory per project, so
nothing lands in your repository and two checkouts can each watch their own
Linear project:

```
~/.chord/sessions/<project>/
```

| File            | What it is                          |
| --------------- | ----------------------------------- |
| `chord.pid`     | The watcher's process, if running   |
| `chord.log`     | What `chord watch` follows          |
| `state.json`    | Which issues have been handed over  |
| `chord.lock`    | Held while `chord start` is running |

The project is the nearest directory containing a `chord.toml`, so `chord stop`
and `chord info` find the right watcher from anywhere inside it.

`chord stop` checks that the process behind the pid really is a Chord watcher
before signalling it. A pid file can outlive a reboot and pids get reused, and
terminating a stranger's process is worse than refusing.

## Credentials

- Client credentials come from `.env`, which is git-ignored. Like `chord.toml`,
  it is looked for in the current directory and then upward.
- Tokens are stored in your OS keychain — the macOS Keychain, the libsecret
  keyring on Linux — never in a file Chord writes.
- Chord never prints a token, and reports Linear failures by status code rather
  than by echoing a response body.
- `chord info` reads the keychain and nothing else, so it stays offline.
- Text that came from Linear — issue titles, comments, error messages — is
  flattened to a single printable line before it reaches the log, so it can't
  break a log entry or carry terminal escapes.

Linear's tokens last 24 hours, and `chord refresh` is deliberately never
automatic: Linear rotates the refresh token on every exchange, so renewing
silently would be a decision you never made.

---

## Direction

Chord starts with one simple goal: connect project updates to the tools that
act on them. Trigger modes come one at a time — label first, assignment next,
agent sessions later — and they stay mutually exclusive.

Start small. Build outward.
