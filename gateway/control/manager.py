from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock


@dataclass(frozen=True)
class ControlLease:
    client_id: str
    priority: int
    acquired_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class ControlAcquireResult:
    success: bool
    message: str
    lease: ControlLease | None
    previous_client_id: str | None = None

    #
    # Seconds after which trying again can succeed, when the
    # refusal is only a matter of waiting (startup grace, or a
    # higher-priority client contending for a free lease).
    #
    retry_after: float | None = None


@dataclass(frozen=True)
class _Attempt:
    priority: int
    at: datetime


class ControlManager:
    """
    The single control lease.

    A strictly higher priority preempts the holder. A free lease
    (none yet, expired or released) goes to the highest-priority
    client that is contending for it: a client is contending if it
    tried to acquire or renew within the last contention_seconds,
    and a lower-priority client is refused while a higher one is.
    Without this, every waiting client races for a lapsed lease
    and the first request wins, which can hand control to a lower
    tier for a moment before the right one preempts it.

    For the first startup_grace_seconds after the manager is
    created nobody gets the lease; attempts are only recorded, so
    that when the grace ends the highest client already trying
    wins instead of whichever reconnected first.
    """

    def __init__(
        self,
        lease_seconds: float,
        contention_seconds: float = 0.0,
        startup_grace_seconds: float = 0.0,
    ) -> None:
        self.lease_seconds = lease_seconds
        self.contention_seconds = contention_seconds
        self._lease: ControlLease | None = None
        self._attempts: dict[str, _Attempt] = {}
        self._lock = Lock()

        self._grace_until = self._now() + timedelta(
            seconds=startup_grace_seconds
        )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _remove_expired_locked(
        self,
        now: datetime,
    ) -> None:
        if (
            self._lease is not None
            and self._lease.expires_at <= now
        ):
            self._lease = None

    def _note_attempt_locked(
        self,
        client_id: str,
        priority: int,
        now: datetime,
    ) -> None:
        self._attempts[client_id] = _Attempt(
            priority=priority,
            at=now,
        )

    def _higher_contender_locked(
        self,
        priority: int,
        now: datetime,
    ) -> tuple[str, float] | None:
        """The highest-priority client above `priority` that tried
        within the contention window, and the seconds until it
        stops counting, or None."""

        window = timedelta(seconds=self.contention_seconds)

        contenders = [
            (attempt.priority, client_id, attempt.at)
            for client_id, attempt in self._attempts.items()
            if attempt.priority > priority
            and now - attempt.at < window
        ]

        if not contenders:
            return None

        _, client_id, at = max(contenders)

        return (
            client_id,
            (at + window - now).total_seconds(),
        )

    def get(self) -> ControlLease | None:
        with self._lock:
            now = self._now()

            self._remove_expired_locked(now)

            return self._lease

    async def acquire(
        self,
        client_id: str,
        priority: int,
    ) -> ControlAcquireResult:
        with self._lock:
            now = self._now()

            self._remove_expired_locked(now)

            self._note_attempt_locked(
                client_id,
                priority,
                now,
            )

            if now < self._grace_until:
                return ControlAcquireResult(
                    success=False,
                    message=(
                        "Gateway is starting; control is handed "
                        "out once the startup grace period ends"
                    ),
                    lease=None,
                    retry_after=(
                        self._grace_until - now
                    ).total_seconds(),
                )

            if self._lease is None:
                contender = self._higher_contender_locked(
                    priority,
                    now,
                )

                if contender is not None:
                    contender_id, retry_after = contender

                    return ControlAcquireResult(
                        success=False,
                        message=(
                            f"Higher-priority client "
                            f"{contender_id} is contending "
                            f"for control"
                        ),
                        lease=None,
                        retry_after=retry_after,
                    )

                self._lease = ControlLease(
                    client_id=client_id,
                    priority=priority,
                    acquired_at=now,
                    expires_at=now
                    + timedelta(seconds=self.lease_seconds),
                )

                return ControlAcquireResult(
                    success=True,
                    message="Control acquired",
                    lease=self._lease,
                )

            if self._lease.client_id == client_id:
                return ControlAcquireResult(
                    success=False,
                    message="Client already owns control; renew the lease instead",
                    lease=self._lease,
                )

            if priority > self._lease.priority:
                previous_client_id = self._lease.client_id

                self._lease = ControlLease(
                    client_id=client_id,
                    priority=priority,
                    acquired_at=now,
                    expires_at=now
                    + timedelta(seconds=self.lease_seconds),
                )

                return ControlAcquireResult(
                    success=True,
                    message="Control acquired",
                    lease=self._lease,
                    previous_client_id=previous_client_id,
                )

            return ControlAcquireResult(
                success=False,
                message=(
                    "Control is owned by an equal "
                    "or higher-priority client"
                ),
                lease=self._lease,
            )

    def renew(
        self,
        client_id: str,
        priority: int,
    ) -> tuple[bool, str, ControlLease | None]:
        with self._lock:
            now = self._now()

            self._remove_expired_locked(now)

            #
            # A client renewing a lease it lost is still alive and
            # wants control, so it counts as contending.
            #
            self._note_attempt_locked(
                client_id,
                priority,
                now,
            )

            if self._lease is None:
                return (
                    False,
                    "No active control lease",
                    None,
                )

            if self._lease.client_id != client_id:
                return (
                    False,
                    "Client does not own control",
                    self._lease,
                )

            self._lease = ControlLease(
                client_id=self._lease.client_id,
                priority=self._lease.priority,
                acquired_at=self._lease.acquired_at,
                expires_at=now
                + timedelta(seconds=self.lease_seconds),
            )

            return (
                True,
                "Control lease renewed",
                self._lease,
            )

    def release(
        self,
        client_id: str,
    ) -> tuple[bool, str]:
        with self._lock:
            now = self._now()

            self._remove_expired_locked(now)

            #
            # Releasing (even without holding the lease) withdraws
            # the client, so the next one need not wait for it.
            #
            self._attempts.pop(client_id, None)

            if self._lease is None:
                return (
                    False,
                    "No active control lease",
                )

            if self._lease.client_id != client_id:
                return (
                    False,
                    "Client does not own control",
                )

            self._lease = None

            return (
                True,
                "Control released",
            )

    def owns_control(
        self,
        client_id: str,
    ) -> bool:
        lease = self.get()

        return (
            lease is not None
            and lease.client_id == client_id
        )