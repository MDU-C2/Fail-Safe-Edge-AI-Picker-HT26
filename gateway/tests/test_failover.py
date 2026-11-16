"""
Fail-safe chain with several clients running at once.

Each Device below behaves like a model device on the network: a
background thread that, every tick, renews the lease if it holds
it and otherwise tries to acquire it, and that sends a command
whenever it believes it is in control. A Watcher polls
GET /control as the read-only diagnostics client and records who
held the lease when.

Devices are then crashed (thread stops, lease left to lapse),
partitioned (stops talking, then comes back still believing it
holds control), shut down (lease released) and restarted, and the
tests check that the gateway always ends up with the highest
priority device that is alive, never hands control to a lower one
while a higher one is renewing, and refuses commands from every
device that is not the current holder.

The gateway hands a free lease only to the highest client that
tried within CONTENTION_SECONDS, so when a lease lapses the next
tier gets it even if a lower one asks first: holders are checked
exactly, with no lower tier getting in even for a moment. After
startup it hands out nothing for a grace period, so the highest
client reconnecting wins rather than the quickest.

The lease is LEASE_SECONDS (2 s) in the test gateways; devices
tick every TICK_SECONDS, well inside it and inside the contention
window.

The "command" is POST /calibration: like every route that acts on
the robot it is behind require_control (permission and lease), and
against the mock gateways it answers 200 for the holder and 409 for
anyone else, in both implementations (the C++ gateway has no
legacy /robot/command; the RWS moves answer 503 without a
controller).
"""

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from gateway.tests.conftest import mock_settings
from gateway.tests.support import (
    LEASE_SECONDS,
    Client,
    Gateway,
    expect,
)


TICK_SECONDS = 0.25

#
# What a device does while it holds the lease (see the docstring).
#
CONTROLLED_PATH = "/calibration"

#
# A crashed holder's lease lapses after LEASE_SECONDS; the next
# device notices on its next tick. The rest is scheduling slack.
#
FAILOVER_TIMEOUT_SECONDS = LEASE_SECONDS + 2.0

#
# Preemption and handover after a release need no lease to lapse,
# only a tick or two.
#
PREEMPT_TIMEOUT_SECONDS = 1.5

TIERS = ("primary", "secondary", "tertiary")

#
# Startup grace of the gateway in the startup test: long enough for
# all devices to be trying before it ends.
#
STARTUP_GRACE_SECONDS = 8.0


# ================================================================
# Simulated devices
# ================================================================

class Device:
    """One model device competing for control."""

    def __init__(
        self,
        gateway: Gateway,
        client_id: str,
    ) -> None:
        self.client_id = client_id
        self.client: Client = gateway.client(client_id)

        self.holding = False

        self.acquired = 0
        self.lost = 0
        self.commands_accepted = 0
        self.commands_refused = 0
        self.unexpected: list[str] = []

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------
    # Life cycle
    # ------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None

    def start(self) -> None:
        assert not self.running, f"{self.client_id} already running"

        self._stop.clear()

        self._thread = threading.Thread(
            target=self._run,
            name=f"device-{self.client_id}",
            daemon=True,
        )

        self._thread.start()

    def _halt(self) -> None:
        if self._thread is None:
            return

        self._stop.set()
        self._thread.join()
        self._thread = None

    def crash(self) -> None:
        """Stop without releasing: the lease is left to lapse."""

        self._halt()
        self.holding = False

    def partition(self) -> None:
        """Stop talking but keep believing it holds control, like a
        device cut off from the network that later reconnects."""

        self._halt()

    def shutdown(self) -> None:
        """Stop cleanly, releasing control if held."""

        self._halt()

        if self.holding:
            self.client.post("/control/release")

        self.holding = False

    # ------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()

            except Exception as error:  # noqa: BLE001
                self.unexpected.append(repr(error))

            self._stop.wait(TICK_SECONDS)

    def _tick(self) -> None:
        if self.holding:
            response = self.client.post("/control/renew", timeout=5)

            if response.status_code == 200:
                self._command()
                return

            self._record(response, 409, "renew")

            with self._lock:
                self.holding = False
                self.lost += 1

        response = self.client.post("/control/acquire", timeout=5)

        if response.status_code == 200:
            with self._lock:
                self.holding = True
                self.acquired += 1

            self._command()
            return

        self._record(response, 409, "acquire")

    def _command(self) -> None:
        response = self.client.post(
            CONTROLLED_PATH,
            json={"work": self.client_id},
            timeout=5,
        )

        if response.status_code == 200:
            with self._lock:
                self.commands_accepted += 1
            return

        self._record(response, 409, "command")

        #
        # Preempted between the renew and the command: the gateway
        # refused, so the device must stop acting as the holder.
        #
        with self._lock:
            self.commands_refused += 1
            self.holding = False
            self.lost += 1

    def _record(
        self,
        response,
        expected: int,
        what: str,
    ) -> None:
        if response.status_code != expected:
            self.unexpected.append(
                f"{what}: {response.status_code} {response.text[:200]}"
            )

    def counters(self) -> tuple[int, int]:
        with self._lock:
            return self.acquired, self.commands_accepted


