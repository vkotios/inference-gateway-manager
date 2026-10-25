"""Deterministic installer configuration builder for Inference Gateway Manager.

Builds a validated config.local.json from the example configuration by keeping
only the selected engines, pointing model paths at the chosen models directory,
and applying the requested recovery options. Run by install.sh or directly:

    python3 installer/generate_config.py --help
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ENGINE_IDS = ("llama", "strata", "kyojin", "dwarfstar", "gufo")

MATCHERS = {
    "llama": [{"exe": "llama-server"}],
    "strata": [{"argv_contains": "serve.server"}],
    "kyojin": [
        {"argv_contains": "tools/qwen/serve.py"},
        {"argv_contains": "tools/glm/serve.py"},
        {"argv_contains": "tools/mimo/serve.py"},
    ],
    "dwarfstar": [{"exe": "ds4-server"}],
    "gufo": [{"exe": "gufo"}, {"exe": ".gufo-wrapped"}, {"exe": "gufo-server"}],
}

EXAMPLE_MODEL_ROOT = "/opt/models"


def _rewrite(value, models_dir):
    if isinstance(value, str) and value.startswith(EXAMPLE_MODEL_ROOT):
        suffix = value[len(EXAMPLE_MODEL_ROOT) :]
        return str(models_dir) + suffix
    if isinstance(value, list):
        return [_rewrite(item, models_dir) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite(item, models_dir) for key, item in value.items()}
    return value


def _substitute_model_paths(spec, models_dir):
    if not isinstance(spec, dict):
        return json.loads(json.dumps(spec))
    return json.loads(json.dumps(_rewrite(spec, models_dir)))


def build_config(selected_engines, models_dir, enable_mcp, restart_gateway, reboot_host):
    if not selected_engines:
        raise ValueError("At least one engine must be selected")
    unknown = [engine for engine in selected_engines if engine not in ENGINE_IDS]
    if unknown:
        raise ValueError(f"Unknown engine(s): {', '.join(unknown)}")
    example = json.loads((ROOT / "examples" / "config.example.json").read_text())
    engines = {}
    for engine_id in ENGINE_IDS:
        if engine_id in selected_engines:
            engines[engine_id] = {
                key: _substitute_model_paths(value, models_dir)
                for key, value in example["engines"][engine_id].items()
            }
    config = {
        "host": example.get("host", "127.0.0.1"),
        "port": example.get("port", 9000),
        "recovery": {
            "restart_gateway": bool(restart_gateway),
            "reboot_host": bool(reboot_host),
        },
        "allowed_hosts": example.get("allowed_hosts", ["127.0.0.1", "localhost"]),
        "allowed_networks": example.get("allowed_networks", ["127.0.0.0/8"]),
        "native_matchers": [
            matcher
            for engine in ENGINE_IDS
            if engine in selected_engines
            for matcher in MATCHERS[engine]
        ],
        "engines": engines,
        "model_roots": {"models": str(models_dir)},
    }
    if enable_mcp:
        config["mcp"] = example["mcp"] if "mcp" in example else {}
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build a validated IGM config.local.json")
    parser.add_argument(
        "--engine",
        action="append",
        required=True,
        choices=ENGINE_IDS,
        help="Engine to include; repeat for multiple engines",
    )
    parser.add_argument(
        "--models-dir",
        type=Path,
        required=True,
        help="Directory holding the model files this gateway serves",
    )
    parser.add_argument(
        "--enable-mcp",
        action="store_true",
        help="Enable the optional remote MCP adapter configuration",
    )
    parser.add_argument(
        "--restart-gateway",
        action="store_true",
        help="Expose the explicit gateway-restart recovery action",
    )
    parser.add_argument(
        "--reboot-host",
        action="store_true",
        help="Expose the explicit host-reboot recovery action",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Destination path for the generated config.local.json",
    )
    args = parser.parse_args(argv)

    try:
        config = build_config(
            sorted(set(args.engine)),
            args.models_dir,
            args.enable_mcp,
            args.restart_gateway,
            args.reboot_host,
        )
        from gateway.config import validate

        validate(config)
    except (ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    written = json.dumps(config, indent=2) + "\n"
    if args.output.exists():
        existing = args.output.read_text()
        if existing == written:
            args.output.write_text(written)
            return 0
        print(f"error: refusing to overwrite existing {args.output}", file=sys.stderr)
        return 1
    args.output.write_text(written)
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
