# schedule

`schedule` is a persistent sequential command queue for the Fish shell. It runs
on macOS and Linux using only Python 3.9+ and Fish.

## Install

```fish
./install.sh
fish_add_path ~/.local/bin
```

The installer honors `PREFIX`. The executable is self-contained, so it may also
be copied directly to any directory in `PATH`.

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
stable and monotonically increasing. `mv` swaps the queue
positions of two IDs; `rm` accepts one or more IDs.

Run the queue in the foreground:

```fish
schedule run
```

Foreground output is streamed to the terminal and saved to a log. By default,
the run stops at the first failed command. The attempted command is removed,
and commands not yet attempted remain queued.

Continue through failures with:

```fish
schedule run --no-exit-on-error
```

Detach a run from the terminal and SSH session with:

```fish
schedule run --background
```

The command returns immediately. The detached worker and its commands have no
terminal input and write their output only to the run log. Detachment survives
an ordinary SSH disconnect or logout, but is not designed to survive a reboot
or a host policy that explicitly kills all user processes on logout.

Inspect logs with:

```fish
schedule log
schedule log 3
schedule log --list
schedule log --clear
```

`schedule log` displays the latest run. Completed logs are retained until
cleared. Logs combine stdout and stderr in observed order.

Only one run can be active. While it runs, `add`, `list`, and `log` remain
available; `mv`, `rm`, and a second `run` are rejected. Commands added during a
run wait for the next run.

## State and environment

State is stored in `${XDG_STATE_HOME}/schedule`, or
`~/.local/state/schedule` when `XDG_STATE_HOME` is unset. Set
`SCHEDULE_STATE_DIR` to override the complete state directory, which is useful
for testing.

Every entry runs in a separate `fish -c` process. Commands inherit the
environment present when `schedule run` starts; shell-local changes made by one
entry do not carry into the next. Fish syntax is checked when a command is
added, while command availability and other runtime failures are determined
during execution.

## Why did I make this?
I made it to be a simple way to schedule compute heavy jobs on a remote maschine, but you can feel free to tweak this to your liking if your usecase needs something different.
I also apologize if the code quality is bad as this was more of a vibe project.
