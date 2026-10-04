# Chord

> Connect Linear together with the tools you use to build.

Chord watches Linear for issues carrying a label (default: `Chord`), and hands
each one to a coding harness of your choosing. That's the whole thing.

```
Linear  →  Chord  →  Context  →  chosen harness
```

Chord is the layer that connects your issue tracker to your coding tools. Put the
`Chord` label on an issue, and whatever you normally build with — opencode,
Claude Code, Codex, or a custom script — is handed the issue on stdin and gets
to work on it.

Want a different harness for a particular issue? Give it a different label.
`Chord/opencode/space-bunny-free` sends that issue to the `space-bunny-free`
harness instead of the default one, while everything else keeps going to the
default. See [Routing](#routing).

It never writes back to Linear. Linear stays the source of truth; Chord is only
a trigger that reads and forwards.

---

## Requirements

- [uv](https://docs.astral.sh/uv/)
- Python 3.11 or newer
- A Linear OAuth application (for authentication)

Chord asks Linear for the `read` scope only. It never writes to Linear.

## Setup

### 1. Create a Linear OAuth application

In Linear: **Settings → Security & access → OAuth applications → New OAuth
application**.

- **Redirect URL**: `http://127.0.0.1:23841/callback`
- **Permissions**: `read`

Linear shows the client secret once, at creation. Copy it somewhere safe.

### 2. Put the credentials in `.env`

```bash
LINEAR_CLIENT_ID="your client id"
LINEAR_CLIENT_SECRET="your client secret"
```

`.env` is already git-ignored, and the token itself never goes near it — see
[Credentials](#credentials) below.

### 3. Connect to Linear

```bash
uv sync
uv run chord setup
```

This opens a browser, asks Linear to confirm, and puts the resulting token in
your keychain. `chord setup` is the only step that needs a person.

### 4. (Optional) Set up a Linear webhook for instant notifications

Without a webhook, Chord polls Linear every 60 seconds (configurable). To get
instant notifications when issues are labeled:

1. In Linear: **Settings → Integrations → Webhooks → Add webhook**
2. **URL**: `http://<your-external-url>:23842/webhook` (see note below)
3. **Events**: Select "Issue created" and "Issue updated"
4. **Teams**: Select the team(s) you want to watch

**Note on the URL**: Chord's webhook listens on `127.0.0.1:23842` by default, which
only works from your machine. To receive Linear webhooks, expose this port publicly
using a tunnel service (like ngrok, Cloudflare Tunnel, or Tailscale) and use
that public URL. Run `chord info` to see the exact webhook URL Chord is listening on.

If you skip this step, polling still works — webhooks are purely for speed.

### 5. Start watching

```bash
uv run chord start
```

Put the `Chord` label on an issue. Within a minute (or instantly with webhooks) it
appears in the log:

```bash
uv run chord watch
```

That is the whole loop, and it works with no config file at all.

To send a particular issue somewhere else, label it `Chord/<name>` — see
[Routing](#routing).

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
project when you want to change the harness, the curated harnesses, the label,
or the webhook port. A user config at `~/.chord/chord.toml` is also supported;
the project config wins.

```toml
# The label that triggers a run. Default: "Chord".
label = "Chord"

# What does the work by default. Default: "opencode".
harness = "opencode"

# Seconds between checks of Linear. Default: 60, floor 5.
interval = 60

# Local webhook listener. Default: 23842; use 0 for an ephemeral test port.
webhook_port = 23842

# Who may trigger a run. Default: everyone in the workspace — read the
# "Who can trigger a run" section below before leaving it that way.
allowed_actors = ["11111111-1111-1111-1111-111111111111"]

# Extra named harnesses. A `Chord/<name>` label routes an issue to the one
# called `<name>` instead of the default. See "Routing" below.
#
# Tables come last: in TOML everything after a `[table]` header belongs to that
# table, so `interval` above has to be written before the first one of these.
[harnesses."opencode/space-bunny-free"]
command = ["opencode", "run", "--model", "space-bunny-free"]

[harnesses.claude]
command = ["claude", "-p"]
```

Commit this file. It holds no secrets. Legacy root-level `chord.toml` files are
still read for compatibility.

Chord looks for `chord.toml` in the directory you run it from and then upward,
so `chord start` works the same from a subdirectory. The nearest one wins. That
directory is also what identifies the project to the rest of Chord — see
[State on disk](#state-on-disk).

### Whose chord.toml is it?

**Running `chord start` in a repository runs that repository's `harness`
command.** This is the same trust model as a `Makefile`, a `package.json`
script, or a `.env` your shell loads: the config in a directory is code you have
agreed to run by being in that directory. Chord does not prompt, because
prompting on every start would be worse than the thing it protects against.

So the practical rule is the one you already apply to build files: read it
before you run it, and don't run `chord start` in a repository you just cloned
to look around. Nothing here is a Chord bug to be fixed — a project config that
didn't configure the project would not be a project config.

`.env` is read the same way and for the same reason. It supplies your Linear
client id and secret, which are how Chord obtains a token — it holds no token
itself, because those live in your OS keychain.

## Who can trigger a run

Adding a `Chord` label runs a command on your machine. With no configuration,
anyone whose Linear account can label an issue can do that — which is a much
lower bar than "may run code here". It is the default because every existing
install is in that state and changing it silently would stop every hand-over,
but it is worth closing.

`allowed_actors` is the list of Linear user ids allowed to trigger a run.
`chord info` prints yours, because Linear's UI does not:

```
$ chord info
  ...
  allowed    1 Linear user id(s)
             yours: 11111111-1111-1111-1111-111111111111
```

It is a list of **ids**, and that is deliberate. Linear lets anyone rewrite their
own `name` and `displayName` through its API, and neither is unique, so an
allowlist keyed on either could be satisfied by somebody who merely typed a name
they were not given. `User.email` is not self-editable and would work, but it
churns when someone's address changes, and a churn here fails *closed* — the
issue is skipped, and it reads as Chord being broken. The id never changes.

Chord checks the person who added the **routing label**, because that is the act
which caused the run. An issue narrowed from `Chord` to `Chord/claude` is
authorised by whoever narrowed it, not by whoever filed it.

Two cases fall back rather than checking, and both say so in the log:

- **The label-add has fallen off Linear's 50-entry history page.** The issue's
  creator stands in.
- **An integration applied the label.** Linear reports no actor for a bot, and
  Chord refuses — an automated rule applying `Chord` is code execution
  triggered by a rule rather than by a person, which is the thing this setting
  exists to make deliberate.

A refusal is recorded like every other hand-over that did not happen, so it is
not re-examined on every poll. The log says which person and which id, and how
to allow them.

## Routing

Which harness runs an issue is decided by its labels.

| Label                       | Harness                     |
| --------------------------- | --------------------------- |
| `Chord`                     | whatever `harness` says     |
| `Chord/claude`              | the curated `claude`        |
| `Chord/opencode/tiny`       | the curated `opencode/tiny` |

The whole part after `Chord/` is the harness name. Nothing in Chord knows what
a harness is or how many segments its name has, so `opencode/tiny` is a name
you chose, not a hierarchy Chord has to interpret.

**An issue can carry more than one.** Label it `Chord`, then narrow it to
`Chord/opencode/space-bunny-free` when you know where you want it to go, and
both labels sit on the issue. Chord routes on **the one added most recently**,
which it reads from Linear's own audit trail — not from a guess about which
label is more specific. Narrowing an issue therefore changes where it goes,
and handing it back to the default is just adding `Chord` again.

If Linear can't be asked, Chord falls back to the most specific label on the
issue and says so in the log.

**A route with no harness behind it stops the issue.** `Chord/typo-here` isn't
a typo Chord can shrug off: running the work on the default harness instead
would hand it to an agent nobody asked for, on arguments nobody chose. So the
issue is skipped, the log names the label and how to fix it, and the issues
behind it still get their turn.

`chord info` lists every route, which is the place to check what a label you
typed in Linear will actually do. `chord start` builds every route before it
spawns anything, so a curated harness naming a command that isn't installed is
reported by the command rather than by the first issue that trips it.

## How webhooks work

If you set up a Linear webhook (see Setup step 4), the watcher receives instant
notifications. The webhook payload is only a wake-up signal; Chord always re-fetches
the full issue and discussion from Linear, so duplicate or malformed payloads
are harmless.

Polling continues at your configured `interval` as a reliable backup. You can
set `interval` high (even hours) when webhooks are working — it only controls
how long Chord waits to catch anything missed during a delivery outage, not how
quickly you see new issues.

## Harnesses

`harness` is either a name Chord knows or a command to run. Under
`[harnesses.<name>]`, `command` means the same thing, and the entry's name is
what a `Chord/<name>` label routes to.

**`opencode`** is the default built-in harness. **`print`** is a built-in
debugging harness: it writes the issue to Chord's log so you can see exactly
what would be handed over before trusting it with a real agent.

Anything else is run as a command, with the issue **on standard input**:

```toml
harness = ["claude", "-p"]

[harnesses.review]
command = ["./scripts/on-issue.sh"]

[harnesses.my-agent]
command = ["my-agent", "--prompt", "-"]
```

A bare name works too, if it's on your `PATH`:

```toml
harness = "claude"

[harnesses.review]
command = "claude"
```

That is the whole compatibility layer. Chord doesn't need to know what a
harness is, so pointing it at a tool Chord has never heard of is a config
change, not a release.

A curated name and the default are separate things. `harness` is what a bare
`Chord` runs; `Chord/foo` always means the entry named `foo`, even when the two
happen to spell the same command.

Two things follow from the issue arriving on stdin:

- It works regardless of how long the issue is. A description plus a
  discussion can run past the operating system's limit on command-line
  arguments, and stdin has no such limit.
- Your harness has to read stdin. Most agent CLIs take a prompt this way; if
  yours wants a file, wrap it: `["sh", "-c", "cat > /tmp/issue.md; my-agent --file /tmp/issue.md"]`

Whatever your harness prints goes into Chord's log, and a non-zero exit is
reported as a failure.

**After the hand-over it isn't Chord's problem.** A harness that half-finishes,
needs a nudge, or wants a PR opened is doing that on its own. Chord waits for
the command to exit and writes down what it printed.

Command harnesses have a 30-minute execution timeout. Set `HARNESS_TIMEOUT`
in the environment to use a different value in seconds. It has to be a positive
number of seconds — a negative or non-numeric value is reported in the log and
the default is used, because a timeout that fires immediately would mark your
whole backlog as done without running any of it. Long-running work should
checkpoint so it can be resumed after a timeout.

On a timeout, and on `chord stop`, Chord kills the whole process group rather
than just the command it started — a harness with a worker pool, a background
server, or anything else it spawned goes too. Nothing it started is left
running against your checkout.

## Adding Built-in Harnesses

A new harness is a line in `chord.toml`, not a change to Chord. Reach for a
built-in only when a harness needs to *do* something rather than run something —
`print` is the only one that does, and it exists so a first run is inspectable
instead of speculative.

To add one anyway, in `src/chord/harness.py`:

1. Write a class with a `name` (string) and an
   `async def send(self, prompt: str) -> None`.
2. Add a name check to `build()`.

A built-in is reachable from `[harnesses.<name>]` like any other command, since
`build()` is what resolves both.

---

## How it works

Once a minute, Chord asks Linear for issues carrying a route label — the bare
`Chord` label or any `Chord/...` one, in a single request however many harnesses
you have curated. For each issue it hasn't seen, it picks a route, fetches the
discussion, renders the two together, and hands that to the chosen harness.

**One poll reads every route, and all of it.** The filter goes to Linear, so the
number of curated harnesses doesn't change how often Chord talks to Linear, and
Chord follows the cursor up to 1000 issues rather than reading a prefix. If the
routes somehow exceed that, the log says so rather than leaving you to wonder
why a few issues never arrive.

**Each issue is handed over once.** The record lives in the project's state
file rather than in the process, so restarting Chord doesn't offer your whole
backlog again. To start over, stop Chord and delete that file. Chord remembers
the most recent 1000 hand-overs, matching the most a single poll can offer.

This is at-least-once, not exactly-once, and the difference is worth being
precise about. The record is written *after* the harness has run, because
writing it first would mean an issue that never got worked on is never offered
again — and losing your work is worse than doing it twice. So if Chord is killed
in the instant between your agent finishing and the record landing, that issue
is offered again next time. That window is small and it is deliberate; closing
it is not possible without trading it for the opposite failure.

Everything else in that window is already closed. A crash, an interrupt, or an
error part-way through is recorded before it can propagate, and the record is
flushed to disk rather than left in memory, so a power cut does not lose it
either. What is left is only the case where the process is gone before it can
say anything at all.

**A backlog works oldest first**, so issues are picked up in the order they
were written.

**Chord keeps running when Linear doesn't.** An unreachable Linear, a rate
limit, or a token that needs renewing is written to the log once and retried on
the next tick; the watcher doesn't die. Run `chord refresh` and a running
watcher picks the new token up on its next poll.

**A failed hand-over is recorded anyway.** If your harness is missing or
errors, the log says so and Chord moves to the next issue. Retrying forever
would mean one broken harness blocked everything behind it. The same goes for a
route label with no harness behind it: skipped, logged, and out of the way.

**The issue is delimited, not just concatenated.** Everything a person wrote in
Linear goes to the harness between explicit `BEGIN`/`END` markers, under a line
saying it is the work to be done and not instructions about how to behave. An
issue body is written by whoever filed it, and a harness is often an agent that
does what the text tells it to. This is a prompt boundary, not a security
boundary: it makes the distinction legible, and nothing more.

**Which harness ran is in the log, not in the prompt.** The rendered issue is
the same whichever route it took. The label that decided it is already on the
issue and appears in the `Labels:` line, and Chord writes the route it chose to
its own log. Nothing about routing leaks into the work.

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
