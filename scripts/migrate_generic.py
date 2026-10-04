"""Compatibility entry point; new deployments should use jev4pg migrate."""

import os
from sdd.bootstrap import migrate
from sdd.config import database_url, load_env, secret


def main():
    load_env()
    migrate(
        database_url(admin=True),
        os.getenv("SDD_DB_USER", "sdd_app"),
        secret("SDD_DB_PASSWORD"),
        os.getenv("SDD_SQL_INTERFACE") == "1",
    )
    print("Schema, grants and tenant policies installed.")


if __name__ == "__main__":
    main()