# ================================================================
# Watching the lease
# ================================================================

class Watcher:
    """Polls GET /control and keeps the history of holders."""

    def __init__(
        self,
        gateway: Gateway,
    ) -> None:
        self.client = gateway.client("diagnostics")

        #
        # (monotonic time, holder or None), one entry per change.
        #
        self.history: list[tuple[float, str | None]] = []

        self._lock = threading.Lock()
        self._sample_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="watcher",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.sample()
            self._stop.wait(0.05)

    def sample(self) -> str | None:
        #
        # One request at a time (the background thread and wait_for
        # both sample), so a slow reply is never recorded after a
        # newer one.
        #
        with self._sample_lock:
            lease = expect(self.client.get("/control", timeout=5), 200)

            holder = lease["client_id"] if lease["active"] else None

            with self._lock:
                if not self.history or self.history[-1][1] != holder:
                    self.history.append((time.monotonic(), holder))

        return holder

    def mark(self) -> float:
        return time.monotonic()

    def holders_since(
        self,
        since: float,
    ) -> list[str | None]:
        """Holders seen since `since`, in order, including the one
        holding at that moment."""

        with self._lock:
            holders = [
                holder
                for at, holder in self.history
                if at >= since
            ]

            before = [
                holder
                for at, holder in self.history
                if at < since
            ]

        if before:
            holders.insert(0, before[-1])

        return holders

    def wait_for(
        self,
        holder: str | None,
        timeout: float,
    ) -> float:
        """Wait until `holder` holds the lease; returns how long
        that took."""

        start = time.monotonic()
        deadline = start + timeout

        while True:
            current = self.sample()

            if current == holder:
                return time.monotonic() - start

            if time.monotonic() > deadline:
                raise AssertionError(
                    f"expected {holder!r} in control within "
                    f"{timeout:.1f} s, still {current!r}; "
                    f"history: {self.describe()}"
                )

            time.sleep(0.05)

    def hold_steady(
        self,
        holder: str | None,
        seconds: float,
    ) -> None:
        """Assert nobody but `holder` holds the lease for `seconds`."""

        since = self.mark()

        time.sleep(seconds)

        seen = self.holders_since(since)

        assert seen == [holder], (
            f"expected only {holder!r} for {seconds:.1f} s, "
            f"saw {seen}; history: {self.describe()}"
        )

    def describe(self) -> str:
        with self._lock:
            if not self.history:
                return "[]"

            origin = self.history[0][0]

            return ", ".join(
                f"{at - origin:.2f}s {holder}"
                for at, holder in self.history
            )


def without_gaps(
    holders: list[str | None],
) -> list[str]:
    """Holders without the moments of nobody in control (whether
    the watcher catches those is down to timing)."""

    return [
        holder
        for holder in holders
        if holder is not None
    ]


@contextmanager
def fleet(
    gateway: Gateway,
    *client_ids: str,
) -> Iterator[tuple[dict[str, Device], Watcher]]:
    """Devices (not yet started) and a running watcher. Every
    device is shut down afterwards, and none may have seen an
    unexpected reply."""

    devices = {
        client_id: Device(gateway, client_id)
        for client_id in client_ids
    }

    watcher = Watcher(gateway)
    watcher.start()

    try:
        yield devices, watcher

    finally:
        for device in devices.values():
            device.shutdown()

        watcher.stop()

    for device in devices.values():
        assert not device.unexpected, (
            f"{device.client_id}: {device.unexpected[:5]}"
        )


# ================================================================
# Start-up
# ================================================================

