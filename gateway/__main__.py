"""CLI. Credentials are generated locally and never printed."""

import argparse
import fcntl
import json
import os
import secrets
from pathlib import Path

from .config import validate


def main():
    parser = argparse.ArgumentParser(description="LAN inference gateway manager (Linux)")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser(
        "init-keys", help="Create a private credential file; never overwrite"
    )
    init.add_argument("--output", default=".local/credentials.env")
    serve = commands.add_parser("serve", help="Serve UI, control API and inference proxy")
    serve.add_argument("--config", required=True)
    serve.add_argument("--state-dir", default=".local/state")
    recovery = commands.add_parser("recovery", help="Schedule a disruptive remote recovery action")
    recovery.add_argument("action", choices=["restart-gateway", "reboot-host"])
    recovery.add_argument("--url", required=True, help="Gateway URL; use TLS or a trusted tunnel")
    recovery.add_argument(
        "--confirm", required=True, help="Repeat the exact action; interrupts work"
    )
    args = parser.parse_args()
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
