"""CPU GGUF embeddings over a private llama-server Unix socket."""

import asyncio
from pathlib import Path

import httpx
from fastapi import HTTPException


def launch_args(spec, directory, memory_limit):
    context = str(spec["context"])
    threads = str(spec.get("threads", 4))
    return [
        "prlimit",
        f"--as={memory_limit}",
        "--",
        spec["runner"],
        "--model",
        spec["path"],
        "--host",
        str(Path(directory) / "llama.sock"),
        "--embeddings",
        "--device",
        "none",
        "--gpu-layers",
        "0",
        "--fit",
        "off",
        "--ctx-size",
        context,
        "--batch-size",
        context,
        "--ubatch-size",
        context,
        "--parallel",
        "1",
        "--threads",
        threads,
        "--threads-batch",
        threads,
        "--cache-ram",
        "0",
        "--repack" if spec.get("repack", False) else "--no-repack",
    ]


def client(directory):
    transport = httpx.AsyncHTTPTransport(uds=str(Path(directory) / "llama.sock"))
    return httpx.AsyncClient(transport=transport, base_url="http://llama", timeout=120)


async def wait_ready(process, directory):
    async with asyncio.timeout(180), client(directory) as http:
        while process.returncode is None:
            try:
                response = await http.get("/health", timeout=2)
                if response.is_success and response.json().get("status") == "ok":
                    return
            except httpx.RequestError:
                pass
            await asyncio.sleep(0.2)
    raise ValueError("llama-server exited before becoming ready")


async def embed(directory, texts, context):
    async with client(directory) as http:
        try:
            return await _embed(http, texts, context)
        except httpx.HTTPError as exc:
            raise RuntimeError("llama-server embedding request failed") from exc


async def _embed(http, texts, context):
    batches = []
    batch = []
    batch_tokens = 0
    tokens = 0
    for text in texts:
        response = await http.post(
            "/tokenize", json={"content": text, "parse_special": True, "add_special": False}
        )
        response.raise_for_status()
        ids = response.json()["tokens"]
        # llama-server requires one slot position beyond the input tokens.
        if not ids or len(ids) >= context:
            raise HTTPException(400, f"Input must contain between 1 and {context - 1} tokens")
        if batch and (len(batch) == 8 or batch_tokens + len(ids) > context):
            batches.append(batch)
            batch = []
            batch_tokens = 0
        batch.append(ids)
        batch_tokens += len(ids)
        tokens += len(ids)
    batches.append(batch)

    vectors = []
    for batch in batches:
        response = await http.post("/v1/embeddings", json={"input": batch})
        response.raise_for_status()
        data = sorted(response.json()["data"], key=lambda entry: entry["index"])
        if [entry["index"] for entry in data] != list(range(len(batch))):
            raise ValueError("Unexpected llama-server embedding indices")
        vectors.extend(entry["embedding"] for entry in data)
    return {"vectors": vectors, "tokens": tokens}
