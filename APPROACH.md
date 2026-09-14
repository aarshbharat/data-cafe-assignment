# ACPL Sales Focus & Action Assistant — Approach

The service answers questions about ACPL's FY26 sales and turns what it finds into
actions from ACPL's own playbook, refusing whatever the data and the playbook
cannot support.

**Everything else hangs off this file:**

| Artefact | What it is |
|---|---|
| [ARTEFACT.md](ARTEFACT.md) | Self-audit: row counts, the national total, every reconciliation, measured accuracy / cost / latency |
| [README.md](README.md) | How to prepare the data, run the service, and the fields beyond the contract |
| [eval/RESULTS.md](eval/RESULTS.md) | Committed evaluation results |
| [src/solution.py](src/solution.py) | The HTTP service — `POST /ask`, `POST /actions` |
| [src/store.py](src/store.py) | Loading and reconciling the seven CSVs, the workbook and the documents |
| [src/metrics.py](src/metrics.py) | Every figure: aggregates, promo uplift, stock-out runs |
| [src/rules.py](src/rules.py) | The action engine — playbook conditions over measured data |
| [src/assistant.py](src/assistant.py) | The `/ask` pipeline: routing, computation, narration, grounding check |
| [src/llm.py](src/llm.py) | OpenAI-compatible client with real cost and latency metering |
| [src/prepare.py](src/prepare.py) | One-command data preparation |
| [eval/](eval/) | [cases.py](eval/cases.py) (the set), [gold.py](eval/gold.py) (independent ground truth), [run_eval.py](eval/run_eval.py) (the harness) |

---

## A. Problem decomposition

### Question categories built

Every category is a branch in [`compute()`](src/assistant.py) and is exercised by
[eval/cases.py](eval/cases.py).

| Category | The question behind it | Sources |
|---|---|---|
| `performance` | "How is GlucoJoy doing in the South this year?" | sales × targets |
| `gap_ranking` | "Where are we losing the most against target this quarter?" | sales × targets |
| `top_ranking` | "What's performing best?" | sales × targets |
| `compare` | "Compare West and East on achievement" | sales × targets |
| `stockouts` | "Any availability problems in the West?" | stock-out log × masters |
| `promotions` | "Did the Buy 2 Get 1 mechanic work?" | promo calendar × sales |
| `explain` | "Why did CremeDelight miss in the North in February?" | all of the above + documents |
| `actions` | "Give me this week's action list for the West" | playbook × everything |
| `national_total` | "What were total FY26 sales?" | sales |
| `product_info` | "What's the MRP on GlucoJoy 100g?" | product master |
| `territory` | "Is Ahmedabad a problem?" | sales × stock-outs, **no target** |
| `unsupported` | anything else | — declined |

`territory` earns a category of its own because the honest answer is partial:
targets are set by brand × region, so a territory has sales and stock-outs but no
achievement percentage. It says so rather than quietly answering about the region.

### Action categories built

All eight playbook rules R-01…R-08 are implemented in
[`src/rules.py`](src/rules.py). Approval follows the workbook's `needs_approval`
column, which the escalation SOP defines as "notifies another team or changes a
commitment".

| Rules | What they cover | State |
|---|---|---|
| R-01, R-04, R-08 | supply escalation, chronic replenishment, distributor review | `PENDING_APPROVAL` |
| R-02, R-03, R-05, R-06, R-07 | promo review, market check, over-delivery, manual-review flag, mechanic replication | `RECOMMENDED` |

### Built by design, not by omission

* **Refusal is a first-class output.** Unknown brands, unknown regions, periods
  outside FY26, measures no ACPL file holds (margin, market share, headcount,
  secondary sales), forecasts, false premises and prompt injection each have their
  own path and their own message. 23 of 59 evaluation cases are refusals.
* **A false premise is refused, not answered around.** "Why did Aqualite beat
  target in the West this quarter?" is declined with the real figure, including
  when only the *magnitude* is wrong ("missed by 40%" when the shortfall was 0.4%).

### Deliberately not built

| Left out | Why |
|---|---|
| Forecasting | No model would be grounded in 52 weeks of one FY; the brief asks what the evidence supports |
| Multi-turn dialogue | The contract is one question per request, stateless |
| Territory-level achievement | No territory targets exist — the gap is stated, not synthesised |
| Free-form SQL / open analytics | Every answer would stop being checkable against a fixed evidence shape |
| Executing actions | Everything that would notify or change is `PENDING_APPROVAL` and stops there |

