"""Portable gateway client shared by the remote CLI and stdio MCP adapter."""

import json
import re
from urllib.parse import urlsplit

import httpx


class GatewayError(RuntimeError):
    """A sanitized connectivity, protocol or gateway refusal error."""


def gateway_origin(url):
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
            or any(c.isspace() for c in url)
            or "\\" in url
        ):
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError("Use an HTTP/HTTPS gateway origin without credentials or a path") from None
    return url.rstrip("/")


def model_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]+", value):
        raise ValueError("Use a model or engine ID from the gateway catalog")
    return value


def positive_int(value, name, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    return value


class GatewayClient:
    def __init__(self, url, control_key, inference_key=None, *, transport=None):
        self.url = gateway_origin(url)
        if not isinstance(control_key, str) or len(control_key) < 32:
            raise ValueError("Set IGM_CONTROL_KEY in the environment")
        self.control_key = control_key
        self.inference_key = inference_key or control_key
        if not isinstance(self.inference_key, str) or len(self.inference_key) < 32:
            raise ValueError("Set a valid IGM_INFERENCE_KEY in the environment")
        self.http = httpx.AsyncClient(
            base_url=self.url,
            timeout=httpx.Timeout(900, connect=5),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.http.aclose()

    async def request(self, method, path, body=None, *, inference=False, selection_id=None):
        headers = {
            "Authorization": "Bearer " + (self.inference_key if inference else self.control_key),
            "X-Client-Name": "igm-controller",
        }
        if selection_id:
            headers["X-IGM-Selection-ID"] = selection_id
        try:
            async with self.http.stream(method, path, json=body, headers=headers) as response:
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 2 * 1024 * 1024:
                        raise GatewayError("Gateway response exceeds the controller's 2 MiB limit")
                if not response.is_success:
                    # Inference errors may contain upstream request content. Control
                    # refusals can explain how to correct registration/settings.
                    hint = {
                        401: "Check the API credential",
                        403: "Credential or access policy refused",
                        409: "Busy or selection changed; inspect status before retrying",
                        422: "Invalid request settings",
                        503: "Backend unavailable; inspect status",
                    }
                    detail = hint.get(
                        response.status_code, "Request refused; inspect gateway status"
                    )
                    if not inference and path.startswith("/api/"):
                        try:
                            refusal = json.loads(data).get("detail")
                            if isinstance(refusal, str):
                                for credential in (self.control_key, self.inference_key):
                                    refusal = refusal.replace(credential, "[credential]")
                                refusal = "".join(c if c.isprintable() else " " for c in refusal)
                                detail = " ".join(refusal.split())[:300] or detail
                        except (ValueError, AttributeError, RecursionError):
                            pass
                    raise GatewayError(f"Gateway HTTP {response.status_code}: " + detail)
                try:
                    result = json.loads(data)
                except (ValueError, RecursionError):
                    raise GatewayError("Gateway returned an invalid JSON response") from None
                if not isinstance(result, dict):
                    raise GatewayError("Gateway returned an unexpected response")
                return result
        except httpx.HTTPError:
            raise GatewayError(
                "Gateway connection failed or timed out; check its URL and status"
            ) from None

    async def catalog(self):
        return await self.request("GET", "/api/engines")

    async def status(self):
        return await self.request("GET", "/api/status")

    async def telemetry(self):
        return await self.request("GET", "/api/telemetry")

    async def load(self, engine, model, *, context=None, reasoning=None):
        body = {"engine": model_id(engine), "model": model_id(model), "force": False}
        if context is not None:
            body["context"] = positive_int(context, "context", 10_000_000)
        if reasoning is not None:
            if reasoning not in ("auto", "on", "off"):
                raise ValueError("reasoning must be auto, on or off")
            body["reasoning"] = reasoning
        return await self.request("POST", "/api/model", body)

    async def unload(self):
        return await self.request("POST", "/api/unload", {"force": False})

    async def inspect_model(
        self,
        engine,
        path,
        profile=None,
        artifacts=None,
        *,
        model=None,
        context=None,
        reasoning=None,
    ):
        body = {"engine": model_id(engine), "path": path}
        if profile:
            body["profile"] = model_id(profile)
        if artifacts:
            body["artifacts"] = artifacts
        if model is not None:
            body["model"] = model_id(model)
        if context is not None:
            body["context"] = positive_int(context, "context", 10_000_000)
        if reasoning is not None:
            if reasoning not in ("auto", "on", "off"):
                raise ValueError("reasoning must be auto, on or off")
            body["reasoning"] = reasoning
        return await self.request("POST", "/api/models/inspect", body)

    async def add_model(self, candidate):
        return await self.request("POST", "/api/models", candidate)

    async def remove_model(self, engine, model):
        return await self.request("DELETE", f"/api/models/{model_id(engine)}/{model_id(model)}")

    async def delegate(
        self, model, prompt, selection_id, *, context="", max_tokens=4096, reasoning=None
    ):
        positive_int(max_tokens, "max_tokens", 16384)
        if not isinstance(selection_id, str) or not re.fullmatch(
            r"[a-zA-Z0-9_.-]{1,128}", selection_id
        ):
            raise ValueError("Pass the selection_id returned by gateway status or load")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 262144:
            raise ValueError("Supply a task prompt of at most 262144 characters")
        if not isinstance(context, str) or len(context) > 524288:
            raise ValueError("Supplied task context must be at most 524288 characters")
        body = {
            "model": model_id(model),
            "stream": False,
            "max_tokens": max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                    + ("\n\nSupplied task context:\n" + context if context else ""),
                }
            ],
        }
        if reasoning is not None:
            if reasoning not in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
                raise ValueError("Unsupported reasoning effort")
            body["reasoning_effort"] = reasoning
        result = await self.request(
            "POST", "/v1/chat/completions", body, inference=True, selection_id=selection_id
        )
        try:
            choice = result["choices"][0]
            message = choice["message"]
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                raise ValueError()
            return {
                "model": model,
                "selection_id": selection_id,
                "content": content or "",
                "reasoning_content": message.get("reasoning_content"),
                "tool_calls": message.get("tool_calls"),
                "finish_reason": choice.get("finish_reason"),
                "usage": result.get("usage"),
            }
        except (KeyError, IndexError, TypeError, ValueError):
            raise GatewayError("Gateway returned an unexpected chat completion") from None


def client_fragment(client, url, model, context, output=4096):
    url = gateway_origin(url)
    model_id(model)
    positive_int(context, "context", 10_000_000)
    positive_int(output, "output", context)
    if client == "opencode":
        return {
            "$schema": "https://opencode.ai/config.json",
            "model": "igm/" + model,
            "provider": {
                "igm": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "IGM",
                    "options": {"baseURL": url + "/v1", "apiKey": "{env:IGM_INFERENCE_KEY}"},
                    "models": {
                        model: {"name": model, "limit": {"context": context, "output": output}}
                    },
                }
            },
        }
    if client == "hermes":
        return {
            "model": {
                "provider": "igm",
                "default": model,
                "base_url": url + "/v1",
                "context_length": context,
            },
            "providers": {
                "igm": {
                    "api": url + "/v1",
                    "key_env": "IGM_INFERENCE_KEY",
                    "transport": "chat_completions",
                    "models": {model: {"context_length": context}},
                }
            },
        }
    raise ValueError("client must be hermes or opencode")
