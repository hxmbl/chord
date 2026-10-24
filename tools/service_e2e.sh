#!/usr/bin/env bash
# End-to-end check of `chord service` against the real service manager:
# systemd --user on Linux, launchd (gui/<uid>) on macOS. Run by
# .github/workflows/service-e2e.yml; runnable by hand on a machine where
# installing a throwaway user service is fine.
#
#   CHORD=/path/to/venv/bin/chord tools/service_e2e.sh
#
# Never touches a real keychain or a real Linear account. The keyring is
# keyrings.alt's plaintext file (PYTHON_KEYRING_BACKEND, which the service
# inherits), seeded with a fake token when a step needs "signed in" — the same
# trick ci.yml's test job uses — and emptied for the signed-out checks. The
# harness is the built-in `print`, so nothing is ever run on an issue.
set -euo pipefail

CHORD="${CHORD:-chord}"
PYTHON="$(dirname "$(command -v "$CHORD")")/python"
OS="$(uname -s)"
export PYTHON_KEYRING_BACKEND="${PYTHON_KEYRING_BACKEND:-keyrings.alt.file.PlaintextKeyring}"
export _TYPER_FORCE_DISABLE_TERMINAL=1

PROJECT="${CHORD_E2E_PROJECT:-$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/chord-e2e.XXXXXX")}"
mkdir -p "$PROJECT/.config/chord"
# `print` needs nothing installed; interval 5 (the floor) is also how often a
# signed-out service looks for a token again, which keeps the waits short.
printf 'harness = "print"\ninterval = 5\n' > "$PROJECT/.config/chord/chord.toml"
cd "$PROJECT"

# Every check captures a command's output first and searches the copy. Piping
# straight into `grep -q` or an awk that exits early closes the pipe while chord
# is still writing, chord dies of SIGPIPE, and under pipefail that kills the
# script with no message — exactly how the first CI run of this went.
has() { grep -q -- "$1" <<<"$2"; }

step() { printf '\n::group::%s\n' "$*"; }
end() { echo "::endgroup::"; }
fail() { echo "::error::$*"; exit 1; }

seed() {
  "$PYTHON" - <<'PY'
import json, time, keyring
keyring.set_password("chord", "linear", json.dumps({
    "access_token": "e2e-fake-token",
    "refresh_token": "e2e-fake-token",
    "obtained_at": time.time(),
}))
PY
}

unseed() {
  "$PYTHON" - <<'PY'
import keyring, keyring.errors
try:
    keyring.delete_password("chord", "linear")
except keyring.errors.PasswordDeleteError:
    pass
PY
}

# Fields of `chord service status`, e.g. field name -> chord-x-1234.service
status_text() { "$CHORD" service status; }
field() {
  local out
  out="$(status_text)"
  awk -v k="$1" '$1 == k && !done { $1 = ""; sub(/^ +/, ""); print; done = 1 }' <<<"$out"
}
service_pid() {
  local out
  out="$(status_text)"
  sed -n 's/^  running    yes (pid \([0-9]*\)).*/\1/p' <<<"$out"
}
is_stopped() { has '^  running    no' "$(status_text)"; }

wait_running() {
  local deadline=$((SECONDS + ${1:-20})) pid=""
  while ((SECONDS < deadline)); do
    pid="$(service_pid)"
    [[ -n "$pid" ]] && { echo "$pid"; return 0; }
    sleep 1
  done
  "$CHORD" service status >&2 || true
  return 1
}

wait_stopped() {
  local deadline=$((SECONDS + ${1:-20}))
  while ((SECONDS < deadline)); do
    is_stopped && return 0
    sleep 1
  done
  "$CHORD" service status >&2 || true
  return 1
}

# How many times the manager has (re)started it. systemd: NRestarts counts the
# automatic restarts only. launchd: `runs` counts every spawn, manual or not.
restarts() {
  local out
  if [[ "$OS" == Linux ]]; then
    systemctl --user show -p NRestarts --value "$UNIT"
  else
    out="$(launchctl print "gui/$(id -u)/$UNIT")"
    awk '$1 == "runs" && !done { print $3; done = 1 }' <<<"$out"
  fi
}

logs() { "$CHORD" service logs --no-follow -n "${1:-200}"; }

# Watchers for this project, counted from ps: one `-m chord.cli _serve` per
# project is the whole point of routing `chord start` through the service.
watchers() {
  local out
  out="$(ps -axo command=)"
  grep -c '[c]hord\.cli _serve' <<<"$out" || true
}

wait_for_log() {
  local pattern="$1" deadline=$((SECONDS + ${2:-30}))
  while ((SECONDS < deadline)); do
    has "$pattern" "$(logs)" && return 0
    sleep 1
  done
  return 1
}

