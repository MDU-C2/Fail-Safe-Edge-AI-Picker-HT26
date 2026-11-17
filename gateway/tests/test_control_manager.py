"""
ControlManager on its own, with a fake clock: the contention
window that gives a free lease to the highest client trying, and
the startup grace. No gateway process, so the timing is exact.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from gateway.control.manager import ControlManager


START = datetime(2026, 1, 1, tzinfo=timezone.utc)

LEASE = 5.0
WINDOW = 3.0

PRIORITY = {
    "admin": 1000,
    "primary": 300,
    "secondary": 200,
    "tertiary": 100,
}


class Clock:
    def __init__(self) -> None:
        self.now = START
        self.monotonic = 0.0

    def advance(
        self,
        seconds: float,
    ) -> None:
        self.now += timedelta(seconds=seconds)
        self.monotonic += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


def make_manager(
    clock: Clock,
    grace: float = 0.0,
) -> ControlManager:
    manager = ControlManager(
        lease_seconds=LEASE,
        contention_seconds=WINDOW,
        startup_grace_seconds=0.0,
    )

    manager._now = lambda: clock.now  # type: ignore[method-assign]
    manager._monotonic = lambda: clock.monotonic

    #
    # The grace is measured from creation, which used the real
    # clock; restart it on the fake one.
    #
    manager._grace_until = clock.monotonic + grace

    return manager


@pytest.fixture
def manager(
    clock: Clock,
) -> ControlManager:
    return make_manager(clock)


def acquire(
    manager: ControlManager,
    client_id: str,
):
    return asyncio.run(
        manager.acquire(
            client_id,
            PRIORITY[client_id],
        )
    )


def renew(
    manager: ControlManager,
    client_id: str,
) -> bool:
    return manager.renew(
        client_id,
        PRIORITY[client_id],
    )[0]


def holder(
    manager: ControlManager,
) -> str | None:
    lease = manager.get()

    return None if lease is None else lease.client_id


# ================================================================
# Contention for a free lease
# ================================================================

def test_first_client_gets_a_free_lease_when_nobody_else_tries(
    manager: ControlManager,
) -> None:
    assert acquire(manager, "tertiary").success
    assert holder(manager) == "tertiary"


def test_lower_tier_is_refused_while_a_higher_one_is_trying(
    manager: ControlManager,
    clock: Clock,
) -> None:
    assert acquire(manager, "primary").success
    assert not acquire(manager, "secondary").success

    #
    # Primary dies; its lease lapses. Secondary tried recently, so
    # tertiary asking first is refused and told when to retry.
    #
    clock.advance(LEASE - 1.0)
    assert not acquire(manager, "secondary").success

    clock.advance(1.0)
    assert holder(manager) is None

    result = acquire(manager, "tertiary")

    assert not result.success
    assert "secondary" in result.message
    assert result.retry_after == pytest.approx(WINDOW - 1.0)

    assert acquire(manager, "secondary").success
    assert holder(manager) == "secondary"


def test_a_crashed_holder_does_not_block_the_next_tier(
    manager: ControlManager,
    clock: Clock,
) -> None:
    assert acquire(manager, "primary").success

    #
    # Primary's last renew is a full lease ago when it lapses,
    # longer than the window, so secondary is not held back.
    #
    clock.advance(LEASE)

    assert acquire(manager, "secondary").success


def test_lower_tier_gets_the_lease_once_the_higher_one_stops_trying(
    manager: ControlManager,
    clock: Clock,
) -> None:
    assert not renew(manager, "secondary")

    assert not acquire(manager, "tertiary").success

    clock.advance(WINDOW)

    assert acquire(manager, "tertiary").success


def test_failed_renew_counts_as_contending(
    manager: ControlManager,
    clock: Clock,
) -> None:
    assert acquire(manager, "secondary").success

    clock.advance(LEASE)

    #
    # Secondary was partitioned and comes back renewing a lapsed
    # lease: refused, but it is alive, so tertiary waits for it.
    #
    assert not renew(manager, "secondary")
    assert not acquire(manager, "tertiary").success
    assert acquire(manager, "secondary").success


def test_release_withdraws_from_contention(
    manager: ControlManager,
) -> None:
    assert acquire(manager, "primary").success
    assert manager.release("primary")[0]

    #
    # Released, so secondary need not wait out primary's window.
    #
    assert acquire(manager, "secondary").success


def test_release_without_the_lease_also_withdraws(
    manager: ControlManager,
) -> None:
    assert not renew(manager, "secondary")
    assert not acquire(manager, "tertiary").success

    #
    # Secondary gives up: it holds nothing (409), but no longer
    # counts as contending.
    #
    assert not manager.release("secondary")[0]

    assert acquire(manager, "tertiary").success


def test_equal_or_lower_attempts_do_not_block(
    manager: ControlManager,
) -> None:
    assert not renew(manager, "tertiary")

    assert acquire(manager, "secondary").success


def test_preemption_is_unchanged(
    manager: ControlManager,
) -> None:
    assert acquire(manager, "tertiary").success
    assert not acquire(manager, "tertiary").success

    result = acquire(manager, "primary")

    assert result.success
    assert result.previous_client_id == "tertiary"

    #
    # Admin preempts primary while primary keeps renewing.
    #
    assert acquire(manager, "admin").success
    assert not renew(manager, "primary")
    assert holder(manager) == "admin"


def test_after_admin_releases_the_waiting_highest_tier_wins(
    manager: ControlManager,
) -> None:
    assert acquire(manager, "admin").success

    for client_id in ("tertiary", "secondary", "primary"):
        assert not acquire(manager, client_id).success

    assert manager.release("admin")[0]

    assert not acquire(manager, "tertiary").success
    assert not acquire(manager, "secondary").success
    assert acquire(manager, "primary").success


# ================================================================
# Startup grace
# ================================================================

def test_nobody_gets_the_lease_during_startup_grace(
    clock: Clock,
) -> None:
    manager = make_manager(clock, grace=5.0)

    result = acquire(manager, "admin")

    assert not result.success
    assert result.retry_after == pytest.approx(5.0)
    assert holder(manager) is None


def test_after_startup_grace_the_highest_client_trying_wins(
    clock: Clock,
) -> None:
    manager = make_manager(clock, grace=5.0)

    #
    # Clients reconnect lowest first, all inside the grace.
    #
    assert not acquire(manager, "tertiary").success

    clock.advance(1.5)
    assert not acquire(manager, "secondary").success

    clock.advance(1.5)
    assert not acquire(manager, "primary").success

    #
    # Grace over: the lower tiers asking first are refused because
    # primary tried 2 s ago; primary gets it.
    #
    clock.advance(2.0)

    assert not acquire(manager, "tertiary").success
    assert not acquire(manager, "secondary").success
    assert acquire(manager, "primary").success


def test_startup_grace_of_zero_hands_out_control_at_once(
    manager: ControlManager,
) -> None:
    assert acquire(manager, "tertiary").success


def test_wall_clock_changes_do_not_end_grace_or_contention(clock: Clock) -> None:
    manager = make_manager(clock, grace=5.0)
    assert not acquire(manager, "primary").success
    clock.now += timedelta(days=10)
    assert acquire(manager, "tertiary").retry_after == pytest.approx(5.0)
    clock.advance(4.0)
    assert not acquire(manager, "primary").success
    clock.now -= timedelta(days=20)
    clock.advance(1.0)
    refused = acquire(manager, "tertiary")
    assert not refused.success and "primary" in refused.message
    assert refused.retry_after == pytest.approx(2.0)
    assert acquire(manager, "primary").success


def test_fenced_preemption_keeps_priority_and_invalidates_old_session(clock: Clock) -> None:
    from gateway.control.manager import ControlConflict

    manager = ControlManager(LEASE, contention_seconds=WINDOW, require_session=True)
    manager._monotonic = lambda: clock.monotonic
    manager._grace_until = 0.0
    first = acquire(manager, "tertiary").lease
    authorization = manager.authorize("tertiary", first.session_id)
    manager.clear_recovery(authorization)
    next_lease = acquire(manager, "primary").lease
    assert next_lease.session_id != first.session_id
    assert manager.recovery_required
    with pytest.raises(ControlConflict):
        authorization()
    assert not manager.renew("tertiary", PRIORITY["tertiary"], session_id=first.session_id)[0]
    assert manager.renew("primary", PRIORITY["primary"], session_id=next_lease.session_id)[0]


def test_stale_same_token_release_does_not_withdraw_current_contender(clock: Clock) -> None:
    manager = make_manager(clock)
    first = acquire(manager, "primary").lease
    assert manager.release("primary", first.session_id)[0]
    current = acquire(manager, "primary").lease
    assert not manager.release("primary", first.session_id)[0]
    assert manager.owns_control("primary", current.session_id)
    assert manager._attempts["primary"].priority == PRIORITY["primary"]
