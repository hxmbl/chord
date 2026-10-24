"""`chord service`: the unit and the plist as text, and the manager calls.

Nothing here reaches a real systemctl or launchctl: `service._run` is a
recorder, and conftest's autouse fixture fails any test that forgets that.
"""

import asyncio
import os
import plistlib
import signal
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chord import daemon, service
from chord.cli import app

runner = CliRunner()

# Taken before conftest's autouse fixture stubs it out for every test.
REAL_CURRENT = service.current

PYTHON = "/home/me/my venv/bin/python"


def systemd(tmp_path: Path, root: Path | None = None, **env: str) -> service.Service:
    return service.describe(
        service.SYSTEMD,
        "app-1a2b3c",
        root or tmp_path / "project",
        home=tmp_path / "home",
        command=service.argv(PYTHON),
        env=env or {"PATH": "/usr/bin"},
    )


def launchd(tmp_path: Path, root: Path | None = None, **env: str) -> service.Service:
    return service.describe(
        service.LAUNCHD,
        "app-1a2b3c",
        root or tmp_path / "project",
        home=tmp_path / "home",
        command=service.argv("/opt/chord/bin/python"),
        env=env or {"PATH": "/usr/bin"},
        uid=501,
    )


class Calls:
    """Stands in for subprocess.run, answering by the command's first words."""

    def __init__(self, answers: dict[tuple[str, ...], tuple[int, str]] | None = None):
        self.made: list[list[str]] = []
        self.answers = answers or {}

    def __call__(self, args, **kwargs):
        self.made.append(list(args))
        for prefix, (code, out) in self.answers.items():
            if tuple(args[: len(prefix)]) == prefix:
                return subprocess.CompletedProcess(args, code, out, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def ran(self, *prefix: str) -> bool:
        return any(tuple(c[: len(prefix)]) == prefix for c in self.made)


@pytest.fixture
def calls(monkeypatch):
    recorder = Calls()
    monkeypatch.setattr(service, "_run", recorder)
    return recorder


# --- which manager ---


@pytest.mark.parametrize(
    ("system", "tools", "expected"),
    [
        ("Linux", {"systemctl"}, service.SYSTEMD),
        ("Linux", set(), None),
        ("Darwin", {"launchctl"}, service.LAUNCHD),
        ("Windows", {"systemctl", "launchctl"}, None),
        ("FreeBSD", {"systemctl"}, None),
    ],
)
def test_the_manager_follows_the_platform(system, tools, expected):
    which = lambda name: f"/bin/{name}" if name in tools else None
    assert service.kind(system, which) == expected


def test_units_are_named_per_project(tmp_path):
    """Two checkouts get two services, named from the same slug as their state."""
    unit = systemd(tmp_path)
    assert unit.name == "chord-app-1a2b3c.service"
    assert unit.path == tmp_path / "home/.config/systemd/user/chord-app-1a2b3c.service"
    agent = launchd(tmp_path)
    assert agent.name == "io.hxmbl.chord.app-1a2b3c"
    assert agent.path == (
        tmp_path / "home/Library/LaunchAgents/io.hxmbl.chord.app-1a2b3c.plist"
    )
    assert agent.log == tmp_path / "home/Library/Logs/chord/app-1a2b3c.log"
    assert agent.target == "gui/501/io.hxmbl.chord.app-1a2b3c"


def test_xdg_config_home_is_respected(tmp_path):
    unit = service.describe(
        service.SYSTEMD,
        "p",
        tmp_path,
        home=tmp_path / "home",
        command=("x",),
        env={},
        config_home=tmp_path / "xdg",
    )
    assert unit.path == tmp_path / "xdg/systemd/user/chord-p.service"


def test_the_service_runs_the_watcher_chord_start_runs():
    """Same entry point, and still recognisably ours to `chord stop`."""
    command = service.argv("/venv/bin/python")
    assert command == ("/venv/bin/python", "-m", "chord.cli", "_serve", "--managed")
    assert daemon._is_watcher_argv(" ".join(command))


def test_the_interpreter_symlink_is_not_resolved(tmp_path):
    """A venv's python is a symlink; resolving it would lose the venv."""
    real = tmp_path / "python3.12"
    real.touch()
    link = tmp_path / "venv-python"
    link.symlink_to(real)
    assert service.argv(str(link))[0] == str(link)


def test_the_current_project_is_described(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "current", REAL_CURRENT)
    monkeypatch.setattr(service, "kind", lambda: service.SYSTEMD)
    monkeypatch.setenv("PATH", "/custom/bin:/usr/bin")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    svc = service.current()
    assert svc is not None
    assert svc.name == f"chord-{daemon.PROJECT}.service"
    assert svc.env["PATH"] == "/custom/bin:/usr/bin"
    assert svc.argv[-2:] == ("_serve", "--managed")


def test_no_manager_means_no_service(monkeypatch):
    monkeypatch.setattr(service, "current", REAL_CURRENT)
    monkeypatch.setattr(service, "kind", lambda: None)
    assert service.current() is None


# --- the environment ---


def test_path_and_home_are_captured_and_secrets_are_not():
    env = service.capture_environment(
        {
            "PATH": "/home/me/.local/bin:/usr/bin",
            "HOME": "/home/me",
            "ANTHROPIC_API_KEY": "sk-not-for-a-file",
            "OPENAI_API_KEY": "sk-nor-this",
            "LANG": "en_GB.UTF-8",
            "CHORD_LOG_LEVEL": "DEBUG",
        }
    )
    assert env == {
        "PATH": "/home/me/.local/bin:/usr/bin",
        "HOME": "/home/me",
        "LANG": "en_GB.UTF-8",
        "CHORD_LOG_LEVEL": "DEBUG",
    }


def test_unset_empty_and_multiline_values_are_left_out():
    env = service.capture_environment(
        {"PATH": "/usr/bin", "SHELL": "", "LANG": "a\nb", "USER": "x\ry"}
    )
    assert env == {"PATH": "/usr/bin"}


# --- the systemd unit ---


def test_the_unit_restarts_on_failure_and_starts_at_login(tmp_path):
    text = service.systemd_unit(systemd(tmp_path))
    assert "Restart=on-failure" in text
    assert "RestartSec=10" in text
    assert "KillMode=mixed" in text
    assert "WantedBy=default.target" in text
    assert "StartLimitBurst=5" in text
    assert f"WorkingDirectory={tmp_path / 'project'}\n" in text


def test_exec_start_quotes_a_path_with_spaces(tmp_path):
    text = service.systemd_unit(systemd(tmp_path))
    assert (
        'ExecStart="/home/me/my venv/bin/python" "-m" "chord.cli" "_serve" '
        '"--managed"\n'
    ) in text


def test_exec_start_escapes_quotes_dollars_percent_and_backslashes(tmp_path):
    svc = service.describe(
        service.SYSTEMD,
        "p",
        tmp_path,
        home=tmp_path,
        command=('/opt/a "b"/$HOME/100%/c\\d/python', "-m"),
        env={},
    )
    line = next(
        x for x in service.systemd_unit(svc).splitlines() if x.startswith("ExecStart=")
    )
    assert line == 'ExecStart="/opt/a \\"b\\"/$$HOME/100%%/c\\\\d/python" "-m"'


def test_specifiers_in_the_project_path_are_literal(tmp_path):
    text = service.systemd_unit(systemd(tmp_path, root=Path("/work/100% done")))
    assert "WorkingDirectory=/work/100%% done\n" in text
    assert "Description=Chord, watching Linear for /work/100%% done\n" in text


def test_the_environment_is_written_one_quoted_line_each(tmp_path):
    text = service.systemd_unit(
        systemd(tmp_path, PATH="/a b/bin:/usr/bin", HOME="/home/me", LANG='x"y%z')
    )
    assert 'Environment="PATH=/a b/bin:/usr/bin"\n' in text
    assert 'Environment="HOME=/home/me"\n' in text
    assert 'Environment="LANG=x\\"y%%z"\n' in text
    assert 'Environment="PYTHONUNBUFFERED=1"\n' in text


# --- the LaunchAgent ---


def test_the_plist_keeps_it_alive_and_logs_to_library_logs(tmp_path):
    svc = launchd(tmp_path, PATH="/opt/homebrew/bin:/usr/bin")
    body = plistlib.loads(service.launchd_plist(svc))
    assert body["Label"] == "io.hxmbl.chord.app-1a2b3c"
    assert body["ProgramArguments"] == [
        "/opt/chord/bin/python",
        "-m",
        "chord.cli",
        "_serve",
        "--managed",
    ]
    assert body["RunAtLoad"] is True
    assert body["KeepAlive"] == {"SuccessfulExit": False}
    assert body["WorkingDirectory"] == str(tmp_path / "project")
    assert body["EnvironmentVariables"]["PATH"] == "/opt/homebrew/bin:/usr/bin"
    assert body["StandardOutPath"] == str(svc.log)
    assert body["StandardErrorPath"] == str(svc.log)


def test_the_plist_escapes_what_xml_would_choke_on(tmp_path):
    svc = launchd(tmp_path, root=Path("/Users/me/R&D <new> project"), PATH="/a&b")
    raw = service.launchd_plist(svc)
    assert b"R&amp;D &lt;new&gt; project" in raw
    body = plistlib.loads(raw)
    assert body["WorkingDirectory"] == "/Users/me/R&D <new> project"
    assert body["EnvironmentVariables"]["PATH"] == "/a&b"


# --- what the manager says ---


def test_systemd_show_running():
    state = service.parse_systemd_show(
        "LoadState=loaded\nActiveState=active\nSubState=running\nMainPID=4321\n"
    )
    assert state == service.Status(True, True, 4321, "active (running)")


def test_systemd_show_failed():
    state = service.parse_systemd_show(
        "LoadState=loaded\nActiveState=failed\nSubState=failed\nMainPID=0\n"
    )
    assert not state.running
    assert state.pid is None
    assert state.state == "failed"


def test_launchctl_print_running():
    state = service.parse_launchctl_print(
        "gui/501/io.hxmbl.chord.x = {\n\tstate = running\n\tpid = 812\n"
        "\tsubsystem = {\n\t\tstate = other\n\t}\n}\n"
    )
    assert state == service.Status(True, True, 812, "running")


def test_launchctl_print_waiting():
    state = service.parse_launchctl_print("x = {\n\tstate = not running\n}\n")
    assert not state.running
    assert state.state == "not running"


# --- talking to the manager ---


def test_install_writes_reloads_enables_and_starts(tmp_path, calls):
    svc = systemd(tmp_path)
    service.install(svc)
    assert svc.path.read_text() == service.systemd_unit(svc)
    assert calls.made == [
        ["systemctl", "--user", "show-environment"],
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", svc.name],
        ["systemctl", "--user", "restart", svc.name],
    ]


def test_install_without_a_user_session_says_to_use_chord_start(tmp_path, monkeypatch):
    recorder = Calls({("systemctl", "--user", "show-environment"): (1, "")})
    monkeypatch.setattr(service, "_run", recorder)
    svc = systemd(tmp_path)
    with pytest.raises(service.Unsupported, match="chord start"):
        service.install(svc)
    assert not svc.path.exists()


def test_a_failing_systemctl_is_reported_with_what_it_said(tmp_path, monkeypatch):
    def run(args, **kwargs):
        if "enable" in args:
            return subprocess.CompletedProcess(args, 1, "", "Unit is masked.\n")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service, "_run", run)
    with pytest.raises(service.ServiceError, match="Unit is masked"):
        service.install(systemd(tmp_path))


