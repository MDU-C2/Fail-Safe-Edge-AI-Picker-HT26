"""Keep terminal Stop responsive while one ordinary command is running."""

from concurrent.futures import ThreadPoolExecutor
import threading

from operator_commands import STOP_COMMANDS


class TerminalController:
    """No ordinary command queue; Stop has a separate, bounded worker.

    The session's stop latch inhibits new robot jobs synchronously, before the
    gateway request starts. Cancelling a proposal waits for the ordinary worker
    to leave the language controller; it never delays the stop request.
    """

    def __init__(self, session, language, handler, *, output=print):
        self.session = session
        self.language = language
        self.handler = handler
        self.output = output
        self._lock = threading.Lock()
        self._normal = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cups-command")
        self._stop = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cups-stop")
        self._normal_active = False
        self._stop_active = False
        self._cancel_pending = False
        self._closed = False

    @property
    def busy(self):
        with self._lock:
            return self._normal_active or self._stop_active

    def submit(self, line):
        """Return False to leave the input loop; never queue an ordinary action."""
        command = " ".join(line.strip().lower().split())
        if command in STOP_COMMANDS:
            self.request_stop()
            return True
        if command in ("status", "help"):
            self._handle(line)
            return True
        if command in ("q", "quit", "exit"):
            if self.busy:
                self.request_stop()
                self.output("Exiting after the active command and software stop request finish. No automatic home or resume.")
            else:
                self._cancel_proposal()
            return False
        with self._lock:
            if self._closed:
                self.output("Blocked: The terminal is closing.")
                return True
            if self._normal_active or self._stop_active:
                self.output("Blocked: A command is running; nothing was queued. Use stop, status, or help.")
                return True
            self._normal_active = True
            self._normal.submit(self._run_command, line)
        return True

    def _handle(self, line):
        try:
            return self.handler(line)
        except Exception as error:
            self.output(f"Blocked: {error}")
            if self.session.motion_uncertain:
                self.output("Inspect both arms and the cup before reset. No automatic retry or return home.")
            return True

    def _cancel_proposal(self):
        if self.language is None:
            return
        try:
            self.language.cancel()
        except Exception as error:
            self.output(f"Could not discard the pending proposal: {error}")

    def _run_command(self, line):
        try:
            self._handle(line)
        finally:
            # Stop can arrive while an LLM response or scan is completing. Keep
            # this worker marked active until its proposal is invalidated.
            while True:
                with self._lock:
                    if not self._cancel_pending:
                        self._normal_active = False
                        break
                    self._cancel_pending = False
                self._cancel_proposal()

    def request_stop(self):
        with self._lock:
            if self._closed:
                return
            if self._stop_active:
                self.output("Software stop request is already running. Use the physical emergency stop if immediate stopping is needed.")
                return
            self._stop_active = True
            self._cancel_pending = self._normal_active
            cancel_now = not self._normal_active
            try:
                self.session.latch_stop()
            except Exception as error:
                # Still try the gateway stop if writing local recovery state
                # fails. The session latches locally before persistence.
                self.output(f"Stop recovery state could not be saved: {error}")
            self.output("Stop requested; further robot jobs are blocked. Software stop depends on the gateway and controller.")
            self._stop.submit(self._run_stop)
        if cancel_now:
            self._cancel_proposal()

    def _run_stop(self):
        try:
            result = self.session.request_stop()
            self.output(result["message"])
            if not result.get("dry_run") and not result.get("stopped"):
                self.output("Robot stop is unconfirmed. Use the physical robot controls; recovery remains required.")
        except Exception as error:
            self.output(f"Robot stop is unconfirmed: {error}. Use the physical robot controls; recovery remains required.")
        finally:
            with self._lock:
                self._stop_active = False

    def close(self, *, interrupt=False):
        """An active exit requests Stop; an idle exit sends no robot command."""
        if interrupt and self.busy:
            self.request_stop()
        with self._lock:
            self._closed = True
        self._normal.shutdown(wait=True)
        self._stop.shutdown(wait=True)
