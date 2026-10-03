"""Compatibility entry point for the packaged PostgreSQL policy installer."""

import os
from sdd.config import load_env
from sdd.db import Database
from sdd.postgres_security import secure


if __name__ == "__main__":
    load_env()
    admin_url = os.getenv("SDD_ADMIN_DATABASE_URL")
    if not admin_url:
        raise SystemExit("Set SDD_ADMIN_DATABASE_URL for schema administration")
    secure(Database(admin_url))
    print("RLS and immutable revision guards installed.")
