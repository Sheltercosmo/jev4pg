"""Configure a SystemOne endpoint or a trusted in-process batch predictor."""

import hashlib
import importlib
import json
import os
from typing import Protocol

from .evaluators import DEFAULT_JEV_ENDPOINT, JevBackend, ProviderError, validate_model


class BatchPredictor(Protocol):
    def __call__(self, *, model: str, state: object, questions: dict) -> dict:
        """Return the SystemOne answer envelope for the complete question batch."""
        ...


class PythonJevBackend(JevBackend):
    def __init__(self, predictor: BatchPredictor, *, adapter: str, revision: str):
        if not callable(predictor) or not adapter or not revision:
            raise ValueError("A callable local adapter and explicit revision are required")
        self.predictor, self.adapter, self.revision = predictor, adapter, revision

    def cache_identity(self, model):
        validate_model(model)
        value = json.dumps(["python-jev-v1", self.adapter, self.revision, model])
        return "provider:" + hashlib.sha256(value.encode()).hexdigest()

    def infer(self, model, state, questions):
        try:
            return self.predictor(model=model, state=state, questions=questions)
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("LocalProviderFailure", False) from None


def configured_backend(environment=None):
    env = os.environ if environment is None else environment
    transport = env.get("SDD_JEV_TRANSPORT", "systemone")
    if transport == "python":
        location = env.get("SDD_JEV_ADAPTER", "")
        revision = env.get("SDD_JEV_REVISION", "")
        if ":" not in location or not revision or not env.get("SDD_JEV_MODEL"):
            raise ValueError(
                "Python JEV requires SDD_JEV_ADAPTER=module:factory, SDD_JEV_REVISION and SDD_JEV_MODEL"
            )
        module, name = location.split(":", 1)
        if not module or not name.isidentifier():
            raise ValueError("SDD_JEV_ADAPTER must name a module:factory")
        try:
            factory = getattr(importlib.import_module(module), name)
            predictor = factory()
        except Exception:
            raise ValueError("Could not load the configured JEV adapter factory") from None
        return PythonJevBackend(predictor, adapter=location, revision=revision)
    if transport != "systemone":
        raise ValueError("SDD_JEV_TRANSPORT must be systemone or python")
    endpoint = env.get("SDD_JEV_ENDPOINT") or DEFAULT_JEV_ENDPOINT
    key = env.get("SDD_JEV_API_KEY")
    if endpoint == DEFAULT_JEV_ENDPOINT:
        key = key or env.get("TYPESAFE_API_KEY")
        if not key:
            return None
    elif not env.get("SDD_JEV_MODEL"):
        raise ValueError("Set SDD_JEV_MODEL for a custom JEV endpoint")
    return JevBackend(key, endpoint=endpoint, revision=env.get("SDD_JEV_REVISION", ""))
