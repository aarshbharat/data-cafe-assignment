"""Runtime configuration: paths, LLM provider settings, and model pricing."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "fmcg-sales-copilot-ai-engineer-mid-4to6"
BUILD_DIR = PROJECT_ROOT / "build"

# ── LLM provider (OpenAI-compatible) ───────────────────────────────────────
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_API_BASE = os.environ.get("LLM_API_BASE", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")
LLM_TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT_S", "12"))

# USD per 1M tokens (input, output). Published list prices.
PRICE_TABLE = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "o4-mini": (1.10, 4.40),
}
_DEFAULT_PRICE = (0.15, 0.60)


def price_per_1m(model: str) -> tuple[float, float]:
    """Return (input, output) USD per 1M tokens for `model`.

    An explicit LLM_PRICE_IN / LLM_PRICE_OUT pair always wins, so a model the
    table does not know about can still be costed honestly at deploy time.
    """
    env_in = os.environ.get("LLM_PRICE_IN")
    env_out = os.environ.get("LLM_PRICE_OUT")
    if env_in and env_out:
        return float(env_in), float(env_out)
    m = model.lower()
    if m in PRICE_TABLE:
        return PRICE_TABLE[m]
    for known, price in PRICE_TABLE.items():
        if m.startswith(known):
            return price
    return _DEFAULT_PRICE


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    p_in, p_out = price_per_1m(model)
    return (prompt_tokens / 1_000_000 * p_in) + (completion_tokens / 1_000_000 * p_out)