def test_uninstall_disables_and_removes(tmp_path, calls):
    svc = systemd(tmp_path)
    service.install(svc)
    calls.made.clear()
    assert service.uninstall(svc) is True
    assert not svc.path.exists()
    assert calls.made[0] == ["systemctl", "--user", "disable", "--now", svc.name]
    assert calls.ran("systemctl", "--user", "daemon-reload")


def test_uninstall_when_not_installed(tmp_path, calls):
    assert service.uninstall(systemd(tmp_path)) is False
    assert calls.made == []


def test_launchd_install_bootstraps_into_the_gui_domain(tmp_path, calls):
    svc = launchd(tmp_path)
    service.install(svc)
    assert plistlib.loads(svc.path.read_bytes())["Label"] == svc.name
    assert svc.log is not None and svc.log.parent.is_dir()
    assert calls.made == [
        ["launchctl", "bootout", svc.target],
        ["launchctl", "bootstrap", "gui/501", str(svc.path)],
    ]


def test_launchd_install_falls_back_to_load(tmp_path, monkeypatch):
    recorder = Calls({("launchctl", "bootstrap"): (5, "")})
    monkeypatch.setattr(service, "_run", recorder)
    svc = launchd(tmp_path)
    service.install(svc)
    assert recorder.made[-1] == ["launchctl", "load", "-w", str(svc.path)]


