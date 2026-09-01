"""User-service supervision for terminal-scheduler background runs."""

from __future__ import annotations

import errno
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence
import uuid


SYSTEMD_UNIT_NAME = "terminal-scheduler.service"
LAUNCHD_LABEL = "dev.konni.terminal-scheduler"
CONFIG_FILENAME = "supervisor.json"
STATUS_FILENAME = "supervisor-status.json"
ACK_FILENAME = "supervisor-ack.json"
SETUP_LOCK_FILENAME = "supervisor.lock"
SUPERVISOR_VERSION = 1
STATUS_WAIT_SECONDS = 5.0
ACK_WAIT_SECONDS = 5.0
RUN_POLL_SECONDS = 0.25
WORKER_START_TIMEOUT_SECONDS = 30.0
RESTART_EXIT_CODE = 75


class SupervisorError(Exception):
    """A user-facing supervisor setup or execution error."""


class ActiveRunError(SupervisorError):
    """Raised when the scheduler's execution lock is already owned."""


@dataclass(frozen=True)
class Supervisor:
    kind: str
    executable: str
    domain: Optional[str] = None


def _resolved_path(value: str, cwd: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def state_directory(
    environment: Optional[Mapping[str, str]] = None,
    cwd: Optional[Path] = None,
) -> Path:
    """Resolve state exactly like the scheduler core."""
    env = os.environ if environment is None else environment
    base = Path.cwd() if cwd is None else cwd
    override = env.get("SCHEDULE_STATE_DIR")
    if override:
        return _resolved_path(override, base)
    xdg_state = env.get("XDG_STATE_HOME")
    if xdg_state:
        return (_resolved_path(xdg_state, base) / "schedule").resolve()
    home = Path(env.get("HOME", str(Path.home()))).expanduser()
    return (home / ".local" / "state" / "schedule").resolve()


def wrapper_path() -> Path:
    return Path(__file__).with_name("schedule").resolve()


def core_path(environment: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if environment is None else environment
    override = env.get("SCHEDULE_CORE")
    if override:
        return _resolved_path(override, Path.cwd())
    return Path(__file__).with_name("schedule-core").resolve()


def is_background_run(arguments: Sequence[str]) -> bool:
    return bool(arguments) and arguments[0] == "run" and any(
        argument in ("-b", "--background") for argument in arguments[1:]
    )


def _disabled(value: Optional[str]) -> bool:
    return value is not None and value.strip().lower() in {
        "0",
        "false",
        "no",
        "none",
        "off",
        "disabled",
    }


def _run_quiet(command: Sequence[str], timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(list(command), 127, "", str(error))


def _launchd_domain(launchctl: str) -> Optional[str]:
    uid = os.getuid()
    for domain in (f"gui/{uid}", f"user/{uid}"):
        if _run_quiet([launchctl, "print", domain]).returncode == 0:
            return domain
    return None


def choose_supervisor(environment: Optional[Mapping[str, str]] = None) -> Optional[Supervisor]:
    env = os.environ if environment is None else environment
    requested = env.get("SCHEDULE_SUPERVISOR", "auto").strip().lower()
    if _disabled(requested):
        return None
    if requested not in ("auto", "systemd", "launchd"):
        raise SupervisorError(
            "SCHEDULE_SUPERVISOR must be auto, systemd, launchd, or off"
        )

    if requested in ("auto", "systemd") and sys.platform.startswith("linux"):
        systemctl = shutil.which("systemctl")
        if systemctl and (
            requested == "systemd"
            or _run_quiet([systemctl, "--user", "show-environment"]).returncode == 0
        ):
            return Supervisor("systemd", str(Path(systemctl).resolve()))
        if requested == "systemd":
            raise SupervisorError("systemd user services are not available")

    if requested in ("auto", "launchd") and sys.platform == "darwin":
        launchctl = shutil.which("launchctl")
        domain = _launchd_domain(launchctl) if launchctl else None
        if launchctl and domain:
            return Supervisor("launchd", str(Path(launchctl).resolve()), domain)
        if requested == "launchd":
            raise SupervisorError("a launchd user domain is not available")

    if requested == "systemd":
        raise SupervisorError("systemd user services are not available on this platform")
    if requested == "launchd":
        raise SupervisorError("launchd user agents are not available on this platform")

    return None


def _secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_descriptor = None
        if directory_descriptor is not None:
            try:
                os.fsync(directory_descriptor)
            except OSError as error:
                if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                    raise
            finally:
                os.close(directory_descriptor)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def write_private_json(path: Path, value: Mapping[str, Any]) -> None:
    _secure_directory(path.parent)
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n"
    _atomic_write(path, payload, 0o600)


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _config_path(state_root: Path) -> Path:
    return state_root / CONFIG_FILENAME


def _status_path(state_root: Path) -> Path:
    return state_root / STATUS_FILENAME


def _ack_path(state_root: Path) -> Path:
    return state_root / ACK_FILENAME


@contextmanager
def setup_lock(state_root: Path) -> Iterator[None]:
    _secure_directory(state_root)
    path = state_root / SETUP_LOCK_FILENAME
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(path, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def active_lock_held(state_root: Path) -> bool:
    """Return whether a live scheduler worker currently owns active.lock."""
    _secure_directory(state_root)
    descriptor = os.open(state_root / "active.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                return True
            raise
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def active_run_message(state_root: Path) -> str:
    database = state_root / "schedule.db"
    if not database.exists():
        return "another scheduler operation is active"
    try:
        connection = sqlite3.connect(str(database), timeout=2.0)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT id, pid FROM runs WHERE status IN ('starting', 'running') "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return "another scheduler operation is active"
    if row is None:
        return "another scheduler operation is active"
    pid_text = f" (PID {row['pid']})" if row["pid"] else ""
    return f"run {row['id']} is active{pid_text}"


def _systemd_config_dir(environment: Mapping[str, str], cwd: Path) -> Path:
    configured = environment.get("XDG_CONFIG_HOME")
    if configured:
        return _resolved_path(configured, cwd) / "systemd" / "user"
    home = Path(environment.get("HOME", str(Path.home()))).expanduser()
    return (home / ".config" / "systemd" / "user").resolve()


def _launch_agents_dir(environment: Mapping[str, str]) -> Path:
    home = Path(environment.get("HOME", str(Path.home()))).expanduser()
    return (home / "Library" / "LaunchAgents").resolve()


def systemd_quote(value: str) -> str:
    escaped = []
    replacements = {
        "\\": "\\\\",
        '"': '\\"',
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
        "%": "%%",
        "$": "$$",
    }
    for character in value:
        replacement = replacements.get(character)
        if replacement is not None:
            escaped.append(replacement)
        elif ord(character) < 32 or ord(character) == 127:
            escaped.append(f"\\x{ord(character):02x}")
        else:
            escaped.append(character)
    return f'"{"".join(escaped)}"'


def build_systemd_unit(config: Mapping[str, Any]) -> str:
    command = " ".join(
        systemd_quote(str(value))
        for value in (
            config["python"],
            config["wrapper"],
            "_supervised-run",
            config["config_path"],
        )
    )
    return (
        "[Unit]\n"
        "Description=Terminal Scheduler reboot-resilient background run\n"
        "Documentation=https://github.com/Konni5012/terminal-scheduler\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={command}\n"
        # The core deliberately gives each command its own process group. A
        # mixed stop signals only this supervisor first; any remaining worker
        # and command groups are then killed together through the user-service
        # cgroup. That abrupt final kill leaves the in-flight queue entry in
        # place so it can be restarted after boot.
        "KillMode=mixed\n"
        "TimeoutStopSec=1s\n"
        "Restart=on-failure\n"
        "RestartSec=5s\n"
        "UMask=0077\n"
        "StandardOutput=null\n"
        "StandardError=journal\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def build_launchd_plist(config: Mapping[str, Any]) -> Dict[str, Any]:
    state_root = Path(str(config["state_dir"]))
    return {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [
            str(config["python"]),
            str(config["wrapper"]),
            "_supervised-run",
            str(config["config_path"]),
        ],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "AbandonProcessGroup": False,
        "Umask": 0o077,
        "ThrottleInterval": 5,
        "StandardOutPath": str(state_root / "supervisor.stdout.log"),
        "StandardErrorPath": str(state_root / "supervisor.stderr.log"),
    }


def _manager_error(result: subprocess.CompletedProcess[str], action: str) -> SupervisorError:
    detail = (result.stderr or result.stdout).strip()
    if detail:
        return SupervisorError(f"{action}: {detail}")
    return SupervisorError(f"{action} failed with exit status {result.returncode}")


def _systemd_running(supervisor: Supervisor) -> bool:
    return (
        _run_quiet(
            [supervisor.executable, "--user", "is-active", "--quiet", SYSTEMD_UNIT_NAME]
        ).returncode
        == 0
    )


def _launchd_running(supervisor: Supervisor) -> bool:
    assert supervisor.domain is not None
    return (
        _run_quiet(
            [supervisor.executable, "print", f"{supervisor.domain}/{LAUNCHD_LABEL}"]
        ).returncode
        == 0
    )


def manager_running(supervisor: Supervisor) -> bool:
    if supervisor.kind == "systemd":
        return _systemd_running(supervisor)
    return _launchd_running(supervisor)


def _start_systemd(supervisor: Supervisor, config: Mapping[str, Any]) -> None:
    unit_path = Path(str(config["unit_path"]))
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(unit_path, build_systemd_unit(config).encode("utf-8"), 0o644)
    commands = [
        [supervisor.executable, "--user", "daemon-reload"],
        [supervisor.executable, "--user", "reset-failed", SYSTEMD_UNIT_NAME],
        [supervisor.executable, "--user", "enable", SYSTEMD_UNIT_NAME],
        [supervisor.executable, "--user", "start", SYSTEMD_UNIT_NAME],
    ]
    for index, command in enumerate(commands):
        result = _run_quiet(command, timeout=15.0)
        if result.returncode != 0 and index != 1:
            raise _manager_error(result, "could not start the systemd user service")


def _start_launchd(supervisor: Supervisor, config: Mapping[str, Any]) -> None:
    assert supervisor.domain is not None
    plist_path = Path(str(config["plist_path"]))
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    payload = plistlib.dumps(build_launchd_plist(config), fmt=plistlib.FMT_XML, sort_keys=True)
    _atomic_write(plist_path, payload, 0o644)
    _run_quiet(
        [supervisor.executable, "bootout", f"{supervisor.domain}/{LAUNCHD_LABEL}"]
    )
    result = _run_quiet(
        [supervisor.executable, "bootstrap", supervisor.domain, str(plist_path)],
        timeout=15.0,
    )
    if result.returncode != 0:
        raise _manager_error(result, "could not bootstrap the launchd user agent")
    result = _run_quiet(
        [
            supervisor.executable,
            "kickstart",
            "-k",
            f"{supervisor.domain}/{LAUNCHD_LABEL}",
        ],
        timeout=15.0,
    )
    if result.returncode != 0:
        raise _manager_error(result, "could not start the launchd user agent")


def start_manager(supervisor: Supervisor, config: Mapping[str, Any]) -> None:
    if supervisor.kind == "systemd":
        _start_systemd(supervisor, config)
    else:
        _start_launchd(supervisor, config)


def _supervisor_from_config(config: Mapping[str, Any]) -> Supervisor:
    kind = str(config.get("supervisor", ""))
    executable = str(config.get("manager", ""))
    if kind not in ("systemd", "launchd") or not executable:
        raise SupervisorError("invalid saved supervisor configuration")
    domain_value = config.get("domain")
    domain = str(domain_value) if domain_value else None
    return Supervisor(kind, executable, domain)


def _remove_state_files(config: Mapping[str, Any]) -> None:
    state_root = Path(str(config["state_dir"]))
    nonce = str(config.get("nonce", ""))
    current = _read_json(_config_path(state_root))
    if current is not None and str(current.get("nonce", "")) != nonce:
        return
    for path in (_ack_path(state_root), _status_path(state_root), _config_path(state_root)):
        path.unlink(missing_ok=True)


def cleanup_registration(config: Mapping[str, Any], unload: bool = True) -> None:
    """Disable the saved registration without touching a newer request."""
    supervisor = _supervisor_from_config(config)
    state_root = Path(str(config["state_dir"]))
    nonce = str(config.get("nonce", ""))
    current = _read_json(_config_path(state_root))
    if current is not None and str(current.get("nonce", "")) != nonce:
        return

    if supervisor.kind == "systemd":
        if unload:
            result = _run_quiet(
                [supervisor.executable, "--user", "stop", SYSTEMD_UNIT_NAME],
                timeout=15.0,
            )
            if result.returncode != 0 and _systemd_running(supervisor):
                raise _manager_error(result, "could not stop the systemd user service")
        _run_quiet(
            [supervisor.executable, "--user", "disable", SYSTEMD_UNIT_NAME],
            timeout=15.0,
        )
        unit_value = str(config.get("unit_path", ""))
        if unit_value:
            Path(unit_value).unlink(missing_ok=True)
        _run_quiet([supervisor.executable, "--user", "daemon-reload"], timeout=15.0)
        _remove_state_files(config)
        return

    if unload and supervisor.domain:
        result = _run_quiet(
            [supervisor.executable, "bootout", f"{supervisor.domain}/{LAUNCHD_LABEL}"],
            timeout=15.0,
        )
        if result.returncode != 0 and _launchd_running(supervisor):
            raise _manager_error(result, "could not unload the launchd user agent")
    plist_value = str(config.get("plist_path", ""))
    if plist_value:
        Path(plist_value).unlink(missing_ok=True)
    _remove_state_files(config)


def _replay_status(status: Mapping[str, Any]) -> int:
    stdout = str(status.get("stdout", ""))
    stderr = str(status.get("stderr", ""))
    if stdout:
        sys.stdout.write(stdout)
        sys.stdout.flush()
    if stderr:
        sys.stderr.write(stderr)
        sys.stderr.flush()
    try:
        return int(status.get("returncode", 2))
    except (TypeError, ValueError):
        return 2


def wait_for_status(state_root: Path, nonce: str, supervisor: Supervisor) -> int:
    deadline = time.monotonic() + STATUS_WAIT_SECONDS
    while time.monotonic() < deadline:
        status = _read_json(_status_path(state_root))
        if status is not None and str(status.get("nonce", "")) == nonce:
            write_private_json(_ack_path(state_root), {"nonce": nonce})
            return _replay_status(status)
        if not manager_running(supervisor):
            time.sleep(0.05)
        else:
            time.sleep(0.05)

    status = _read_json(_status_path(state_root))
    if status is not None and str(status.get("nonce", "")) == nonce:
        write_private_json(_ack_path(state_root), {"nonce": nonce})
        return _replay_status(status)
    if manager_running(supervisor):
        print(
            "Started a supervised background run. View it with: schedule log",
            flush=True,
        )
        return 0
    raise SupervisorError("the user service exited before starting the scheduler worker")


def _environment_copy() -> Dict[str, str]:
    return {str(key): str(value) for key, value in os.environ.items()}


def _new_config(
    arguments: Sequence[str],
    supervisor: Supervisor,
    state_root: Path,
    core: Path,
) -> Dict[str, Any]:
    cwd = Path.cwd().resolve()
    environment = _environment_copy()
    config: Dict[str, Any] = {
        "version": SUPERVISOR_VERSION,
        "nonce": uuid.uuid4().hex,
        "supervisor": supervisor.kind,
        "manager": supervisor.executable,
        "domain": supervisor.domain,
        "python": sys.executable,
        "wrapper": str(wrapper_path()),
        "core": str(core),
        "arguments": list(arguments),
        "cwd": str(cwd),
        "state_dir": str(state_root),
        "environment": environment,
        "config_path": str(_config_path(state_root)),
    }
    if supervisor.kind == "systemd":
        config["unit_path"] = str(
            _systemd_config_dir(environment, cwd) / SYSTEMD_UNIT_NAME
        )
    else:
        config["plist_path"] = str(
            _launch_agents_dir(environment) / f"{LAUNCHD_LABEL}.plist"
        )
    return config


def start_supervised(
    arguments: Sequence[str], supervisor: Supervisor, core: Path, state_root: Path
) -> int:
    """Register and start a supervised background run."""
    with setup_lock(state_root):
        if active_lock_held(state_root):
            raise ActiveRunError(active_run_message(state_root))

        old_config = _read_json(_config_path(state_root))
        config = _new_config(arguments, supervisor, state_root, core)
        if old_config is not None:
            cleanup_registration(old_config)
        elif manager_running(supervisor):
            # Recover from a loaded service whose private config was removed
            # manually or by an interrupted cleanup.
            cleanup_registration(config)

        _status_path(state_root).unlink(missing_ok=True)
        _ack_path(state_root).unlink(missing_ok=True)
        write_private_json(_config_path(state_root), config)
        try:
            start_manager(supervisor, config)
        except BaseException:
            if not manager_running(supervisor):
                try:
                    cleanup_registration(config)
                except BaseException:
                    pass
            raise
        return wait_for_status(state_root, str(config["nonce"]), supervisor)


def _active_run(state_root: Path) -> Optional[Dict[str, int]]:
    database = state_root / "schedule.db"
    if not database.exists():
        return None
    connection = sqlite3.connect(str(database), timeout=2.0)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT id, pid FROM runs WHERE status IN ('starting', 'running') "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "pid": int(row["pid"] or 0),
    }


def _worker_pid(stdout: str) -> int:
    match = re.search(r"\bPID (\d+)\b", stdout)
    return int(match.group(1)) if match else 0


def _kill_worker_group(pid: int) -> None:
    """Abruptly stop the core worker without consuming its active queue item."""
    if pid <= 0:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
        return
    except (ProcessLookupError, PermissionError):
        pass
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


@contextmanager
def preserve_run_on_termination(worker_pid: Dict[str, int]) -> Iterator[None]:
    """Turn service-manager termination into an abrupt, resumable stop."""
    previous_handlers: Dict[int, Any] = {}

    def terminate(_signal_number: int, _frame: object) -> None:
        _kill_worker_group(int(worker_pid.get("pid", 0)))
        # Do not clean up the registration or saved environment. The user
        # service must remain enabled so it can restart this run after boot.
        os._exit(RESTART_EXIT_CODE)

    for signal_name in ("SIGTERM", "SIGHUP"):
        signal_number = getattr(signal, signal_name, None)
        if signal_number is None:
            continue
        previous_handlers[signal_number] = signal.getsignal(signal_number)
        signal.signal(signal_number, terminate)
    try:
        yield
    finally:
        for signal_number, previous_handler in previous_handlers.items():
            signal.signal(signal_number, previous_handler)


def _run_core(config: Mapping[str, Any]) -> subprocess.CompletedProcess[str]:
    core = str(config["core"])
    arguments_value = config.get("arguments")
    environment_value = config.get("environment")
    if not isinstance(arguments_value, list) or not all(
        isinstance(value, str) for value in arguments_value
    ):
        raise SupervisorError("invalid saved scheduler arguments")
    if not isinstance(environment_value, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in environment_value.items()
    ):
        raise SupervisorError("invalid saved scheduler environment")
    cwd = Path(str(config["cwd"]))
    if not cwd.is_dir():
        raise SupervisorError(f"saved working directory no longer exists: {cwd}")
    try:
        return subprocess.run(
            [core, *arguments_value],
            cwd=str(cwd),
            env=dict(environment_value),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=WORKER_START_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout if isinstance(error.stdout, str) else ""
        stderr = error.stderr if isinstance(error.stderr, str) else ""
        return subprocess.CompletedProcess(
            [core, *arguments_value],
            2,
            stdout,
            stderr + "schedule: error: timed out starting background worker\n",
        )
    except OSError as error:
        return subprocess.CompletedProcess(
            [core, *arguments_value],
            2,
            "",
            f"schedule: error: could not execute scheduler core: {error}\n",
        )


def _write_start_status(
    state_root: Path,
    nonce: str,
    result: subprocess.CompletedProcess[str],
) -> None:
    write_private_json(
        _status_path(state_root),
        {
            "nonce": nonce,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "worker_pid": _worker_pid(result.stdout),
        },
    )


def _wait_for_ack(state_root: Path, nonce: str) -> None:
    deadline = time.monotonic() + ACK_WAIT_SECONDS
    while time.monotonic() < deadline:
        ack = _read_json(_ack_path(state_root))
        if ack is not None and str(ack.get("nonce", "")) == nonce:
            return
        time.sleep(0.05)


def _config_is_current(config: Mapping[str, Any]) -> bool:
    state_root = Path(str(config["state_dir"]))
    current = _read_json(_config_path(state_root))
    return current is not None and str(current.get("nonce", "")) == str(
        config.get("nonce", "")
    )


def supervised_runner(config_path: Path) -> int:
    config = _read_json(config_path)
    if config is None:
        print(f"schedule: supervisor configuration is missing: {config_path}", file=sys.stderr)
        return 1
    if int(config.get("version", 0)) != SUPERVISOR_VERSION:
        print("schedule: unsupported supervisor configuration version", file=sys.stderr)
        return 1

    state_root = Path(str(config["state_dir"]))
    nonce = str(config["nonce"])
    worker_pid = {"pid": 0}
    try:
        with preserve_run_on_termination(worker_pid):
            result = _run_core(config)
            worker_pid["pid"] = _worker_pid(result.stdout)
            _write_start_status(state_root, nonce, result)
            if result.returncode != 0:
                _wait_for_ack(state_root, nonce)
                cleanup_registration(config, unload=False)
                return 0

            while _config_is_current(config):
                try:
                    lock_held = active_lock_held(state_root)
                    active = _active_run(state_root)
                except (OSError, sqlite3.Error):
                    # A temporary read failure must never be mistaken for an
                    # idle queue, because cleanup would remove reboot recovery.
                    time.sleep(RUN_POLL_SECONDS)
                    continue
                if active is None:
                    _wait_for_ack(state_root, nonce)
                    cleanup_registration(config, unload=False)
                    return 0
                worker_pid["pid"] = int(active.get("pid", 0))
                if not lock_held:
                    # A stale active row means the worker disappeared before
                    # finalizing. Let the service manager tear down every
                    # remaining process in the execution group before it
                    # restarts this runner and snapshots the queued work again.
                    return RESTART_EXIT_CODE
                time.sleep(RUN_POLL_SECONDS)
            return 0
    except BaseException as error:
        print(f"schedule: supervisor failed: {error}", file=sys.stderr)
        return 1


def _delegate(core: Path, arguments: Sequence[str]) -> int:
    if not core.is_file():
        print(f"schedule: error: scheduler core is missing: {core}", file=sys.stderr)
        return 2
    try:
        os.execve(str(core), [str(core), *arguments], dict(os.environ))
    except OSError as error:
        print(f"schedule: error: could not execute scheduler core: {error}", file=sys.stderr)
        return 2
    return 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "_supervised-run":
        if len(arguments) != 2:
            return 2
        return supervised_runner(Path(arguments[1]))

    core = core_path()
    if not is_background_run(arguments):
        return _delegate(core, arguments)

    try:
        supervisor = choose_supervisor()
    except SupervisorError as error:
        print(f"schedule: error: {error}", file=sys.stderr)
        return 2
    if supervisor is None:
        if not _disabled(os.environ.get("SCHEDULE_SUPERVISOR")):
            print(
                "schedule: warning: no supported user service is available; "
                "using detached mode without reboot recovery",
                file=sys.stderr,
            )
        return _delegate(core, arguments)

    state_root = state_directory()
    try:
        result = start_supervised(arguments, supervisor, core, state_root)
    except ActiveRunError as error:
        print(f"schedule: error: {error}", file=sys.stderr)
        return 2
    except (OSError, SupervisorError) as error:
        print(f"schedule: error: could not start the user service: {error}", file=sys.stderr)
        return 2
    return result
