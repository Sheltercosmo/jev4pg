"""Cooperative query cancellation with connection ownership scoped to one execution."""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import math
from threading import Event, RLock, Thread

from sqlalchemy import event


_current = ContextVar("sdd_query_control", default=None)


class QueryInterrupted(RuntimeError):
    def __init__(self, reason):
        self.operation_state = reason
        self.output_state = "NOT_EVALUATED"
        super().__init__("Query deadline exceeded" if reason == "TIMED_OUT" else "Query cancelled")


class QueryControl:
    """A single-use handle. cancel() may be called from another application thread."""

    def __init__(self, timeout_seconds=None):
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 3600
        ):
            raise ValueError("Query timeout must be finite and between 0 and 3600 seconds")
        self.timeout_seconds = timeout_seconds
        self._lock = RLock()
        self._connections = {}
        self._state = "READY"
        self._reason = None
        self._finished = Event()
        self.cancel_errors = []

    def cancel(self):
        return self._stop("CANCELLED")

    def _stop(self, reason):
        # Detachment uses the same lock; cancellation cannot reach a later pool borrower.
        with self._lock:
            if self._state == "FINISHED" or self._reason:
                return False
            self._reason = reason
            for driver, dialect in self._connections.values():
                try:
                    if dialect == "postgresql":
                        driver.cancel_safe(timeout=2)
                    elif dialect == "sqlite":
                        driver.interrupt()
                except Exception as exc:
                    self.cancel_errors.append(type(exc).__name__)
            return True

    def check(self):
        with self._lock:
            if self._reason:
                raise QueryInterrupted(self._reason)

    def continuing(self):
        with self._lock:
            return self._reason is None

    @contextmanager
    def activate(self):
        with self._lock:
            if self._state != "READY":
                raise ValueError("Use a new query control for each execution")
            self._state = "RUNNING"
        token = _current.set(self)
        timer = None

        def deadline():
            if not self._finished.wait(self.timeout_seconds):
                self._stop("TIMED_OUT")

        if self.timeout_seconds is not None:
            timer = Thread(target=deadline, name="sdd-query-deadline", daemon=True)
            timer.start()
        try:
            self.check()
            yield self
        except Exception:
            self.check()
            raise
        else:
            with self._lock:
                self.check()
                self._state = "FINISHED"
        finally:
            with self._lock:
                self._state = "FINISHED"
                self._finished.set()
            _current.reset(token)
            if timer is not None:
                timer.join(timeout=3)

    @contextmanager
    def bind(self, connection):
        driver = connection.connection.driver_connection
        if connection.dialect.name == "postgresql":
            import psycopg

            if not psycopg.capabilities.has_cancel_safe():
                raise ValueError("Controlled PostgreSQL queries require libpq 17 or newer")

        def checkpoint(*_):
            self.check()

        with self._lock:
            self.check()
            self._connections[id(connection)] = driver, connection.dialect.name
            event.listen(connection, "before_cursor_execute", checkpoint)
        try:
            yield connection
            self.check()
        except Exception:
            self.check()
            raise
        finally:
            with self._lock:
                event.remove(connection, "before_cursor_execute", checkpoint)
                self._connections.pop(id(connection), None)
                if self.cancel_errors:
                    connection.invalidate()


@contextmanager
def bind_source(connection):
    control = _current.get()
    if control is None:
        yield connection
    else:
        with control.bind(connection):
            yield connection


def checkpoint():
    control = _current.get()
    if control is not None:
        control.check()


def query_scope(function):
    @wraps(function)
    def execute(*args, **kwargs):
        control = kwargs.get("control")
        if kwargs.get("mutation_token") and (control is not None or _current.get() is not None):
            raise ValueError("Cancellation controls apply to reads and previews, not write commits")
        if control is None:
            return function(*args, **kwargs)
        if not isinstance(control, QueryControl):
            raise ValueError("control must be a QueryControl")
        progress = kwargs.get("progress")
        kwargs["progress"] = lambda: control.continuing() and (progress is None or progress())
        with control.activate():
            return function(*args, **kwargs)

    return execute