---

## B. System design

FastAPI + uvicorn, pandas in memory, one process. Data is loaded and
pre-aggregated once at startup (~1.8 s) so no request touches the 74,880-row
ledger.

```
POST /ask
   │
   ├─ injection guard ........................ code   src/assistant.py looks_like_injection
   ├─ rule-based reading ..................... code   read_question  (brands/regions/periods
   │                                                   matched against the masters)
   ├─ out-of-scope screen .................... code   _OUT_OF_SCOPE, unknown_entity
   ├─ LLM router ............................. MODEL  route  → category + entities as JSON
   │     └─ on any failure: keep the rule-based reading, flag router="rules"
   ├─ entity + period validation ............. code   → NO_ANSWER naming what failed
   ├─ false-premise check .................... code   check_premise
   ├─ compute ................................ code   src/metrics.py  (all figures)
   ├─ actions, if asked ...................... code   src/rules.py evaluate
   ├─ narration .............................. MODEL  narrate  → plain English
   └─ grounding check ........................ code   grounded() — any figure not in the
                                                      evidence ⇒ discard, use the computed
                                                      sentence
POST /actions → src/rules.py evaluate  (no model involved at all)
```

### Where the model decides vs where code decides

**The model never produces a number.** It does two jobs: read a free-text question
into a category plus entities, and put an already-computed result into a sentence.

**Code decides** every aggregate, every rule evaluation, every threshold, whether
to answer or refuse, and whether the model's sentence is allowed out.

The grounding check in [`grounded()`](src/assistant.py) parses the narrated
sentence for digit groups and rejects it if any figure above 10 is absent from the
computed finding and evidence. A fluent wrong answer is discarded in favour of the
deterministic one, and `answer_source` records that it happened.

### Failure path

| Failure | Behaviour |
|---|---|
| LLM unreachable / 401 / timeout | Rule-based routing, computed answer, `router: "rules"`, `cost_usd: 0.0`. **Not** a refusal. |
| LLM returns unparseable JSON | Same fallback |
| Narration contains an ungrounded figure | Discarded; computed sentence used; `answer_source` says so |
| Unknown brand / region / period | `NO_ANSWER` naming the entity and listing valid ones |
| No data for a valid combination | `NO_ANSWER` |
| No playbook rule fires | Empty list from `/actions`; `/ask` says no sanctioned action exists |
| Unhandled exception | `NO_ANSWER`, never a fabricated figure ([src/solution.py](src/solution.py)) |

This path is the main repair over the previous version, where an LLM transport
error was silently reported to the caller as "the data cannot answer this" — which
made every `/ask` return `NO_ANSWER` whenever no key was configured.

---

## C. Data & grounding

### Routing to sources

The interpreted `(category, brand, region, territory, period)` selects the
sources. Brands and regions are matched against `dim_sku` and `dim_geo`
themselves, so a name the data does not contain cannot match and is refused by
name.

Everything comparable is pre-joined at startup into one
**brand × region × month** spine (720 rows) in
[`_build_facts()`](src/store.py) — sales rolled up through `dim_sku` (SKU→brand)
and `dim_geo` (territory→region), with a week belonging to the month containing
its `week_start`, per the data dictionary.

### Documents

Indexed by the entities they actually mention — region, brand, category, month,
distributor id — in [`_load_documents()`](src/store.py). A document reaches an
answer only through [`documents_for()`](src/store.py), by entity overlap, never by
filename. The two noise files name no entity and so never surface:

| Document | Indexed as | Used for |
|---|---|---|
| `visit_note_north_feb2026` | North, CremeDelight, 2026-02 | The R-03 external cause for the Feb miss |
| `distributor_note_west` | West, Beverages→Aqualite, D032/D033, 2026-04…06 | Corroborates the R-01 supply finding |
| `promo_circular_h2fy26` | North/West, Snacks/Beverages, H2 | Promotion context |
| `escalation_sop` | — (policy) | The R-03/R-06 split and the approval rule |
| `hr_circular`, `weekly_summary_w32` | no entities | never retrieved |

A note naming a *category* ("the Beverages 1L line") indexes to every brand in it,
which is how the West supply note reaches Aqualite.

