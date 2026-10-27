"""Build a fresh, unloaded host configuration from the public engine catalog."""

import argparse
import copy
import ipaddress
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def example_config():
    return json.loads((ROOT / "examples/config.example.json").read_text())


# Shared by CLI and shell discovery; engine choices follow the public catalog.
ENGINE_IDS = tuple(example_config()["engines"])
EXAMPLE_MODEL_ROOT = "/opt/models"


def _rewrite(value, models_dir):
    if isinstance(value, str) and (
        value == EXAMPLE_MODEL_ROOT or value.startswith(EXAMPLE_MODEL_ROOT + "/")
    ):
        return str(models_dir / Path(value).relative_to(EXAMPLE_MODEL_ROOT))
    if isinstance(value, list):
        return [_rewrite(item, models_dir) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite(item, models_dir) for key, item in value.items()}
    return value


def validate_inputs(selected_engines, models_dir, host, allowed_network, allowed_hosts=None):
    if not selected_engines:
        raise ValueError("At least one engine must be selected")
    unknown = set(selected_engines) - set(ENGINE_IDS)
    if unknown:
        raise ValueError(f"Unknown engine(s): {', '.join(sorted(unknown))}")
    models_dir = Path(models_dir).expanduser()
    if not models_dir.is_absolute():
        raise ValueError("Models directory must be absolute")
    bind = ipaddress.ip_address(host)
    if bind.is_unspecified or bind.is_multicast or not (bind.is_private or bind.is_loopback):
        raise ValueError("Bind to a specific private or loopback interface address")
    if allowed_network is None:
        if not bind.is_loopback:
            raise ValueError("LAN binding requires an explicit allowed network")
        allowed_network = "127.0.0.0/8" if bind.version == 4 else "::1/128"
    network = ipaddress.ip_network(allowed_network)
    if bind not in network or network.prefixlen == 0:
        raise ValueError("Allowed network must contain the bind address and cannot be unrestricted")
    hosts = [str(bind)] + (["localhost"] if bind.is_loopback else [])
    for name in allowed_hosts or []:
        try:
            ipaddress.ip_address(name)
        except ValueError:
            if len(name) > 253 or not all(
                re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", part)
                for part in name.split(".")
            ):
                raise ValueError(
                    "Allowed host must be an exact hostname or IP, without a port or URL"
                ) from None
        if name not in hosts:
            hosts.append(name)
    return models_dir, bind, network, hosts


def build_config(
    selected_engines,
    models_dir,
    restart_gateway=False,
    reboot_host=False,
    *,
    host="127.0.0.1",
    allowed_network=None,
    allowed_hosts=None,
):
    models_dir, bind, network, hosts = validate_inputs(
        selected_engines,
        models_dir,
        host,
        allowed_network,
        allowed_hosts,
    )
    config = copy.deepcopy(example_config())
    config["engines"] = {
        engine: _rewrite(spec, models_dir)
        for engine, spec in config["engines"].items()
        if engine in selected_engines
    }
    # Detect competing native engines even when they aren't selectable here.
    # In particular ds4 and ds4fa share the ds4-server executable name.
    for engine in config["engines"].values():
        for spec in engine["models"].values():
            spec["enabled"] = False
    config.pop("default", None)
    config.update(
        host=str(bind),
        allowed_hosts=hosts,
        allowed_networks=[str(network)],
        model_roots={"models": str(models_dir)},
        recovery={"restart_gateway": restart_gateway, "reboot_host": reboot_host},
    )
    from gateway.config import validate

    return validate(config)


def write_config(config, output):
    """Never replace an existing config, including dangling symlinks or races."""
    output = Path(output)
    written = json.dumps(config, indent=2) + "\n"
    if output.is_symlink():
        raise ValueError(f"refusing to overwrite existing {output}")
    if output.exists():
        if output.read_text() == written:
            return
        raise ValueError(f"refusing to overwrite existing {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as file:
        file.write(written)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build a validated, unloaded IGM configuration")
    parser.add_argument("--list-engines", action="store_true", help="Print engine IDs and exit")
    parser.add_argument("--engine", action="append", choices=ENGINE_IDS)
    parser.add_argument("--models-dir", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--allowed-network", help="Explicit LAN client subnet in CIDR notation")
    parser.add_argument("--allowed-host", action="append", help="Additional exact hostname or IP")
    parser.add_argument("--restart-gateway", action="store_true")
    parser.add_argument("--reboot-host", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.list_engines:
        print("\n".join(ENGINE_IDS))
        return 0
    if not args.engine or args.models_dir is None or args.output is None:
        parser.error("--engine, --models-dir and --output are required")
    try:
        config = build_config(
            args.engine,
            args.models_dir,
            args.restart_gateway,
            args.reboot_host,
            host=args.host,
            allowed_network=args.allowed_network,
            allowed_hosts=args.allowed_host,
        )
        write_config(config, args.output)
    except (ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Configuration ready: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