def test_launchd_uninstall_boots_out(tmp_path, calls):
    svc = launchd(tmp_path)
    service.install(svc)
    calls.made.clear()
    assert service.uninstall(svc)
    assert calls.made == [["launchctl", "bootout", svc.target]]
    assert not svc.path.exists()


def test_launchd_stop_is_a_clean_sigterm(tmp_path, monkeypatch):
    recorder = Calls({("launchctl", "print"): (0, "state = running\npid = 9\n")})
    monkeypatch.setattr(service, "_run", recorder)
    svc = launchd(tmp_path)
    svc.path.parent.mkdir(parents=True)
    svc.path.write_bytes(service.render(svc))
    service.stop(svc)
    assert recorder.made[-1] == ["launchctl", "kill", "SIGTERM", svc.target]
    service.restart(svc)
    assert recorder.made[-1] == ["launchctl", "kickstart", "-k", svc.target]


def test_actions_on_a_missing_service_say_how_to_install(tmp_path, calls):
    for action in (service.start, service.stop, service.restart):
        with pytest.raises(service.ServiceError, match="chord service install"):
            action(systemd(tmp_path))


def test_status_reads_systemctl_show(tmp_path, monkeypatch):
    recorder = Calls(
        {
            ("systemctl", "--user", "show"): (
                0,
                "LoadState=loaded\nActiveState=active\nSubState=running\nMainPID=77\n",
            )
        }
    )
    monkeypatch.setattr(service, "_run", recorder)
    svc = systemd(tmp_path)
    assert service.status(svc) == service.Status(False, False)
    svc.path.parent.mkdir(parents=True)
    svc.path.write_text("x")
    assert service.status(svc) == service.Status(True, True, 77, "active (running)")


