"""Validate administrator-owned launch configuration before serving requests."""

import ipaddress
import re
from urllib.parse import urlsplit


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
    origins = config.get("allowed_origins", [])
    error = (
        "allowed_origins must be a list of exact HTTP/HTTPS origins without credentials or paths"
    )
    if not isinstance(origins, list):
        raise ValueError(error)
    for origin in origins:
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
    seen = set()
    for name, engine in engines.items():
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name):
            raise ValueError("Invalid engine ID")
        for model, spec in engine["models"].items():
            if model in seen or not re.fullmatch(r"[a-zA-Z0-9_.-]+", model):
                raise ValueError("Model IDs must be unique safe identifiers")
            seen.add(model)
            if not spec.get("enabled", True):
                continue
            url = urlsplit(spec["base_url"])
            try:
                loopback = ipaddress.ip_address(url.hostname).is_loopback
            except ValueError:
                loopback = False
            if url.scheme != "http" or not loopback or url.path or not url.port or url.username:
                raise ValueError("Backend must be an HTTP loopback URL with a port and no path")
            if (
                not isinstance(spec.get("argv"), list)
                or not spec["argv"]
                or not all(isinstance(arg, str) and arg and "\0" not in arg for arg in spec["argv"])
            ):
                raise ValueError("Model argv must be a nonempty string array")
            if spec.get("probe", "slots") not in ("slots", "gateway_only"):
                raise ValueError("Unknown occupancy probe")
    for matcher in config.get("native_matchers", []):
        if not matcher.get("exe") and not matcher.get("argv_contains"):
            raise ValueError("Empty native process matcher is forbidden")
    for network in config.get("allowed_networks", []):
        ipaddress.ip_network(network)
    ipaddress.ip_address(config.get("host", "127.0.0.1"))
    if not 1 <= config.get("port", 9000) <= 65535:
        raise ValueError("Invalid listening port")
    return config
