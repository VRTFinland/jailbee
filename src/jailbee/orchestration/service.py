"""Orchestration broker service hooks."""

from __future__ import annotations


def ensure_running() -> None:
    """Make sure the orchestration broker is running.

    A no-op for now: plan D (the broker) fills it in. Callers may rely on it
    never raising.
    """