def test_logs_come_from_the_journal_or_the_log_file(tmp_path):
    unit = systemd(tmp_path)
    assert service.logs_command(unit, follow=True, lines=20) == [
        "journalctl",
        "--user",
        "-u",
        unit.name,
        "-n",
        "20",
        "-f",
    ]
    assert service.logs_command(unit, follow=False, lines=5)[-1] == "--no-pager"
    agent = launchd(tmp_path)
    assert service.logs_command(agent, follow=True, lines=20) == [
        "tail",
        "-n",
        "20",
        "-F",
        str(agent.log),
    ]


def test_recent_log_keeps_the_newest_lines(tmp_path, monkeypatch):
    out = "\n".join(f"line {n}" for n in range(20)) + "\n"
    monkeypatch.setattr(service, "_run", Calls({("journalctl",): (0, out)}))
    assert service.recent_log(systemd(tmp_path), lines=3) == [
        "line 17",
        "line 18",
        "line 19",
    ]


def test_lingering_is_read_and_never_set(monkeypatch):
    monkeypatch.setenv("USER", "me")
    recorder = Calls({("loginctl",): (0, "no\n")})
    monkeypatch.setattr(service, "_run", recorder)
    assert service.lingering() is False
    assert recorder.made == [["loginctl", "show-user", "me", "-p", "Linger", "--value"]]


def test_an_interpreter_in_a_cache_is_flagged(tmp_path):
    assert service.ephemeral("/home/me/.cache/uv/archive-v0/abc/bin/python")
    assert not service.ephemeral("/home/me/.local/share/uv/tools/chord/bin/python")


# --- the watcher, when a service runs it ---


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "PROJECT_HOME", tmp_path)
    monkeypatch.setattr(daemon, "PID_FILE", tmp_path / "chord.pid")
    monkeypatch.setattr(daemon, "LOCK_FILE", tmp_path / "chord.lock")
    return tmp_path


def test_a_managed_watcher_claims_the_pid_file(session):
    daemon.claim()
    assert daemon.running_pid() == os.getpid()
    assert daemon.started_at() is not None


def test_a_managed_watcher_stands_down_for_a_running_one(session, monkeypatch):
    (session / "chord.pid").write_text("1\n0\n")
    monkeypatch.setattr(daemon, "running_pid", lambda: 4242)
    monkeypatch.setattr(daemon, "is_our_process", lambda pid: True)
    with pytest.raises(daemon.AlreadyRunning):
        daemon.claim()


def test_serve_managed_exits_cleanly_when_already_watching(monkeypatch):
    """A clean exit, so systemd and launchd don't restart into the same refusal."""

    def busy():
        raise daemon.AlreadyRunning(4242)

    monkeypatch.setattr(daemon, "claim", busy)
    result = runner.invoke(app, ["_serve", "--managed"])
    assert result.exit_code == 0
    assert "Already watching (pid 4242" in result.output


# --- the commands ---


@pytest.fixture
def unit(tmp_path, monkeypatch, calls):
    svc = systemd(tmp_path)
    monkeypatch.setattr(service, "current", lambda: svc)
    return svc


