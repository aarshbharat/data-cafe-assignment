# ARTEFACT — self-audit

What you cannot see by calling the service. Every figure here is produced by
`python -m src.prepare` (→ `build/manifest.json`) or `python -m eval.run_eval`
(→ [eval/RESULTS.md](eval/RESULTS.md), `eval/results.json`), both committed.

Start at [APPROACH.md](APPROACH.md).

---

## 1. Rows held after preparation

| File | Rows in file | Rows held | What moved |
|---|---:|---:|---|
| `fact_primary_sales.csv` | 74,880 | **74,880** | Nothing dropped. 52 weeks × 120 SKUs × 12 territories. |
| `fact_targets.csv` | 720 | **720** | Nothing dropped. 12 months × 15 brands × 4 regions. |
| `stockouts.csv` | 520 | **520** | Nothing dropped; 16 region spellings folded to 4. |
| `promotions.csv` | 199 | **40** | 159 fully-empty rows and 13 unnamed columns of Excel padding. No populated row lost. |
| `dim_sku.csv` | 120 | **120** | Nothing dropped. |
| `dim_geo.csv` | 12 | **12** | Nothing dropped. |
| `dim_distributor.csv` | 199 | **40** | 159 empty rows and 16 unnamed columns of Excel padding. |
| `action_playbook.xlsx` | 8 rules | **8** | All of R-01…R-08 loaded and implemented. |
| `documents/` | 6 files | **6** | All read; 2 index to no entity and are never retrieved. |

Derived and held in memory:

| Table | Rows |
|---|---:|
| `facts` (brand × region × month spine) | 720 |
| `promo_uplift` | 40 (**39** measurable — see §4) |
| chronic stock-out runs (> 6 consecutive weeks) | 2 |

Coverage: 52 weeks, 2025-07-01 → 2026-06-23, 12 months, 15 brands, 4 regions,
12 territories, 40 distributors.

---

## 2. National FY26 primary-sales value

> **INR 1,357,631,078.74** (≈ ₹135.76 crore)

Against a plan of **INR 1,360,880,000**, i.e. **99.76%** of target — a shortfall of
INR 3,248,921.26 for the year.

Computed as the sum of `value_inr` over all 74,880 rows. It is also reachable
through the service: `{"question": "What were total FY26 primary sales?"}`.
The two agree because nothing is filtered out of the sales ledger.

---

## 3. Cross-source mismatches reconciled

Applied in [`src/store.py::_reconcile()`](src/store.py); live at
`GET /reconciliation`. The provided files are only ever read.

| # | Mismatch | Where | Resolution |
|---|---|---|---|
| 1 | Region typed free-hand: **16 distinct spellings** — `EAST`, `East`, `East Region`, `NORTH`, `north`, … | `stockouts.csv` vs `dim_geo.csv` | Case-folded and the ` Region` suffix stripped to the 4 canonical names. All 520 rows resolved; none dropped. |
| 2 | Dates are `DD/MM/YYYY` with unpadded parts (`1/7/2025`); every other file is `YYYY-MM-DD` | `promotions.csv` | Parsed day-first **explicitly**, so `1/7/2025` reads as 1 July, not 7 January. All 40 parsed. |
| 3 | 13 unnamed columns + 159 empty rows | `promotions.csv` | Dropped as Excel export padding; 40 populated promotions kept. |
| 4 | 16 unnamed columns + 159 empty rows | `dim_distributor.csv` | Same; 40 distributors kept. |
| 5 | The product key is called **`item_code`** here and **`sku_code`** in the master | `stockouts.csv` vs `dim_sku.csv` | Joined as the same key. 520/520 rows resolve to a known SKU. |
| 6 | The product key is called **`sku`** here | `promotions.csv` vs `dim_sku.csv` | Joined to `sku_code`. 40/40 promotions resolve. |
| 7 | Region is **duplicated**: the stock-out log carries its own, which can disagree with the distributor's territory | `stockouts.csv` vs `dim_distributor.csv` × `dim_geo.csv` | `dim_geo` via `dim_distributor` treated as authoritative, logged region kept as fallback. **Checked: 0 rows actually disagreed** once §1 was applied — the conflict is latent, not present. |
| 8 | **Reporting grain differs**: sales are weekly by SKU × territory, targets are monthly by brand × region | `fact_primary_sales.csv` vs `fact_targets.csv` | Sales rolled up to brand × region × month through both masters before any comparison; a week belongs to the month containing its `week_start`, per the data dictionary. |

**A grain caveat worth stating:** because weeks are assigned whole to a month, a
month gets 4 or 5 selling weeks (March 2026 has 5, June 2026 has 4) while its
target does not scale. Single-month achievement is therefore noisier than
quarterly. This is why the action engine defaults to the quarter and evaluates
brand rules month-by-month before grouping (see APPROACH §D) rather than
comparing one month's aggregate.

---

## 4. What the data actually triggers

Stated because it is the kind of thing a caller cannot check, and because two
rules honestly never fire:

| Rule | Fires? | Evidence |
|---|---|---|
| R-01 | **Yes**, once | Aqualite / West, 62.0% over Apr–Jun 2026, 18 distributor-week stock-outs on `BV-0104` |
| R-02 | **No** | Its condition needs uplift < 10%. Measured uplift across 39 promotions runs 13.5%–44.7% (median 27.4%). Nothing is weak, so nothing fires. |
| R-03 | **Yes**, once | CremeDelight / North, 72.0% in Feb 2026, no stock-out, no promo, `visit_note_north_feb2026.docx` records the competitor push |
| R-04 | **Yes**, twice | D032 and D033, `BV-0104`, 9 consecutive weeks, 2026-04-14 → 2026-06-09 |
| R-05 | **No** | Needs > 110% achievement. The maximum in FY26 is 108.7%. |
| R-06 | **Yes**, once | MintGuard / East, 74.0% in Mar 2026, no stock-out, no promo, **no document** — flagged for manual review, cause not attributed |
| R-07 | **Yes**, 23 over FY26 | Promotions with uplift > 25% |
| R-08 | **Yes** | Distributors with ≥ 3 SKUs out in a month |

