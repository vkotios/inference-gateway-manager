"""CLI. Credentials are generated locally and never printed."""

import argparse
import asyncio
import json
import os
import secrets
from pathlib import Path

from .config import validate


def main():
    parser = argparse.ArgumentParser(description="LAN inference gateway and remote controller")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser(
        "init-keys", help="Create a private credential file; never overwrite"
    )
    init.add_argument("--output", default=".local/credentials.env")
    password = commands.add_parser("set-password", help="Set/reset the local operator password")
    password.add_argument("--state-dir", default=".local/state")
    models = commands.add_parser("models", help="Inspect/register models on the gateway host")
    model_commands = models.add_subparsers(dest="model_command", required=True)
    for name in ("inspect", "add"):
        register = model_commands.add_parser(name)
        register.add_argument("--url", required=True)
        register.add_argument("--engine", required=True)
        register.add_argument("--path", required=True, help="Absolute path on the gateway host")
        register.add_argument("--profile")
        register.add_argument("--artifact", action="append", default=[], metavar="NAME=HOST_PATH")
        register.add_argument("--model", help="Optional unique model ID")
        register.add_argument("--context", type=int)
        register.add_argument("--reasoning", choices=["auto", "on", "off"])
        if name == "add":
            register.add_argument("--dry-run", action="store_true")
    remove = model_commands.add_parser(
        "remove", help="Remove a saved registration; keep model files"
    )
    remove.add_argument("--url", required=True)
    remove.add_argument("--engine", required=True)
    remove.add_argument("--model", required=True)
    fragment = commands.add_parser("client-config", help="Print a Hermes/OpenCode config fragment")
    fragment.add_argument("--url", required=True)
    fragment.add_argument("--client", required=True, choices=["hermes", "opencode"])
    fragment.add_argument("--model", required=True)
    fragment.add_argument(
        "--context", type=int, help="Optional smaller client budget; never resizes engine"
    )
    fragment.add_argument("--output-tokens", type=int, default=4096)
    serve = commands.add_parser("serve", help="Serve UI, control API and inference proxy")
    serve.add_argument("--config", required=True)
    serve.add_argument("--state-dir", default=".local/state")
    recovery = commands.add_parser("recovery", help="Schedule a disruptive remote recovery action")
    recovery.add_argument("action", choices=["restart-gateway", "reboot-host"])
    recovery.add_argument("--url", required=True, help="Gateway URL; use TLS or a trusted tunnel")
    recovery.add_argument(
        "--confirm", required=True, help="Repeat the exact action; interrupts work"
    )
    update_recovery = commands.add_parser(
        "rollback-engine-config",
        help="Offline recovery: restore saved engine configuration while gateway is stopped",
    )
    update_recovery.add_argument("--state-dir", required=True)
    update_recovery.add_argument("--engine", required=True)
    args = parser.parse_args()
    if args.command == "rollback-engine-config":
        from .updates import offline_rollback

        try:
            offline_rollback(args.state_dir, args.engine)
        except (ValueError, OSError, KeyError) as error:
            parser.exit(1, str(error) + "\n")
        print(
            "Configuration restored. Start the gateway unloaded, then authorize rollback verification."
        )
        return
    if args.command == "set-password":
        import getpass

        from .browser_auth import BrowserAuth

        first = getpass.getpass("New operator password: ")
        if first != getpass.getpass("Repeat password: "):
            parser.error("Passwords do not match")
        try:
            BrowserAuth(args.state_dir, {}).set_password(first)
        except (OSError, ValueError) as exc:
            parser.exit(1, str(exc) + "\n")
        print(
            "Password saved; existing browser sessions revoked. Enable browser_auth in configuration."
        )
        return
    if args.command in ("models", "client-config"):
        from .client import GatewayClient, GatewayError, client_fragment

        async def remote():
            async with GatewayClient(args.url, os.environ.get("IGM_CONTROL_KEY", "")) as client:
                if args.command == "client-config":
                    catalog = await client.catalog()
                    state = await client.status()
                    model = next(
                        (
                            m
                            for e in catalog["engines"]
                            for m in e["models"]
                            if m["id"] == args.model and m.get("enabled", True)
                        ),
                        None,
                    )
                    if model is None:
                        raise ValueError("Choose an enabled model from the gateway catalog")
                    capacity = model.get("context")
                    if state.get("loaded_model") == args.model and state.get("phase") == "ready":
                        capacity = (state.get("active_settings") or {}).get("context", capacity)
                    if type(capacity) is not int or capacity <= 0:
                        raise ValueError(
                            "Model context is unknown; configure/verify its capacity first"
                        )
                    budget = args.context if args.context is not None else capacity
                    if budget > capacity:
                        raise ValueError(
                            "Client budget exceeds the served capacity; reload the engine first"
                        )
                    return client_fragment(
                        args.client, args.url, args.model, budget, args.output_tokens
                    )
                if args.model_command == "remove":
                    return await client.remove_model(args.engine, args.model)
                artifacts = {}
                for argument in args.artifact:
                    name, separator, path = argument.partition("=")
                    if not separator or not name or not path or name in artifacts:
                        raise ValueError("Each --artifact must be a unique NAME=HOST_PATH")
                    artifacts[name] = path
                inspection = await client.inspect_model(
                    args.engine,
                    args.path,
                    args.profile,
                    artifacts,
                    model=args.model,
                    context=args.context,
                    reasoning=args.reasoning,
                )
                candidate = inspection["candidate"]
                if args.model_command == "inspect" or getattr(args, "dry_run", False):
                    return inspection
                return await client.add_model(candidate)

        try:
            print(json.dumps(asyncio.run(remote()), indent=2))
        except (ValueError, GatewayError) as exc:
            parser.exit(1, str(exc) + "\n")
        return
    if args.command == "recovery":
        from urllib.parse import urlsplit

        import httpx

        target = urlsplit(args.url)
        if (
            target.scheme not in ("http", "https")
            or not target.hostname
            or target.username
            or target.password
            or target.path not in ("", "/")
            or target.query
            or target.fragment
        ):
            parser.error("--url must be an HTTP/HTTPS gateway origin")
        if args.confirm != args.action:
            parser.error("--confirm must repeat the exact action")
        key = os.environ.get("IGM_CONTROL_KEY", "")
        if len(key) < 32:
            parser.error("Set IGM_CONTROL_KEY in the environment; never pass keys as arguments")
        try:
            result = httpx.post(
                args.url.rstrip("/") + "/api/recovery/" + args.action,
                json={"confirmation": args.confirm},
                headers={"Authorization": "Bearer " + key},
                timeout=15,
                trust_env=False,
                follow_redirects=False,
            )
            result.raise_for_status()
        except httpx.HTTPError:
            parser.exit(
                1,
                "Recovery request failed; check connectivity, configuration and OS permissions.\n",
            )
        print(json.dumps(result.json()))
        return
    if args.command == "init-keys":
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("IGM_CONTROL_KEY=" + secrets.token_urlsafe(48) + "\n")
            f.write("IGM_INFERENCE_KEY=" + secrets.token_urlsafe(48) + "\n")
        print("Private credentials created at", path)
        return
    config = validate(json.loads(Path(args.config).read_text()))
    state = Path(args.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    # Kept open for the entire server lifetime. Never use multiple workers.
    import fcntl

    with (state / "manager.lock").open("a") as lease:
        fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        import uvicorn

        from .app import create_app

        app = create_app(
            config,
            os.environ.get("IGM_CONTROL_KEY", ""),
            os.environ.get("IGM_INFERENCE_KEY", ""),
            state,
        )
        uvicorn.run(
            app,
            host=config.get("host", "127.0.0.1"),
            port=config.get("port", 9000),
            workers=1,
            proxy_headers=False,
            access_log=False,
            timeout_graceful_shutdown=15,
        )


if __name__ == "__main__":
    main()
