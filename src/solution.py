"""ACPL Sales Focus & Action Assistant — HTTP service.

POST /ask     {"question": "..."}   -> answer | reason, status, evidence, cost, latency
POST /actions {"scope": "West"}     -> [{finding, rule_id, action, state}, ...]

Listens on $PORT.
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src import assistant, config
from src import metrics as M
from src import rules as R
from src.llm import LLMClient
from src.store import DataStore


class State:
    ds: DataStore | None = None
    pre: M.Precomputed | None = None
    llm: LLMClient | None = None
    started_at: float = 0.0
    load_ms: float = 0.0


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    t0 = time.perf_counter()
    state.ds = DataStore().load_all()
    state.pre = M.Precomputed(state.ds)
    state.load_ms = (time.perf_counter() - t0) * 1000
    state.started_at = time.time()

    state.llm = LLMClient()
    if state.llm.configured:
        await state.llm.start()

    ds = state.ds
    print(f"[startup] {len(ds.sales):,} sales rows, {len(ds.targets)} targets, "
          f"{len(ds.stockouts)} stock-outs, {len(ds.promos)} promotions, "
          f"{len(ds.playbook)} playbook rules, {len(ds.documents)} documents "
          f"({state.load_ms:.0f} ms)")
    print(f"[startup] LLM: model={config.LLM_MODEL} base={config.LLM_API_BASE} "
          f"key={'set' if state.llm.configured else 'NOT SET — answers will be computed '
                                                   'without narration'}")
    try:
        yield
    finally:
        if state.llm:
            await state.llm.aclose()


app = FastAPI(title="ACPL Sales Focus & Action Assistant", version="2.0.0",
              lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


# ── Contract models ────────────────────────────────────────────────────────

class AskRequest(BaseModel):
    question: str = ""


class ActionsRequest(BaseModel):
    scope: str = "all"
    # Extra, optional: widen or narrow the evaluation window. Documented in README.
    period: str | None = Field(default=None)


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.post("/ask")
async def ask(req: AskRequest):
    """Answer a question, or say why none is given. Always 200 with a status."""
    started = time.perf_counter()
    if state.ds is None or state.pre is None:
        return JSONResponse({
            "answer": "The service is still loading its data; retry shortly.",
            "status": "NO_ANSWER", "evidence": [], "cost_usd": 0.0,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }, status_code=503)
    try:
        return await assistant.answer(state.ds, state.pre, req.question, state.llm)
    except Exception as exc:                    # an internal fault is not an answer
        print(f"[ask] unhandled: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        return {
            "answer": "I hit an internal error working that out, so I am not returning a "
                      "figure I cannot stand behind.",
            "status": "NO_ANSWER", "evidence": [], "cost_usd": 0.0,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "error_type": type(exc).__name__,
        }


@app.post("/actions")
async def actions(req: ActionsRequest):
    """The recommended action list for a region, or 'all'.

    An unknown scope returns an empty list: there is no such region, so there is
    no action the playbook sanctions for it.
    """
    started = time.perf_counter()
    if state.ds is None or state.pre is None:
        return JSONResponse([], status_code=503)
    try:
        months, label = M.months_in_period(state.ds, req.period) if req.period \
            else (None, "")
        if req.period and not months:
            return []
        acts = R.evaluate(state.ds, state.pre, req.scope, months, label)
        print(f"[actions] scope={req.scope!r} period={req.period!r} -> {len(acts)} "
              f"in {(time.perf_counter() - started) * 1000:.1f} ms")
        return acts
    except Exception as exc:
        print(f"[actions] unhandled: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        return []


@app.get("/health")
async def health():
    ds, pre = state.ds, state.pre
    return {
        "status": "ok" if ds is not None else "loading",
        "uptime_s": round(time.time() - state.started_at, 1) if state.started_at else 0,
        "load_ms": round(state.load_ms, 1),
        "rows": {
            "fact_primary_sales": len(ds.sales) if ds else 0,
            "fact_targets": len(ds.targets) if ds else 0,
            "stockouts": len(ds.stockouts) if ds else 0,
            "promotions": len(ds.promos) if ds else 0,
            "dim_sku": len(ds.sku) if ds else 0,
            "dim_geo": len(ds.geo) if ds else 0,
            "dim_distributor": len(ds.distributors) if ds else 0,
            "facts_brand_region_month": len(ds.facts) if ds else 0,
        },
        "playbook_rules": sorted(ds.playbook) if ds else [],
        "documents": [d["file"] for d in ds.documents] if ds else [],
        "llm": {"model": config.LLM_MODEL, "base_url": config.LLM_API_BASE,
                "key_configured": bool(state.llm and state.llm.configured)},
    }


@app.get("/playbook")
async def playbook():
    """The rules the service is allowed to act on, as loaded from the workbook."""
    return list(state.ds.playbook.values()) if state.ds else []


@app.get("/reconciliation")
async def reconciliation():
    """What was repaired across the sources at load time, and how."""
    if state.ds is None:
        return []
    return [r.__dict__ for r in state.ds.reconciliation]


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