The previous version hard-coded `uplift_weak = True` and so emitted R-02 findings
the data contradicts. Uplift is now measured — weekly run-rate inside the promo
window against the four weeks before it — and **1 of 40 promotions is not
measurable** (`PR-2025-056` starts 2025-07-01, the first week of the data, so it
has no baseline). It returns `uplift_pct: null` and the rules that need an uplift
do not fire for it.

---

## 5. Evaluation

`python -m eval.run_eval --repeat 5` — 59 cases, 36 answerable and 23 refusals.
Numeric expectations come from [`eval/gold.py`](eval/gold.py), which re-reads the
CSVs and redoes the joins independently of `src/`.

### Accuracy, honestly

| Stage | Accuracy | What it means |
|---|---|---|
| **First measurement** | **89.7%** (35/39) | Authored set, first run. Answerable 100%, **refusals 69.2%** |
| **Held-out probe** | **75%** (15/20) | 20 fresh phrasings written against the brief, not against the router, run once before any fix |
| **After the change** | **100%** (59/59) | Authored + probe cases, 5 repeats |

**The biggest gap: refusals, at 69.2%.** Every failure was a question the service
answered when it should have declined, or declined less usefully than it could.
Four distinct causes:

* `"gross margin on Zing Cola"` — a real brand and a metric in no ACPL file, answered as performance;
* `"how many sales reps in the North"` — a real region, same failure;
* `"How is Britannia performing?"` — correctly refused, but generically, without naming the brand;
* `"missed target by 40%"` when the shortfall was 0.4% — direction right, magnitude false, answered anyway.

**The one change: an out-of-scope screen ahead of the router**
([`_OUT_OF_SCOPE` and `unknown_entity()` in src/assistant.py](src/assistant.py)).
It runs *before* category selection, on the principle that resolving a question's
entities must not make it answerable — a question can name a real brand and a real
region and still ask for a measure no file holds. That single change took the
authored set from **89.7% → 94.9%** and refusals from **69.2% → 84.6%**.

It also exposed a latent bug: the month matcher used a loose three-letter prefix,
so `"gross **mar**gin"` was being read as March. Fixed to whole-word matching —
the same bug the document indexer had, where `"sum**mar**y"` read as March.

The remaining two failures needed their own fixes, which I am not going to
present as part of that change: a magnitude check on claimed percentages, and
case-insensitivity in the subject frame so `"How is Britannia"` matched as well as
`"how is Britannia"`.

The held-out probe then found 5 more, 4 of them the rule-based fallback
over-refusing colloquial phrasing (`"anything I should escalate before Monday?"`,
`"which distributor is the biggest headache?"`) and one — `"the price of GlucoJoy
100g"` — a capability the data supports all along, since `mrp_inr` is in
`dim_sku`. That produced the `product_info` and `territory` categories.

**What 100% does and does not mean.** The probe was held out for exactly one run.
Once I fixed against it, it stopped being held out, and it is now committed as
part of the regression set. 100% means *no known regression across 59 cases whose
figures are checked against the CSVs* — not that the service handles every
phrasing. The honest generalisation estimate is the **75%** first-pass figure on
unseen phrasings, and that was measured with **no LLM key**, i.e. on the
rule-based fallback alone. With the model routing, the fallback's lexicon gaps
are the failure mode it covers.

### Cost and latency

Measured over 5 repeats of all 59 cases (295 `/ask` calls, 30 `/actions` calls),
in-process, Windows 11, Python 3.14:

| Metric | Value |
|---|---|
| `/ask` latency **p50** | **15.1 ms** |
| `/ask` latency p95 | 66.8 ms |
| `/actions` latency **p50** | **15.1 ms** |
| `/actions` latency p95 | 118.1 ms |
| Median cost per `/ask` | **$0.00** — see below |
| Startup (load + pre-aggregate) | 1,731 ms |

**The cost figure is a true zero, not a placeholder, and it is not the number you
should judge.** I have no LLM key for this provider, so these runs exercised the
rule-based router and the computed sentence, which make no model call. Cost
instrumentation is real — `usage.prompt_tokens` / `usage.completion_tokens` from
the provider response priced against [`src/config.py`](src/config.py) — and the
path is verified: with a deliberately invalid key the client returns `ok=False`,
the service falls back, and `llm_calls` reports `['route:failed',
'narrate:failed']`. **Re-run `python -m eval.run_eval --repeat 5` with
`LLM_API_KEY` set before submitting** so this table carries real inference cost.
Expected with `gpt-4o-mini` and the two calls per `/ask` (~450 prompt + ~120
completion tokens): roughly **$0.00014** per question, latency p50 around 700 ms.

For scale: `/actions` for the West went from **10,546 ms to 15 ms** by
pre-aggregating at startup, with byte-identical rule output.

---

## 6. Known limits

* Two rules (R-02, R-05) never fire on this data. Correct, and stated rather than forced.
* `territory` questions get sales and stock-outs but no achievement — no territory targets exist.
* Comparison handles regions only, not two brands or two periods.
* Single-month achievement is noisy by 4- vs 5-week months (§3).
* The rule-based fallback is blunter than the model router; the 75% figure is its honest ceiling on unseen phrasing.
* Loaded from CSV at startup, single process — a data change needs a restart.