def test_service_commands_are_listed():
    result = runner.invoke(app, ["service", "--help"])
    assert result.exit_code == 0
    for name in ("install", "uninstall", "status", "logs", "start", "stop", "restart"):
        assert name in result.output


def test_unsupported_platforms_point_at_chord_start():
    result = runner.invoke(app, ["service", "install"])
    assert result.exit_code == 1
    assert "chord start" in result.output


def test_install_checks_setup_like_start_does(unit, no_env):
    result = runner.invoke(app, ["service", "install"])
    assert result.exit_code == 1
    assert not unit.path.exists()


def test_install_hands_over_from_chord_start(
    unit, stored_token, no_env, monkeypatch, calls
):
    stored_token()
    (no_env / "chord.toml").write_text('harness = "print"\n')
    monkeypatch.setattr(daemon, "stop", lambda: 4242)
    monkeypatch.setenv("USER", "me")
    result = runner.invoke(app, ["service", "install"])
    assert result.exit_code == 0, result.output
    assert "Stopped the watcher `chord start` began (pid 4242)" in result.output
    assert "Installed the Chord service" in result.output
    assert "loginctl enable-linger" in result.output
    assert unit.path.is_file()
    assert calls.ran("systemctl", "--user", "enable", unit.name)


def test_status_when_not_installed(unit):
    result = runner.invoke(app, ["service", "status"])
    assert result.exit_code == 0
    assert "isn't installed" in result.output
    assert "chord service install" in result.output


def test_status_when_running(unit, monkeypatch):
    unit.path.parent.mkdir(parents=True)
    unit.path.write_text("x")
    monkeypatch.setattr(
        service,
        "_run",
        Calls(
            {
                ("systemctl", "--user", "show"): (
                    0,
                    (
                        "LoadState=loaded\nActiveState=active\nSubState=running\n"
                        "MainPID=77\n"
                    ),
                ),
                ("journalctl",): (0, "2026-10-24 11:00:00  INFO    Watching.\n"),
            }
        ),
    )
    result = runner.invoke(app, ["service", "status"])
    assert result.exit_code == 0, result.output
    assert "running    yes (pid 77)" in result.output
    assert "Watching." in result.output
    assert str(unit.path) in result.output


def test_uninstall_command(unit):
    unit.path.parent.mkdir(parents=True)
    unit.path.write_text("x")
    result = runner.invoke(app, ["service", "uninstall"])
    assert result.exit_code == 0
    assert "no longer starts at login" in result.output
    assert not unit.path.exists()


def test_start_and_stop_go_through_an_installed_service(
    unit, stored_token, no_env, monkeypatch, calls
):
    """One watcher per project: with a service, it is the service's to run."""
    stored_token()
    (no_env / "chord.toml").write_text('harness = "print"\n')
    monkeypatch.setattr(daemon, "STATE_FILE", no_env / "state.json")
    unit.path.parent.mkdir(parents=True)
    unit.path.write_text("x")
    spawned = []
    monkeypatch.setattr(daemon, "start", lambda: spawned.append(1) or 1)

    result = runner.invoke(app, ["start"])
    assert result.exit_code == 0, result.output
    assert "Started the Chord service" in result.output
    assert not spawned
    assert calls.ran("systemctl", "--user", "start", unit.name)

    result = runner.invoke(app, ["stop"])
    assert result.exit_code == 0, result.output
    assert calls.ran("systemctl", "--user", "stop", unit.name)


def test_watch_follows_the_service_log(unit, monkeypatch):
    unit.path.parent.mkdir(parents=True)
    unit.path.write_text("x")
    ran = []
    monkeypatch.setattr(
        "chord.cli.subprocess.run", lambda args, check: ran.append(args)
    )
    result = runner.invoke(app, ["watch"])
    assert result.exit_code == 0
    assert ran == [service.logs_command(unit, follow=True, lines=50)]


def test_info_has_a_service_line(unit, keyring_backend, no_env, monkeypatch):
    monkeypatch.setattr(daemon, "PID_FILE", no_env / "absent.pid")
    monkeypatch.setattr(daemon, "STATE_FILE", no_env / "absent.json")
    result = runner.invoke(app, ["info"])
    assert "service    not installed (`chord service install`" in result.output
    unit.path.parent.mkdir(parents=True)
    unit.path.write_text("x")
    result = runner.invoke(app, ["info"])
    assert f"service    installed, systemd user unit {unit.name}" in result.output


