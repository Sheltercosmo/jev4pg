"""Execute durable application queries without holding queue locks across inference."""

import logging
import signal
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Thread

from .db import Database
from .generic.query_jobs import QueryJobs
from .generic.results import OversizedResultRow
from .generic.sql import SQLService
from .query_control import QueryControl, QueryInterrupted


log = logging.getLogger(__name__)


class QueryWorker:
    def __init__(
        self,
        db,
        decisions=None,
        *,
        lease_seconds=30,
        poll_seconds=0.5,
        stop=None,
        heartbeat_path=None,
    ):
        if not 5 <= lease_seconds <= 3600 or not 0.05 <= poll_seconds <= lease_seconds / 3:
            raise ValueError("Invalid query worker lease or polling interval")
        self.metadata = Database(db.engine.url) if db.engine.dialect.name == "postgresql" else db
        self.owns_metadata = self.metadata is not db
        self.jobs, self.sql = QueryJobs(self.metadata), SQLService(db, decisions)
        self.lease_seconds, self.poll_seconds = lease_seconds, poll_seconds
        self.stop = stop or Event()
        self.heartbeat_path = Path(heartbeat_path) if heartbeat_path else None

    def close(self):
        if self.owns_metadata:
            self.metadata.engine.dispose()

    def touch(self):
        if self.heartbeat_path:
            self.heartbeat_path.touch()

    def work_one(self, tenant):
        if self.stop.is_set():
            return False
        job = self.jobs.claim(tenant, self.lease_seconds)
        self.touch()
        if job is None:
            return False
        control = QueryControl(job["request"]["timeout_seconds"])
        done, lost = Event(), Event()

        def monitor():
            while not done.is_set():
                try:
                    state = self.jobs.heartbeat(job, self.lease_seconds)
                    self.touch()
                except Exception as exc:
                    log.warning("Query job %s heartbeat failed: %s", job["id"], type(exc).__name__)
                    state = None
                if state is None:
                    lost.set()
                    control.cancel()
                    return
                if state == "CANCELLING" or self.stop.is_set():
                    control.cancel()
                if done.wait(self.poll_seconds):
                    return

        thread = Thread(target=monitor, name="jev-query-lease", daemon=True)
        thread.start()
        result, state, error = None, "SUCCEEDED", None
        try:
            options = {
                key: value
                for key, value in job["request"].items()
                if key not in ("parent_history_id", "timeout_seconds")
            }
            result = self.sql.execute(
                tenant,
                actor=job["actor"],
                control=control,
                expected_catalog=job["catalog_hash"],
                **options,
            )
        except QueryInterrupted as exc:
            state, error = exc.operation_state, exc.operation_state
        except OversizedResultRow:
            state, error = "FAILED", "RESULT_TOO_LARGE"
        except Exception as exc:
            state, error = "FAILED", type(exc).__name__
            log.warning("Query job %s failed: %s", job["id"], error)
        finally:
            if lost.is_set() or self.stop.is_set():
                state, result = "FAILED", None
                error = "LEASE_LOST" if lost.is_set() else "WORKER_STOPPED"
            try:
                if not self.jobs.finish(job, result, state=state, error=error):
                    log.warning("Query job %s lost ownership; result was not published", job["id"])
            finally:
                done.set()
                thread.join(timeout=12)
        return True


def run(db, decisions, tenants, *, concurrency=2, once=False, heartbeat_path=None):
    tenants = list(dict.fromkeys(tenants))
    if not tenants or any(not tenant or len(tenant) > 100 for tenant in tenants):
        raise ValueError("Configure at least one valid worker tenant")
    if not 1 <= concurrency <= 8:
        raise ValueError("Query worker concurrency must be between 1 and 8")
    stop, previous = Event(), {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, lambda *_: stop.set())

    def consume(offset):
        worker = QueryWorker(db, decisions, stop=stop, heartbeat_path=heartbeat_path)
        ordered = tenants[offset % len(tenants) :] + tenants[: offset % len(tenants)]
        try:
            while not stop.is_set():
                worked = False
                for tenant in ordered:
                    try:
                        worked = worker.work_one(tenant) or worked
                    except Exception as exc:
                        log.warning("Query queue unavailable: %s", type(exc).__name__)
                    if stop.is_set():
                        break
                if once:
                    return
                if not worked:
                    stop.wait(1)
        finally:
            worker.close()

    try:
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="jev-query") as pool:
            tasks = [pool.submit(consume, offset) for offset in range(concurrency)]
            for task in tasks:
                task.result()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
