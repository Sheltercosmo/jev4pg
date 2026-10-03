"""Providers return evidence, never SQL, permissions, or authoritative labels."""

from dataclasses import dataclass, field
import math
import hashlib
import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
import httpx


@dataclass
class Evaluation:
    probability: float
    responder: str
    usage: dict = field(default_factory=dict)
    evidence: list = field(default_factory=list)

    def validate(self, expected_model):
        if isinstance(self.probability, bool) or not isinstance(self.probability, (int, float)):
            raise ValueError("Probability must be numeric")
        if not math.isfinite(self.probability) or not 0 <= self.probability <= 1:
            raise ValueError("Invalid probability")
        if self.responder != expected_model:
            raise ValueError("Responder differs from pinned evaluator model")
        return self


class ProviderError(RuntimeError):
    """Sanitized failure metadata; never retain the provider response body."""

    def __init__(self, code, retryable, retry_after=None):
        super().__init__(code)
        self.code, self.retryable = code, retryable
        self.retry_after = retry_after


DEFAULT_JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"


def validate_model(model):
    if not isinstance(model, str) or not model.strip() or len(model) > 100:
        raise ValueError("Use a nonempty model ID of at most 100 characters")
    if any(alias in model.lower() for alias in ("latest", "preview")):
        raise ValueError("Pin the JEV model version")
    return model


def decision_identity(decisions):
    if decisions is None:
        return "unconfigured"
    return getattr(decisions, "identity", decisions.model)


def retry_delay(header):
    if not header:
        return None
    try:
        delay = float(header)
    except ValueError:
        try:
            delay = (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return delay if math.isfinite(delay) and delay >= 0 else None


class JevBackend:
    def __init__(self, api_key=None, client=None, *, endpoint=DEFAULT_JEV_ENDPOINT, revision=""):
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "JEV endpoint must be an HTTP(S) URL without credentials or query parameters"
            )
        if endpoint == DEFAULT_JEV_ENDPOINT and not api_key:
            raise ValueError("TYPESAFE_API_KEY or SDD_JEV_API_KEY is required")
        self.api_key, self.endpoint, self.revision = api_key, endpoint, revision
        self.client = client or httpx.Client(timeout=httpx.Timeout(45, connect=10))

    def cache_identity(self, model):
        validate_model(model)
        if self.endpoint == DEFAULT_JEV_ENDPOINT and not self.revision:
            return model
        value = json.dumps(["systemone-v1", self.endpoint, self.revision, model])
        return "provider:" + hashlib.sha256(value.encode()).hexdigest()

    def preprocessing(self, model):
        identity = self.cache_identity(model)
        if identity == model:
            return "identity-v1"
        return "provider:" + hashlib.sha256(identity.encode()).hexdigest()[:24]

    def validate_evaluator(self, evaluator):
        if evaluator.get("preprocessing", "identity-v1") != self.preprocessing(evaluator["model"]):
            raise ValueError("Provider configuration changed; create a new evaluator revision")

    def infer(self, model, state, questions):
        headers = {"Authorization": "Bearer " + self.api_key} if self.api_key else {}
        try:
            response = self.client.post(
                self.endpoint,
                headers=headers,
                json={"model": model, "state": state, "questions": questions},
                follow_redirects=False,
            )
        except httpx.RequestError as exc:
            raise ProviderError(type(exc).__name__, True) from None
        if not response.is_success:
            status = response.status_code
            raise ProviderError(
                f"HTTP_{status}",
                status in (408, 429) or status >= 500,
                retry_after=retry_delay(response.headers.get("Retry-After")),
            )
        try:
            return response.json()
        except ValueError:
            raise ProviderError("InvalidDecisionResponse", False) from None

    def evaluate(self, version, concept, evaluator):
        context = {k: version["context"][k] for k in concept["context_fields"]}
        question = {
            "type": "noul",
            "instructions": evaluator["instructions"] + "\n" + concept["definition"],
        }
        if concept["inclusion"] or concept["exclusion"]:
            question["criteria"] = {
                "true": concept["inclusion"] or concept["definition"],
                "false": concept["exclusion"] or "The definition does not apply.",
            }
        self.validate_evaluator(evaluator)
        try:
            payload = self.infer(
                evaluator["model"],
                {"message": version["text"], "context": context},
                {"predicate": question},
            )
            answer = payload["answers"]["predicate"]
            if answer["type"] != "noul":
                raise ValueError("Expected Noul")
            usage = payload.get("usage", {})
            if not isinstance(usage, dict):
                raise ValueError("Invalid usage")
            if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in usage.values()):
                raise ValueError("Invalid token usage")
            return Evaluation(
                answer["noul"],
                payload["model"],
                usage,
                [{"source_version": version["id"], "scope": "whole_message"}],
            ).validate(evaluator["model"])
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ProviderError("InvalidResponse", False) from exc


class FixtureBackend:
    """Explicit synthetic-test backend, never a substitute for Jev classification."""

    def __init__(self, probabilities=None):
        self.probabilities = probabilities or {}
        self.calls = 0

    def evaluate(self, version, concept, evaluator):
        self.calls += 1
        value = self.probabilities.get(
            (version["text"], concept["id"]), self.probabilities.get(version["text"])
        )
        if value is None:
            raise ValueError("No fixture outcome for this input")
        if isinstance(value, Exception):
            raise value
        return Evaluation(
            value,
            evaluator["model"],
            {"input_tokens": 0},
            [{"source_version": version["id"], "scope": "synthetic_fixture"}],
        )
