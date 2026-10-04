from contextlib import contextmanager, nullcontext
from threading import RLock
import math
import os
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import StaticPool
from .schema import CATALOG_SCHEMA, metadata


def pool_setting(name, default, minimum, *, integer=True):
    try:
        value = (int if integer else float)(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} must be a finite number at least {minimum}") from None
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} must be a finite number at least {minimum}")
    return value


class Database:
    def __init__(self, url):
        self._transaction_lock = RLock()
        options = {"pool_pre_ping": True, "hide_parameters": True}
        url_text = str(url)
        if url_text.startswith("sqlite"):
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
            options["execution_options"] = {"schema_translate_map": {CATALOG_SCHEMA: None}}
            if ":memory:" in url_text:
                options["poolclass"] = StaticPool
        elif url_text.startswith("postgresql"):
            options["connect_args"] = (
                {} if "connect_timeout" in make_url(url).query else {"connect_timeout": 10}
            )
            options.update(
                pool_size=pool_setting("SDD_DB_POOL_SIZE", 5, 1),
                max_overflow=pool_setting("SDD_DB_MAX_OVERFLOW", 10, 0),
                pool_timeout=pool_setting("SDD_DB_POOL_TIMEOUT", 30, 0.01, integer=False),
                pool_recycle=pool_setting("SDD_DB_POOL_RECYCLE", 1800, 1),
            )
            guard_size = pool_setting("SDD_DB_GUARD_POOL_SIZE", 2, 1)
        self.engine = create_engine(url, **options)
        if self.engine.dialect.name == "postgresql":
            self._guard_engine = create_engine(
                url, **{**options, "pool_size": guard_size, "max_overflow": 0}
            )

            @event.listens_for(self.engine, "engine_disposed")
            def dispose_guards(_):
                self._guard_engine.dispose()

        if self.engine.dialect.name == "sqlite":

            @event.listens_for(self.engine, "connect")
            def pragmas(connection, _):
                from .sqlite_functions import register

                register(connection)
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")

    def initialize(self):
        from .generic import schema as generic_schema  # noqa: F401 - register generic tables

        from .operators import schema as operator_schema  # noqa: F401 - register operator tables

        with self.engine.begin() as connection:
            if self.engine.dialect.name == "postgresql":
                connection.exec_driver_sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG_SCHEMA}")
            metadata.create_all(connection)

    @contextmanager
    def transaction(self, tenant, isolation_level=None, *, before_snapshot=None):
        engine = (
            self.engine.execution_options(isolation_level=isolation_level)
            if isolation_level
            else self.engine
        )
        with self._transaction(engine, tenant, before_snapshot) as connection:
            yield connection

    @contextmanager
    def source_guard(self, tenant):
        if self.engine.dialect.name != "postgresql":
            raise ValueError("Source guards require PostgreSQL")
        with self._transaction(self._guard_engine, tenant) as connection:
            yield connection

    @contextmanager
    def _transaction(self, engine, tenant, before_snapshot=None):
        if not tenant or len(tenant) > 100:
            raise ValueError("A tenant identity is required")
        guard = self._transaction_lock if self.engine.dialect.name == "sqlite" else nullcontext()
        with guard, engine.begin() as conn:
            if before_snapshot is not None:
                before_snapshot(conn)
            if self.engine.dialect.name == "postgresql":
                conn.execute(
                    text("SELECT set_config('sdd.tenant', :tenant, true)"), {"tenant": tenant}
                )
            yield conn
