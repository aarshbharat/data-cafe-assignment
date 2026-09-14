# ACPL Sales Focus & Action Assistant

An HTTP service over ACPL's FY26 sales data and its own working documents. It
answers plain-English questions about the business, turns findings into actions
from ACPL's action playbook, and declines what the evidence cannot support.

Design and rationale: **[APPROACH.md](APPROACH.md)**.
Self-audit with real numbers: **[ARTEFACT.md](ARTEFACT.md)**.

---

## Quick start

```bash
python -m venv venv && venv/Scripts/activate     # Windows; use bin/activate on Linux/macOS
pip install -r requirements.txt
```

Point it at your own LLM provider (any OpenAI-compatible endpoint):

```bash
export LLM_API_KEY="sk-..."
export LLM_API_BASE="https://api.openai.com/v1"   # default
export LLM_MODEL="gpt-4o-mini"                    # default
```

Prepare the data — one command:

```bash
python -m src.prepare
```

Run the service:

```bash
PORT=8000 python -m src.solution
```

The data pack must be unzipped at
`data/fmcg-sales-copilot-ai-engineer-mid-4to6/`. Source files are only ever read,
never modified.

### Without a key

The service still runs and answers correctly. Routing falls back to its
rule-based reading and answers are the computed sentence rather than a narrated
one; `cost_usd` is then a true `0.0` and `router` reports `"rules"`. See
[ARTEFACT.md §5](ARTEFACT.md) for what that costs in accuracy.

---

## Data preparation

```bash
python -m src.prepare
```

Reads the seven CSVs, the playbook workbook and the Word documents exactly as
provided; applies every reconciliation; writes to `build/`:

| File | Contents |
|---|---|
| `manifest.json` | Row counts before/after, the national total, every reconciliation, the document index, the playbook |
| `facts_brand_region_month.csv` | The 720-row brand × region × month spine |
| `stockouts_clean.csv`, `promotions_clean.csv` | Reconciled source tables |
| `promo_uplift.csv` | Measured uplift per promotion |

The service performs this same preparation in memory at startup, so running
`prepare` is **not** a prerequisite for serving — it exists so the preparation is
reproducible and inspectable, and it is where ARTEFACT.md's figures come from.

---

## Endpoints

### `POST /ask`

```json
{"question": "Where are we losing the most against target this quarter, and what should we do about it?"}
```

```json
{
  "answer": "Over Q4 FY26 (Apr 2026 - Jun 2026) the largest shortfall against target is Aqualite in West: INR 1,912,659 short, at 62.0% of plan ...",
  "status": "OK",
  "evidence": [
    {"brand": "Aqualite", "region": "West", "sales_inr": 3120341.08,
     "target_inr": 5033000.0, "gap_inr": 1912658.92, "achievement_pct": 62.0}
  ],
  "cost_usd": 0.00014,
  "latency_ms": 712.4
}
```

`status` is `NO_ANSWER`, with the reason in `answer`, for anything unanswerable
from the data, resting on a false premise, or naming an entity the data does not
contain.

### `POST /actions`

```json
{"scope": "West"}
```

```json
[
  {
    "finding": "Aqualite in West reached 62.0% of target over 2026-04 to 2026-06 (INR 3,120,341 against INR 5,033,000, INR 1,912,659 short), with 18 distributor-week stock-outs on 1 SKU(s) in the same window.",
    "rule_id": "R-01",
    "action": "Expedite replenishment and escalate to the regional supply lead",
    "state": "PENDING_APPROVAL"
  }
]
```

`scope` is a region name (`North`/`South`/`East`/`West`, case-insensitive, with or
without the word "Region") or `"all"`. An unrecognised scope returns `[]` — there
is no such region, so there is no action the playbook sanctions for it.

### `GET /health`, `GET /playbook`, `GET /reconciliation`

Row counts and LLM configuration; the rules as loaded from the workbook; the
repair log applied at load time.

---

## Fields beyond the contract

Additive only; the contracted fields are always present and unchanged.

### On `/ask` responses

| Field | Meaning |
|---|---|
| `category` | The routed question category (`performance`, `gap_ranking`, `explain`, `actions`, `unsupported`, …) |
| `router` | `"llm"`, `"rules"` (model unavailable or unparseable), or `"rules+llm"` |
| `answer_source` | `"narrated"`, `"computed"`, `"computed (model unavailable)"`, or `"computed (narration rejected: ungrounded figure)"` |
| `llm_calls` | Per-call outcome, e.g. `["route:ok", "narrate:ok"]` |
| `llm_tokens` | `{"prompt": n, "completion": n}` — what `cost_usd` was priced from |
| `period` | The resolved window, e.g. `"Q4 FY26 (Apr 2026 - Jun 2026)"` |
| `actions` | Full action objects, when the question also asked what to do |
| `error_type` | Exception class name, only on an internal fault |

