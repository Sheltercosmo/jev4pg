# JEV providers

jev4pg supports TypeSafe, HTTP services that implement the same typed decision contract, and local Python model adapters. The planner and operators use the same interface in all three cases.

## TypeSafe

The existing configuration continues to work:

```dotenv
TYPESAFE_API_KEY=<your-typesafe-key>
SDD_JEV_MODEL=jev-1.13.0
```

The default endpoint is `https://api.typesafe.ai/v1/systemone`. You can use `SDD_JEV_API_KEY` instead of `TYPESAFE_API_KEY`.

## Third-party or local HTTP service

Set the complete inference URL, a stable model ID and, if required, that service's bearer key:

```dotenv
SDD_JEV_TRANSPORT=systemone
SDD_JEV_ENDPOINT=https://your-provider.example/v1/systemone
SDD_JEV_API_KEY=<that-provider-key>
SDD_JEV_MODEL=<pinned-model-id>
SDD_JEV_REVISION=<weights-or-adapter-revision>
```

For a local server:

```dotenv
SDD_JEV_TRANSPORT=systemone
SDD_JEV_ENDPOINT=http://127.0.0.1:9100/v1/systemone
SDD_JEV_MODEL=local-model-2026-10-01
SDD_JEV_REVISION=weights-and-prompt-v1
```

Leave `SDD_JEV_API_KEY` unset when the local server needs no authentication. A custom endpoint never inherits `TYPESAFE_API_KEY`. Redirects are rejected, and credentials must not be embedded in the endpoint URL. Use HTTPS for remote services.

The URL receives a POST with the complete question batch. This setting does not turn an arbitrary chat-completions server into JEV: it must implement the contract below or sit behind your adapter.

## Local Python model

Use an installed module that exposes a factory returning a callable:

```dotenv
SDD_JEV_TRANSPORT=python
SDD_JEV_ADAPTER=my_model_adapter:create_predictor
SDD_JEV_MODEL=local-model-2026-10-01
SDD_JEV_REVISION=weights-and-prompt-v1
```

The server loads the factory at startup. The callable receives keyword arguments `model`, `state` and `questions`, and returns the response dictionary described below. Load your model in the factory and reuse it across calls.

```python
def create_predictor():
    engine = load_your_model()

    def predict(*, model: str, state: object, questions: dict) -> dict:
        raw = engine.evaluate_batch(state, questions)
        return {"model": model, "answers": convert_answers(raw, questions)}

    return predict
```

Here `load_your_model`, `evaluate_batch` and `convert_answers` represent your inference library and its output mapping. Implement the mapping from its outputs to the contract; the project does not supply model weights or invent probabilities from generated labels. A complete example of the interface is the `BatchPredictor` protocol in [providers.py](../sdd/providers.py).

Factories are trusted server configuration and cannot be selected by a query request. Independent batches may call the predictor concurrently. Use a thread-safe inference runtime, or guard your local model with a lock if it requires exclusive access. Keep all questions in a shared-context batch together where the model supports that operation.

## Request and response contract

A request contains a pinned model, a JSON state and questions keyed by caller-selected IDs:

```json
{
  "model": "local-model-2026-10-01",
  "state": {"text": "The delivery has arrived."},
  "questions": {
    "arrived": {"type": "noul", "instructions": "The delivery has arrived."},
    "status": {
      "type": "choice",
      "instructions": "Select the delivery status.",
      "criteria": {"received": "Already arrived", "pending": "Still expected"}
    },
    "urgency": {
      "type": "score",
      "instructions": "Rate urgency stated in the text.",
      "criteria": ["No stated urgency", "Explicit urgency"]
    }
  }
}
```

An illustrative response:

```json
{
  "model": "local-model-2026-10-01",
  "answers": {
    "arrived": {"type": "noul", "noul": 0.95},
    "status": {
      "type": "choice",
      "choice": "received",
      "probabilities": {"received": 0.95, "pending": 0.05}
    },
    "urgency": {
      "type": "score",
      "score": 0.1,
      "probabilities": {"0": 0.9, "1": 0.1},
      "legend": {"0": "No stated urgency", "1": "Explicit urgency"}
    }
  },
  "usage": {"input_tokens": 64, "output_tokens": 12}
}
```

The returned model and answer IDs must match the request. Noul probabilities must be finite numbers between zero and one. Choice probabilities must cover every supplied option and sum to one within a 0.03 tolerance. Score uses zero-based level IDs, a value within the rubric range and the unchanged legend. Booleans are not valid numeric probabilities. Optional confidence must be between zero and one.

Usage values, when supplied, must be nonnegative integer token counts. Omit unavailable usage instead of inventing it. The application keeps unknown results separate from failed or unexecuted work. Invalid answers fail validation; they do not become negative classifications.

HTTP 408, 429 and server errors are retryable under the existing execution budget. Other HTTP failures are permanent. A Python adapter can raise `ProviderError(code, retryable, retry_after)` to describe a recoverable failure. Other adapter exceptions become a sanitized `LocalProviderFailure`.

## Evidence and configuration changes

Evidence and saved review work are scoped to the endpoint or adapter, configured revision and model ID. API keys are excluded from that identity so rotating a key does not discard compatible observations. If a key selects different weights or account-specific behavior under the same model name, change `SDD_JEV_REVISION` as well.

Changing providers or revisions invalidates model-dependent reuse. Saved approvals and resumable runs cannot silently move to a different provider. Automatic materialization refresh stops when its provider identity changes. Human assertions retain their own attribution.

Use a new revision whenever weights, preprocessing, prompts or probability mappings change. Model aliases containing `latest` or `preview` are rejected. The legacy evaluator API also binds newly created revisions to provider configuration; create a new evaluator after switching providers.

## Check a connection

From the repository root, run:

```bash
python examples/providers/check_provider.py
```

This sends one batch containing Noul, Choice and Score questions to the configured provider. It checks the response contract and incurs provider usage. Add `--text "货物已经送达。"` to check a Chinese payload.

Contract compatibility does not establish semantic accuracy. Validate your model on your data, languages and uncertainty thresholds before relying on its decisions. Configure `SDD_JEV_INPUT_USD_PER_MILLION` for the provider's input-token rate; setting it to zero records no provider token charge and does not account for local hardware cost.

## Container deployment

Compose reads endpoint, model, revision and concurrency from `.env`. Store provider keys in the files created under `.secrets/`: `typesafe_api_key`, `jev_api_key` and `llm_api_key`. Restart `app` and `sql-worker` after changes.

A model in another container must be reachable by its service name; `127.0.0.1` inside the app refers to the app container. For a model on the Docker host, configure a reachable host address and its firewall rules. A Python adapter must be installed into a derived application image and selected with `SDD_JEV_TRANSPORT=python`, `SDD_JEV_ADAPTER`, `SDD_JEV_MODEL` and `SDD_JEV_REVISION` for both services.
