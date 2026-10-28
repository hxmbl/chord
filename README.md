# Chord

> Connect Linear together with the tools you use to build.

Chord watches Linear for issues carrying a label (default: `Chord`), and hands
each one to a coding harness of your choosing. That's the whole thing.

```
Linear  →  Chord  →  Context  →  chosen harness
```

Chord is the layer that connects your issue tracker to your coding tools. Put the
`Chord` label on an issue, and whatever you normally build with — opencode,
Claude Code, Codex, Cursor, or a custom script — is handed the issue and gets
to work on it.

Want a different harness for a particular issue? Give it a different label.
`Chord -> opencode -> big-pickle` sends that issue to the curated
`opencode/big-pickle` harness instead of the default one, while everything
else keeps going to the default. See [Routing](#routing).

It never writes back to Linear. Linear stays the source of truth; Chord is only
a trigger that reads and forwards.

---

## Requirements

- [uv](https://docs.astral.sh/uv/)
- Python 3.12 or newer
- A Linear account (`chord setup` walks you through making the OAuth app)

Chord asks Linear for the `read` scope only. It never writes to Linear.

## Setup

```bash
uv tool install git+https://github.com/hxmbl/chord
chord setup
```

`chord setup` asks a few questions and does the rest. Pressing Enter takes the
default every time:

1. **Your Linear app.** Chord signs in through an OAuth app you own. Setup says
   where to make one in Linear (redirect URL `http://127.0.0.1:23841/callback`,
   permission `read`), then takes the client ID and secret and saves them to
   `.env`. It offers to git-ignore `.env` if your repository would commit it.
2. **Signing in.** Your browser opens, Linear asks you to confirm, and the token
   goes in your keychain. Chord renews it on its own from then on.
3. **This project.** The label that sends an issue to Chord (`Chord` by
   default) and the coding tool to hand it to, picked from the ones it finds
   installed. You're added to `allowed_actors`, so only you can trigger a run
   until you add teammates. That's written to `.config/chord/chord.toml`.

Every step that's already done is skipped, so running it again is safe. It
never overwrites a `chord.toml` you already have, and it never touches Linear.

Then create the label in Linear (Chord never creates one) and start watching:

```bash
chord start
```

`chord start` runs setup first if Chord isn't signed in yet. Put the label on an
issue and, within a minute, it appears in the log:

```bash
chord watch
```

To send a particular issue somewhere else, label it `Chord -> <name>`. See
[Routing](#routing).

### Webhooks (optional)

Without a webhook Chord polls Linear every 60 seconds. For instant pick-up, see
[Webhooks](#webhooks); `chord info` prints the URL to give Linear.

---

## Commands

| Command                  | What it does                                            |
| ------------------------ | ------------------------------------------------------- |
| `chord setup`            | Get Chord ready. Safe to run again.                     |
| `chord init`             | Write the stock `chord.toml`, annotated.               |
| `chord config`           | Edit `chord.toml` in your editor; checked before saving. |
| `chord harness add`      | Curate a harness: `chord harness add NAME -- CMD ...`.  |
| `chord start`            | Watch in the background. Sets up first if needed.       |
| `chord refresh`          | Renew the token now. Chord also does it on its own.     |
| `chord stop`       | Stop watching.                                          |
| `chord watch`      | Follow the log, live.                                   |
| `chord info`       | Show the settings, the labels Chord acts on, and the state. |
| `chord service ...` | Run Chord at login as a systemd/launchd service. See below. |
| `chord help`       | The same as `chord --help`.                             |

`chord help <command>` is `chord <command> --help`, and `chord info` is the one
to reach for when something isn't happening.

## Running as a service

`chord start` keeps watching until you stop it or the machine restarts. To have it start by itself at login and come back if it
crashes, hand it to your operating system's service manager:

```sh
chord service install    # write the service and start it now
chord service status     # installed? running? its newest log lines
chord service logs       # follow its log (--no-follow, -n 200)
chord service restart    # after editing chord.toml
chord service stop       # until the next login; `start` brings it back
chord service uninstall  # stop it and remove it
```

Run them from inside the project, like every other command: each project gets
its own service, named from the same per-project id as its state directory, so
two checkouts never share one. `chord setup` offers to install it at the end
(the answer defaults to no). With a service installed, `chord start`,
`chord stop` and `chord watch` go through it, so there is only ever one watcher
per project; `chord info` has a `service` line saying which it is.

| | Linux | macOS |
| --- | --- | --- |
| What | systemd **user** unit, no root | LaunchAgent in your login session |
| File | `~/.config/systemd/user/chord-<project>.service` | `~/Library/LaunchAgents/io.hxmbl.chord.<project>.plist` |
| Restarts | on failure, 10s apart; gives up after 5 in 5 minutes | after a crash (not a clean stop), 10s apart |
| Log | the journal: `journalctl --user -u chord-<project>` | `~/Library/Logs/chord/<project>.log` |

The service runs the same watcher `chord start` does, with the Python that ran
`chord service install` and in the project's directory. Install Chord somewhere
permanent first (`uv tool install`, `pipx`); a service pointing into `uvx`'s
cache breaks when the cache is cleaned, and the install warns if it looks like
that.

**Your PATH is copied in at install time.** A service doesn't start from your
shell, so it wouldn't otherwise find `opencode`, `claude` or `codex` in
`~/.local/bin`, Homebrew or nvm. `PATH`, `HOME`, the locale, the `XDG_*`
directories and Chord's own variables (`CHORD_LOG_LEVEL`, `CHORD_MAX_AGE`,
`HARNESS_TIMEOUT`, `PYTHON_KEYRING_BACKEND`) are written into the unit or
plist. Nothing else is, API keys included: they would end up in a file on disk.
Harnesses that sign in on their own (`claude`, `codex`, `opencode auth`) don't
need them. Installed a tool somewhere new? Run `chord service install` again; it
replaces the service in place.

**Linux: logged out.** A user service runs while you are logged in. To keep it
running after you log out, and from boot, enable lingering for your account:

```sh
loginctl enable-linger $USER
```

Chord never runs that for you; it is a setting for your whole account. Note
that Chord's token lives in your keyring, and some desktop keyrings stay locked
until you log in, so a service started from boot may log that it isn't signed
in until you do.

**Not signed in is a wait, not a crash.** A service that finds no usable token
(installed by a script before `chord setup`, a keyring still locked, a token
past its max age after a week off) stays up, logs the reason once, and looks
again every `interval` seconds. Once `chord setup` or `chord refresh` stores a
token it starts watching by itself; there is nothing to restart.

Notifications and token renewal work as they do under `chord start`: a user
unit has your session's D-Bus for banners, a LaunchAgent can reach the login
keychain and Notification Centre.

Without systemd or launchd — Windows, a container, WSL without systemd —
`chord service` says so and `chord start` is the way to run it.

---

## chord.toml

Chord runs with no config file. `chord config` opens the one it reads (or the
stock one) in `$VISUAL`/`$EDITOR`, and won't save it until it loads; `chord
config --path` prints which file that is. `chord init` writes
`.config/chord/chord.toml` in your project — every setting at its default,
each with a comment — and `chord harness add NAME -- CMD ...` curates a
harness for you, creating the file if there isn't one:

```bash
chord harness add big-pickle -- opencode run --model big-pickle
```

It prints the `Chord -> <name>` label to create in Linear, because Chord never
creates a label itself. Give a nested name with `/`:
`chord harness add opencode/big-pickle -- opencode run --model big-pickle`
prints `Chord -> opencode -> big-pickle`. A user-wide config at
`~/.config/chord/chord.toml` or `~/.chord/chord.toml` is also read; a project's
own config wins. The home directory is never treated as a project, so a config
in `~/.config/chord` doesn't make `~` where issues run.

```toml
# The label that triggers a run. Default: "Chord".
label = "Chord"

# What does the work by default: "opencode" (runs `opencode run`, the issue on
# stdin), a preset ("claude", "codex", "cursor"), "print", or a command.
# Default: "opencode".
harness = "opencode"

# Seconds between checks of Linear. Default: 60, floor 5.
interval = 60

# Local webhook listener. Default: 23842; use 0 for an ephemeral test port.
webhook_port = 23842

# Shared secret for the webhook, sent as the X-Chord-Secret header. Default: ""
# — no authentication. Set it to anything else and every request must carry a
# matching header or get a 401. See "Webhooks" below before setting one.
webhook_secret = ""

# Who may trigger a run. Default: everyone in the workspace — read the
# "Who can trigger a run" section below before leaving it that way.
allowed_actors = ["11111111-1111-1111-1111-111111111111"]

# Tables come last: in TOML everything after a `[table]` header belongs to that
# table, so `interval` above has to be written before the first one of these.

# Desktop banners. Failed, skipped and refused always post one. `received` is
# the banner for an issue that reached its harness, posted once delivery is
# confirmed; false keeps deliveries quiet (the log and `chord info` still show
# every one). Default: true. See "Notifications" below.
[notify]
received = true

# Extra named harnesses. A `Chord -> <name>` label routes an issue to the one
# called `<name>` instead of the default. See "Routing" below.
[harnesses."opencode/big-pickle"]
command = ["opencode", "run", "--model", "big-pickle"]

[harnesses.claude]
command = ["claude"]   # the Claude Code preset; see "Harnesses" below

# Optional T3 Code CLI settings, used only when the selected harness is "t3".
[t3]
# provider = "provider-instance-id"  # Set with model.
# model = "model-slug"                # Set with provider.
# worktree = "main"                   # Optional T3 git worktree base.
# branch = "chord/issue-branch"       # Optional; requires worktree.
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

### T3 Code

Chord's built-in `t3` harness uses the official `t3 thread` CLI. Install the
T3 Code CLI, keep its server running, and add the repository Chord resolves for
each issue as a T3 Code project. Chord passes that exact resolved directory to
`t3 thread start` as the project path; it will not silently fall back to a
different project. Select it with `harness = "t3"` or a curated `t3` route:

```toml
harness = "t3"

# Alternatively, use a Linear route label named `Chord -> t3`:
# [harnesses.t3]
# command = ["t3"]
```

The optional `[t3]` table shown above selects a provider/model pair or a T3 git
worktree. `provider` and `model` must be configured together; `branch` requires
`worktree`. Without those options, T3 Code uses the project's default model,
permission mode, and project directory. Chord does not elevate permissions,
merge work, or create commits.

Chord's part ends at a successful send. Once `t3 thread start` accepts the
task — which is what counts as delivered for T3, shown in `chord info` as
"T3 Code has it now" — the thread id is logged and Chord lets go: it never polls the thread,
follows up on it, or reports how the run went. Reading the work — or asking it
anything — happens in T3 Code itself.

The one thing Chord still holds is its own send: a pending-dispatch marker is
written before `t3 thread start`, so a dispatch interrupted before it was
confirmed is not automatically repeated. Check `t3 thread list` and resolve
the issue manually before retrying. To retry, remove that issue's id from both
`t3_pending` and `handed_over` in the project's `~/.chord/sessions/.../state.json`
only after confirming that no T3 thread was created.

T3 support depends on the installed CLI's `t3 thread` commands and JSON output.
Chord does not adopt arbitrary threads from `t3 thread list`; that command is
only useful for manual recovery if a launch was interrupted before it was
confirmed.

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
which caused the run. An issue narrowed from `Chord` to `Chord -> claude` is
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

| Label                               | Harness                           |
| ----------------------------------- | --------------------------------- |
| `Chord`                             | whatever `harness` says           |
| `Chord -> codex`                    | the curated `codex`               |
| `Chord -> opencode`                 | the curated `opencode`            |
| `Chord -> opencode -> big-pickle`   | the curated `opencode/big-pickle` |
| `Chord -> opencode -> Qwen3.4:8B`   | the curated `opencode/Qwen3.4:8B` |

Route labels read as a parent -> child relationship: `Chord -> opencode` under
`Chord`, and `Chord -> opencode -> big-pickle` under that. Everything after the
first arrow is the harness name, with each further arrow standing for a `/` in
the name. **Linear allows neither `/` nor `:` in label names**, which is why
route labels use an arrow. Chord prints the arrow as ` -> `, and reads it with
any spacing, so `Chord->opencode->big-pickle` routes to the same harness; the
parts between arrows are trimmed. Everything else in a name is its own, colons,
dots and uppercase included, so a model name like `Qwen3.4:8B` goes in as it is.
Nothing in Chord knows what a harness is or how many parts its name has, so
`opencode/big-pickle` is a name you chose, not a hierarchy Chord has to
interpret. Matching is exact and case sensitive, and only curated names route.

In chord.toml, keys always use `/` — `[harnesses."opencode/big-pickle"]` — and
the arrows go only in the Linear label. A harness name can't contain `->`, and
no part of it can be empty or start or end with a space, so every name has
exactly one label; config validation says so when one does. `chord harness add`
takes the name with `/` as well, and prints the label to create.

**Older `Chord/<name>` labels still work.** Route labels used `/` before Linear
stopped allowing it, and Chord still reads that spelling: `Chord/t3` routes
exactly where `Chord -> t3` does, so labels already in your workspace keep
working. Chord only ever prints the arrow spelling.

**An issue can carry more than one.** Label it `Chord`, then narrow it to
`Chord -> opencode -> big-pickle` when you know where you want it to go, and
both labels sit on the issue. Chord routes on **the one added most recently**,
which it reads from Linear's own audit trail — not from a guess about which
label is more specific. Narrowing an issue therefore changes where it goes,
and handing it back to the default is just adding `Chord` again.

If Linear can't be asked, Chord falls back to the most specific label on the
issue and says so in the log.

**A route with no harness behind it stops the issue.** `Chord -> typo-here` isn't
a typo Chord can shrug off: running the work on the default harness instead
would hand it to an agent nobody asked for, on arguments nobody chose. So the
issue is skipped, the log names the label and how to fix it, and the issues
behind it still get their turn.

`chord info` is where you check what a label you typed in Linear will actually
do. It prints every label Chord runs work for, one per line, next to the command
it starts — because **Chord never creates a label**. The set of labels in your
workspace and the set Chord answers to are two things you keep in step by hand,
so `chord info` is the only place the second can be read before it matters:

```
  labels     Chord                            opencode  (opencode run)
             Chord -> claude                  claude
             Add these in Linear by hand — Chord never creates a
             label. Adding one is what makes this machine run a
             harness, so this list is the whole trigger surface.
             Anything else under that prefix is reported and
             skipped, never run on the default harness.
```

Treat that list as a trigger surface rather than a naming convention. Adding one
of those labels to an issue is what makes your machine run a command, which is
the same thing `allowed_actors` restricts and the same thing a stranger with
access to your workspace could do on purpose.

`chord start` builds every route and looks each command up on your `PATH`
before it spawns anything, so a harness that isn't installed — the default, a
preset, or a curated command — is reported by the command, every broken route at
once, rather than by the first issue that trips it. `chord info` marks the same
routes with `! can't run:` and the reason, so you can check before starting.

## Where the work happens

A harness is launched **in a repository**, not in whatever directory the daemon
happened to be started from. Chord decides which one, in this order:

1. **The Linear project name.** Matched against the directory Chord is rooted
   at, and against a directory of that name directly inside it, in any case
   (project `Chord` finds `chord`). Both, and nothing else — a project called
   `home` will not match `$HOME`.
2. **A `[Chord_Working_Path:]` line in the issue description.** Write it on its own line,
   with the path in single or double quotes; relative paths are read from the
   directory Chord was started in. The name is matched in any case, so
   `[chord_working_path: "..."]` works just the same.

   ```
   [Chord_Working_Path: "~/Projects/billing"]
   ```
3. **The project root**, which is where you started Chord.

So start Chord in the repository, or in the folder that holds your checkouts
(`~/Projects`, with Linear projects named after the folders in it). `chord watch`
says at startup where issues with no repository of their own will run, and warns
straight away if that directory isn't a Git repository.

The third one is the common case, not a failure, and every hand-over says which
of the three it used:

```
  repository /Users/you/Projects/billing (project title)
  repository /Users/you/Projects/chord (project root (fallback))
```

Falling back is what lets Chord work on issues that have no Linear project,
which is most of them. But the fallback is only used when *nothing* named a
repository. If a project name or a `[Chord_Working_Path:]` line points somewhere that
isn't a Git repository, the issue is **skipped, not relocated** — somebody wrote
that location down, and working somewhere else instead would be worse than not
working at all. A skipped issue is not recorded as handed over, so it is picked
up again as soon as the directory is there.

Because that only searches two levels, a repository somewhere else on the
machine needs the `[Chord_Working_Path:]` line. That is the whole configuration surface;
there is no `chord.toml` setting for it.

## Notifications

**At most one banner per issue: the ones that didn't go to plan always, and a
delivery unless you turn it off.** Four outcomes, four verbs:

| What happened                 | Title              | Body                                          |
| ----------------------------- | ------------------ | --------------------------------------------- |
| The harness has the issue (`[notify] received`, on by default) | `ENG-201 received` | `Delivered to opencode.` |
| The harness didn't finish, or never got it | `ENG-201 failed`   | `` `opencode` exited 1. ``       |
| Stopped before it started     | `ENG-201 skipped`  | `'Chord -> typo' names a harness chord.toml doesn't define.` |
| Asked by someone not allowed  | `ENG-201 refused`  | `sam added 'Chord', and that is not in allowed_actors.` |

Each is a title saying what happened over a line saying why, because a banner is
read at a glance by somebody who did not ask for it. The two are separate
functions rather than one you fill in with two strings, because the failure is
the case where the halves disagree — and they used to: a hand-over that raised
sent "Chord done" over "ENG-201 did not finish", which is the one banner nobody
can act on.

`refused` has its own verb on purpose. It isn't a malfunction; the allowlist did
the one thing it's there for, and you want to know it fired.

**`received` fires on confirmed delivery, and is the one banner you can switch
off.** It goes out the moment the harness has the issue (see below for what
that means), not as the hand-over starts and not when the run ends, so it says
the issue got there rather than that Chord tried. A harness that then fails
replaces it with `failed`. For T3 Code, delivery is T3 accepting the task. There
is no "finished" banner: a harness can run for half an hour, and the news is
that work started somewhere you didn't start it.

```toml
[notify]
received = false   # default true
```

turns it off for a quiet screen; `failed`, `skipped` and `refused` have no
switch, because an issue that is not being worked on is what banners are for.
`chord info` says which way it is set (`received   banner when an issue reaches
its harness ([notify] received = true)`).

**Delivery is recorded quietly either way.** Once the harness has the issue, the
watcher logs one line — `delivered to claude.` — and records it in
`state.json`, and `chord info` shows it, even while the harness is still
working:

```
  delivery   ENG-201 delivered to claude at 23:59, still running
             ENG-200 delivered to opencode at 23:41, finished 23:52
             ENG-199 NOT delivered to codex (23:30)
```

"Delivered" means the harness started, took the whole issue on stdin, stdin was
closed cleanly, and it was still running (or had exited 0) a second later. For
Cursor, which takes the issue as an argument, it means it started and stayed
up. It is kept apart from how the run ended — `finished`, `failed`, or `stopped`
by `chord stop` — so you can tell the message arrived long before the harness is
done. A harness that never got the issue (command missing, dead on startup, or
hung up on its input) is the loud case: the `failed` banner, `it never reached
<harness>.` in the log, and `NOT delivered` in `chord info`. `chord info` lists
the newest five.

Everything announced is a case where an issue was going to be worked on and
wasn't, or where work was going to happen and something stopped it. Those used
to be log-only, and an issue that quietly stops looks exactly like an issue
being worked on — right up until somebody opens the ticket and finds nothing has
happened for a week. The log still has all of it; the banner is what saves you
from reaching for the log first.

All banners for one issue share a group, so a newer one replaces an older one
rather than sitting under it.

**Where they go depends on the platform, and Chord asks rather than assumes.**
macOS attributes every banner to a bundle identity, and the only Apple API that
takes a title and a body reads that identity off whatever is running — so it
works inside a signed `.app` and nowhere else. Chord is `python3` in a
virtualenv: a bare, unsigned executable in no bundle at all, so it asks the
notification daemon by a route that needs none:

- **`terminal-notifier`** if you have it (`brew install terminal-notifier`). It is
  itself a signed app bundle, so the banner is attributed to it rather than to
  Script Editor.
- **`osascript`** otherwise. It is in every macOS and installs nothing. The banner
  is attributed to Script Editor, which is cosmetic — the alternative is no
  banner.

Linux and Windows go through `desktop_notifier`, which works from a script
there: on Linux it is a D-Bus call to the session's own daemon.

`chord info` reports which of these is in play, because the failure is invisible
by construction — a notification that reaches no daemon raises nothing, so the
only way to know one is going somewhere is to ask before sending it:

```
  notify     osascript (Notification Centre, attributed to
             Script Editor — `brew install terminal-notifier`
             gives the banners their own name instead)
```

Notifications are best-effort and always will be. Nothing about a desktop
notification is worth an issue not being worked on, so every failure ends in
silence — including a notifier that hangs, which is killed rather than waited on.

## Webhooks

Polling is how Chord works. A webhook makes it faster; it never makes it
correct on its own. Everything below follows from that.

### What the webhook actually is

A wake-up signal. Chord does not read the payload, does not act on it, and
does not trust it — it uses the arrival to re-read Linear through the normal
path. So a duplicated delivery, a truncated body, or a payload that is not JSON
at all costs one extra read of Linear and nothing else.

The label filter, the de-duplication, the allowlist and the routing all happen
in the poll, in one place. A webhook cannot bypass any of them, because none of
them are on the path a webhook takes.

### Setting one up

In Linear: **Settings → Integrations → Webhooks → Add webhook**.

- **URL** — your tunnel's public URL, plus `/webhook`
- **Events** — Issue created, Issue updated
- **Teams** — the teams whose issues you want to wake for

Chord listens on `127.0.0.1:23842` by default, which nothing outside your
machine can reach. You need a tunnel: ngrok, Cloudflare Tunnel, Tailscale,
whatever you already use. `chord info` prints the exact URL to put in Linear.

`webhook_port = 0` asks the operating system for a free port, which is what to
use when something else already has 23842. `chord info` shows what you got.

### What it replies with

Chord answers every request, because a sender that gets silence does not know
whether to retry.

| Request                                     | Reply |
| ------------------------------------------- | ----- |
| `POST` to the configured path               | `200 OK` |
| Any method other than `POST`                 | `404 Not Found` |
| A path other than the configured one         | `404 Not Found` |
| Malformed request line or headers            | `400 Bad Request` |
| A body that stops short of its `Content-Length` | `400 Bad Request`, after a timeout |
| Wrong or missing `X-Chord-Secret`            | `401 Unauthorized` |
| Headers past 32 KiB                          | `400 Bad Request` |

Only the first row wakes the watcher. The rest are answered so that whoever sent
them learns something and stops.

### Requests are bounded

Every read has a 5-second ceiling and headers over 32 KiB are refused. Both
exist because a listener on a public port is reachable by things that are not
Linear, and one that announced a body it never sent would otherwise hold a
connection open indefinitely — which used to be enough to stop `chord stop`
finishing at all.

### A burst is one poll

If twenty deliveries arrive while Chord is busy, the next poll happens once, not
twenty times. Linear's own activity while Chord is mid-harness is also absorbed
into that one poll. So a noisy workspace does not turn into a rate-limit
problem.

### Authentication

By default there is none, and the default is deliberate: every existing install
is in that state, and failing closed would stop every working webhook on
upgrade. With no secret, the worst an unauthorised caller can do is make Chord
re-read Linear — spending rate limit on a machine that already holds a valid
token, repeatedly, for free.

If your tunnel is public, set one:

```toml
webhook_secret = "a-long-random-string"
```

Every request must then carry it, and the check happens before the body is read
so an unauthorised caller cannot hold a connection open with bytes it may not
send:

```
X-Chord-Secret: a-long-random-string
```

The comparison is constant-time. A secret with leading or trailing whitespace
is rejected at load, because HTTP strips it and no sender could transmit it —
which would otherwise be a setup that looks right and 401s forever.

Linear cannot send a custom header, so a secret means you terminate the
connection somewhere that can: most tunnels can add a header, and if yours
cannot, the secret is not usable with Linear directly. Check before you rely
on it. `chord info` says whether a secret is set.

### Polling still runs

`interval` is unaffected by any of this. It is how long Chord waits before
checking again regardless, so a missed or misconfigured delivery costs at most
one interval. Raise it when webhooks are working; it does not change how
quickly you see something that arrived.

### When it is not working

`chord info` prints the URL Chord is listening on and whether a secret is set.

Chord does not log each webhook arrival — a poll that finds nothing to do is
silent, which is the same whether it was triggered by a webhook or by the
interval. So there is nothing to watch for in `chord watch` to confirm the
webhook path is alive. Check it directly instead:

```bash
curl -i -X POST http://127.0.0.1:23842/webhook
```

A `200 OK` means the listener is up and the path matches. With a secret set,
add `-H "X-Chord-Secret: …"` and expect `401` without it. If the connection is
refused or times out, the listener is not running — `chord start` prints
`Webhook listening at …` when it is.

If `curl` gets a `200` but Linear's deliveries never arrive, the problem is
upstream of Chord: the tunnel, or Linear's own delivery log for that webhook.
If deliveries arrive but issues are picked up only on the interval, the wake-up
is not reaching the watcher — check `webhook_secret` is not set on one side
only, and that the URL in Linear has the `/webhook` path on the end.

## Harnesses

`harness` is either a name Chord knows or a command to run. Under
`[harnesses.<name>]`, `command` means the same thing, and the entry's name is
what a `Chord -> <name>` label routes to.

**`opencode`** is the default built-in harness. It runs `opencode run`,
OpenCode's non-interactive form, with the issue on stdin: with no message
argument and stdin not a terminal, `opencode run` reads the prompt from stdin
([docs](https://opencode.ai/docs/cli/#run)). Plain `opencode` would start the
terminal UI and wait for a person. No flag is needed to keep it from
prompting: in `run` mode a permission that would ask is refused rather than
waited on, and OpenCode's defaults already allow edits and shell commands in
the directory it starts in. Chord doesn't add `--auto` (approve everything not
denied); curate `["opencode", "run", "--auto"]` yourself if your OpenCode config
asks for things you want approved. **`print`** is a built-in
debugging harness: it writes the issue to Chord's log so you can see exactly
what would be handed over before trusting it with a real agent.

### Presets

Three coding agents are known by name, so you don't have to look up their
non-interactive flags. Each is an ordinary command harness with the flags
filled in — checked against each tool's documentation — and the least
permission that still lets the agent edit the checkout it's started in:

| Name     | Runs                                                   | The issue goes      |
| -------- | ------------------------------------------------------ | ------------------- |
| `claude` | `claude -p --permission-mode acceptEdits`              | on stdin            |
| `codex`  | `codex exec --sandbox workspace-write -`               | on stdin            |
| `cursor` | `cursor-agent -p --force --trust` (or `agent`)         | as the last argument |

```toml
harness = "claude"

[harnesses.review]
command = ["codex"]     # a one-word command naming a preset is the preset
```

`chord setup` offers whichever of `opencode`, `claude`, `codex` and `cursor` it
finds on your `PATH`, and writes the one you pick by name.

What those permissions mean, so nothing surprises you:

- **Claude Code** in `-p` mode refuses file edits unless something approves
  them; `acceptEdits` allows edits and simple filesystem commands, and still
  refuses other shell commands. Want it to run tests too? Curate the full
  command yourself, e.g. `["claude", "-p", "--permission-mode", "acceptEdits",
  "--allowedTools", "Bash(uv run pytest *)"]`.
- **Codex** `exec` is read-only by default; `workspace-write` lets it write in
  the repository. Codex also insists on running inside a Git repository, which
  is what Chord's [repository resolution](#where-the-work-happens) gives it.
- **Cursor** in print mode only *proposes* changes without `--force`, and
  `--force` also allows shell commands it isn't told to deny. `--trust` skips a
  workspace-trust prompt nobody is there to answer. Cursor's print mode doesn't
  read a prompt from stdin, so this one preset takes the issue as its last
  argument — and an issue over 100,000 bytes is refused before anything runs,
  because a single argument can't be much longer than that on Linux.

Want different flags — a model, a budget, a stricter mode? Write the command
out; only a one-word `["claude"]` means the preset, and anything longer is run
exactly as written.

### Any other command

Anything else is run as a command, with the issue **on standard input**:

```toml
harness = ["claude", "-p", "--model", "opus"]

[harnesses.review]
command = ["./scripts/on-issue.sh"]

[harnesses.my-agent]
command = ["my-agent", "--prompt", "-"]
```

A bare name works too, if it's on your `PATH`:

```toml
harness = "my-agent"

[harnesses.review]
command = "my-agent"
```

That is the whole compatibility layer. Chord doesn't need to know what a
harness is, so pointing it at a tool Chord has never heard of is a config
change, not a release.

A curated name and the default are separate things. `harness` is what a bare
`Chord` runs; `Chord -> foo` always means the entry named `foo`, even when the two
happen to spell the same command.

Two things follow from the issue arriving on stdin:

- It works regardless of how long the issue is. A description plus a
  discussion can run past the operating system's limit on command-line
  arguments, and stdin has no such limit.
- Your harness has to read stdin. Most agent CLIs take a prompt this way; if
  yours wants a file, wrap it: `["sh", "-c", "cat > /tmp/issue.md; my-agent --file /tmp/issue.md"]`

### Checking a harness before it matters

`chord start` refuses to start while any route's command is missing from your
`PATH`, listing every one, and `chord info` marks them (`! can't run: ...`). A
command given by path, like `./scripts/on-issue.sh`, isn't checked: it runs in
the issue's repository, which isn't known until an issue arrives.

### When a harness fails

Whatever your harness prints goes into Chord's log, and a non-zero exit is
reported as a failure. The failure — in the log and in the "failed"
notification — carries the last line the harness printed, which is usually the
reason (`` `claude` exited 1: Invalid API key. Its full output is in the log. ``).

A failed hand-over is recorded, like every other issue Chord won't do, so it
isn't retried on every poll and the queue behind it keeps moving. The log says
how to retry it: fix the cause, then drop the issue's id from `state.json`
(see [State on disk](#state-on-disk)). That covers a missing command, a non-zero
exit, and a timeout alike.

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

A preset is the next step down: a tool whose non-interactive flags are worth
getting right once for everybody. To add one, give it an entry in `PRESETS` in
`src/chord/harness.py` — the executable names to look for, its flags, where to
install it, and whether it needs the issue as an argument — and a test in
`test/test_presets.py` pinning the flags. `chord setup` offers it from then on.

To add a real built-in anyway, in `src/chord/harness.py`:

1. Write a class with a `name` (string) and an
   `async def send(self, prompt: str, cwd: Path | None = None)`.
2. Add a name check to `build()`, and to `missing()` if it runs something.

A built-in is reachable from `[harnesses.<name>]` like any other command, since
`build()` is what resolves both.

---

## How it works

Once a minute, Chord asks Linear for issues carrying a route label — the bare
`Chord` label or any `Chord -> ...` one, in a single request however many harnesses
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
limit, or a failed token renewal is written to the log once and retried later;
the watcher doesn't die. If Linear stops accepting the refresh token itself,
run `chord setup` and a running watcher picks the new token up on its next poll.

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
| `state.json`    | Which issues have been handed over, and whether each reached its harness |
| `chord.lock`    | Held while `chord start` is running |

A watcher run by `chord service` writes the same pid and state files, but its
log goes to the journal (Linux) or `~/Library/Logs/chord/` (macOS) instead of
`chord.log`; `chord watch` follows whichever is in use.

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

Linear's tokens last 24 hours. A running Chord renews its token an hour before
it runs out, and again if Linear refuses it, so there is nothing to run by hand.
`chord start` renews a stale one too.

Linear rotates the refresh token on every exchange, so the old one dies the
moment a renewal succeeds. Two things follow. The new pair goes into the
keychain before anything uses it. And only one Chord on the machine renews at a
time (a lock in `~/.chord/renew.lock`); one that was waiting re-reads the
keychain and uses the pair the other just stored, rather than spending a
refresh token Linear has already retired. A renewal that fails is logged,
tried again in five minutes, and announced with one desktop notification
(not one per retry) saying to run `chord refresh` or `chord setup`.

---

## Direction

Chord starts with one simple goal: connect project updates to the tools that
act on them. Trigger modes come one at a time — label first, assignment next,
agent sessions later — and they stay mutually exclusive.

Start small. Build outward.

## License

MIT — see [LICENSE](LICENSE).
