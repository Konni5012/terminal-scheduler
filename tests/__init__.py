"""Test package configuration."""

import os


# Integration tests exercise the scheduler core, not the host's real user
# service manager. Supervisor behavior has isolated tests in test_supervisor.py.
os.environ.setdefault("SCHEDULE_SUPERVISOR", "off")
