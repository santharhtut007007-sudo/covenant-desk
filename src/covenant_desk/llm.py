"""Local LLM client (Ollama). Free, private, no API key: loan documents never leave the machine.

Responses are cached on disk by a hash of the request, so re-runs are free, fast and exactly
reproducible. Each call records time and token counts for the evaluation's cost column.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass

import httpx
from pydantic import BaseModel

from covenant_desk import config

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL = os.environ.get("COVENANT_DESK_MODEL", "qwen2.5vl:7b")


@dataclass
class CallStats:
    seconds: float
    prompt_tokens: int
    output_tokens: int
    cached: bool


class LocalLLM:
    def __init__(self, model: str | None = None, num_ctx: int = 16384, temperature: float = 0.0,
                 seed: int = 42, timeout: float = 600):
        self.model = model or DEFAULT_MODEL
        self.options = {"num_ctx": num_ctx, "temperature": temperature, "seed": seed}
        self.timeout = timeout
        self.calls: list[CallStats] = []
        self.cache_dir = config.RAW_DIR / "llm"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def chat_json(self, messages: list[dict], schema: type[BaseModel], use_cache: bool = True) -> BaseModel:
        """Send a chat and force the reply to match a Pydantic schema (Ollama structured output)."""
        body = {
            "model": self.model,
            "messages": messages,
            "format": schema.model_json_schema(),
            "options": self.options,
            "stream": False,
        }
        key = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:32]
        cache_file = self.cache_dir / f"{key}.json"
        if use_cache and cache_file.exists():
            data = json.loads(cache_file.read_text())
            self.calls.append(CallStats(0.0, data.get("prompt_eval_count", 0),
                                        data.get("eval_count", 0), cached=True))
        else:
            start = time.monotonic()
            response = httpx.post(f"{OLLAMA_URL}/api/chat", json=body, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            self.calls.append(CallStats(time.monotonic() - start, data.get("prompt_eval_count", 0),
                                        data.get("eval_count", 0), cached=False))
            cache_file.write_text(json.dumps(data))
        return schema.model_validate_json(data["message"]["content"])

    def usage(self) -> dict:
        return {
            "model": self.model,
            "calls": len(self.calls),
            "seconds": round(sum(c.seconds for c in self.calls), 1),
            "prompt_tokens": sum(c.prompt_tokens for c in self.calls),
            "output_tokens": sum(c.output_tokens for c in self.calls),
            "cost_usd": 0.0,  # local model
        }
