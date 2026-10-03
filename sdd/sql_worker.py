"""Claim committed SQL jobs and dispatch them through the shared operator runtime."""

import json
import logging
import signal
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Thread

from sqlalchemy import text
from .operators.service import OperatorService

log = logging.getLogger(__name__)


class SQLWorker:
    def __init__(self, db, decisions, *, lease_seconds=60, heartbeat_path=None):
        if not 10 <= lease_seconds <= 3600:
            raise ValueError("Worker lease must be between 10 and 3600 seconds")
        self.db, self.decisions = db, decisions
        self.lease_seconds = lease_seconds
        self.worker_id = str(uuid.uuid4())
        self.heartbeat_path = Path(heartbeat_path) if heartbeat_path else None

    def touch(self):
        if self.heartbeat_path:
            self.heartbeat_path.touch()

    def claim(self):
        with self.db.engine.begin() as connection:
            job = connection.execute(
                text("SELECT jev._claim(CAST(:worker AS uuid), :lease)"),
                {"worker": self.worker_id, "lease": self.lease_seconds},
            ).scalar_one()
        self.touch()
        return job

    def heartbeat(self, job_id):
        with self.db.engine.begin() as connection:
            alive = connection.execute(
                text("SELECT jev._heartbeat(CAST(:job AS uuid), CAST(:worker AS uuid), :lease)"),
                {"job": job_id, "worker": self.worker_id, "lease": self.lease_seconds},
            ).scalar_one()
        self.touch()
        return alive

    def finish(self, job_id, result):
        with self.db.engine.begin() as connection:
            return connection.execute(
                text(
                    "SELECT jev._finish(CAST(:job AS uuid), CAST(:worker AS uuid), CAST(:result AS jsonb))"
                ),
                {
                    "job": job_id,
                    "worker": self.worker_id,
                    "result": json.dumps(result, ensure_ascii=False),
                },
            ).scalar_one()

    def work_one(self):
        job = self.claim()
        if job is None:
            return False
        done = Event()

        def keep_lease():
            while not done.wait(self.lease_seconds / 3):
                try:
                    if not self.heartbeat(job["id"]):
                        return
                except Exception as exc:
                    log.warning("SQL job %s heartbeat failed: %s", job["id"], type(exc).__name__)

        thread = Thread(target=keep_lease, daemon=True, name="jev-sql-lease")
        thread.start()
        try:
            service = OperatorService(
                self.db, self.decisions, job["tenant"], job["actor"], job["role"]
            )
            try:
                result = service.call(**job["request"])
            except Exception as exc:
                # Driver exceptions may embed SQL or data; expose only the failure category.
                result = {
                    "output_state": "NOT_EVALUATED",
                    "operation_state": "FAILED",
                    "value": None,
                    "reason": "Operator validation or execution failed",
                    "error_type": type(exc).__name__,
                }
                runtime = getattr(service, "runtime", None)
                if runtime:
                    result["run_id"] = runtime.run_id
                log.warning("SQL job %s failed: %s", job["id"], type(exc).__name__)
            if not self.finish(job["id"], result):
                log.error("SQL job %s lost its lease; its result was not published", job["id"])
        finally:
            done.set()
            thread.join(timeout=12)
        return True


def run(db, decisions, *, concurrency=2, once=False, heartbeat_path=None):
    if not 1 <= concurrency <= 8:
        raise ValueError("SQL worker concurrency must be between 1 and 8")
    stop = Event()
    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, lambda *_: stop.set())

    def consume():
        worker = SQLWorker(db, decisions, heartbeat_path=heartbeat_path)
        while not stop.is_set():
            try:
                worked = worker.work_one()
            except Exception as exc:
                log.warning("SQL queue unavailable: %s", type(exc).__name__)
                worked = False
            if once:
                return
            if not worked:
                stop.wait(1)

    try:
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="jev-sql") as pool:
            futures = [pool.submit(consume) for _ in range(concurrency)]
            for future in futures:
                future.result()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
