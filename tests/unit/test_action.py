"""Guard tests for the published GitHub Action (Sprint 22).

The action.yml is the distribution vehicle; these keep it from silently
rotting — valid YAML, expected inputs, and shell steps that only invoke
cgir subcommands that actually exist.
"""

from __future__ import annotations

import pathlib

import yaml

from cgir.cli import app

ACTION = pathlib.Path(__file__).resolve().parents[2] / "action.yml"


def _action() -> dict:
    return yaml.safe_load(ACTION.read_text())


def test_action_is_valid_yaml() -> None:
    data = _action()
    assert data["runs"]["using"] == "composite"
    assert data["runs"]["steps"]


def test_action_declares_expected_inputs() -> None:
    inputs = set(_action()["inputs"])
    assert {"paths", "exclude", "fail-on", "comment"} <= inputs


def test_action_only_calls_real_cgir_commands() -> None:
    commands = {
        (cmd.name or (cmd.callback.__name__ if cmd.callback else "")).replace("_", "-")
        for cmd in app.registered_commands
    }
    scripts = "\n".join(
        step.get("run", "") for step in _action()["runs"]["steps"] if step.get("shell") == "bash"
    )
    for line in scripts.splitlines():
        stripped = line.strip()
        if stripped.startswith("cgir "):
            sub = stripped.split()[1]
            assert sub in commands, f"action calls unknown `cgir {sub}`"