@pytest.mark.parametrize(
    "start_order",
    [
        ("tertiary", "secondary", "primary"),
        ("primary", "secondary", "tertiary"),
        ("secondary", "tertiary", "primary"),
    ],
)
def test_highest_priority_ends_up_in_control_whatever_the_start_order(
    strict_gateway: Gateway,
    start_order: tuple[str, ...],
) -> None:
    with fleet(strict_gateway, *TIERS) as (devices, watcher):
        for client_id in start_order:
            devices[client_id].start()

            #
            # Whoever is the best device started so far takes over.
            #
            best = min(
                (c for c in TIERS if devices[c].running),
                key=TIERS.index,
            )

            watcher.wait_for(best, PREEMPT_TIMEOUT_SECONDS)

        #
        # The lower tiers keep trying all the time; while primary
        # renews they must never get in.
        #
        watcher.hold_steady("primary", LEASE_SECONDS * 2)


def test_all_devices_starting_at_once_settle_on_primary(
    strict_gateway: Gateway,
) -> None:
    with fleet(strict_gateway, *TIERS) as (devices, watcher):
        threads = [
            threading.Thread(target=device.start)
            for device in devices.values()
        ]

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join()

        watcher.wait_for("primary", PREEMPT_TIMEOUT_SECONDS)
        watcher.hold_steady("primary", LEASE_SECONDS * 2)


def test_after_startup_grace_primary_gets_control_first(
    tmp_path,
) -> None:
    """
    A gateway (re)starting with every device already trying: the
    quickest device must not get control, primary must, and nobody
    holds it during the grace period.
    """

    gateway = Gateway(
        tmp_path,
        "strict",
        {
            **mock_settings(),
            "GATEWAY_CONTROL_STARTUP_GRACE_SECONDS": str(STARTUP_GRACE_SECONDS),
        },
    )

    gateway.start()

    try:
        with fleet(gateway, *TIERS) as (devices, watcher):
            #
            # Lowest first, so without the grace tertiary would get
            # control and then be preempted twice.
            #
            devices["tertiary"].start()

            refused = expect_refused_with_retry_after(gateway, "secondary")

            assert 0 < refused <= STARTUP_GRACE_SECONDS

            time.sleep(TICK_SECONDS * 2)
            devices["secondary"].start()
            time.sleep(TICK_SECONDS * 2)
            devices["primary"].start()

            assert watcher.sample() is None

            watcher.wait_for("primary", STARTUP_GRACE_SECONDS + 2.0)
            watcher.hold_steady("primary", LEASE_SECONDS)

            assert without_gaps(watcher.holders_since(0.0)) == ["primary"], (
                watcher.describe()
            )

            assert devices["secondary"].acquired == 0
            assert devices["tertiary"].acquired == 0

    finally:
        gateway.stop()


def expect_refused_with_retry_after(
    gateway: Gateway,
    client_id: str,
) -> int:
    response = gateway.client(client_id).post("/control/acquire")

    expect(response, 409)

    #
    # Only asking: withdraw again so this client does not count as
    # contending.
    #
    gateway.client(client_id).post("/control/release")

    return int(response.headers["Retry-After"])


# ================================================================
# Failover and failback
# ================================================================

def test_crashes_cascade_down_the_chain_and_restarts_climb_back(
    strict_gateway: Gateway,
) -> None:
    with fleet(strict_gateway, *TIERS) as (devices, watcher):
        for device in devices.values():
            device.start()

        watcher.wait_for("primary", PREEMPT_TIMEOUT_SECONDS)

        #
        # Tertiary may have got in at start-up before the others
        # asked; only what happens from here on counts.
        #
        tertiary_acquired = devices["tertiary"].acquired

        #
        # Down: each crash hands control to the next tier once the
        # crashed one's lease has lapsed, never skipping a tier.
        #
        since = watcher.mark()

        devices["primary"].crash()
        took = watcher.wait_for("secondary", FAILOVER_TIMEOUT_SECONDS)
        assert took >= LEASE_SECONDS - 0.5, "took over before the lease lapsed"
        watcher.hold_steady("secondary", LEASE_SECONDS)

        assert devices["tertiary"].acquired == tertiary_acquired

        devices["secondary"].crash()
        watcher.wait_for("tertiary", FAILOVER_TIMEOUT_SECONDS)
        watcher.hold_steady("tertiary", LEASE_SECONDS)

        devices["tertiary"].crash()
        watcher.wait_for(None, FAILOVER_TIMEOUT_SECONDS)
        watcher.hold_steady(None, LEASE_SECONDS)

        assert without_gaps(watcher.holders_since(since)) == [
            "primary",
            "secondary",
            "tertiary",
        ], watcher.describe()

        assert watcher.holders_since(since)[-1] is None

        #
        # Up: each restarted higher tier preempts at once, without
        # waiting for a lease to lapse.
        #
        since = watcher.mark()

        devices["tertiary"].start()
        watcher.wait_for("tertiary", PREEMPT_TIMEOUT_SECONDS)

        devices["secondary"].start()
        watcher.wait_for("secondary", PREEMPT_TIMEOUT_SECONDS)

        devices["primary"].start()
        watcher.wait_for("primary", PREEMPT_TIMEOUT_SECONDS)
        watcher.hold_steady("primary", LEASE_SECONDS)

        assert watcher.holders_since(since) == [
            None,
            "tertiary",
            "secondary",
            "primary",
        ], watcher.describe()


