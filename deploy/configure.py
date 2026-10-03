"""Create local deployment secrets without overwriting existing credentials."""

import argparse
import getpass
import json
import os
import secrets
from pathlib import Path


def configure(directory, *, prompt=True):
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError("The secrets directory must not be a symbolic link")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        directory.chmod(0o700)
    values = {
        "postgres_password": lambda: secrets.token_urlsafe(36),
        "app_password": lambda: secrets.token_urlsafe(36),
        "api_tokens.json": lambda: json.dumps(
            {secrets.token_urlsafe(32): {"tenant": "demo", "name": "owner", "role": "reviewer"}},
            indent=2,
        ),
        "typesafe_api_key": lambda: (
            os.getenv("TYPESAFE_API_KEY")
            or (
                getpass.getpass("TypeSafe API key (leave empty for another provider): ")
                if prompt
                else ""
            )
        ),
        "jev_api_key": lambda: os.getenv("SDD_JEV_API_KEY", ""),
        "llm_api_key": lambda: os.getenv("OPENAI_API_KEY", ""),
    }
    for name, create in values.items():
        path = directory / name
        if path.is_symlink():
            raise ValueError("Secret files must not be symbolic links")
        if not path.exists():
            with path.open("x", encoding="utf-8") as output:
                output.write(create() + "\n")
            # The private parent protects host access; containers mount individual files.
            if os.name != "nt":
                path.chmod(0o644)
    return directory


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default=".secrets")
    parser.add_argument("--no-prompt", action="store_true")
    parser.add_argument("--show-token", action="store_true")
    args = parser.parse_args()
    if args.show_token:
        tokens = json.loads((Path(args.directory) / "api_tokens.json").read_text())
        print(next(iter(tokens)))
    else:
        directory = configure(args.directory, prompt=not args.no_prompt)
        print(f"Deployment secrets ready in {directory}. Existing files were preserved.")
