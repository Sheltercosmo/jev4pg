"""Check resolved deployment contracts with Docker Compose, without starting containers."""

import argparse
import json
from pathlib import Path
import subprocess


def check(compose):
    root = Path(__file__).resolve().parent.parent

    def resolve(*arguments):
        result = subprocess.run(
            [
                *compose,
                "--env-file",
                "deploy/external.env.example",
                *arguments,
                "config",
                "--format",
                "json",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return json.loads(result.stdout)

    bundled = resolve("-f", "compose.yaml")
    assert set(bundled["services"]) == {"postgres", "migrate", "app", "sql-worker"}
    for name in ("app", "migrate", "sql-worker"):
        service = bundled["services"][name]
        assert service["environment"]["SDD_DB_HOST"] == "postgres"
        assert service["environment"]["SDD_DB_USER"] == "sdd_app"
        assert service["environment"]["SDD_SQL_INTERFACE"] == "1"
    assert (
        bundled["services"]["migrate"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    )
    native = resolve(
        "-f", "compose.yaml", "-f", "compose.native.yaml", "-f", "deploy/compose.test.yaml"
    )
    assert native["services"]["app"]["environment"]["SDD_SEMANTIC_ENGINE"] == "native"
    assert "--native-registry" in native["services"]["migrate"]["command"]
    assert native["services"]["postgres"]["build"]["dockerfile"] == "deploy/native.Dockerfile"

    external = resolve("-f", "compose.external.yaml")
    assert set(external["services"]) == {"app"}
    assert "postgres_password" not in external.get("secrets", {})
    enabled = resolve("-f", "compose.external.yaml", "--profile", "sql", "--profile", "tools")
    assert set(enabled["services"]) == {"app", "sql-worker", "migrate"}
    assert not enabled.get("volumes")
    for name, service in enabled["services"].items():
        assert not service.get("depends_on")
        assert service["environment"]["SDD_DB_HOST"] == "postgres.example.internal"
        assert service["environment"]["SDD_DB_SSLMODE"] == "verify-full"
        assert service["environment"]["SDD_DB_SSLROOTCERT"] == "/run/secrets/database_ca"
        assert service["read_only"]
        if name != "migrate":
            assert not any(key.startswith("SDD_ADMIN_") for key in service["environment"])
            assert "postgres_password" not in {secret["source"] for secret in service["secrets"]}
    assert enabled["services"]["migrate"]["command"] == ["migrate"]
    assert Path(enabled["services"]["app"]["build"]["context"]).resolve() == root
    print("Bundled, native and external Compose contracts passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-bin", help="Standalone docker-compose executable")
    args = parser.parse_args()
    check([args.compose_bin] if args.compose_bin else ["docker", "compose"])
