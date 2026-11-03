"""Shared call lifecycle policy for clinic and outreach phone sessions."""

TERMINAL_CALL_STATUSES = frozenset(
    {"completed", "failed", "busy", "no-answer", "cancelled"}
)
ACTIVE_CALL_STATUSES = frozenset(
    {"dialing", "unknown", "queued", "ringing", "in-progress"}
)
_PROVIDER_STATUS_RANK = {"queued": 0, "ringing": 1, "in-progress": 2}
PROVIDER_CALL_STATUSES = TERMINAL_CALL_STATUSES | _PROVIDER_STATUS_RANK.keys()


def should_advance_call_status(current: str, incoming: str) -> bool:
    """Accept provider confirmation without regressing or reopening a call.

    Repeated confirmations are accepted so they can clear an ambiguous dispatch
    error. A terminal outcome is final, even if another callback arrives later.
    """
    if current in TERMINAL_CALL_STATUSES or incoming not in PROVIDER_CALL_STATUSES:
        return False
    return incoming in TERMINAL_CALL_STATUSES or _PROVIDER_STATUS_RANK.get(
        incoming, -1
    ) >= _PROVIDER_STATUS_RANK.get(current, -1)
