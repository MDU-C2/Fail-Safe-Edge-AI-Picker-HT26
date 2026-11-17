"""Single-owner control leases with optional session fencing and an active watchdog."""
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import math
import secrets
import threading
import time


class ControlConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class ControlLease:
    client_id: str
    priority: int
    acquired_at: datetime
    expires_at: datetime
    session_id: str
    deadline: float


@dataclass(frozen=True)
class ControlAcquireResult:
    success: bool
    message: str
    lease: ControlLease | None
    previous_client_id: str | None = None
    retry_after: float | None = None


@dataclass(frozen=True)
class _Attempt:
    priority: int
    at: float


class ControlAuthorization:
    """The exact acquisition, never another process sharing its client token."""
    def __init__(self, manager, client_id, session_id, require_clear=False):
        self.manager, self.client_id, self.session_id = manager, client_id, session_id
        self.require_clear = require_clear

    def __call__(self):
        with self.manager._lock:
            self.manager._check_locked(self.client_id, self.session_id, self.require_clear)

    @contextmanager
    def dispatch(self):
        # Hold only around the final dispatch, not robot movement or completion polling.
        with self.manager._lock:
            self.manager._check_locked(self.client_id, self.session_id, self.require_clear)
            yield

    @contextmanager
    def operation(self):
        with self.manager._lock:
            self.manager._check_locked(self.client_id, self.session_id, self.require_clear)
            self.manager._operations[self.session_id] = self.manager._operations.get(self.session_id, 0) + 1
        try:
            yield
        finally:
            with self.manager._lock:
                self.manager._operations[self.session_id] -= 1
                if not self.manager._operations[self.session_id]:
                    del self.manager._operations[self.session_id]


