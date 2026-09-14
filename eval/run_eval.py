"""Run the evaluation against the live app and write committed results.

    python -m eval.run_eval            # in-process, no network, no port needed
    python -m eval.run_eval --repeat 3 # repeat every case for latency percentiles
    BASE_URL=https://... python -m eval.run_eval   # against a deployed instance

Scores three things separately, because they fail for different reasons:

  routing   — did the question reach the right category / the right refusal
  grounding — does the evidence agree with figures recomputed from the CSVs
  behaviour — refusals, approval gating, rule citation

Writes eval/results.json and eval/RESULTS.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from pathlib import Path

import httpx

from eval import gold
from eval.cases import ACTION_CASES, ASK_CASES

HERE = Path(__file__).resolve().parent
TOL = 0.005          # 0.5% — tolerates display rounding, not a different number


def _numbers(obj) -> list[float]:
    out = []
    if isinstance(obj, dict):
        for v in obj.values():
            out += _numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            out += _numbers(v)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.append(float(obj))
    return out


def _matches(expected: float, found: list[float]) -> bool:
    if expected is None:
        return True
    for f in found:
        if expected == 0 and f == 0:
            return True
        if expected and abs(f - expected) <= abs(expected) * TOL:
            return True
    return False


def check_ask(case: dict, resp: dict, g: dict) -> tuple[bool, list[str]]:
    fails = []
    status = resp.get("status")
    if status != case["expect_status"]:
        fails.append(f"status {status} != {case['expect_status']}")

    if case.get("expect_category") and status == "OK":
        if resp.get("category") != case["expect_category"]:
            fails.append(f"category {resp.get('category')} != {case['expect_category']}")

    if case.get("expect_evidence") and status == "OK" and not resp.get("evidence"):
        fails.append("evidence is empty on an OK answer")

    phrase = case.get("expect_phrase")
    if phrase:
        hay = (resp.get("answer", "") + json.dumps(resp.get("evidence", []))).lower()
        if phrase.lower() not in hay:
            fails.append(f"answer/evidence does not mention {phrase!r}")

    key = case.get("expect_gold")
    if key and status == "OK":
        found = _numbers(resp.get("evidence", []))
        for field, expected in g[key].items():
            if not isinstance(expected, (int, float)) or isinstance(expected, bool):
                continue
            if not _matches(float(expected), found):
                fails.append(f"{key}.{field}={expected} not present in evidence")

    # Contract invariants, checked on every response.
    for field in ("answer", "status", "evidence", "cost_usd", "latency_ms"):
        if field not in resp:
            fails.append(f"missing contract field {field}")
    if resp.get("status") not in ("OK", "NO_ANSWER"):
        fails.append("status is not OK or NO_ANSWER")
    if not isinstance(resp.get("latency_ms"), (int, float)) or resp.get("latency_ms", -1) < 0:
        fails.append("latency_ms is not a measured number")
    return not fails, fails


def check_actions(case: dict, resp, g: dict) -> tuple[bool, list[str]]:
    fails = []
    if not isinstance(resp, list):
        return False, ["response is not a list"]
    if case.get("expect_empty"):
        if resp:
            fails.append(f"expected no actions for an unknown scope, got {len(resp)}")
        return not fails, fails

    ids = {a.get("rule_id") for a in resp}
    for rid in case.get("expect_rules", []):
        if rid not in ids:
            fails.append(f"rule {rid} missing (got {sorted(ids)})")
    for rid in case.get("expect_absent", []):
        if rid in ids:
            fails.append(f"rule {rid} should not fire")

    for a in resp:
        for field in ("finding", "rule_id", "action", "state"):
            if field not in a:
                fails.append(f"action missing contract field {field}")
        if a.get("state") not in ("RECOMMENDED", "PENDING_APPROVAL"):
            fails.append(f"bad state {a.get('state')}")
        if not a.get("rule_id", "").startswith("R-"):
            fails.append(f"action does not cite a playbook rule: {a.get('rule_id')}")
        # Approval gating must follow the workbook, not the finding's tone.
        if a.get("rule_id") in ("R-01", "R-04", "R-08") and a.get("state") != "PENDING_APPROVAL":
            fails.append(f"{a['rule_id']} must be PENDING_APPROVAL")
        if a.get("rule_id") in ("R-02", "R-03", "R-05", "R-06", "R-07") \
                and a.get("state") != "RECOMMENDED":
            fails.append(f"{a['rule_id']} must be RECOMMENDED")
    return not fails, fails


async def run(repeat: int, base_url: str | None) -> dict:
    g = gold.compute()

    if base_url:
        client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=60)
        ctx = None
    else:
        from src.solution import app
        transport = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(transport=transport, base_url="http://eval", timeout=60)
        ctx = app.router.lifespan_context(app)
        await ctx.__aenter__()

    results, ask_lat, act_lat, costs = [], [], [], []
    try:
        for rep in range(repeat):
            for case in ASK_CASES:
                t0 = time.perf_counter()
                r = await client.post("/ask", json={"question": case["question"]})
                wall = (time.perf_counter() - t0) * 1000
                body = r.json()
                ok, fails = check_ask(case, body, g)
                ask_lat.append(wall)
                costs.append(float(body.get("cost_usd", 0.0)))
                if rep == 0:
                    results.append({"id": case["id"], "endpoint": "/ask", "ok": ok,
                                    "fails": fails, "status": body.get("status"),
                                    "category": body.get("category"),
                                    "router": body.get("router"),
                                    "answer_source": body.get("answer_source"),
                                    "reported_latency_ms": body.get("latency_ms"),
                                    "wall_latency_ms": round(wall, 1),
                                    "cost_usd": body.get("cost_usd"),
                                    "answer": body.get("answer", "")[:300]})
            for case in ACTION_CASES:
                payload = {"scope": case["scope"]}
                if case.get("period"):
                    payload["period"] = case["period"]
                t0 = time.perf_counter()
                r = await client.post("/actions", json=payload)
                wall = (time.perf_counter() - t0) * 1000
                body = r.json()
                ok, fails = check_actions(case, body, g)
                act_lat.append(wall)
                if rep == 0:
                    results.append({"id": case["id"], "endpoint": "/actions", "ok": ok,
                                    "fails": fails, "n_actions": len(body),
                                    "rules": sorted({a.get("rule_id") for a in body}),
                                    "pending": sum(1 for a in body
                                                   if a.get("state") == "PENDING_APPROVAL"),
                                    "wall_latency_ms": round(wall, 1)})
    finally:
        await client.aclose()
        if ctx is not None:
            await ctx.__aexit__(None, None, None)

    passed = sum(1 for r in results if r["ok"])
    ask_results = [r for r in results if r["endpoint"] == "/ask"]
    refusals = [r for r in ask_results if r["id"].startswith(("refuse_", "inject_"))]
    answers = [r for r in ask_results if not r["id"].startswith(("refuse_", "inject_"))]

    def pct(xs, p):
        return round(statistics.quantiles(sorted(xs), n=100)[p - 1], 1) if len(xs) > 2 \
            else round(max(xs), 1)

    summary = {
        "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mode": "deployed" if base_url else "in-process (ASGI)",
        "llm_key_configured": bool(os.environ.get("LLM_API_KEY")),
        "llm_model": os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        "repeat": repeat,
        "cases": len(results),
        "passed": passed,
        "accuracy_pct": round(passed / len(results) * 100, 1),
        "answerable_accuracy_pct": round(
            sum(1 for r in answers if r["ok"]) / max(len(answers), 1) * 100, 1),
        "refusal_accuracy_pct": round(
            sum(1 for r in refusals if r["ok"]) / max(len(refusals), 1) * 100, 1),
        "ask_latency_ms": {"p50": pct(ask_lat, 50), "p95": pct(ask_lat, 95),
                           "min": round(min(ask_lat), 1), "max": round(max(ask_lat), 1)},
        "actions_latency_ms": {"p50": pct(act_lat, 50), "p95": pct(act_lat, 95),
                               "min": round(min(act_lat), 1), "max": round(max(act_lat), 1)},
        "cost_usd_per_ask": {"median": round(statistics.median(costs), 8),
                             "mean": round(statistics.fmean(costs), 8),
                             "max": round(max(costs), 8),
                             "total": round(sum(costs), 6)},
    }
    return {"summary": summary, "results": results, "gold": g}


def write_markdown(out: dict) -> None:
    s, rs = out["summary"], out["results"]
    failed = [r for r in rs if not r["ok"]]
    lines = [
        "# Evaluation results",
        "",
        f"Generated by `python -m eval.run_eval` at {s['ran_at']} "
        f"({s['mode']}, {s['repeat']} repeat(s)).",
        f"LLM key configured: **{s['llm_key_configured']}** (model `{s['llm_model']}`). "
        "With no key the router falls back to its rule-based reading and answers are the "
        "computed sentence, so `cost_usd` is a true 0.0 — re-run with a key for real "
        "inference cost.",
        "",
        "## Accuracy",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Cases | {s['cases']} |",
        f"| Passed | {s['passed']} |",
        f"| Overall accuracy | **{s['accuracy_pct']}%** |",
        f"| Answerable questions | {s['answerable_accuracy_pct']}% |",
        f"| Refusals / injections | {s['refusal_accuracy_pct']}% |",
        "",
        "## Cost and latency",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Median cost per `/ask` | ${s['cost_usd_per_ask']['median']} |",
        f"| Mean cost per `/ask` | ${s['cost_usd_per_ask']['mean']} |",
        f"| Max cost per `/ask` | ${s['cost_usd_per_ask']['max']} |",
        f"| `/ask` latency p50 | {s['ask_latency_ms']['p50']} ms |",
        f"| `/ask` latency p95 | {s['ask_latency_ms']['p95']} ms |",
        f"| `/actions` latency p50 | {s['actions_latency_ms']['p50']} ms |",
        f"| `/actions` latency p95 | {s['actions_latency_ms']['p95']} ms |",
        "",
    ]
    if failed:
        lines += ["## Failing cases", "", "| Case | Why |", "|---|---|"]
        lines += [f"| `{r['id']}` | {'; '.join(r['fails'])} |" for r in failed]
        lines.append("")
    else:
        lines += ["All cases pass.", ""]

    lines += ["## Every case", "", "| Case | Endpoint | Result | Detail |", "|---|---|---|---|"]
    for r in rs:
        detail = (f"{r.get('status')} / {r.get('category') or '-'}"
                  if r["endpoint"] == "/ask"
                  else f"{r.get('n_actions')} actions, rules {r.get('rules')}, "
                       f"{r.get('pending')} pending approval")
        lines.append(f"| `{r['id']}` | {r['endpoint']} | {'pass' if r['ok'] else 'FAIL'} "
                     f"| {detail} |")
    (HERE / "RESULTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1,
                    help="repeat every case N times for latency percentiles")
    ap.add_argument("--base-url", default=os.environ.get("BASE_URL"),
                    help="evaluate a deployed service instead of running in-process")
    args = ap.parse_args()

    out = asyncio.run(run(args.repeat, args.base_url))
    (HERE / "results.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    write_markdown(out)

    s = out["summary"]
    print(f"{s['passed']}/{s['cases']} passed — {s['accuracy_pct']}% "
          f"(answerable {s['answerable_accuracy_pct']}%, "
          f"refusals {s['refusal_accuracy_pct']}%)")
    print(f"/ask      p50 {s['ask_latency_ms']['p50']} ms  p95 {s['ask_latency_ms']['p95']} ms")
    print(f"/actions  p50 {s['actions_latency_ms']['p50']} ms  "
          f"p95 {s['actions_latency_ms']['p95']} ms")
    print(f"median cost per /ask ${s['cost_usd_per_ask']['median']}")
    for r in out["results"]:
        if not r["ok"]:
            print(f"  FAIL {r['id']}: {'; '.join(r['fails'])}")
    return 0 if s["passed"] == s["cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
