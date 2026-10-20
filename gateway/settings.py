"""Administrator-declared model capabilities and validated selection settings."""

import copy
import json

from fastapi import HTTPException

REASONING_MODES = {"auto", "on", "off"}
ENDPOINTS = {"/v1/chat/completions", "/v1/responses", "/v1/messages", "/v1/completions"}


def capabilities(spec):
    context = spec.get("context")
    limits = spec.get(
        "context_limits",
        {
            "min": context,
            "max": context,
            "presets": [context] if context is not None else [],
            "adjustable": False,
        },
    )
    return {
        "context_limits": copy.deepcopy(limits),
        "reasoning_modes": list(spec.get("reasoning_modes", ["auto"])),
        "reasoning_change": spec.get("reasoning_change", "fixed"),
    }


def summary(model, spec):
    return {
        "id": model,
        "context": spec.get("context"),
        "reasoning": spec.get("reasoning", "auto"),
        "enabled": spec.get("enabled", True),
        **(
            {"unavailable_reason": spec["unavailable_reason"]}
            if "unavailable_reason" in spec
            else {}
        ),
        **capabilities(spec),
    }


def validate_capabilities(spec):
    context = spec.get("context")
    if context is not None and (type(context) is not int or context <= 0):
        raise ValueError("context must be a positive integer")
    raw_modes = spec.get("reasoning_modes", ["auto"])
    if not isinstance(raw_modes, list) or any(not isinstance(mode, str) for mode in raw_modes):
        raise ValueError("reasoning_modes must be a string list")
    if not isinstance(spec.get("reasoning_change", "fixed"), str):
        raise ValueError("reasoning_change must be a string")
    caps = capabilities(spec)
    limits = caps["context_limits"]
    if not isinstance(limits, dict) or set(limits) != {"min", "max", "presets", "adjustable"}:
        raise ValueError("context_limits requires min, max, presets, adjustable")
    if type(limits["adjustable"]) is not bool or not isinstance(limits["presets"], list):
        raise ValueError("Invalid context limits")
    if context is None:
        if limits != {"min": None, "max": None, "presets": [], "adjustable": False}:
            raise ValueError("Adjustable context needs a declared default")
    else:
        lower, upper = limits["min"], limits["max"]
        if (
            any(type(v) is not int or v <= 0 for v in (lower, upper))
            or not lower <= context <= upper
        ):
            raise ValueError("Invalid context range/default")
        if any(type(v) is not int or not lower <= v <= upper for v in limits["presets"]):
            raise ValueError("Invalid context presets")
        if not limits["adjustable"] and (lower != context or upper != context):
            raise ValueError("Fixed context limits must match the default")
        if (
            limits["adjustable"]
            and not any("{context}" in arg for arg in spec.get("argv", []))
            and "{context}" not in json.dumps(spec.get("config_template", {}))
        ):
            raise ValueError("Adjustable context requires an administrator {context} argv binding")
    modes, change = caps["reasoning_modes"], caps["reasoning_change"]
    if (
        not modes
        or len(set(modes)) != len(modes)
        or not set(modes) <= REASONING_MODES
        or "auto" not in modes
        or change not in {"fixed", "request", "restart"}
    ):
        raise ValueError("Invalid reasoning capabilities")
    if change == "fixed" and modes != ["auto"]:
        raise ValueError("Fixed models cannot advertise reasoning controls")
    defaults = spec.get("reasoning_defaults", {})
    argv = spec.get("reasoning_argv", {})
    if not isinstance(defaults, dict) or not set(defaults) <= {"on", "off"}:
        raise ValueError("Invalid reasoning defaults")
    try:
        json.dumps(defaults, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise ValueError("Reasoning defaults must be finite JSON") from None
    if spec.get("reasoning", "auto") not in modes:
        raise ValueError("Default reasoning mode is unsupported")
    for endpoints in defaults.values():
        if not isinstance(endpoints, dict) or not endpoints or not set(endpoints) <= ENDPOINTS:
            raise ValueError("Reasoning defaults must map supported API paths to JSON objects")
        if any(
            not isinstance(fields, dict) or "model" in fields or "stream" in fields
            for fields in endpoints.values()
        ):
            raise ValueError("Reasoning defaults cannot change model or streaming")
    if (
        not isinstance(argv, dict)
        or not set(argv) <= REASONING_MODES
        or any(
            not isinstance(args, list) or any(not isinstance(a, str) or "\0" in a for a in args)
            for args in argv.values()
        )
    ):
        raise ValueError("Invalid reasoning argv")
    for mode in set(modes) - {"auto"}:
        if change == "request" and mode not in defaults:
            raise ValueError("Request reasoning controls need explicit protocol defaults")
        if change == "restart" and mode not in argv:
            raise ValueError("Restart reasoning controls need administrator argv")


def select(spec, context=None, reasoning=None):
    try:
        validate_capabilities(spec)
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(422, f"Invalid model capabilities: {exc}") from None
    caps = capabilities(spec)
    requested_context = spec.get("context") if context is None else context
    limits = caps["context_limits"]
    if context is not None and (
        type(context) is not int
        or context <= 0
        or (not limits["adjustable"] and context != spec.get("context"))
        or limits["min"] is None
        or not limits["min"] <= context <= limits["max"]
    ):
        raise HTTPException(422, "Context is not supported by this model")
    reasoning = spec.get("reasoning", "auto") if reasoning is None else reasoning
    if reasoning not in caps["reasoning_modes"]:
        raise HTTPException(422, "Reasoning mode is not supported by this model")
    chosen = copy.deepcopy(spec)
    chosen["argv"] = [a.replace("{context}", str(requested_context)) for a in chosen["argv"]]
    chosen["argv"].extend(chosen.get("reasoning_argv", {}).get(reasoning, []))
    if "config_template" in chosen:
        chosen["_config_template"] = copy.deepcopy(chosen["config_template"])

        def render(value):
            if isinstance(value, str):
                return value.replace("{context}", str(requested_context))
            if isinstance(value, list):
                return [render(item) for item in value]
            if isinstance(value, dict):
                return {key: render(item) for key, item in value.items()}
            return value

        chosen["config_template"] = render(chosen["config_template"])
    return chosen, {"context": requested_context, "reasoning": reasoning}


def apply_reasoning(body, path, spec, mode):
    """Fill only absent fields. Explicit protocol controls always win."""
    if mode == "auto":
        return body

    def explicit(container, names):
        return isinstance(container, dict) and any(
            container.get(name) is not None for name in names
        )

    if (
        explicit(body, ("reasoning_effort", "enable_thinking"))
        or explicit(body.get("reasoning"), ("effort", "enabled"))
        or explicit(body.get("thinking"), ("type", "enabled"))
        or explicit(
            body.get("chat_template_kwargs"),
            ("enable_thinking", "reasoning_effort", "thinking_mode"),
        )
        or explicit(body.get("output_config"), ("effort",))
        or any(
            body.get(name) is not None and not isinstance(body[name], dict)
            for name in ("reasoning", "thinking", "chat_template_kwargs")
        )
    ):
        return body
    defaults = spec.get("reasoning_defaults", {}).get(mode, {}).get(path)
    if defaults is None:
        if spec.get("reasoning_change") == "request":
            raise HTTPException(
                422,
                "Default reasoning is not configured for this endpoint; "
                "choose Auto or supply a supported client reasoning control",
            )
        return body

    def fill(target, source):
        for key, value in source.items():
            if key not in target or target[key] is None:
                target[key] = copy.deepcopy(value)
            elif isinstance(value, dict) and isinstance(target[key], dict):
                fill(target[key], value)

    fill(body, defaults)
    return body
