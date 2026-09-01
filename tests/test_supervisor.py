from __future__ import annotations

from contextlib import nullcontext
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

import schedule_supervisor as supervisor


ROOT = Path(__file__).resolve().parents[1]
SCHEDULE = ROOT / "schedule"


class SupervisorTests(unittest.TestCase):
    def test_background_detection_is_narrow(self) -> None:
        self.assertTrue(supervisor.is_background_run(["run", "--background"]))
        self.assertTrue(supervisor.is_background_run(["run", "--parallel", "-b"]))
        self.assertFalse(supervisor.is_background_run(["run"]))
        self.assertFalse(supervisor.is_background_run(["log", "--background"]))

    def test_state_directory_matches_relative_core_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cwd = Path(temporary)
            self.assertEqual(
                supervisor.state_directory({"SCHEDULE_STATE_DIR": "state"}, cwd),
                (cwd / "state").resolve(),
            )
            self.assertEqual(
                supervisor.state_directory({"XDG_STATE_HOME": "xdg"}, cwd),
                (cwd / "xdg" / "schedule").resolve(),
            )

    def test_private_json_is_mode_600(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state" / "value.json"
            supervisor.write_private_json(path, {"secret": "value"})
            self.assertEqual(json.loads(path.read_text()), {"secret": "value"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_systemd_unit_owns_the_full_command_group_and_is_reboot_enabled(self) -> None:
        config = {
            "python": "/usr/bin/python3",
            "wrapper": "/home/me/.local/bin/schedule",
            "config_path": "/home/me/.local/state/schedule/supervisor.json",
        }
        unit = supervisor.build_systemd_unit(config)
        self.assertIn("KillMode=mixed", unit)
        self.assertIn("TimeoutStopSec=1s", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("WantedBy=default.target", unit)
        self.assertIn("_supervised-run", unit)

    def test_systemd_quoting_cannot_inject_new_unit_lines(self) -> None:
        quoted = supervisor.systemd_quote('path\nExecStart=/tmp/evil\t$HOME%h"')
        self.assertNotIn("\n", quoted)
        self.assertIn("\\n", quoted)
        self.assertIn("\\t", quoted)
        self.assertIn("$$HOME", quoted)
        self.assertIn("%%h", quoted)
        self.assertIn('\\"', quoted)

    def test_manager_file_write_does_not_rechmod_standard_config_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary) / "systemd" / "user"
            config_dir.mkdir(parents=True, mode=0o755)
            config_dir.chmod(0o755)
            config = {
                "python": "/usr/bin/python3",
                "wrapper": "/home/me/.local/bin/schedule",
                "config_path": "/home/me/.local/state/schedule/supervisor.json",
                "unit_path": str(config_dir / supervisor.SYSTEMD_UNIT_NAME),
            }
            fake = supervisor.Supervisor("systemd", "/bin/true")
            with mock.patch.object(
                supervisor,
                "_run_quiet",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ):
                supervisor._start_systemd(fake, config)
            self.assertEqual(stat.S_IMODE(config_dir.stat().st_mode), 0o755)

    def test_launchd_agent_keeps_descendants_in_the_user_job(self) -> None:
        config = {
            "python": "/usr/bin/python3",
            "wrapper": "/Users/me/.local/bin/schedule",
            "config_path": "/Users/me/.local/state/schedule/supervisor.json",
            "state_dir": "/Users/me/.local/state/schedule",
        }
        value = supervisor.build_launchd_plist(config)
        encoded = plistlib.dumps(value)
        decoded = plistlib.loads(encoded)
        self.assertTrue(decoded["RunAtLoad"])
        self.assertFalse(decoded["AbandonProcessGroup"])
        self.assertEqual(decoded["Umask"], 0o077)
        self.assertEqual(decoded["KeepAlive"], {"SuccessfulExit": False})
        self.assertIn("_supervised-run", decoded["ProgramArguments"])

    def test_start_supervised_saves_environment_and_exact_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            fake = supervisor.Supervisor("systemd", "/bin/true")
            with mock.patch.object(supervisor, "start_manager") as start_manager:
                with mock.patch.object(supervisor, "wait_for_status", return_value=0):
                    result = supervisor.start_supervised(
                        ["run", "--background", "--parallel"],
                        fake,
                        Path("/tmp/schedule-core"),
                        root,
                    )
            self.assertEqual(result, 0)
            start_manager.assert_called_once()
            config = json.loads((root / supervisor.CONFIG_FILENAME).read_text())
            self.assertEqual(config["arguments"], ["run", "--background", "--parallel"])
            self.assertEqual(config["environment"]["PATH"], os.environ["PATH"])
            self.assertEqual(config["state_dir"], str(root.resolve()))

    def test_active_background_run_is_rejected_without_detached_race(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            fake = supervisor.Supervisor("systemd", "/bin/true")
            with mock.patch.object(supervisor, "active_lock_held", return_value=True):
                with mock.patch.object(
                    supervisor, "active_run_message", return_value="run 9 is active (PID 123)"
                ):
                    with self.assertRaisesRegex(supervisor.ActiveRunError, "run 9 is active"):
                        supervisor.start_supervised(
                            ["run", "--background"],
                            fake,
                            Path("/tmp/schedule-core"),
                            root,
                        )

    def test_start_supervised_replaces_a_loaded_registration_without_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            fake = supervisor.Supervisor("systemd", "/bin/true")
            with mock.patch.object(supervisor, "active_lock_held", return_value=False):
                with mock.patch.object(supervisor, "manager_running", return_value=True):
                    with mock.patch.object(supervisor, "cleanup_registration") as cleanup:
                        with mock.patch.object(supervisor, "start_manager") as start_manager:
                            with mock.patch.object(
                                supervisor, "wait_for_status", return_value=0
                            ):
                                result = supervisor.start_supervised(
                                    ["run", "--background"],
                                    fake,
                                    Path("/tmp/schedule-core"),
                                    root,
                                )
            self.assertEqual(result, 0)
            cleanup.assert_called_once()
            start_manager.assert_called_once()

    def test_stale_active_run_requests_a_full_service_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / supervisor.CONFIG_FILENAME
            supervisor.write_private_json(
                config_path,
                {
                    "version": supervisor.SUPERVISOR_VERSION,
                    "nonce": "test-nonce",
                    "state_dir": str(root),
                },
            )
            started = subprocess.CompletedProcess(
                ["schedule-core"], 0, "Started run 1 (PID 4321)\n", ""
            )
            with mock.patch.object(
                supervisor,
                "preserve_run_on_termination",
                return_value=nullcontext(),
            ):
                with mock.patch.object(supervisor, "_run_core", return_value=started):
                    with mock.patch.object(supervisor, "_write_start_status"):
                        with mock.patch.object(supervisor, "active_lock_held", return_value=False):
                            with mock.patch.object(
                                supervisor, "_active_run", return_value={"id": 1, "pid": 4321}
                            ):
                                result = supervisor.supervised_runner(config_path)
            self.assertEqual(result, supervisor.RESTART_EXIT_CODE)

    def test_completed_run_cleans_up_without_stopping_its_own_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / supervisor.CONFIG_FILENAME
            supervisor.write_private_json(
                config_path,
                {
                    "version": supervisor.SUPERVISOR_VERSION,
                    "nonce": "test-nonce",
                    "state_dir": str(root),
                },
            )
            started = subprocess.CompletedProcess(
                ["schedule-core"], 0, "Started run 1 (PID 4321)\n", ""
            )
            with mock.patch.object(
                supervisor,
                "preserve_run_on_termination",
                return_value=nullcontext(),
            ):
                with mock.patch.object(supervisor, "_run_core", return_value=started):
                    with mock.patch.object(supervisor, "_write_start_status"):
                        with mock.patch.object(supervisor, "active_lock_held", return_value=True):
                            with mock.patch.object(supervisor, "_active_run", return_value=None):
                                with mock.patch.object(supervisor, "_wait_for_ack"):
                                    with mock.patch.object(
                                        supervisor, "cleanup_registration"
                                    ) as cleanup:
                                        result = supervisor.supervised_runner(config_path)
            self.assertEqual(result, 0)
            cleanup.assert_called_once_with(mock.ANY, unload=False)

    def test_wrapper_delegates_exactly_when_supervision_is_off(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fake_core = Path(temporary) / "schedule-core"
            fake_core.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "print(json.dumps(sys.argv[1:]))\n"
            )
            fake_core.chmod(0o755)
            environment = os.environ.copy()
            environment["SCHEDULE_CORE"] = str(fake_core)
            environment["SCHEDULE_SUPERVISOR"] = "off"
            result = subprocess.run(
                [str(SCHEDULE), "run", "--background", "--parallel"],
                cwd=ROOT,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["run", "--background", "--parallel"])
        self.assertEqual(result.stderr, "")

    def test_requested_supervisor_failure_does_not_silently_detach(self) -> None:
        with mock.patch.object(
            supervisor,
            "choose_supervisor",
            side_effect=supervisor.SupervisorError("systemd user services are not available"),
        ):
            with mock.patch.object(supervisor, "_delegate") as delegate:
                result = supervisor.main(["run", "--background"])
        self.assertEqual(result, 2)
        delegate.assert_not_called()

    def test_explicit_supervisor_on_wrong_platform_is_rejected(self) -> None:
        requested = {"SCHEDULE_SUPERVISOR": "launchd"}
        with mock.patch.object(supervisor.sys, "platform", "linux"):
            with self.assertRaisesRegex(supervisor.SupervisorError, "not available"):
                supervisor.choose_supervisor(requested)

    def test_database_probe_failure_is_not_reported_as_idle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "schedule.db").write_text("not a sqlite database")
            with self.assertRaises(sqlite3.DatabaseError):
                supervisor._active_run(root)

    def test_worker_group_is_killed_abruptly(self) -> None:
        with mock.patch.object(supervisor.os, "killpg") as killpg:
            supervisor._kill_worker_group(1234)
        killpg.assert_called_once_with(1234, supervisor.signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
