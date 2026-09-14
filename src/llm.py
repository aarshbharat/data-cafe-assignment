"""A small OpenAI-compatible chat client with real cost and latency metering.

Cost is taken from the `usage` block the provider returns, priced against the
published rate for the configured model — not estimated from word counts. A
call that fails returns an `LLMResult` with `ok=False` and the reason, so the
caller can tell "the model was unreachable" apart from "the data cannot answer
this". Collapsing those two is how the previous version reported every outage
as an unanswerable question.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import httpx

from src import config


@dataclass
class LLMResult:
    ok: bool
    text: str = ""
    error: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0


@dataclass
class Meter:
    """Accumulates what one HTTP request spent across all its model calls."""

    cost_usd: float = 0.0
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: list[str] = field(default_factory=list)

    def add(self, label: str, r: LLMResult) -> LLMResult:
        self.cost_usd += r.cost_usd
        self.latency_ms += r.latency_ms
        self.prompt_tokens += r.prompt_tokens
        self.completion_tokens += r.completion_tokens
        self.calls.append(f"{label}:{'ok' if r.ok else 'failed'}")
        return r


class LLMClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, timeout: float | None = None) -> None:
        self.api_key = api_key if api_key is not None else config.LLM_API_KEY
        self.base_url = (base_url or config.LLM_API_BASE).rstrip("/")
        self.model = model or config.LLM_MODEL
        self.timeout = timeout or config.LLM_TIMEOUT_S
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            timeout=self.timeout,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def chat(self, system: str, user: str, max_tokens: int = 400,
                   temperature: float = 0.0, json_mode: bool = False) -> LLMResult:
        """One chat completion. Never raises; failures come back as ok=False."""
        if not self.configured:
            return LLMResult(ok=False, error="no LLM_API_KEY configured")
        if self._client is None:
            await self.start()

        payload: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        start = time.perf_counter()
        try:
            resp = await self._client.post("/chat/completions", json=payload)
            elapsed = (time.perf_counter() - start) * 1000
            if resp.status_code >= 400:
                return LLMResult(ok=False, latency_ms=elapsed,
                                 error=f"HTTP {resp.status_code} from the LLM provider")
            body = resp.json()
            text = body["choices"][0]["message"]["content"] or ""
            usage = body.get("usage") or {}
            pt = int(usage.get("prompt_tokens", 0))
            ct = int(usage.get("completion_tokens", 0))
            return LLMResult(ok=True, text=text.strip(), prompt_tokens=pt,
                             completion_tokens=ct,
                             cost_usd=config.cost_usd(self.model, pt, ct),
                             latency_ms=elapsed)
        except Exception as exc:
            return LLMResult(ok=False, latency_ms=(time.perf_counter() - start) * 1000,
                             error=f"{type(exc).__name__}: {exc}")


def parse_json(text: str) -> dict | None:
    """Pull a JSON object out of a model reply, fenced or not."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        t = t.rsplit("```", 1)[0]
    t = t.strip()
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if 0 <= start < end:
            try:
                v = json.loads(t[start:end + 1])
                return v if isinstance(v, dict) else None
            except json.JSONDecodeError:
                return None
    return None
