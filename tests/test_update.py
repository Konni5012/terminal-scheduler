from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

import schedule_update as updater


COMMIT = "a" * 40


def bundle(suffix: bytes = b"") -> dict[str, bytes]:
    return {
        "schedule-core": b"#!/usr/bin/env python3\nprint('core')\n" + suffix,
        "schedule_supervisor.py": b"def main(arguments=None):\n    return 0\n" + suffix,
        "schedule_update.py": b"def main(arguments=None):\n    return 0\n" + suffix,
        "schedule": (
            b"#!/usr/bin/env python3\n"
            b"from schedule_supervisor import main as supervisor_main\n"
            b"from schedule_update import main as update_main\n" + suffix
        ),
    }


def fetcher(remote: dict[str, bytes], commit: str = COMMIT):
    responses = {updater.commit_url(): json.dumps({"sha": commit}).encode()}
    responses.update(
        {updater.raw_file_url(commit, name): data for name, data in remote.items()}
    )

    def fetch(url: str, _accept: str, _limit: int) -> bytes:
        return responses[url]

    return fetch


def install(directory: Path, files: dict[str, bytes]) -> None:
    for package_file in updater.PACKAGE_FILES:
        path = directory / package_file.name
        path.write_bytes(files[package_file.name])
        path.chmod(package_file.mode)


class UpdateTests(unittest.TestCase):
    def test_update_installs_the_pinned_bundle_and_modes(self) -> None:
        remote = bundle()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            install(directory, bundle(b"# old\n"))
            output = io.StringIO()
            with redirect_stdout(output):
                result = updater.run_update(
                    directory=directory,
                    fetcher=fetcher(remote),
                )
            self.assertEqual(result, 0)
            self.assertIn(COMMIT[:12], output.getvalue())
            for package_file in updater.PACKAGE_FILES:
                path = directory / package_file.name
                self.assertEqual(path.read_bytes(), remote[package_file.name])
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), package_file.mode)

    def test_noop_and_check_do_not_replace_files(self) -> None:
        remote = bundle()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            install(directory, remote)
            before = {
                item.name: (directory / item.name).stat().st_ino
                for item in updater.PACKAGE_FILES
            }
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    updater.run_update(directory=directory, fetcher=fetcher(remote)),
                    0,
                )
            after = {
                item.name: (directory / item.name).stat().st_ino
                for item in updater.PACKAGE_FILES
            }
            self.assertEqual(before, after)

            install(directory, bundle(b"# local\n"))
            snapshot = {
                item.name: (directory / item.name).read_bytes()
                for item in updater.PACKAGE_FILES
            }
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    updater.run_update(
                        check=True,
                        directory=directory,
                        fetcher=fetcher(remote),
                    ),
                    1,
                )
            self.assertEqual(
                snapshot,
                {
                    item.name: (directory / item.name).read_bytes()
                    for item in updater.PACKAGE_FILES
                },
            )

    def test_invalid_download_is_rejected_before_replacement(self) -> None:
        remote = bundle()
        remote["schedule_supervisor.py"] = b"def broken(:\n"
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            old = bundle(b"# old\n")
            install(directory, old)
            with self.assertRaisesRegex(updater.UpdateError, "invalid Python syntax"):
                updater.run_update(directory=directory, fetcher=fetcher(remote))
            self.assertEqual(
                {item.name: (directory / item.name).read_bytes() for item in updater.PACKAGE_FILES},
                old,
            )

    def test_failed_final_replace_rolls_back_every_file(self) -> None:
        remote = bundle()
        old = bundle(b"# old\n")
        failed = False

        def replace(source: str, destination: str) -> None:
            nonlocal failed
            source_path = Path(source)
            destination_path = Path(destination)
            if (
                destination_path.name == "schedule"
                and source_path.parent.name == "new"
                and not failed
            ):
                failed = True
                raise OSError("simulated failure")
            os.replace(source, destination)

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            install(directory, old)
            with self.assertRaisesRegex(updater.UpdateError, "could not install"):
                updater.install_bundle(directory, remote, replace=replace)
            self.assertEqual(
                {item.name: (directory / item.name).read_bytes() for item in updater.PACKAGE_FILES},
                old,
            )

    def test_unsafe_installations_and_parallel_updates_are_rejected(self) -> None:
        remote = bundle()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            install(directory, remote)
            (directory / ".git").mkdir()
            with self.assertRaisesRegex(updater.UpdateError, "Git checkout"):
                updater.bundle_differences(directory, remote)
            (directory / ".git").rmdir()

            target = directory / "real-core"
            target.write_bytes(remote["schedule-core"])
            (directory / "schedule-core").unlink()
            os.symlink(target, directory / "schedule-core")
            with self.assertRaisesRegex(updater.UpdateError, "symlinked"):
                updater.bundle_differences(directory, remote)

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with updater.update_lock(directory):
                with self.assertRaisesRegex(updater.UpdateError, "already running"):
                    with updater.update_lock(directory):
                        self.fail("the second updater acquired the lock")

    def test_invalid_commit_and_cli_errors_are_clean(self) -> None:
        def invalid_commit(_url: str, _accept: str, _limit: int) -> bytes:
            return b'{"sha": "main"}'

        with self.assertRaisesRegex(updater.UpdateError, "invalid main commit ID"):
            updater.latest_commit(invalid_commit)

        stderr = io.StringIO()
        original = updater.run_update
        updater.run_update = lambda **_kwargs: (_ for _ in ()).throw(
            updater.UpdateError("network gone")
        )
        try:
            with redirect_stderr(stderr):
                self.assertEqual(updater.main([]), 2)
        finally:
            updater.run_update = original
        self.assertIn("network gone", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_installer_and_entry_point_include_the_updater(self) -> None:
        root = Path(__file__).resolve().parents[1]
        installer = (root / "install.sh").read_text(encoding="utf-8")
        self.assertLess(
            installer.index('"$source_dir/schedule_update.py"'),
            installer.index('"$source_dir/schedule"'),
        )
        entry_point = (root / "schedule").read_text(encoding="utf-8")
        self.assertIn('arguments[0] == "update"', entry_point)
        self.assertIn("update_main(arguments[1:])", entry_point)


if __name__ == "__main__":
    unittest.main()