dump() {
  local code=$?
  ((code == 0)) && return
  echo "::group::diagnostics (exit $code)"
  "$CHORD" service status || true
  "$CHORD" info || true
  echo "--- watchers"; ps -axo pid=,command= | grep '[c]hord\.cli' || true
  if [[ "$OS" == Linux ]]; then
    for f in "${XDG_CONFIG_HOME:-$HOME/.config}"/systemd/user/chord-*.service; do
      [[ -e "$f" ]] && { echo "--- $f"; cat "$f"; }
    done
    systemctl --user status 'chord-*' --no-pager -l || true
    journalctl --user -u 'chord-*' --no-pager -n 300 || true
  else
    for f in "$HOME"/Library/LaunchAgents/io.hxmbl.chord.*.plist; do
      [[ -e "$f" ]] && { echo "--- $f"; cat "$f"; }
    done
    for f in "$HOME"/Library/Logs/chord/*.log; do
      [[ -e "$f" ]] && { echo "--- $f"; tail -n 300 "$f"; }
    done
    [[ -n "${UNIT:-}" ]] && launchctl print "gui/$(id -u)/$UNIT" || true
  fi
  echo "::endgroup::"
}
trap dump EXIT

step "before install"
unseed
OUT="$(status_text)"; echo "$OUT"
has "isn't installed" "$OUT" || fail "status should say not installed"
end

step "install (signed in with a fake token)"
seed
"$CHORD" service install
UNIT="$(field name)"
FILE="$(field file)"
echo "unit=$UNIT file=$FILE"
[[ -n "$UNIT" && -f "$FILE" ]] || fail "install didn't write the unit/plist"
cat "$FILE"
end

step "running within 20s"
PID="$(wait_running 20)" || fail "not running 20s after install"
echo "pid $PID"
wait_for_log "Watching Linear" 20 || fail "no 'Watching Linear' line in the service log"
logs 50
end

step "signed out: stays up, idle, says so once, no crash loop"
unseed
"$CHORD" service restart
PID="$(wait_running 20)" || fail "not running after restart"
wait_for_log "Not signed in to Linear" 20 || fail "no 'not signed in' line in the log"
BEFORE="$(restarts)"
sleep 15
AFTER="$(restarts)"
NOW="$(service_pid)"
echo "pid $PID -> ${NOW:-none}; restarts $BEFORE -> $AFTER"
[[ "$NOW" == "$PID" ]] || fail "signed-out service didn't stay up (pid $PID -> ${NOW:-none})"
[[ "$AFTER" == "$BEFORE" ]] || fail "signed-out service restarted ($BEFORE -> $AFTER)"
LOG="$(logs 200)"
SAID="$(grep -c 'Not signed in to Linear' <<<"$LOG" || true)"
echo "'Not signed in' lines in the log: $SAID"
[[ "$SAID" == 1 ]] || fail "expected the signed-out reason once, got $SAID lines"
logs 50
end

step "signing in while it waits: picked up with no restart"
seed
wait_for_log "Signed in to Linear. Starting to watch." 20 || fail "the waiting service never noticed the token"
[[ "$(service_pid)" == "$PID" ]] || fail "it restarted instead of picking the token up"
logs 20
end

step "stop"
"$CHORD" service stop
wait_stopped 40 || fail "still running after stop"
sleep 12  # past RESTART_DELAY: a stop must not be taken for a crash and restarted
is_stopped || fail "came back by itself after stop"
[[ "$(watchers)" == 0 ]] || fail "a watcher process outlived the stop"
end

step "start"
"$CHORD" service start
PID="$(wait_running 20)" || fail "not running after start"
echo "pid $PID"
end

step "restart"
"$CHORD" service restart
NEW="$(wait_running 20)" || fail "not running after restart"
echo "pid $PID -> $NEW"
[[ "$NEW" != "$PID" ]] || fail "restart kept the same pid"
PID="$NEW"
end

step "chord info"
OUT="$("$CHORD" info)"; echo "$OUT"
has "^  service    installed, .*$UNIT" "$OUT" || fail "chord info has no installed-service line"
has "^  daemon     running (pid $PID" "$OUT" || fail "chord info's daemon line doesn't name the service's pid"
end

step "chord start goes through the service"
"$CHORD" service stop
wait_stopped 40 || fail "still running after stop"
OUT="$("$CHORD" start)"; echo "$OUT"
has "Started the Chord service" "$OUT" || fail "chord start didn't hand over to the service"
PID="$(wait_running 20)" || fail "service not running after chord start"
"$CHORD" start
sleep 3
COUNT="$(watchers)"
echo "watchers: $COUNT (service pid $PID)"
OUT="$(ps -axo pid=,command=)"; grep '[c]hord\.cli _serve' <<<"$OUT" || true
[[ "$COUNT" == 1 ]] || fail "expected exactly one watcher, found $COUNT"
has "^  daemon     running (pid $PID" "$("$CHORD" info)" || fail "the pid file isn't the service's"
end

step "uninstall"
"$CHORD" service uninstall
[[ ! -e "$FILE" ]] || fail "uninstall left $FILE"
has "isn't installed" "$(status_text)" || fail "status still says installed"
sleep 2
[[ "$(watchers)" == 0 ]] || fail "a watcher outlived uninstall"
has "^  service    not installed" "$("$CHORD" info)" || fail "chord info still lists the service"
end

unseed
echo "service e2e OK on $OS"
