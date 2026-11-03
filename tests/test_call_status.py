"""Pure lifecycle invariants shared by both phone services."""

import pytest

from app.business.call_status import (
    ACTIVE_CALL_STATUSES,
    PROVIDER_CALL_STATUSES,
    TERMINAL_CALL_STATUSES,
    should_advance_call_status,
)


@pytest.mark.parametrize("terminal", sorted(TERMINAL_CALL_STATUSES))
def test_finished_call_never_reopens(terminal):
    for incoming in PROVIDER_CALL_STATUSES:
        assert not should_advance_call_status(terminal, incoming)


@pytest.mark.parametrize("current", sorted(ACTIVE_CALL_STATUSES))
def test_terminal_confirmation_resolves_any_active_call(current):
    for terminal in TERMINAL_CALL_STATUSES:
        assert should_advance_call_status(current, terminal)


@pytest.mark.parametrize("current", ["dialing", "unknown"])
def test_provider_confirmation_resolves_unconfirmed_dispatch(current):
    for incoming in ["queued", "ringing", "in-progress"]:
        assert should_advance_call_status(current, incoming)


@pytest.mark.parametrize(
    ("current", "incoming", "expected"),
    [
        ("queued", "ringing", True),
        ("ringing", "in-progress", True),
        ("in-progress", "queued", False),
        ("in-progress", "ringing", False),
        ("ringing", "queued", False),
        ("in-progress", "in-progress", True),
        ("unknown", "pending", False),
        ("ringing", "unknown", False),
    ],
)
def test_callbacks_advance_monotonically(current, incoming, expected):
    assert should_advance_call_status(current, incoming) is expected