### On `/actions` requests

| Field | Meaning |
|---|---|
| `period` | Optional. Evaluation window — `"FY26"`, `"Q3"`, `"this month"`, `"2026-02"`. Defaults to the current quarter. |

### On `/actions` response items

| Field | Meaning |
|---|---|
| `condition` | The playbook condition that matched, verbatim |
| `recommendation` | The playbook's reading of the situation, verbatim |
| `scope` | The region the action belongs to |
| `period` | The window the finding was measured over |
| `evidence` | Figures behind the finding, plus any supporting document excerpt |

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8000` | Listen port |
| `LLM_API_KEY` | *(unset)* | Your provider key. Never committed. |
| `LLM_API_BASE` | `https://api.openai.com/v1` | OpenAI-compatible base URL |
| `LLM_MODEL` | `gpt-4o-mini` | Model id |
| `LLM_TIMEOUT_S` | `12` | Per-call timeout; on timeout the service falls back to rules |
| `LLM_PRICE_IN` / `LLM_PRICE_OUT` | *(from table)* | USD per 1M tokens, for a model `src/config.py` does not list |

---

## Evaluation

```bash
python -m eval.run_eval --repeat 5          # in-process, no port needed
python -m eval.test_llm_path                # model-success path, stubbed provider
BASE_URL=https://your-host python -m eval.run_eval   # against a deployment
```

59 cases — 36 answerable, 23 refusals and injections. Numeric expectations come
from [`eval/gold.py`](eval/gold.py), which re-reads the CSVs and redoes the joins
independently of `src/`, so a case passes only when the service agrees with the
source files rather than with itself. Results are written to
[`eval/RESULTS.md`](eval/RESULTS.md) and `eval/results.json`.

`eval/test_llm_path.py` covers the half a keyless run cannot reach: with a stub
provider that actually replies, it asserts the router merge, that an ungrounded
or truncated narration is discarded, that either reader can force a refusal, and
that an injected instruction is refused before any model call is made. 22
assertions, no network, no spend.

---

## Deploying

The service reads its port from `PORT` and needs no authentication. `data/` is
committed, so the container has the pack at startup.

**Render** (`render.yaml` is committed) — new Blueprint from the repo, then set
`LLM_API_KEY` in the dashboard. Health check is `/health`.

**Docker** (anywhere):

```bash
docker build -t acpl-assistant .
docker run -p 8000:8000 -e PORT=8000 -e LLM_API_KEY="sk-..." acpl-assistant
```

**A tunnel to a local process**, for a short review window:

```bash
LLM_API_KEY="sk-..." PORT=8000 python -m src.solution
cloudflared tunnel --url http://localhost:8000
```

Verify a deployment end to end:

```bash
BASE_URL=https://your-host python -m eval.run_eval --repeat 3
```

The key is read from the environment at startup and is never committed. The
build-time proxy token is not used by the running service — it authenticates to
the provider in `LLM_API_BASE` with `LLM_API_KEY` only.

---

## Layout

```
APPROACH.md          design, decomposition, trade-offs — start here
ARTEFACT.md          self-audit: counts, reconciliations, measured accuracy/cost/latency
README.md            this file
src/
  solution.py        FastAPI service: /ask, /actions, /health, /playbook, /reconciliation
  store.py           loading + reconciliation + the brand × region × month spine
  metrics.py         every figure: aggregates, promo uplift, stock-out runs, periods
  rules.py           the action engine (R-01…R-08)
  assistant.py       /ask pipeline: route, compute, narrate, grounding check
  llm.py             OpenAI-compatible client with cost/latency metering
  config.py          paths, provider settings, model pricing
  prepare.py         one-command data preparation
eval/
  cases.py           the evaluation set
  gold.py            ground truth recomputed from the CSVs
  run_eval.py        the harness
  RESULTS.md         committed results
build/               output of `python -m src.prepare` (manifest.json committed)
data/                the provided pack, read-only, committed so deploys can boot
Dockerfile           container build
render.yaml          Render blueprint
Procfile             process definition for Procfile-based hosts
```

## Notes

* No secret is committed. The key is read from the environment at startup.
* The service has no side-effecting capability — no mail, no writes, no
  scheduling. Anything that would notify or change is returned as
  `PENDING_APPROVAL` and stops there.
* Instructions embedded in a question are treated as data. The service will not
  disclose its configuration or set aside its grounding rules on request.
