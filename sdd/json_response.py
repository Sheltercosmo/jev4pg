"""Preserve integers that browser JSON numbers cannot represent exactly."""

from fastapi.responses import JSONResponse


def exact_json(value):
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2**53 - 1:
        return str(value)
    if isinstance(value, dict):
        return {key: exact_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [exact_json(item) for item in value]
    return value


class ExactJSONResponse(JSONResponse):
    def render(self, content):
        return super().render(exact_json(content))
