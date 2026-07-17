from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCHEDULE = ROOT / "schedule"


class ScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.work = self.root / "work"
        self.work.mkdir()
        self.env = os.environ.copy()
        self.env["SCHEDULE_STATE_DIR"] = str(self.state)

    def tearDown(self) -> None:
        self.wait_until_idle(timeout=5, fail=False)
        self.temp.cleanup()

    def cli(self, *arguments: str, cwd: Path | None = None, timeout: float = 15) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCHEDULE), *arguments],
            cwd=cwd or self.work,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )

    def add(self, command: str, cwd: Path | None = None) -> int:
        result = self.cli("add", command, cwd=cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        return int(result.stdout.split()[2])

    def database(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.state / "schedule.db")
        connection.row_factory = sqlite3.Row
        return connection

    def wait_until_idle(self, timeout: float = 10, fail: bool = True) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not (self.state / "schedule.db").exists():
                return
            connection = self.database()
            try:
                row = connection.execute(
                    "SELECT COUNT(*) FROM runs WHERE status IN ('starting', 'running')"
                ).fetchone()
            finally:
                connection.close()
            if row[0] == 0:
                return
            time.sleep(0.05)
        if fail:
            self.fail("background run did not finish")

    @staticmethod
    def listed_ids(output: str) -> list[int]:
        return [int(line.split(maxsplit=2)[1]) for line in output.splitlines()[1:]]

    def test_queue_add_list_swap_remove_and_stable_ids(self) -> None:
        first = self.add("echo first")
        second = self.add("echo second")
        third = self.add("echo third")
        self.assertEqual((first, second, third), (1, 2, 3))

        swapped = self.cli("mv", str(first), str(third))
        self.assertEqual(swapped.returncode, 0, swapped.stderr)
        listing = self.cli("list")
        ids = self.listed_ids(listing.stdout)
        self.assertEqual(ids, [3, 2, 1])

        removed = self.cli("rm", str(third), str(second))
        self.assertEqual(removed.returncode, 0, removed.stderr)
        fourth = self.add("echo fourth")
        self.assertEqual(fourth, 4)
        listing = self.cli("list")
        ids = self.listed_ids(listing.stdout)
        self.assertEqual(ids, [1, 4])

    def test_add_requires_one_valid_fish_string(self) -> None:
        unquoted_shape = self.cli("add", "echo", "hello")
        self.assertEqual(unquoted_shape.returncode, 2)
        self.assertIn("exactly one quoted", unquoted_shape.stderr)

        invalid = self.cli("add", "if true; echo broken")
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("invalid Fish syntax", invalid.stderr)

        leading_dash = self.cli("add", "--", "-not-a-real-command")
        self.assertEqual(leading_dash.returncode, 0, leading_dash.stderr)

    def test_foreground_streams_logs_and_remembers_cwd(self) -> None:
        nested = self.work / "nested"
        nested.mkdir()
        self.add("pwd; echo stderr-message >&2", cwd=nested)
        result = self.cli("run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(nested), result.stdout)
        self.assertIn("stderr-message", result.stdout)
        self.assertIn("Queue is empty", self.cli("list").stdout)
        latest = self.cli("log")
        self.assertEqual(latest.returncode, 0, latest.stderr)
        self.assertIn(str(nested), latest.stdout)
        self.assertIn("✓ succeeded", latest.stdout)

    def test_default_failure_consumes_attempted_and_preserves_rest(self) -> None:
        failed = self.add("echo failed; false")
        remaining = self.add("echo remaining")
        result = self.cli("run")
        self.assertEqual(result.returncode, 1)
        listing = self.cli("list")
        self.assertNotIn(failed, self.listed_ids(listing.stdout))
        self.assertIn(remaining, self.listed_ids(listing.stdout))
        self.assertNotIn("remaining\n", result.stdout)

    def test_no_exit_on_error_consumes_every_attempted_command(self) -> None:
        self.add("echo failed; false")
        self.add("echo continued")
        result = self.cli("run", "--no-exit-on-error")
        self.assertEqual(result.returncode, 1)
        self.assertIn("continued", result.stdout)
        self.assertIn("Queue is empty", self.cli("list").stdout)
        self.assertIn("1 failure", self.cli("log").stdout)

    def test_background_returns_quickly_and_logs_output(self) -> None:
        marker = self.work / "background-finished"
        self.add(f"sleep 1; echo detached > {marker}")
        started = time.monotonic()
        result = self.cli("run", "--background")
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(elapsed, 0.8)
        self.assertNotIn("detached", result.stdout)
        self.wait_until_idle()
        self.assertEqual(marker.read_text().strip(), "detached")
        self.assertIn("✓ succeeded", self.cli("log").stdout)

    def test_active_run_rules(self) -> None:
        self.add("sleep 3")
        background = self.cli("run", "--background")
        self.assertEqual(background.returncode, 0, background.stderr)
        self.assertEqual(self.add("echo later"), 2)
        self.assertEqual(self.cli("list").returncode, 0)
        self.assertEqual(self.cli("log").returncode, 0)
        self.assertEqual(self.cli("run").returncode, 2)
        self.assertEqual(self.cli("mv", "1", "2").returncode, 2)
        self.assertEqual(self.cli("rm", "2").returncode, 2)
        self.wait_until_idle()
        listing = self.cli("list")
        self.assertIn(2, self.listed_ids(listing.stdout))
        self.assertNotIn(1, self.listed_ids(listing.stdout))

    def test_logs_by_id_list_and_clear(self) -> None:
        self.add("echo first-run")
        self.assertEqual(self.cli("run").returncode, 0)
        self.add("echo second-run")
        self.assertEqual(self.cli("run").returncode, 0)
        self.assertIn("first-run", self.cli("log", "1").stdout)
        self.assertIn("second-run", self.cli("log").stdout)
        run_list = self.cli("log", "--list")
        self.assertRegex(run_list.stdout, r"(?m)^1\s+succeeded\s+")
        self.assertRegex(run_list.stdout, r"(?m)^2\s+succeeded\s+")
        self.assertEqual(self.add("echo queued-during-reset"), 3)
        cleared = self.cli("log", "--clear")
        self.assertEqual(cleared.returncode, 0, cleared.stderr)
        self.assertIn("Cleared 2", cleared.stdout)
        self.assertIn("No run logs", self.cli("log", "--list").stdout)
        self.assertEqual(self.listed_ids(self.cli("list").stdout), [1])
        self.assertEqual(self.add("echo reset-command-id"), 2)
        rerun = self.cli("run")
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        self.assertIn("run 1 · foreground", rerun.stdout)

    def test_duplicate_and_missing_ids_are_atomic(self) -> None:
        first = self.add("echo one")
        self.add("echo two")
        result = self.cli("rm", str(first), "999")
        self.assertEqual(result.returncode, 2)
        listing = self.cli("list")
        self.assertIn(first, self.listed_ids(listing.stdout))
        self.assertEqual(self.cli("mv", str(first), "999").returncode, 2)

    def test_large_combined_output_does_not_deadlock(self) -> None:
        self.add("for i in (seq 1 12000); echo out-$i; echo err-$i >&2; end")
        result = self.cli("run", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("out-12000", result.stdout)
        self.assertIn("err-12000", result.stdout)

        process = subprocess.Popen(
            [str(SCHEDULE), "log"],
            cwd=self.work,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None
        process.stdout.read(100)
        process.stdout.close()
        stderr = process.stderr.read().decode(errors="replace") if process.stderr else ""
        if process.stderr:
            process.stderr.close()
        process.wait(timeout=10)
        self.assertNotIn("Traceback", stderr)

    def test_empty_run_recovers_and_clears_stale_status(self) -> None:
        self.add("true")
        self.assertEqual(self.cli("run").returncode, 0)
        connection = self.database()
        try:
            connection.execute(
                "UPDATE runs SET status = 'running', finished_at = NULL WHERE id = 1"
            )
            connection.commit()
        finally:
            connection.close()

        empty = self.cli("run")
        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertIn("Queue is empty", empty.stdout)
        history = self.cli("log", "--list")
        self.assertRegex(history.stdout, r"(?m)^1\s+interrupted\s+")
        cleared = self.cli("log", "--clear")
        self.assertIn("Cleared 1", cleared.stdout)

    def test_legacy_verbose_log_is_rendered_compactly(self) -> None:
        self.add('echo "hello"')
        self.assertEqual(self.cli("run").returncode, 0)
        connection = self.database()
        try:
            path = Path(connection.execute("SELECT log_path FROM runs WHERE id = 1").fetchone()[0])
        finally:
            connection.close()
        path.write_text(
            "=== schedule run 1 started 2026-07-17T13:00:00+00:00 mode=background ===\n"
            "\n"
            "--- command id=1 cwd=/tmp ---\n"
            '$ echo \\"hello\\"\n'
            "hello\n"
            "--- command id=1 exit=0 ---\n"
            "\n"
            "=== schedule run 1 finished 2026-07-17T13:00:01+00:00 "
            "status=succeeded attempted=1 failures=0 exit=0 ===\n"
        )
        rendered = self.cli("log", "1")
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertEqual(
            rendered.stdout,
            'run 1 · foreground\n[1] echo "hello"\nhello\n✓ succeeded · 1 command\n',
        )


if __name__ == "__main__":
    unittest.main()
