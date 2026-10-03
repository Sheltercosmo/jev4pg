import os
import json
from pathlib import Path


def load_env(path=".env"):
    """Small .env reader; existing environment always wins. Never logs values."""
    if Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def runtime():
    from .db import Database
    from .execution import Executor
    from .providers import configured_backend

    load_env()
    load_secrets()
    db = Database(database_url())
    if os.getenv("SDD_ENV") == "production":
        from .deployment import check_database

        check_database(db)
    backends = {}
    backend = configured_backend()
    if backend is not None:
        backends["jev"] = backend
    return db, Executor(db, backends)


def secret(name, default=None):
    path = os.getenv(name + "_FILE")
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    return os.getenv(name, default)


def load_secrets():
    for name in ("SDD_API_TOKENS", "TYPESAFE_API_KEY", "SDD_JEV_API_KEY", "OPENAI_API_KEY"):
        if os.getenv(name + "_FILE"):
            os.environ[name] = secret(name)


def database_url(admin=False):
    from sqlalchemy import URL

    url = secret("SDD_ADMIN_DATABASE_URL" if admin else "DATABASE_URL")
    if url:
        return url
    prefix = "SDD_ADMIN_DB_" if admin else "SDD_DB_"
    user, password = os.getenv(prefix + "USER"), secret(prefix + "PASSWORD")
    if user and password:
        return URL.create(
            "postgresql+psycopg",
            username=user,
            password=password,
            host=os.getenv("SDD_DB_HOST", "127.0.0.1"),
            port=int(os.getenv("SDD_DB_PORT", "5432")),
            database=os.getenv("SDD_DB_NAME", "sdd"),
        )
    if admin or os.getenv("SDD_ENV") == "production":
        raise ValueError("Configure the database URL or database user and password file")
    return "sqlite:///sdd.db"


def api_tokens():
    tokens = json.loads(secret("SDD_API_TOKENS", "{}"))
    if not isinstance(tokens, dict):
        raise ValueError("SDD_API_TOKENS must be a token-to-principal object")
    production = os.getenv("SDD_ENV") == "production"
    if production and not tokens:
        raise ValueError("Production requires API authentication tokens")
    for token, principal in tokens.items():
        if (
            not isinstance(principal, dict)
            or not isinstance(token, str)
            or not token
            or not isinstance(principal.get("tenant"), str)
            or not 1 <= len(principal["tenant"]) <= 100
            or not isinstance(principal.get("name"), str)
            or not principal["name"]
            or principal.get("role") not in {"reader", "reviewer"}
            or (production and len(token) < 32)
        ):
            raise ValueError(
                "Each API token needs a tenant, name and reader/reviewer role; production tokens need 32 characters"
            )
    return tokens
