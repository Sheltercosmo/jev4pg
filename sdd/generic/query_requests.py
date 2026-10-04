"""Shared request contract for immediate and queued SQL execution."""

from pydantic import Field

from ..ir import Strict


class QueryInput(Strict):
    parent_history_id: str | None = Field(default=None, max_length=64)
    sql: str = Field(min_length=1, max_length=30000)
    max_evaluations: int = Field(default=100, ge=0, le=1000)
    accept: float = Field(default=0.8, ge=0, le=1)
    reject: float = Field(default=0.2, ge=0, le=1)
    allow_all: bool = False
    max_affected: int = Field(default=1000, ge=1, le=1000)
    timeout_seconds: float | None = Field(default=None, ge=0.1, le=3600, allow_inf_nan=False)


class QueryJobInput(QueryInput):
    idempotency_key: str = Field(min_length=1, max_length=128)