def test_returning_primary_takes_back_control_and_the_others_stay_out(
    strict_gateway: Gateway,
) -> None:
    with fleet(strict_gateway, *TIERS) as (devices, watcher):
        for device in devices.values():
            device.start()

        watcher.wait_for("primary", PREEMPT_TIMEOUT_SECONDS)

        devices["primary"].crash()
        watcher.wait_for("secondary", FAILOVER_TIMEOUT_SECONDS)

        lost_before = devices["secondary"].lost

        devices["primary"].start()
        watcher.wait_for("primary", PREEMPT_TIMEOUT_SECONDS)
        watcher.hold_steady("primary", LEASE_SECONDS * 2)

        #
        # Secondary noticed it was preempted (its renew or command
        # was refused) and stopped acting as the holder.
        #
        assert devices["secondary"].lost > lost_before
        assert not devices["secondary"].holding


def test_a_lower_tier_restarting_does_not_disturb_the_holder(
    strict_gateway: Gateway,
) -> None:
    with fleet(strict_gateway, *TIERS) as (devices, watcher):
        devices["primary"].start()
        devices["secondary"].start()

        watcher.wait_for("primary", PREEMPT_TIMEOUT_SECONDS)

        since = watcher.mark()

        #
        # Secondary may have got in before primary's first request
        # at start-up; only what happens from here on counts.
        #
        acquired_before = {
            client_id: devices[client_id].acquired
            for client_id in ("secondary", "tertiary")
        }

        for _ in range(3):
            devices["tertiary"].start()
            time.sleep(TICK_SECONDS * 3)
            devices["tertiary"].crash()

            devices["secondary"].crash()
            time.sleep(TICK_SECONDS * 2)
            devices["secondary"].start()

        watcher.hold_steady("primary", LEASE_SECONDS)

        assert watcher.holders_since(since) == ["primary"], watcher.describe()

        for client_id, acquired in acquired_before.items():
            assert devices[client_id].acquired == acquired, client_id


def test_flapping_primary_always_gets_control_back(
    strict_gateway: Gateway,
) -> None:
    with fleet(strict_gateway, *TIERS) as (devices, watcher):
        for device in devices.values():
            device.start()

        watcher.wait_for("primary", PREEMPT_TIMEOUT_SECONDS)

        #
        # Tertiary may have got in at start-up before the others
        # asked; only what happens from here on counts.
        #
        tertiary_acquired = devices["tertiary"].acquired

        since = watcher.mark()

        for _ in range(3):
            devices["primary"].crash()
            watcher.wait_for("secondary", FAILOVER_TIMEOUT_SECONDS)

            devices["primary"].start()
            watcher.wait_for("primary", PREEMPT_TIMEOUT_SECONDS)

        watcher.hold_steady("primary", LEASE_SECONDS)

        #
        # Secondary was always alive to cover for primary, so
        # tertiary never got in, not even for a moment.
        #
        assert without_gaps(watcher.holders_since(since)) == (
            ["primary", "secondary"] * 3 + ["primary"]
        ), watcher.describe()

        assert devices["tertiary"].acquired == tertiary_acquired


def test_clean_shutdown_hands_over_without_waiting_for_the_lease(
    strict_gateway: Gateway,
) -> None:
    with fleet(strict_gateway, *TIERS) as (devices, watcher):
        for device in devices.values():
            device.start()

        watcher.wait_for("primary", PREEMPT_TIMEOUT_SECONDS)

        #
        # Tertiary may have got in at start-up before the others
        # asked; only what happens from here on counts.
        #
        tertiary_acquired = devices["tertiary"].acquired

        devices["primary"].shutdown()
        took = watcher.wait_for("secondary", PREEMPT_TIMEOUT_SECONDS)
        assert took < LEASE_SECONDS
        assert devices["tertiary"].acquired == tertiary_acquired

        devices["secondary"].shutdown()
        took = watcher.wait_for("tertiary", PREEMPT_TIMEOUT_SECONDS)
        assert took < LEASE_SECONDS

        devices["tertiary"].shutdown()
        watcher.wait_for(None, PREEMPT_TIMEOUT_SECONDS)


