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


class ControlManager:
    def __init__(self, lease_seconds: float) -> None:
        self.lease_seconds = lease_seconds
        self._lease: ControlLease | None = None
        self._lock = Lock()

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

            if self._lease is None:
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
    ) -> tuple[bool, str, ControlLease | None]:
        with self._lock:
            now = self._now()

            self._remove_expired_locked(now)

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