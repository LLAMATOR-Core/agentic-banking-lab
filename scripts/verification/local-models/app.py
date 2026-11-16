"""Test deployment provider: real local multilingual embeddings, DeepSeek chat.

No application policies or stored records are changed by this adapter.
"""

import asyncio
import hmac
import json
import os
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastembed import TextEmbedding

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
model = None
lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(app):
    global model
    model = await asyncio.to_thread(TextEmbedding, model_name=MODEL, cache_dir="/models", threads=2)
    yield


app = FastAPI(lifespan=lifespan)


def audit(kind, data):
    # Only synthetic test inputs. Never write HTTP credentials or environment.
    with open("/evidence/provider.jsonl", "a", encoding="utf-8") as f:
        f.write(
            json.dumps({"time": time.time(), "kind": kind, "data": data}, ensure_ascii=False) + "\n"
        )


def authorize(request):
    expected = "Bearer " + os.environ["UPSTREAM_API_KEY"]
    if not hmac.compare_digest(request.headers.get("authorization", ""), expected):
        raise HTTPException(401, "Invalid provider credential")


@app.get("/health")
def health():
    return {
        "ready": model is not None,
        "embedding_model": MODEL,
        "chat_provider": "api.deepseek.com",
    }


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    authorize(request)
    body = await request.json()
    if body.get("model") != MODEL:
        raise HTTPException(400, "Unknown embedding model")
    texts = body.get("input", [])
    if isinstance(texts, str):
        texts = [texts]
    if not texts or len(texts) > 256 or any(not isinstance(x, str) for x in texts):
        raise HTTPException(400, "Expected 1..256 strings")
    audit("embedding-input", {"model": MODEL, "input": texts})
    async with lock:
        vectors = await asyncio.to_thread(
            lambda: [v.tolist() for v in model.embed(texts, batch_size=8)]
        )
    return {
        "object": "list",
        "model": MODEL,
        "data": [
            {"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)
        ],
    }


@app.post("/v1/chat/completions")
async def chat(request: Request):
    authorize(request)
    body = await request.json()
    audit("chat-input", body)
    try:
        async with httpx.AsyncClient(timeout=100) as client:
            r = await client.post(
                "https://api.deepseek.com/chat/completions",
                json=body,
                headers={"Authorization": "Bearer " + os.environ["UPSTREAM_API_KEY"]},
            )
    except httpx.RequestError:
        raise HTTPException(503, "Upstream unavailable") from None
    audit("chat-result", {"status": r.status_code})
    return Response(r.content, status_code=r.status_code, media_type="application/json")
