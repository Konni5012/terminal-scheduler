# schedule

`schedule` is a persistent command queue for the Fish shell. It runs on macOS
and Linux using only Python 3.9+ and Fish.

## Install

```fish
./install.sh
fish_add_path ~/.local/bin
```

The installer honors `PREFIX`. The installed command consists of the `schedule`
entry point, `schedule-core`, `schedule_supervisor.py`, and
`schedule_update.py`; keep those four files together when installing without
the script.

## Update

Update the installed command from the latest commit on GitHub's `main` branch:

```fish
schedule update
```

The updater resolves `main` to one commit, downloads every command file from
that exact commit, validates the UTF-8 contents and Python syntax, stages the
complete bundle in the installation directory, and replaces the public entry
point last. If replacement fails, files already changed are rolled back. The
update runs only when explicitly invoked and requires network access plus write
permission for the installation directory.

Check without changing any files:

```fish
schedule update --check
```

The check exits with status `0` when the installed bundle matches `main` and
status `1` when an update is available. Use `schedule update --force` to
reinstall the current `main` bundle. A source checkout or symlinked installation
is not overwritten; update a checkout with Git or reinstall the standalone
command instead.

## Usage

Commands must be passed as one quoted Fish command string:

```fish
schedule add 'printf "first\\n"'
schedule add 'sleep 2; printf "second\\n"'
schedule list
schedule mv 1 2
schedule rm 1
```

Each command remembers the working directory from which it was added. IDs are
stable and monotonically increasing. `mv` swaps the queue positions of two IDs;
`rm` accepts one or more IDs.

Run the queue in the foreground:

```fish
schedule run
```

Foreground output is streamed to the terminal and saved to a log. Commands run
sequentially by default. The run stops at the first failed command. The
attempted command is removed, and commands not yet attempted remain queued.

Run all queued commands concurrently with:

```fish
schedule run --parralel
```

The correctly spelled `--parallel` alias is also accepted. Parallel runs
attempt every queued command; a failure makes the overall run fail after the
other commands finish. Output from concurrent commands may interleave. To
protect system resources, at most 32 commands run at once by default. Set
`SCHEDULE_MAX_PARALLEL` to a positive limit, or to `0` to launch every queued
command simultaneously. Bounded runs start later commands as slots open, so a
command that waits for every peer to start requires a sufficiently high limit
or `SCHEDULE_MAX_PARALLEL=0`.

Continue through failures with:

```fish
schedule run --no-exit-on-error
```

## Reboot-resilient background runs

Detach a run from the terminal and SSH session with:

```fish
schedule run --background
```

When a user service manager is available, `schedule` registers the run as a
persistent per-user service:

- Linux uses `systemd --user`. The worker and every command stay in the service
  cgroup even though individual commands have their own process groups.
- macOS uses a per-user `launchd` agent.

The registration and a private snapshot of the launch environment remain only
while the run is active. After a reboot, the service starts again when the user
service manager starts. Commands completed before the reboot are not repeated;
the command interrupted by the reboot starts again from the beginning, followed
by every command still queued at restart, including commands added while the
earlier run was active. Commands with external side effects should therefore be
safe to retry.

A normal Linux user manager starts at login. To resume before login as well,
enable lingering for the account once:

```fish
loginctl enable-linger $USER
```

A macOS LaunchAgent resumes when the user logs in. If no supported user service
is available, `schedule` prints a warning and uses the previous detached mode,
which survives logout but not reboot. Set `SCHEDULE_SUPERVISOR` to control this:

```fish
set -x SCHEDULE_SUPERVISOR auto     # default
set -x SCHEDULE_SUPERVISOR systemd  # require systemd --user
set -x SCHEDULE_SUPERVISOR launchd  # require a launchd user domain
set -x SCHEDULE_SUPERVISOR off      # always use detached mode
```

Forcing a manager fails instead of silently dropping reboot recovery when that
manager is unavailable. Once a manager is selected, setup failures are also
reported as errors rather than racing a second, unsupervised background run.

Background commands have no terminal input and write their output only to the
run log. Only one run can be active. While it runs, `add`, `list`, and `log`
remain available; `mv`, `rm`, and a second `run` are rejected. Commands added
during a run wait for the next run.

## Logs

Inspect logs with:

```fish
schedule log
schedule log 3
schedule log --list
schedule log --clear
```

`schedule log` displays the latest run. Completed logs are retained until
cleared. Logs combine stdout and stderr in observed order.

## State and environment

State is stored in `${XDG_STATE_HOME}/schedule`, or
`~/.local/state/schedule` when `XDG_STATE_HOME` is unset. Set
`SCHEDULE_STATE_DIR` to override the complete state directory, which is useful
for testing.

Every entry runs in a separate `fish -c` process. Commands inherit the
environment present when `schedule run` starts; shell-local changes made by one
entry do not carry into the next. For a supervised background run, that
environment and the invocation directory are saved in `supervisor.json` with
mode `0600` so they can be restored after reboot, then deleted when the run
finishes. Fish syntax is checked when a command is added, while command
availability and other runtime failures are determined during execution.
