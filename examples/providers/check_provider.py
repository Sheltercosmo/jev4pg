"""Send one live primitive batch to the configured provider and validate its contract."""

import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sdd.config import load_env  # noqa: E402
from sdd.generic.jev import choice, noul, validate_response  # noqa: E402
from sdd.providers import configured_backend  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", default="The delivery has arrived.")
    args = parser.parse_args()
    load_env()
    provider = configured_backend()
    if provider is None:
        parser.error("Configure a JEV provider first; see docs/PROVIDERS.md")
    model = os.getenv("SDD_JEV_MODEL", "jev-1.13.0")
    provider.cache_identity(model)
    questions = {
        "arrived": noul("The delivery has arrived."),
        "status": choice(
            "Select delivery status.",
            {
                "received": "Already arrived",
                "pending": "Still expected",
            },
        ),
        "urgency": {
            "type": "score",
            "instructions": "Rate urgency stated in the text.",
            "criteria": ["No stated urgency", "Explicit urgency"],
        },
    }
    result = validate_response(
        provider.infer(model, {"text": args.text}, questions), questions, model
    )
    print(f"Provider returned valid Noul, Choice and Score answers for {model}.")
    print(f"Usage reported: {result.get('usage', {})}")
    print("This checks the response contract, not semantic accuracy.")


if __name__ == "__main__":
    main()