def test_partitioned_holder_coming_back_is_refused_then_retakes_control(
    strict_gateway: Gateway,
) -> None:
    with fleet(strict_gateway, "primary", "secondary") as (devices, watcher):
        devices["primary"].start()
        devices["secondary"].start()

        watcher.wait_for("primary", PREEMPT_TIMEOUT_SECONDS)

        #
        # Primary goes quiet still thinking it holds control; its
        # lease lapses and secondary takes over.
        #
        devices["primary"].partition()
        assert devices["primary"].holding

        watcher.wait_for("secondary", FAILOVER_TIMEOUT_SECONDS)

        #
        # Back on the network, its stale lease is refused (renew
        # 409), so it re-acquires and, being higher, preempts.
        #
        lost_before = devices["primary"].lost
        acquired_before = devices["primary"].acquired

        devices["primary"].start()
        watcher.wait_for("primary", PREEMPT_TIMEOUT_SECONDS)

        assert devices["primary"].lost == lost_before + 1
        assert devices["primary"].acquired == acquired_before + 1


def test_partitioned_lower_holder_coming_back_stays_out(
    strict_gateway: Gateway,
) -> None:
    with fleet(strict_gateway, *TIERS) as (devices, watcher):
        devices["secondary"].start()
        devices["tertiary"].start()

        watcher.wait_for("secondary", PREEMPT_TIMEOUT_SECONDS)

        devices["secondary"].partition()
        watcher.wait_for("tertiary", FAILOVER_TIMEOUT_SECONDS)

        devices["primary"].start()
        watcher.wait_for("primary", PREEMPT_TIMEOUT_SECONDS)

        #
        # Secondary returns believing it still holds control. It is
        # refused and, being lower than primary, stays out.
        #
        devices["secondary"].start()
        watcher.hold_steady("primary", LEASE_SECONDS * 2)

        assert not devices["secondary"].holding


# ================================================================
# Commands and the admin override
# ================================================================

def test_only_the_holder_gets_commands_through(
    strict_gateway: Gateway,
) -> None:
    with fleet(strict_gateway, *TIERS) as (devices, watcher):
        for device in devices.values():
            device.start()

        watcher.wait_for("primary", PREEMPT_TIMEOUT_SECONDS)

        #
        # From here on only primary may get anything through. The
        # others keep hammering acquire; any command they send
        # would have to be after a successful acquire, so their
        # counters must not move.
        #
        before = {
            client_id: device.counters()
            for client_id, device in devices.items()
        }

        watcher.hold_steady("primary", LEASE_SECONDS * 2)

        for client_id in ("secondary", "tertiary"):
            assert devices[client_id].counters() == before[client_id]

        assert devices["primary"].commands_accepted > before["primary"][1]

        #
        # Direct commands from the waiting tiers are refused too.
        #
        for client_id in ("secondary", "tertiary"):
            expect(
                devices[client_id].client.post(
                    CONTROLLED_PATH,
                    json={"work": "sneak"},
                ),
                409,
            )


def test_admin_overrides_every_tier_and_hands_back_on_release(
    strict_gateway: Gateway,
) -> None:
    with fleet(strict_gateway, *TIERS) as (devices, watcher):
        for device in devices.values():
            device.start()

        watcher.wait_for("primary", PREEMPT_TIMEOUT_SECONDS)

        #
        # Tertiary may have got in at start-up before the others
        # asked; only what happens from here on counts.
        #
        tertiary_acquired = devices["tertiary"].acquired

        admin = strict_gateway.client("admin")

        with admin.control():
            watcher.wait_for("admin", PREEMPT_TIMEOUT_SECONDS)
            watcher.hold_steady("admin", LEASE_SECONDS * 2)

            for device in devices.values():
                assert not device.holding

            #
            # Even with primary gone, nobody gets past admin.
            #
            devices["primary"].crash()
            watcher.hold_steady("admin", LEASE_SECONDS)

        #
        # Admin released: the best device still alive takes over
        # straight away, and primary takes it back when it returns.
        #
        watcher.wait_for("secondary", PREEMPT_TIMEOUT_SECONDS)

        assert devices["tertiary"].acquired == tertiary_acquired

        devices["primary"].start()
        watcher.wait_for("primary", PREEMPT_TIMEOUT_SECONDS)
        watcher.hold_steady("primary", LEASE_SECONDS)
