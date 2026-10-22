"""Create a separate local deployment profile for an existing PostgreSQL database."""

import getpass
import json
import os
from pathlib import Path
import secrets

from sqlalchemy.engine import make_url


def write_profile(directory, admin_url, runtime_user, runtime_password, provider_key=""):
    try:
        admin = make_url(admin_url)
    except Exception:
        raise ValueError("Enter a valid PostgreSQL connection URL") from None
    if admin.drivername not in {"postgres", "postgresql", "postgresql+psycopg"}:
        raise ValueError("Use a PostgreSQL connection URL")
    if not admin.host or not admin.database or not admin.username:
        raise ValueError("The connection URL needs a host, database and installation owner")
    if (
        admin.host not in {"localhost", "127.0.0.1", "::1"}
        and admin.query.get("sslmode") != "verify-full"
    ):
        raise ValueError(
            "Remote PostgreSQL connections require sslmode=verify-full and a trusted CA"
        )
    if any(
        k in admin.query
        for k in ("user", "password", "service", "servicefile", "options", "passfile")
    ):
        raise ValueError("Use URL credentials and ordinary TLS options, without identity overrides")
    if (
        not runtime_user
        or runtime_user != runtime_user.strip()
        or len(runtime_user.encode()) > 63
        or "\x00" in runtime_user
        or runtime_user == admin.username
        or any(c in runtime_user for c in "\r\n")
    ):
        raise ValueError("Choose a separate restricted runtime login")
    if (
        not runtime_password
        or runtime_password != runtime_password.strip()
        or any(c in runtime_password for c in "\r\n")
    ):
        raise ValueError("Provide a single-line runtime password")
    if any(c in provider_key for c in "\r\n"):
        raise ValueError("Provide a single-line provider key")
    directory = Path(directory).absolute()
    if any(c in str(directory) for c in "\r\n"):
        raise ValueError("The profile path must be a single line")
    # Exclusive creation keeps reruns from rotating credentials or replacing a profile.
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    private = directory / ".secrets"
    private.mkdir(mode=0o700)
    admin = admin.set(drivername="postgresql+psycopg")
    runtime = admin.set(username=runtime_user, password=runtime_password)

    def save(path, value):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value + "\n")

    save(private / "admin-url", admin.render_as_string(hide_password=False))
    save(private / "runtime-url", runtime.render_as_string(hide_password=False))
    save(private / "runtime-password", runtime_password)
    save(private / "provider-key", provider_key)
    save(
        private / "api-tokens.json",
        json.dumps(
            {secrets.token_urlsafe(32): {"tenant": "demo", "name": "owner", "role": "reviewer"}}
        ),
    )
    save(
        directory / ".env",
        "\n".join(
            [
                "SDD_ENV=production",
                "SDD_SQL_INTERFACE=1",
                f"SDD_DB_USER={runtime_user}",
                "DATABASE_URL_FILE=.secrets/runtime-url",
                "SDD_DB_PASSWORD_FILE=.secrets/runtime-password",
                "SDD_API_TOKENS_FILE=.secrets/api-tokens.json",
                "TYPESAFE_API_KEY_FILE=.secrets/provider-key",
            ]
        ),
    )
    save(directory / "admin.env", "SDD_ADMIN_DATABASE_URL_FILE=.secrets/admin-url")
    save(directory / ".gitignore", "*\n!.gitignore")
    return directory


def interactive_setup(directory):
    print("Configure an existing PostgreSQL database. This step creates local files only.")
    admin = getpass.getpass("Installation-owner connection URL (hidden, include TLS options): ")
    user = input("Restricted runtime login [sdd_app]: ").strip() or "sdd_app"
    password = getpass.getpass(
        "Runtime password (existing password, or empty to generate for a NEW login): "
    )
    key = getpass.getpass("TypeSafe key (empty to configure a different provider later): ")
    directory = write_profile(directory, admin, user, password or secrets.token_urlsafe(36), key)
    print(f"Profile created: {directory}")
    print("Change into that directory, then run:")
    print("  jev4pg --env-file admin.env migrate --check --sql-interface portable")
    print("  jev4pg --env-file admin.env migrate --sql-interface portable")
    print(
        "  jev4pg --env-file admin.env sql-grant YOUR_SQL_LOGIN --tenant demo --actor YOUR_SQL_LOGIN"
    )
    print("  jev4pg sql-worker")
