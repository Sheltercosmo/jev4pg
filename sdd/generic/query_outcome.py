"""Preserve execution output states when presenting saved query outcomes."""

import re


def query_states(result):
    manifest = result.get("manifest") or {}
    output = manifest.get("result_output_state")
    reason = manifest.get("result_hold_reason")
    operation = manifest.get("result_operation_state")
    if output is None:
        complete = manifest.get("complete")
        output = (
            "VALUE" if complete is True else "UNKNOWN" if complete is False else "NOT_EVALUATED"
        )
    elif not isinstance(output, str) or output not in {"VALUE", "UNKNOWN", "NOT_EVALUATED"}:
        output, operation, reason = "NOT_EVALUATED", "FAILED", "Invalid saved result output state"
    if operation is not None and (
        not isinstance(operation, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", operation)
    ):
        output, operation, reason = (
            "NOT_EVALUATED",
            "FAILED",
            "Invalid saved result operation state",
        )
    operation = (
        operation
        or {"VALUE": "SUCCEEDED", "UNKNOWN": "PARTIAL", "NOT_EVALUATED": "NOT_EVALUATED"}[output]
    )
    if output != "NOT_EVALUATED" and operation in {"SUCCEEDED", "PARTIAL"}:
        if result.get("mutation_preview"):
            operation = "AWAITING_REVIEW"
        elif manifest.get("truncated"):
            operation = "TRUNCATED"
    return {
        "output_state": output,
        "operation_state": operation,
        "hold_reason": reason if isinstance(reason, str) else None,
    }


def history_status(result, *, previous=None):
    states = query_states(result)
    manifest = result.get("manifest") or {}
    failure = {"FAILED": "error", "CANCELLED": "cancelled", "TIMED_OUT": "timed_out"}.get(
        states["operation_state"]
    )
    if failure:
        return failure
    if states["output_state"] == "NOT_EVALUATED" and manifest:
        return "held"
    if result.get("mutation_preview") or previous == "preview":
        return "preview"
    if manifest.get("committed") or previous == "committed":
        return "committed"
    if not manifest:
        return "plan"
    return "complete" if states["output_state"] == "VALUE" else "partial"
