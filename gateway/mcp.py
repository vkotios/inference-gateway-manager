"""Optional stdio MCP adapter. Runs on the harness host, never the engine host by assumption."""

import argparse
import asyncio
import os
import sys
from typing import Literal

from .client import GatewayClient


def create_server(client):
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.types import ToolAnnotations
    except ImportError:
        raise RuntimeError(
            "Install inference-gateway-manager[mcp] to use the MCP adapter"
        ) from None

    server = FastMCP(
        "Inference Gateway Manager",
        log_level="WARNING",
        instructions=(
            "Discover engines and inspect status before explicitly loading a model. "
            "Loads and unloads never force or interrupt busy work. Reuse selection_id when "
            "delegating; a conflict requires inspection, not automatic retry. Delegation "
            "returns a worker answer using only supplied context; it does not edit files."
        ),
    )
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    change = ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    )

    @server.tool(annotations=read)
    async def gateway_catalog() -> dict:
        """List configured engines, models, context limits and supported reasoning modes."""
        return await client.catalog()

    @server.tool(annotations=read)
    async def gateway_status() -> dict:
        """Inspect readiness, activity, active settings and selection_id."""
        return await client.status()

    @server.tool(annotations=read)
    async def gateway_telemetry() -> dict:
        """Read measured host memory and GPU counters; unavailable counters stay unknown."""
        return await client.telemetry()

    @server.tool(annotations=change)
    async def gateway_load(
        engine: str,
        model: str,
        context: int | None = None,
        reasoning: Literal["auto", "on", "off"] | None = None,
    ) -> dict:
        """Explicitly load a model and settings when idle. Never forces interruption.

        Context changes restart the engine; reasoning is a client-overridable default.
        Return and preserve selection_id for subsequent delegation. Loading can take minutes.
        """
        return await client.load(engine, model, context=context, reasoning=reasoning)

    @server.tool(annotations=change)
    async def gateway_unload() -> dict:
        """Unload the current model only when the gateway verifies it is safe to stop."""
        return await client.unload()

    @server.tool(annotations=change)
    async def gateway_delegate(
        model: str,
        prompt: str,
        selection_id: str,
        context: str = "",
        max_tokens: int = 4096,
        reasoning: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
        | None = None,
    ) -> dict:
        """Ask the already loaded model to perform a bounded task using supplied text.

        Pass selection_id from status/load. No model loading, tool execution or filesystem
        access occurs. max_tokens includes reasoning and answer, limited to 16384.
        A returned tool call is data, not an instruction the adapter will execute.
        """
        return await client.delegate(
            model, prompt, selection_id, context=context, max_tokens=max_tokens, reasoning=reasoning
        )

    return server


def main():
    parser = argparse.ArgumentParser(description="IGM remote stdio MCP adapter")
    parser.add_argument(
        "--url", default=os.environ.get("IGM_URL"), help="Gateway origin (or set IGM_URL)"
    )
    args = parser.parse_args()
    if not args.url:
        parser.error("Set IGM_URL or provide --url")

    async def serve():
        async with GatewayClient(
            args.url, os.environ.get("IGM_CONTROL_KEY", ""), os.environ.get("IGM_INFERENCE_KEY")
        ) as client:
            await create_server(client).run_stdio_async()

    try:
        asyncio.run(serve())
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