### Reconciling the sources

Eight repairs, applied in code and logged to `GET /reconciliation`; the full table
with row counts is in [ARTEFACT.md](ARTEFACT.md). The source files are only ever
read.

### Staying tied to evidence and to a rule

* Every `OK` answer carries the figures it was computed from in `evidence`.
* Every action carries its `rule_id`, plus the playbook `condition` and
  `recommendation` it matched, and its own `evidence` block.
* [`_emit()`](src/rules.py) cannot produce an action without a playbook row —
  an action that cannot cite a rule is not returned at all.
* A rule needing a figure the data cannot produce **does not fire**. R-02 needs a
  measured uplift; where there is no pre-promotion baseline, no uplift is assumed.

---

## D. Actions & approval

### What an action is

A playbook row whose condition a deterministic check found true, carried with the
figures that made it true. Computed, never written by a model.

### How one is warranted

Brand rules are evaluated **month by month** and then grouped per
(brand, rule): a 62% April is a finding even when the quarter average hides it,
but three bad months in a row are one thing to act on, not three. Default window
is the current quarter, anchored to the last week in the data rather than the wall
clock; `period` on the request widens or narrows it.

`_classify_month()` assigns at most one brand rule per month. R-03 and R-06 both
cover "a miss with nothing in the numbers", and are split on the SOP's own words —
*"commission a market check, or if nothing explains it, flag it for manual review
— never invent a reason"*:

* a working document explains it → **R-03**, commission the market check;
* nothing explains it → **R-06**, flag for manual review, attribute nothing.

### What is gated, and why

`needs_approval` is read from the workbook, not hard-coded. R-01 (escalates to the
supply lead), R-04 (raises a replenishment order) and R-08 (schedules a
distributor call) all notify another team or change a commitment, so they are
`PENDING_APPROVAL` and are returned, not carried out. The service has no
side-effecting capability at all — no mail, no writes, no scheduling.

---

## E. Operations

### Cost

Taken from the `usage` block the provider returns — `prompt_tokens` and
`completion_tokens` — priced against the model's published rate in
[`src/config.py`](src/config.py), and accumulated per request across both model
calls by [`Meter`](src/llm.py). Not estimated from word counts. A request that
makes no model call reports a true `0.0`. `LLM_PRICE_IN` / `LLM_PRICE_OUT` let an
unlisted model be costed honestly at deploy time.

### Latency

`time.perf_counter()` from entry to serialisation in
[`answer()`](src/assistant.py), covering routing, computation and narration.
The evaluation additionally records wall-clock latency at the client so the
reported figure can be checked against an independent one.

### Accuracy

[eval/run_eval.py](eval/run_eval.py) runs 59 cases against the live ASGI app (or
a deployed URL via `BASE_URL`), scoring routing, grounding and behaviour
separately. Numeric expectations come from [eval/gold.py](eval/gold.py), which
**re-reads the CSVs and redoes the joins by hand** — a case passes only when the
service agrees with the source files rather than with itself. Contract invariants
and approval gating are asserted on every response. Results:
[eval/RESULTS.md](eval/RESULTS.md).

---

## F. Trade-offs

| Decision | What it bought | What it gave up |
|---|---|---|
| Model routes and narrates; code computes | No fabricated figure can reach the caller | Phrasings outside the lexicon depend on the model being up |
| Rule-based router underneath the model | Service still answers correctly with no key or a dead provider | Two routing paths to keep in step; the fallback is blunter |
| Grounding check on narration | A fluent wrong answer is caught and discarded | Occasionally discards a fine rephrasing, costing tokens already spent |
| Pre-aggregate everything at startup | `/actions` 15 ms p50 instead of 10.5 s | ~1.8 s startup; a data change needs a restart |
| Refuse on any doubt | No invented figures; 100% on the refusal cases | Some answerable-but-oddly-phrased questions are declined |
| At most one brand rule per month | Clean, non-overlapping action list | A month with two plausible readings shows only the stronger |
| Quarter as the default action window | A miss reads as a trend, not one short week | A brand-new one-month collapse waits for context |
| Evidence capped at 12 items | Readable responses | Long tails are truncated (the aggregate row is always first) |
| In-memory pandas, single process | Simple, fast, no database | No horizontal scale; ~200 MB resident |
