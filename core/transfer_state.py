"""Process-global handle for the file-transfer mission runner.

The hosted Core seeds this in build_stack (after the mission, relay
and execution engines exist). Unit tests build explicit runners and
may seed or omit; functions return None honesty when unwired.
"""
from __future__ import annotations

_runner = None


def set_runner(runner) -> None:
    global _runner
    _runner = runner


def get_runner():
    return _runner


def reset() -> None:
    global _runner
    _runner = None