def test_info_where_there_is_no_manager(keyring_backend, no_env, monkeypatch):
    monkeypatch.setattr(daemon, "PID_FILE", no_env / "absent.pid")
    monkeypatch.setattr(daemon, "STATE_FILE", no_env / "absent.json")
    result = runner.invoke(app, ["info"])
    assert "service    not available here" in result.output


def test_setup_offers_the_service_and_defaults_to_no(unit, monkeypatch):
    from chord import cli

    asked = []
    monkeypatch.setattr(
        cli.typer, "confirm", lambda text, default: asked.append(default) or False
    )
    assert cli._offer_service() is False
    assert asked == [False]
    assert not unit.path.exists()


def test_setup_does_not_offer_what_is_already_installed(unit, monkeypatch):
    from chord import cli

    unit.path.parent.mkdir(parents=True)
    unit.path.write_text("x")
    monkeypatch.setattr(
        cli.typer, "confirm", lambda *a, **k: pytest.fail("asked again")
    )
    assert cli._offer_service() is False


# --- a service-run watcher that isn't signed in ---


def _loads(*answers):
    """Stands in for credentials.load, giving each answer in turn, then the last."""
    queue = list(answers)

    def load():
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return load


@pytest.fixture
def said(monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr("chord.cli.watcher.log", lines.append)
    return lines


def test_signed_out_waits_and_says_so_once(monkeypatch, said):
    """Not signed in under a service is a wait, not a crash loop."""
    from chord import cli, credentials

    signed_out = (1, "Not set up yet. Run `chord setup`.")
    monkeypatch.setattr(
        credentials,
        "load",
        _loads(signed_out, signed_out, signed_out, (0, {"access_token": "tok"})),
    )
    token = asyncio.run(cli._wait_for_sign_in(0.01))
    assert token == "tok"
    assert len([line for line in said if "Not signed in to Linear" in line]) == 1
    assert "Run `chord setup`" in said[0]
    assert said[-1] == "Signed in to Linear. Starting to watch."


def test_signed_in_from_the_start_says_nothing(monkeypatch, said):
    from chord import cli, credentials

    monkeypatch.setattr(credentials, "load", _loads((0, {"access_token": "tok"})))
    assert asyncio.run(cli._wait_for_sign_in(0.01)) == "tok"
    assert said == []


def test_a_new_reason_to_wait_is_said_again(monkeypatch, said):
    from chord import cli, credentials

    monkeypatch.setattr(
        credentials,
        "load",
        _loads(
            (1, "Not set up yet. Run `chord setup`."),
            (1, "Token is 1500m old, past max age. Run `chord setup`."),
            (1, "Token is 1500m old, past max age. Run `chord setup`."),
            (0, {"access_token": "tok"}),
        ),
    )
    asyncio.run(cli._wait_for_sign_in(0.01))
    waits = [line for line in said if "Not signed in to Linear" in line]
    assert len(waits) == 2
    assert "past max age" in waits[1]


def test_sigterm_while_signed_out_ends_the_wait_cleanly(monkeypatch, said):
    """`chord service stop` has to be a clean exit, or launchd restarts it."""
    from chord import cli, credentials

    monkeypatch.setattr(credentials, "load", _loads((1, "Not set up yet.")))

    async def run():
        asyncio.get_running_loop().call_later(
            0.05, os.kill, os.getpid(), signal.SIGTERM
        )
        return await cli._wait_for_sign_in(30)

    # Reaching this line at all means the handler caught it: the default
    # action would have ended the test process.
    assert asyncio.run(run()) is None


def test_serve_managed_signed_out_exits_zero_when_stopped(monkeypatch, no_env):
    """The managed path never takes `_token()`'s exit-1 on a missing sign-in."""
    from chord import cli

    monkeypatch.setattr(daemon, "claim", lambda: None)
    released = []
    monkeypatch.setattr(daemon, "release", lambda: released.append(1))

    async def stopped(every):
        return None

    monkeypatch.setattr(cli, "_wait_for_sign_in", stopped)
    monkeypatch.setattr(
        cli, "_token", lambda: pytest.fail("managed watcher took the exit-1 path")
    )
    result = runner.invoke(app, ["_serve", "--managed"])
    assert result.exit_code == 0, result.output
    assert "Stopping." in result.output
    assert released == [1]


def test_unmanaged_serve_still_fails_when_signed_out(keyring_backend, no_env):
    """`chord start` checked before spawning; a missing token here is a failure."""
    result = runner.invoke(app, ["_serve"])
    assert result.exit_code == 1
    assert "Not set up yet" in result.output
