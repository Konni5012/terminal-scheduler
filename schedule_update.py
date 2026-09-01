"""User-invoked updates for terminal-scheduler."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from typing import Callable, Dict, Iterator, Mapping, Optional, Sequence
import urllib.error
import urllib.parse
import urllib.request


REPOSITORY = "Konni5012/terminal-scheduler"
BRANCH = "main"
API_ROOT = "https://api.github.com"
RAW_ROOT = "https://raw.githubusercontent.com"
REQUEST_TIMEOUT_SECONDS = 20.0
MAX_API_BYTES = 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024


class UpdateError(Exception):
    """A user-facing update failure."""


@dataclass(frozen=True)
class PackageFile:
    name: str
    mode: int


# Replace the entry point last. Until that final rename, both the old entry
# point and every newly installed module remain a usable combination.
PACKAGE_FILES = (
    PackageFile("schedule-core", 0o755),
    PackageFile("schedule_supervisor.py", 0o644),
    PackageFile("schedule_update.py", 0o644),
    PackageFile("schedule", 0o755),
)

Fetcher = Callable[[str, str, int], bytes]
Replacer = Callable[[str, str], None]


def _repository_parts(repository: str) -> tuple[str, str]:
    pieces = repository.split("/")
    if len(pieces) != 2 or not all(
        re.fullmatch(r"[A-Za-z0-9_.-]+", piece or "") for piece in pieces
    ):
        raise UpdateError("the update repository must have the form owner/name")
    return pieces[0], pieces[1]


def commit_url(repository: str = REPOSITORY, branch: str = BRANCH) -> str:
    owner, name = _repository_parts(repository)
    return (
        f"{API_ROOT}/repos/{urllib.parse.quote(owner, safe='')}/"
        f"{urllib.parse.quote(name, safe='')}/commits/"
        f"{urllib.parse.quote(branch, safe='')}"
    )


def raw_file_url(
    commit: str,
    filename: str,
    repository: str = REPOSITORY,
) -> str:
    owner, name = _repository_parts(repository)
    return (
        f"{RAW_ROOT}/{urllib.parse.quote(owner, safe='')}/"
        f"{urllib.parse.quote(name, safe='')}/{commit}/"
        f"{urllib.parse.quote(filename, safe='/')}"
    )


def _download_bytes(url: str, accept: str, limit: int) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "Cache-Control": "no-cache",
            "User-Agent": "terminal-scheduler-self-update",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            final_url = response.geturl()
            parsed = urllib.parse.urlparse(final_url)
            if parsed.scheme != "https" or parsed.hostname not in {
                "api.github.com",
                "raw.githubusercontent.com",
            }:
                raise UpdateError(f"GitHub redirected the update to an unsafe URL: {final_url}")
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    declared_size = int(content_length)
                except ValueError:
                    declared_size = 0
                if declared_size > limit:
                    raise UpdateError(f"update response is too large: {url}")
            data = response.read(limit + 1)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise UpdateError(f"the update file was not found on GitHub: {url}") from error
        if error.code == 403:
            remaining = error.headers.get("X-RateLimit-Remaining", "")
            detail = " (GitHub API rate limit reached)" if remaining == "0" else ""
            raise UpdateError(f"GitHub refused the update request{detail}") from error
        raise UpdateError(
            f"GitHub returned HTTP {error.code} while checking for updates"
        ) from error
    except urllib.error.URLError as error:
        raise UpdateError(f"could not reach GitHub: {error.reason}") from error
    except TimeoutError as error:
        raise UpdateError("the GitHub update request timed out") from error
    except OSError as error:
        raise UpdateError(f"could not download the update: {error}") from error

    if len(data) > limit:
        raise UpdateError(f"update response is too large: {url}")
    return data


def latest_commit(
    fetcher: Fetcher = _download_bytes,
    repository: str = REPOSITORY,
    branch: str = BRANCH,
) -> str:
    payload = fetcher(commit_url(repository, branch), "application/vnd.github+json", MAX_API_BYTES)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise UpdateError("GitHub returned an invalid commit response") from error
    commit = value.get("sha") if isinstance(value, dict) else None
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise UpdateError("GitHub returned an invalid main commit ID")
    return commit.lower()


def download_bundle(
    commit: str,
    fetcher: Fetcher = _download_bytes,
    repository: str = REPOSITORY,
) -> Dict[str, bytes]:
    return {
        package_file.name: fetcher(
            raw_file_url(commit, package_file.name, repository),
            "application/octet-stream",
            MAX_FILE_BYTES,
        )
        for package_file in PACKAGE_FILES
    }


def validate_bundle(bundle: Mapping[str, bytes]) -> None:
    expected = {package_file.name for package_file in PACKAGE_FILES}
    if set(bundle) != expected:
        raise UpdateError("the downloaded update bundle is incomplete")

    decoded: Dict[str, str] = {}
    for package_file in PACKAGE_FILES:
        data = bundle[package_file.name]
        if not data:
            raise UpdateError(f"downloaded update file is empty: {package_file.name}")
        if len(data) > MAX_FILE_BYTES:
            raise UpdateError(f"downloaded update file is too large: {package_file.name}")
        if b"\x00" in data:
            raise UpdateError(f"downloaded update file contains a NUL byte: {package_file.name}")
        try:
            text = data.decode("utf-8")
        except UnicodeError as error:
            raise UpdateError(
                f"downloaded update file is not UTF-8: {package_file.name}"
            ) from error
        try:
            compile(text, package_file.name, "exec")
        except SyntaxError as error:
            location = f" line {error.lineno}" if error.lineno else ""
            raise UpdateError(
                f"downloaded update file has invalid Python syntax: "
                f"{package_file.name}{location}"
            ) from error
        decoded[package_file.name] = text

    for executable in ("schedule", "schedule-core"):
        if not decoded[executable].startswith("#!/usr/bin/env python3\n"):
            raise UpdateError(f"downloaded executable has an unexpected header: {executable}")
    if "schedule_update" not in decoded["schedule"]:
        raise UpdateError("downloaded entry point does not provide the update command")
    if "schedule_supervisor" not in decoded["schedule"]:
        raise UpdateError("downloaded entry point does not provide background supervision")


def install_directory() -> Path:
    return Path(__file__).resolve().parent


def _validate_install_directory(directory: Path) -> None:
    if not directory.is_dir():
        raise UpdateError(f"installation directory does not exist: {directory}")
    if (directory / ".git").exists():
        raise UpdateError(
            "refusing to overwrite a Git checkout; update the checkout with Git instead"
        )
    for package_file in PACKAGE_FILES:
        target = directory / package_file.name
        if target.is_symlink():
            raise UpdateError(
                f"refusing to replace a symlinked installation file: {target}"
            )
        if target.exists() and not target.is_file():
            raise UpdateError(f"installation target is not a regular file: {target}")


@contextmanager
def update_lock(directory: Path) -> Iterator[None]:
    path = directory / ".schedule-update.lock"
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        os.chmod(path, 0o600)
    except OSError as error:
        raise UpdateError(f"cannot lock the installation directory {directory}: {error}") from error
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise UpdateError("another schedule update is already running") from error
            raise UpdateError(f"cannot lock the updater: {error}") from error
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def bundle_differences(
    directory: Path,
    bundle: Mapping[str, bytes],
) -> list[PackageFile]:
    _validate_install_directory(directory)
    changed = []
    for package_file in PACKAGE_FILES:
        target = directory / package_file.name
        try:
            current = target.read_bytes()
            current_mode = stat.S_IMODE(target.stat().st_mode)
        except FileNotFoundError:
            changed.append(package_file)
            continue
        except OSError as error:
            raise UpdateError(f"could not inspect installed file {target}: {error}") from error
        if current != bundle[package_file.name] or current_mode != package_file.mode:
            changed.append(package_file)
    return changed


def _write_staged(path: Path, data: bytes, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as file:
            descriptor = -1
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            unsupported = {errno.EINVAL}
            not_supported = getattr(errno, "ENOTSUP", None)
            if not_supported is not None:
                unsupported.add(not_supported)
            if error.errno not in unsupported:
                raise
    finally:
        os.close(descriptor)


def install_bundle(
    directory: Path,
    bundle: Mapping[str, bytes],
    force: bool = False,
    replace: Replacer = os.replace,
) -> list[str]:
    validate_bundle(bundle)
    changed = list(PACKAGE_FILES) if force else bundle_differences(directory, bundle)
    if not changed:
        return []

    try:
        with tempfile.TemporaryDirectory(
            prefix=".schedule-update-", dir=str(directory)
        ) as temporary:
            temporary_root = Path(temporary)
            new_root = temporary_root / "new"
            backup_root = temporary_root / "backup"
            new_root.mkdir(mode=0o700)
            backup_root.mkdir(mode=0o700)

            backups: Dict[str, Optional[Path]] = {}
            original_modes: Dict[str, int] = {}
            for package_file in changed:
                target = directory / package_file.name
                staged = new_root / package_file.name
                _write_staged(staged, bundle[package_file.name], package_file.mode)
                if target.exists():
                    backup = backup_root / package_file.name
                    shutil.copy2(target, backup)
                    with backup.open("rb") as file:
                        os.fsync(file.fileno())
                    backups[package_file.name] = backup
                    original_modes[package_file.name] = stat.S_IMODE(target.stat().st_mode)
                else:
                    backups[package_file.name] = None

            replaced: list[PackageFile] = []
            try:
                for package_file in changed:
                    staged = new_root / package_file.name
                    target = directory / package_file.name
                    replace(str(staged), str(target))
                    replaced.append(package_file)
                    os.chmod(target, package_file.mode)
                _fsync_directory(directory)
            except BaseException as error:
                rollback_errors = []
                for package_file in reversed(replaced):
                    target = directory / package_file.name
                    backup = backups[package_file.name]
                    try:
                        if backup is None:
                            target.unlink(missing_ok=True)
                        else:
                            replace(str(backup), str(target))
                            os.chmod(target, original_modes[package_file.name])
                    except BaseException as rollback_error:
                        rollback_errors.append(
                            f"{package_file.name}: {rollback_error}"
                        )
                try:
                    _fsync_directory(directory)
                except OSError as rollback_error:
                    rollback_errors.append(f"directory sync: {rollback_error}")
                if rollback_errors:
                    details = "; ".join(rollback_errors)
                    raise UpdateError(
                        f"update failed and rollback was incomplete ({details})"
                    ) from error
                raise UpdateError(f"could not install the update: {error}") from error
    except UpdateError:
        raise
    except OSError as error:
        raise UpdateError(f"could not prepare the update in {directory}: {error}") from error

    return [package_file.name for package_file in changed]


def run_update(
    check: bool = False,
    force: bool = False,
    directory: Optional[Path] = None,
    fetcher: Fetcher = _download_bytes,
    repository: str = REPOSITORY,
    branch: str = BRANCH,
) -> int:
    target_directory = install_directory() if directory is None else directory.resolve()
    _validate_install_directory(target_directory)
    with update_lock(target_directory):
        commit = latest_commit(fetcher, repository, branch)
        bundle = download_bundle(commit, fetcher, repository)
        validate_bundle(bundle)
        changed = bundle_differences(target_directory, bundle)

        if not changed and not force:
            print(f"schedule is already up to date with {branch} at {commit[:12]}.")
            return 0
        if check:
            names = ", ".join(package_file.name for package_file in changed)
            print(f"An update is available from {branch} at {commit[:12]}: {names}")
            return 1

        installed = install_bundle(target_directory, bundle, force=force)
        names = ", ".join(installed)
        print(f"Updated schedule to {branch} commit {commit[:12]}: {names}")
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="schedule update",
        description="Update the installed schedule command from GitHub main.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--check",
        action="store_true",
        help="report whether an update is available without installing it",
    )
    group.add_argument(
        "--force",
        action="store_true",
        help="reinstall the current main bundle even when it already matches",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    parsed = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return run_update(check=parsed.check, force=parsed.force)
    except UpdateError as error:
        print(f"schedule: update error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
