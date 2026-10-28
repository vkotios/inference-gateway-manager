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
    seen = set()
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
