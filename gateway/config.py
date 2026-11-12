"""Validate administrator-owned launch configuration before serving requests."""

import ipaddress
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from .maintenance import validate_options
from .settings import validate_capabilities


def validate(config):
    engines = config.get("engines")
    if not isinstance(engines, dict) or not engines:
        raise ValueError("At least one engine is required")
    recovery = config.get("recovery", {})
    if (
        not isinstance(recovery, dict)
        or set(recovery) - {"restart_gateway", "reboot_host"}
        or any(type(value) is not bool for value in recovery.values())
    ):
        raise ValueError("recovery accepts only restart_gateway/reboot_host boolean options")
    if "maintenance" in config:
        validate_options(config["maintenance"])
    auxiliary = config.get("auxiliary", {})
    if not isinstance(auxiliary, dict) or set(auxiliary) - {
        "minimum_free_bytes",
        "chat_growth_bytes",
        "host_baseline_bytes",
        "restore",
        "restore_holdoff_seconds",
        "drain_seconds",
        "worker_python",
        "transcription",
        "embeddings",
    }:
        raise ValueError("Invalid auxiliary service configuration")
    for key in ("minimum_free_bytes", "host_baseline_bytes"):
        if key in auxiliary and (type(auxiliary[key]) is not int or auxiliary[key] <= 0):
            raise ValueError(f"{key} must be a positive integer")
    if "chat_growth_bytes" in auxiliary and (
        type(auxiliary["chat_growth_bytes"]) is not int or auxiliary["chat_growth_bytes"] < 0
    ):
        raise ValueError("chat_growth_bytes must be a non-negative integer")
    if auxiliary.get("restore", "never") not in ("never", "after_clean_shutdown", "on_demand"):
        raise ValueError("restore must be never, after_clean_shutdown or on_demand")
    if "restore_holdoff_seconds" in auxiliary and (
        type(auxiliary["restore_holdoff_seconds"]) is not int
        or auxiliary["restore_holdoff_seconds"] < 0
    ):
        raise ValueError("restore_holdoff_seconds must be a non-negative integer")
    drain = auxiliary.get("drain_seconds", {})
    if type(drain) is int:
        drain = {"embeddings": drain, "transcription": drain}
    if (
        not isinstance(drain, dict)
        or set(drain) - {"embeddings", "transcription"}
        or any(type(v) is not int or v < 0 for v in drain.values())
    ):
        raise ValueError("drain_seconds must be a non-negative integer or a per-service object")
    footprints = config.get("footprints", {})
    if not isinstance(footprints, dict) or set(footprints) - {
        "interval_seconds",
        "settle_seconds",
        "abort_below_available_bytes",
    }:
        raise ValueError("Invalid footprints configuration")
    for key in ("interval_seconds", "settle_seconds"):
        if key in footprints and (
            type(footprints[key]) not in (int, float)
            or footprints[key] < 0
            or (key == "interval_seconds" and footprints[key] <= 0)
        ):
            raise ValueError(f"footprints.{key} must be a non-negative number")
    if "abort_below_available_bytes" in footprints and (
        type(footprints["abort_below_available_bytes"]) is not int
        or footprints["abort_below_available_bytes"] <= 0
    ):
        raise ValueError("footprints.abort_below_available_bytes must be a positive integer")
    seeds = config.get("memory_footprints", [])
    if not isinstance(seeds, list):
        raise ValueError("memory_footprints must be a list")
    for item in seeds:
        if (
            not isinstance(item, dict)
            or set(item)
            - {"kind", "key", "steady_bytes", "peak_bytes", "request_bytes", "context", "note"}
            or item.get("kind") not in ("chat", "transcription", "embeddings")
            or not isinstance(item.get("key"), str)
            or not item["key"]
            or type(item.get("steady_bytes")) is not int
            or item["steady_bytes"] <= 0
            or any(
                key in item and (type(item[key]) is not int or item[key] < 0)
                for key in ("peak_bytes", "request_bytes")
            )
            or ("context" in item and (type(item["context"]) is not int or item["context"] <= 0))
            or ("note" in item and (not isinstance(item["note"], str) or len(item["note"]) > 200))
        ):
            raise ValueError("Invalid memory_footprints entry")
    if "worker_python" in auxiliary and (
        not isinstance(auxiliary["worker_python"], str)
        or not Path(auxiliary["worker_python"]).is_absolute()
    ):
        raise ValueError("worker_python must be an absolute path")
    auxiliary_names = set()
    for name in ("transcription", "embeddings"):
        service = auxiliary.get(name, {})
        if not isinstance(service, dict) or set(service) - {"models"}:
            raise ValueError("Invalid auxiliary service")
        models = service.get("models", {})
        if not isinstance(models, dict):
            raise ValueError("Auxiliary models must be an object")
        for model_id, spec in models.items():
            if not isinstance(model_id, str) or not model_id or not isinstance(spec, dict):
                raise ValueError("Invalid auxiliary model")
            if model_id in auxiliary_names:
                raise ValueError("Auxiliary model IDs must be unique")
            auxiliary_names.add(model_id)
            if set(spec) - {"path", "load_bytes", "request_bytes", "threads"}:
                raise ValueError("Invalid auxiliary model fields")
            if not isinstance(spec.get("path"), str) or not Path(spec["path"]).is_absolute():
                raise ValueError("Auxiliary model path must be absolute")
            # Budgets are optional seeds for the first load; measured footprints replace them.
            for key in ("load_bytes", "request_bytes"):
                if key in spec and (type(spec[key]) is not int or spec[key] <= 0):
                    raise ValueError("Auxiliary model budgets must be positive integers")
            if type(spec.get("threads", 4)) is not int or not 1 <= spec.get("threads", 4) <= 64:
                raise ValueError("Auxiliary threads must be between 1 and 64")
    browser = config.get("browser_auth", {})
    if (
        not isinstance(browser, dict)
        or set(browser) - {"enabled", "allow_insecure_http_sessions", "public_origin"}
        or any(
            type(browser[key]) is not bool
            for key in ("enabled", "allow_insecure_http_sessions")
            if key in browser
        )
    ):
        raise ValueError(
            "browser_auth accepts enabled/allow_insecure_http_sessions booleans and public_origin"
        )
    origins = config.get("allowed_origins", [])
    error = (
        "allowed_origins must be a list of exact HTTP/HTTPS origins without credentials or paths"
    )
    if not isinstance(origins, list):
        raise ValueError(error)
    for origin in origins + ([browser["public_origin"]] if "public_origin" in browser else []):
        if not isinstance(origin, str) or not re.fullmatch(r"https?://[^\s\\?#*]+", origin):
            raise ValueError(error)
        try:
            url = urlsplit(origin)
            host = url.hostname
            port = url.port
            if (
                not host
                or url.username is not None
                or url.password is not None
                or url.path
                or url.netloc.endswith(":")
                or (port is not None and not 1 <= port <= 65535)
            ):
                raise ValueError(error)
            try:
                ipaddress.ip_address(host)
            except ValueError:
                if len(host) > 253 or not all(
                    re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
                    for label in host.rstrip(".").split(".")
                ):
                    raise ValueError(error) from None
        except ValueError:
            raise ValueError(error) from None
    roots = config.get("model_roots", {})
    if not isinstance(roots, dict) or any(
        not isinstance(name, str)
        or not re.fullmatch(r"[a-zA-Z0-9_.-]+", name)
        or not isinstance(path, str)
        or not Path(path).is_absolute()
        for name, path in roots.items()
    ):
        raise ValueError("model_roots must map safe IDs to absolute host directories")
    seen = set(auxiliary_names)
    for name, engine in engines.items():
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name):
            raise ValueError("Invalid engine ID")
        profiles = engine.get("profiles", {})
        if not isinstance(profiles, dict):
            raise ValueError("profiles must be an object")
        for profile_id, profile in profiles.items():
            if not re.fullmatch(r"[a-zA-Z0-9_.-]+", profile_id) or profile.get("format") not in {
                "gguf",
                "strata",
                "kyojin",
                "native",
            }:
                raise ValueError("Invalid launch profile ID or artifact format")
            for field in ("required_files", "extensions"):
                entries = profile.get(field, [])
                if not isinstance(entries, list) or any(
                    not isinstance(p, str)
                    or not p
                    or Path(p).is_absolute()
                    or ".." in Path(p).parts
                    for p in entries
                ):
                    raise ValueError("Profile companion paths must be relative and confined")
            if not (
                any("{path}" in a for a in profile.get("argv", []))
                or "{path}" in json.dumps(profile.get("config_template", {}))
            ):
                raise ValueError("Launch profile needs an administrator {path} argument binding")
            artifacts = profile.get("artifacts", {})
            if not isinstance(artifacts, dict):
                raise ValueError("Profile artifacts must be named declarations")
            for artifact_name, definition in artifacts.items():
                if (
                    not re.fullmatch(r"[a-zA-Z0-9_.-]+", artifact_name)
                    or not isinstance(definition, dict)
                    or set(definition) - {"kind", "required_files", "extensions", "format"}
                    or definition.get("kind") not in {"file", "directory"}
                ):
                    raise ValueError("Invalid profile artifact declaration")
                if definition.get("format") not in {None, "gguf"}:
                    raise ValueError("Unsupported companion artifact format")
                for item in definition.get("required_files", []):
                    if (
                        not isinstance(item, str)
                        or Path(item).is_absolute()
                        or ".." in Path(item).parts
                    ):
                        raise ValueError("Companion paths must be relative and confined")
            supported = profile.get("supported_architectures", [])
            if not isinstance(supported, list) or any(
                not isinstance(a, str) or not a for a in supported
            ):
                raise ValueError("supported_architectures must be a string list")
            if "{" in profile["argv"][0] or "}" in profile["argv"][0]:
                raise ValueError("Profile executable must be administrator owned and fixed")
            validate_runtime(profile)
        for model, spec in engine["models"].items():
            if model in seen or not re.fullmatch(r"[a-zA-Z0-9_.-]+", model):
                raise ValueError("Model IDs must be unique safe identifiers")
            seen.add(model)
            if not spec.get("enabled", True):
                continue
            validate_runtime(spec)
    for matcher in config.get("native_matchers", []):
        if not matcher.get("exe") and not matcher.get("argv_contains"):
            raise ValueError("Empty native process matcher is forbidden")
    for network in config.get("allowed_networks", []):
        ipaddress.ip_network(network)
    ipaddress.ip_address(config.get("host", "127.0.0.1"))
    if not 1 <= config.get("port", 9000) <= 65535:
        raise ValueError("Invalid listening port")
    if "engine_updates" in config:
        from .updates import validate_options as validate_updates

        validate_updates(config)
    return config


