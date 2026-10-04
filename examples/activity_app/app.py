"""A small application backend using jev4pg's bounded read API."""

from contextlib import asynccontextmanager
from decimal import Decimal
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


STATIC = Path(__file__).with_name("static")


@asynccontextmanager
async def lifespan(app):
    url, token, dataset = (
        os.environ[name] for name in ("SDD_API_URL", "SDD_API_TOKEN", "SDD_ACTIVITY_DATASET")
    )
    async with httpx.AsyncClient(
        base_url=url.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    ) as client:
        app.state.client, app.state.dataset = client, dataset
        yield


app = FastAPI(title="Activity monitor", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=STATIC), name="assets")


@app.get("/")
def index():
    return FileResponse(
        STATIC / "index.html",
        headers={
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'"
        },
    )


@app.get("/api/activity")
async def activity(
    category: str = Query(default="", max_length=80),
    minimum: Decimal | None = None,
    after: int | None = Query(default=None, ge=0, le=2**63 - 1),
):
    filters = []
    if category:
        filters.append({"column": "category", "op": "eq", "value": category})
    if minimum is not None:
        if not minimum.is_finite():
            raise HTTPException(422, "Minimum must be finite")
        filters.append({"column": "amount", "op": "gte", "value": str(minimum)})
    try:
        response = await app.state.client.post(
            f"/datasets/{app.state.dataset}/scan",
            json={
                "columns": ["id", "category", "amount", "note", "created_at"],
                "filters": filters,
                "after": [str(after)] if after is not None else None,
                "limit": 50,
            },
        )
    except httpx.TimeoutException:
        raise HTTPException(504, "Database request timed out; retry this page") from None
    except httpx.RequestError:
        raise HTTPException(503, "Database service is unavailable") from None
    if response.status_code != 200:
        raise HTTPException(
            503, "Database request failed; check the application's source and reader identity"
        )
    return response.json()