class ControlManager:
    """A fenced lease; free control goes to the highest active contender."""

    def __init__(self, lease_seconds: float, contention_seconds: float = 0.0,
                 startup_grace_seconds: float = 0.0, *, require_session: bool = False) -> None:
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("Control lease duration must be finite and positive")
        if any(not math.isfinite(value) or value < 0
               for value in (contention_seconds, startup_grace_seconds)):
            raise ValueError("Control contention and startup grace must be finite and non-negative")
        self.lease_seconds = lease_seconds
        self.contention_seconds = contention_seconds
        self._attempts: dict[str, _Attempt] = {}
        self._grace_until = self._monotonic() + startup_grace_seconds
        self.require_session = require_session
        self._lease = None
        self._lock = threading.RLock()
        self._operations = {}
        self.recovery_required = require_session
        self.recovery_reason = "Gateway restarted; inspect the robot and explicitly reset RAPID" if require_session else None
        self._revision = 0
        self._invalidate_callback = None
        self._stop_callback = None
        self._shutdown = threading.Event()
        self._stop_signal = threading.Event()
        self._stop_pending = False
        self._stop_in_progress = False
        self._threads = []

    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    @staticmethod
    def _monotonic():
        return time.monotonic()

    def _note_attempt_locked(self, client_id, priority, now):
        self._attempts[client_id] = _Attempt(priority, now)

    def _higher_contender_locked(self, priority, now):
        contenders = [(attempt.priority, client_id, attempt.at)
                      for client_id, attempt in self._attempts.items()
                      if attempt.priority > priority
                      and now - attempt.at < self.contention_seconds]
        if not contenders:
            return None
        _, client_id, at = max(contenders)
        return client_id, at + self.contention_seconds - now

    def _invalidate_locked(self, reason):
        self._lease = None
        if self.require_session:
            self.recovery_required = True
            self.recovery_reason = reason + "; software stop pending, explicit reset required"
            self._revision += 1
            if self._invalidate_callback:
                self._invalidate_callback()
            self._stop_pending = True
            self._stop_signal.set()

    def _remove_expired_locked(self, now=None):
        # UTC is reported for audit only. NTP/wall-clock changes cannot extend a lease.
        if self._lease is not None and self._lease.deadline <= self._monotonic():
            self._invalidate_locked("Control lease expired")

    def get(self):
        with self._lock:
            self._remove_expired_locked()
            return self._lease

    def _matches_locked(self, client_id, session_id):
        return (self._lease is not None and self._lease.client_id == client_id
                and (session_id == self._lease.session_id if session_id is not None else not self.require_session))

    def _check_locked(self, client_id, session_id, require_clear=False):
        self._remove_expired_locked()
        if not self._matches_locked(client_id, session_id):
            raise ControlConflict("The control lease expired or changed, or X-Control-Session is missing or stale")
        if require_clear and self.recovery_required:
            raise ControlConflict(self.recovery_reason or "Gateway recovery requires explicit RAPID reset")
        return self._lease

    def authorize(self, client_id, session_id=None, require_clear=False):
        with self._lock:
            lease = self._check_locked(client_id, session_id, require_clear)
            return ControlAuthorization(self, client_id, lease.session_id, require_clear)

    async def acquire(self, client_id, priority):
        with self._lock:
            self._remove_expired_locked()
            tick = self._monotonic()
            self._note_attempt_locked(client_id, priority, tick)
            if tick < self._grace_until:
                return ControlAcquireResult(
                    False, "Gateway is starting; control is handed out once the startup grace period ends",
                    None, retry_after=self._grace_until - tick)
            if self._lease is None:
                contender = self._higher_contender_locked(priority, tick)
                if contender is not None:
                    contender_id, retry_after = contender
                    return ControlAcquireResult(
                        False, f"Higher-priority client {contender_id} is contending for control",
                        None, retry_after=retry_after)
            previous_client_id = None
            if self._lease is not None:
                if self._lease.client_id == client_id:
                    return ControlAcquireResult(False, "Client already owns control; renew the lease instead", self._lease)
                if priority <= self._lease.priority:
                    return ControlAcquireResult(False, "Control is owned by an equal or higher-priority client", self._lease)
                previous_client_id = self._lease.client_id
                self._invalidate_locked("Control lease revoked by higher-priority acquisition")
            now = self._now()
            self._lease = ControlLease(client_id, priority, now, now + timedelta(seconds=self.lease_seconds),
                                       secrets.token_hex(16), self._monotonic() + self.lease_seconds)
            return ControlAcquireResult(True, "Control acquired", self._lease, previous_client_id)

    def renew(self, client_id, priority=None, *, session_id=None):
        # Older session-aware callers passed the string fence as argument two;
        # the original contention API used an integer priority there.
        if isinstance(priority, str) and session_id is None:
            session_id, priority = priority, None
        with self._lock:
            self._remove_expired_locked()
            if priority is None and self._lease is not None and self._lease.client_id == client_id:
                priority = self._lease.priority
            if priority is not None:
                self._note_attempt_locked(client_id, priority, self._monotonic())
            if not self._matches_locked(client_id, session_id):
                return False, "Client does not own control or X-Control-Session is missing or stale", self._lease
            self._lease = replace(self._lease, expires_at=self._now() + timedelta(seconds=self.lease_seconds),
                                  deadline=self._monotonic() + self.lease_seconds)
            return True, "Control lease renewed", self._lease

    def release(self, client_id, session_id=None):
        with self._lock:
            self._remove_expired_locked()
            # A failed release also withdraws a waiting client, but a stale
            # session must not withdraw the current same-token holder.
            if self._lease is None or self._lease.client_id != client_id or self._matches_locked(client_id, session_id):
                self._attempts.pop(client_id, None)
            if not self._matches_locked(client_id, session_id):
                return False, "Client does not own control or X-Control-Session is missing or stale"
            if self._operations.get(self._lease.session_id, 0):
                self._invalidate_locked("Control released during an outstanding robot action")
            else:
                self._lease = None
            return True, "Control released"

    def owns_control(self, client_id, session_id=None):
        with self._lock:
            self._remove_expired_locked()
            return self._matches_locked(client_id, session_id)

    def capabilities(self):
        with self._lock:
            return dict(session_required=self.require_session,
                        watchdog_enabled=self.require_session and bool(self._threads) and all(t.is_alive() for t in self._threads),
                        recovery_required=self.recovery_required, recovery_reason=self.recovery_reason)

    def ensure_reset_allowed(self):
        with self._lock:
            if self._stop_pending or self._stop_in_progress:
                raise ControlConflict("Wait for the gateway watchdog stop attempt to finish before resetting RAPID")

    def clear_recovery(self, authorization):
        with authorization.dispatch():
            self.ensure_reset_allowed()
            self.recovery_required = False
            self.recovery_reason = None

    def start_watchdog(self, invalidate_callback, stop_callback):
        if not self.require_session or self._threads:
            return
        self._invalidate_callback, self._stop_callback = invalidate_callback, stop_callback
        self._shutdown.clear()
        self._threads = [threading.Thread(target=self._watch_expiry, daemon=True, name="control-lease-watchdog"),
                         threading.Thread(target=self._perform_stops, daemon=True, name="control-watchdog-stop")]
        for thread in self._threads:
            thread.start()

    def _watch_expiry(self):
        while not self._shutdown.wait(min(0.1, self.lease_seconds / 4)):
            self.get()

    def _perform_stops(self):
        while not self._shutdown.is_set():
            if not self._stop_signal.wait(0.1):
                continue
            with self._lock:
                self._stop_signal.clear()
                if not self._stop_pending:
                    continue
                revision = self._revision
                self._stop_pending = False
                self._stop_in_progress = True
            try:
                self._stop_callback()
                outcome = "Software stop confirmed; inspect the robot and explicitly reset RAPID"
            except Exception as error:
                outcome = "Software stop UNCONFIRMED (" + type(error).__name__ + "); inspect controller and explicitly reset RAPID"
            finally:
                with self._lock:
                    self._stop_in_progress = False
                    if revision == self._revision:
                        self.recovery_reason = outcome

    def close(self):
        self._shutdown.set()
        self._stop_signal.set()
        for thread in self._threads:
            thread.join(timeout=0.5)
