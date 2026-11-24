"""Administrator-declared model capabilities and validated selection settings."""

import copy
import json

from fastapi import HTTPException

REASONING_MODES = {"auto", "on", "off"}
ENDPOINTS = {"/v1/chat/completions", "/v1/responses", "/v1/messages", "/v1/completions"}


def capabilities(spec):
    context = spec.get("context")
    slots = spec.get("chat_slots", 1)
    limits = spec.get(
        "context_limits",
        {
            "min": context,
            "max": context,
            "presets": [context] if context is not None else [],
            "adjustable": False,
        },
    )
    slot_limits = spec.get(
        "slot_limits", {"min": slots, "max": slots, "presets": [slots], "adjustable": False}
    )
    return {
        "context_limits": copy.deepcopy(limits),
        "slot_limits": copy.deepcopy(slot_limits),
        "reasoning_modes": list(spec.get("reasoning_modes", ["auto"])),
        "reasoning_change": spec.get("reasoning_change", "fixed"),
        "system_prompt_supported": spec.get("system_prompt_supported", True),
    }


def summary(model, spec):
    return {
        "id": model,
        "context": spec.get("context"),
        "chat_slots": spec.get("chat_slots", 1),
        "reasoning": spec.get("reasoning", "auto"),
        "enabled": spec.get("enabled", True),
        **(
            {"unavailable_reason": spec["unavailable_reason"]}
            if "unavailable_reason" in spec
            else {}
        ),
        **capabilities(spec),
    }


def validate_slot_limits(spec, limits):
    """``slot_limits``: integers of at least 1 around ``chat_slots``; adjustable needs a ``{slots}`` binding."""
    slots = spec.get("chat_slots", 1)
    if type(slots) is not int or slots < 1:
        raise ValueError("chat_slots must be a positive integer")
    if not isinstance(limits, dict) or set(limits) != {"min", "max", "presets", "adjustable"}:
        raise ValueError("slot_limits requires min, max, presets, adjustable")
    if type(limits["adjustable"]) is not bool or not isinstance(limits["presets"], list):
        raise ValueError("Invalid slot limits")
    lower, upper = limits["min"], limits["max"]
    if any(type(v) is not int or v < 1 for v in (lower, upper)) or not lower <= slots <= upper:
        raise ValueError("Invalid slot range/default")
    if any(type(v) is not int or not lower <= v <= upper for v in limits["presets"]):
        raise ValueError("Invalid slot presets")
    if not limits["adjustable"] and (lower != slots or upper != slots):
        raise ValueError("Fixed slot limits must match chat_slots")
    if (
        limits["adjustable"]
        and not any("{slots}" in arg for arg in spec.get("argv", []))
        and "{slots}" not in json.dumps(spec.get("config_template", {}))
    ):
        raise ValueError("Adjustable slots require an administrator {slots} argv binding")


def validate_capabilities(spec):
    if type(spec.get("system_prompt_supported", True)) is not bool:
        raise ValueError("system_prompt_supported must be a boolean")
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
        if limits["adjustable"]:
            bindings = json.dumps([spec.get("argv", []), spec.get("config_template", {})])
            if "{context}" not in bindings and "{slots_x_context}" not in bindings:
                raise ValueError(
                    "Adjustable context requires an administrator {context} or "
                    "{slots_x_context} binding"
                )
    validate_slot_limits(spec, caps["slot_limits"])
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


def render_launch(text, context, chat_slots):
    """Substitute the administrator's launch placeholders in one string.

    ``{context}`` is the selected context; ``{slots}`` is the model's ``chat_slots`` (1 when it
    declares none); ``{slots_x_context}`` is ``chat_slots`` times the context, for engines whose
    shared KV pool is sized for every slot at the full context. The only place that knows the
    placeholders: the argv and every ``config_template`` value both go through it.
    """
    if "{slots_x_context}" in text:
        if type(context) is not int:
            raise HTTPException(422, "{slots_x_context} needs a model with a declared context")
        text = text.replace("{slots_x_context}", str(chat_slots * context))
    return text.replace("{slots}", str(chat_slots)).replace("{context}", str(context))


def select(spec, context=None, reasoning=None, system_prompt=None, slots=None):
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
    slot_limits = caps["slot_limits"]
    default_slots = spec.get("chat_slots", 1)
    if slots is not None and (
        type(slots) is not int
        or not slot_limits["min"] <= slots <= slot_limits["max"]
        or (not slot_limits["adjustable"] and slots != default_slots)
    ):
        raise HTTPException(422, "Slot count is not supported by this model")
    chosen_slots = default_slots if slots is None else slots
    reasoning = spec.get("reasoning", "auto") if reasoning is None else reasoning
    if reasoning not in caps["reasoning_modes"]:
        raise HTTPException(422, "Reasoning mode is not supported by this model")
    if system_prompt is None:
        system_prompt = ""
    if not isinstance(system_prompt, str) or len(system_prompt) > 16384 or "\0" in system_prompt:
        raise HTTPException(422, "System prompt must be text of at most 16384 characters")
    if system_prompt and not caps["system_prompt_supported"]:
        raise HTTPException(422, "System prompt is not supported by this model")
    chosen = copy.deepcopy(spec)
    # The selected count replaces chat_slots in the frozen spec, so rendering, the footprint fingerprint
    # and the chat capacity all follow it.
    chosen["chat_slots"] = chosen_slots
    chosen["argv"] = [render_launch(a, requested_context, chosen_slots) for a in chosen["argv"]]
    chosen["argv"].extend(chosen.get("reasoning_argv", {}).get(reasoning, []))
    if "config_template" in chosen:
        chosen["_config_template"] = copy.deepcopy(chosen["config_template"])

        def render(value):
            if isinstance(value, str):
                return render_launch(value, requested_context, chosen_slots)
            if isinstance(value, list):
                return [render(item) for item in value]
            if isinstance(value, dict):
                return {key: render(item) for key, item in value.items()}
            return value

        chosen["config_template"] = render(chosen["config_template"])
    return chosen, {
        "context": requested_context,
        "slots": chosen_slots,
        "reasoning": reasoning,
        "system_prompt": system_prompt,
    }


def apply_system_prompt(body, path, system_prompt):
    """Add the selected instruction without dropping the client's instructions."""
    if not system_prompt:
        return body
    if path == "/v1/chat/completions":
        messages = body.get("messages")
        if not isinstance(messages, list):
            raise HTTPException(400, "messages must be an array when a system prompt is selected")
        # Combining the first system message preserves templates which accept only one.
        if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
            content = messages[0].get("content")
            if isinstance(content, str):
                messages[0]["content"] = system_prompt + "\n\n" + content
            elif isinstance(content, list):
                messages[0]["content"] = [{"type": "text", "text": system_prompt}, *content]
            else:
                raise HTTPException(400, "Unsupported system message content")
        else:
            messages.insert(0, {"role": "system", "content": system_prompt})
    elif path == "/v1/responses":
        instructions = body.get("instructions")
        if instructions is not None and not isinstance(instructions, str):
            raise HTTPException(400, "instructions must be text when a system prompt is selected")
        body["instructions"] = system_prompt + ("\n\n" + instructions if instructions else "")
    elif path == "/v1/messages":
        existing = body.get("system")
        if existing is None:
            body["system"] = system_prompt
        elif isinstance(existing, str):
            body["system"] = system_prompt + "\n\n" + existing
        elif isinstance(existing, list):
            body["system"] = [{"type": "text", "text": system_prompt}, *existing]
        else:
            raise HTTPException(400, "Unsupported system message content")
    else:
        raise HTTPException(422, "System prompts are not supported on this endpoint")
    return body


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