def validate_runtime(spec):
    if spec.get("generation_metrics") not in (None, "lucebox"):
        raise ValueError("generation_metrics must be lucebox when configured")
    try:
        url = urlsplit(spec["base_url"])
        loopback = ipaddress.ip_address(url.hostname).is_loopback
        valid_url = (
            url.scheme == "http"
            and loopback
            and not url.path
            and url.port
            and not url.username
            and not url.password
            and not url.query
            and not url.fragment
        )
    except (ValueError, KeyError, TypeError):
        valid_url = False
    if not valid_url:
        raise ValueError("Backend must be an HTTP loopback URL with a port and no path")
    if (
        not isinstance(spec.get("argv"), list)
        or not spec["argv"]
        or not all(isinstance(arg, str) and arg and "\0" not in arg for arg in spec["argv"])
    ):
        raise ValueError("Model argv must be a nonempty string array")
    if spec.get("probe", "slots") not in ("slots", "gateway_only"):
        raise ValueError("Unknown occupancy probe")
    if not isinstance(spec.get("ready_path", "/health"), str) or not spec.get(
        "ready_path", "/health"
    ).startswith("/"):
        raise ValueError("Ready path must be an absolute URL path")
    template = spec.get("config_template")
    if template is not None:
        if (
            not isinstance(template, dict)
            or not isinstance(template.get("exe"), str)
            or not Path(template["exe"]).is_absolute()
            or "{" in template["exe"]
            or "}" in template["exe"]
            or not isinstance(template.get("args"), list)
            or any(not isinstance(arg, str) or "\0" in arg for arg in template["args"])
            or not any("{config_path}" in arg for arg in spec["argv"])
        ):
            raise ValueError(
                "config_template needs a fixed executable, args and {config_path} launch binding"
            )
        try:
            json.dumps(template, allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            raise ValueError("config_template must be finite JSON") from None
    elif any("{config_path}" in arg for arg in spec["argv"]):
        raise ValueError("{config_path} requires an administrator config_template")
    validate_capabilities(spec)
