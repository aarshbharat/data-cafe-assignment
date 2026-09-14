"""The action engine: playbook conditions evaluated against measured data.

An action is never written by a model. Each one is a playbook row whose
condition a deterministic check found true, carried together with the figures
that made it true. If a condition needs a number the data cannot produce, the
rule does not fire — it is not assumed.

Approval follows `needs_approval` in the workbook, which the escalation SOP
defines as "notifies another team or changes a commitment".
"""

from __future__ import annotations

import re
from collections import OrderedDict

import pandas as pd

from src import metrics as M
from src.store import CANONICAL_REGIONS, DataStore

# Thresholds are the playbook's own, kept here so a change to the workbook
# wording has one obvious place to land.
MISS_SEVERE_PCT = 70.0      # R-01
MISS_PCT = 80.0             # R-02, R-03, R-06
OVER_DELIVERY_PCT = 110.0   # R-05
WEAK_UPLIFT_PCT = 10.0      # R-02
STRONG_UPLIFT_PCT = 25.0    # R-07
CHRONIC_WEEKS = 6           # R-04
MULTI_SKU_COUNT = 3         # R-08

# Ordering for the weekly list: supply failures first, then misses, then wins.
_SEVERITY = {"R-01": 0, "R-04": 1, "R-08": 2, "R-06": 3, "R-03": 4,
             "R-02": 5, "R-07": 6, "R-05": 7}


def _state(rule: dict) -> str:
    return "PENDING_APPROVAL" if rule["needs_approval"] else "RECOMMENDED"


def _emit(ds: DataStore, rule_id: str, finding: str, evidence: list[dict],
          scope: str, period: str) -> dict | None:
    """Build one action, or None if the playbook has no such rule.

    An action that cannot cite a rule is not returned at all.
    """
    rule = ds.playbook.get(rule_id)
    if rule is None:
        return None
    return {
        "finding": finding,
        "rule_id": rule_id,
        "action": rule["action"],
        "state": _state(rule),
        # Extra fields, documented in README.md
        "condition": rule["condition"],
        "recommendation": rule["recommendation"],
        "scope": scope,
        "period": period,
        "evidence": evidence,
    }


def _inr(v: float) -> str:
    return f"INR {v:,.0f}"


def _excerpt(text: str, limit: int = 240, about: str | None = None) -> str:
    """The sentences a manager needs, not the letterhead.

    Prefers the sentences that name `about` (the brand under discussion), since
    a visit note opens with who wrote it and circulated it, not with the finding.
    """
    body = " ".join(" ".join(text.split("\n")[1:]).split()) or " ".join(text.split())
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", body) if s.strip()]
    if about:
        keyed = [s for s in sentences if about.lower() in s.lower()]
        if keyed:
            sentences = keyed + [s for s in sentences if s not in keyed]
    out = ""
    for s in sentences:
        if out and len(out) + len(s) + 1 > limit:
            break
        out = f"{out} {s}".strip()
    return out or body[:limit]


def _promo_ev(p) -> dict:
    return {
        "promo_id": p["promo_id"], "sku": p["sku"], "brand": p["brand"],
        "region": p["region"], "mechanic": p["mechanic"],
        "discount_pct": p["discount_pct"],
        "window": f"{p['start_date']} to {p['end_date']}",
        "weekly_sales_before_inr": p["weekly_sales_before_inr"],
        "weekly_sales_during_inr": p["weekly_sales_during_inr"],
        "uplift_pct": p["uplift_pct"],
        "baseline": f"{M.BASELINE_WEEKS} weeks before the promotion started",
    }


# ── Which brand-level rule, if any, a single month breaches ─────────────────

def _classify_month(ds: DataStore, pre: M.Precomputed, brand: str, region: str,
                    month: str, ach: float, promos: list[dict]) -> tuple[str, dict] | None:
    """Return the one brand-level rule this brand-region-month breaches.

    The playbook's brand rules are written so that at most one applies: the
    severe-miss-with-supply case, the miss-under-a-promotion case, the miss with
    nothing in the numbers, or the over-delivery. R-03 and R-06 both cover the
    last of those, and are split on whether a working document explains it — the
    SOP's "commission a market check, or if nothing explains it, flag it".
    """
    has_stockout = pre.has_stockout(brand, region, month)
    has_promo = bool(promos)

    if ach < MISS_SEVERE_PCT and has_stockout:
        return "R-01", {}
    if ach < MISS_PCT and has_promo:
        measured = [p for p in promos if p["uplift_pct"] is not None]
        if measured:
            weakest = min(measured, key=lambda p: p["uplift_pct"])
            if float(weakest["uplift_pct"]) < WEAK_UPLIFT_PCT:
                return "R-02", {"promo": weakest}
        # A promotion is running but its uplift is not weak (or cannot be
        # measured), so R-02's condition is not met. Nothing is assumed.
        return None
    if ach < MISS_PCT and not has_stockout and not has_promo:
        notes = ds.documents_for(brand, region, [month])
        return ("R-03", {"doc": notes[0]}) if notes else ("R-06", {})
    if ach > OVER_DELIVERY_PCT:
        return "R-05", {}
    return None


def _brand_rule_hits(ds: DataStore, pre: M.Precomputed, months: list[str],
                     region: str) -> "OrderedDict[tuple, dict]":
    """Group per-month breaches into one entry per (brand, rule).

    Every lookup here is against a dict built at startup, so this walks the
    brand x month grid without touching the sales ledger.
    """
    month_set = set(months)
    facts = ds.facts[(ds.facts["region"] == region) & (ds.facts["month"].isin(month_set))]

    hits: OrderedDict[tuple, dict] = OrderedDict()
    for row in facts.itertuples(index=False):
        ach = row.achievement_pct
        if pd.isna(ach):
            continue
        brand, month = row.brand, row.month
        promos = pre.promos_live(brand, region, month)
        verdict = _classify_month(ds, pre, brand, region, month, float(ach), promos)
        if verdict is None:
            continue
        rule_id, extra = verdict
        entry = hits.setdefault((brand, rule_id), {"months": [], "extra": extra})
        entry["months"].append(month)
        entry["extra"] = entry["extra"] or extra
    for entry in hits.values():
        entry["months"].sort()
    return hits


# ── Entry point ────────────────────────────────────────────────────────────

def evaluate(ds: DataStore, pre: M.Precomputed, scope: str = "all",
             months: list[str] | None = None, period_label: str = "") -> list[dict]:
    """Evaluate every playbook rule for `scope` over `months`.

    Defaults to the current quarter. The target plan is monthly, so there is no
    weekly target to measure a single week against; the quarter is the smallest
    window in which a brand-vs-target miss reads as a trend rather than one
    short week. Callers can pass any other window via the request's `period`.
    """
    raw = str(scope).strip().lower()
    region = None if raw in ("all", "national", "") else ds.resolve_region(scope)
    if region is None and raw not in ("all", "national", ""):
        return []                              # caller turns this into a refusal
    regions = [region] if region else CANONICAL_REGIONS

    if not months:
        months, period_label = M.months_in_period(ds, "this quarter")
    period_label = period_label or f"{months[0]} to {months[-1]}"
    actions: list[dict] = []
    active_promos = M.promos_overlapping(pre.uplifts, months)

    for reg in regions:
        # ── Brand x region rules (R-01, R-02, R-03, R-05, R-06) ──
        for (brand, rule_id), grp in _brand_rule_hits(ds, pre, months, reg).items():
            hit = grp["months"]
            window = hit[0] if len(hit) == 1 else f"{hit[0]} to {hit[-1]}"
            t = M.totals(ds, hit, brand, reg)
            ach = t["achievement_pct"]
            base_ev = {
                "brand": brand, "region": reg, "period": window,
                "months_breaching": hit,
                "sales_inr": t["sales_inr"], "target_inr": t["target_inr"],
                "gap_inr": t["gap_inr"], "achievement_pct": ach,
            }

            if rule_id == "R-01":
                so = M.stockouts_in(ds, hit, brand, reg)
                ev = [base_ev, {
                    "stockout_distributor_weeks": int(len(so)),
                    "skus_out": sorted(so["item_code"].unique().tolist()),
                    "distributors": sorted(so["distributor_id"].unique().tolist()),
                    "days_out_of_stock": int(so["days_out_of_stock"].sum()),
                }]
                notes = ds.documents_for(brand, reg, hit)
                if notes:
                    ev.append({"supporting_document": notes[0]["file"],
                               "excerpt": _excerpt(notes[0]["text"], about=brand)})
                actions.append(_emit(
                    ds, "R-01",
                    f"{brand} in {reg} reached {ach:.1f}% of target over {window} "
                    f"({_inr(t['sales_inr'])} against {_inr(t['target_inr'])}, "
                    f"{_inr(t['gap_inr'])} short), with {len(so)} distributor-week "
                    f"stock-outs on {so['item_code'].nunique()} SKU(s) in the same window.",
                    ev, reg, window))

            elif rule_id == "R-02":
                p = grp["extra"]["promo"]
                actions.append(_emit(
                    ds, "R-02",
                    f"{brand} in {reg} reached {ach:.1f}% of target over {window} while "
                    f"{p['promo_id']} ({p['mechanic']}) ran, lifting weekly sales only "
                    f"{p['uplift_pct']:.1f}% above the four weeks before it.",
                    [base_ev, _promo_ev(p)], reg, window))

            elif rule_id == "R-03":
                doc = grp["extra"]["doc"]
                actions.append(_emit(
                    ds, "R-03",
                    f"{brand} in {reg} reached {ach:.1f}% of target over {window} "
                    f"({_inr(t['gap_inr'])} short) with no stock-out and no promotion in "
                    f"the window. {doc['file']} records an external cause.",
                    [base_ev, {"stockout_distributor_weeks": 0, "promotions_running": 0},
                     {"supporting_document": doc["file"], "excerpt": _excerpt(doc["text"], about=brand)}],
                    reg, window))

            elif rule_id == "R-06":
                actions.append(_emit(
                    ds, "R-06",
                    f"{brand} in {reg} reached {ach:.1f}% of target over {window} "
                    f"({_inr(t['gap_inr'])} short) with no stock-out, no promotion and no "
                    f"working document covering it. No cause is determinable from the data.",
                    [base_ev, {"stockout_distributor_weeks": 0, "promotions_running": 0,
                               "supporting_document": None}],
                    reg, window))

            elif rule_id == "R-05":
                actions.append(_emit(
                    ds, "R-05",
                    f"{brand} in {reg} reached {ach:.1f}% of target over {window} "
                    f"({_inr(t['sales_inr'])} against {_inr(t['target_inr'])}).",
                    [base_ev], reg, window))

        # ── R-04 — one distributor, one SKU, a run of more than six weeks ──
        for _, c in pre.chronic.get(reg, pd.DataFrame()).iterrows():
            actions.append(_emit(
                ds, "R-04",
                f"Distributor {c['distributor_id']} has been out of stock on {c['item_code']} "
                f"({c['brand']}) for {c['consecutive_weeks']} consecutive weeks, "
                f"{c['from_week']} to {c['to_week']}.",
                [{"distributor_id": c["distributor_id"], "item_code": c["item_code"],
                  "brand": c["brand"], "region": c["region"],
                  "consecutive_weeks": int(c["consecutive_weeks"]),
                  "from_week": c["from_week"], "to_week": c["to_week"],
                  "days_out_of_stock": int(c["days_out_of_stock"]),
                  "threshold_weeks": CHRONIC_WEEKS}],
                reg, "FY26 to date"))

        # ── R-08 — one distributor short across several SKUs in a month ──
        by_dist: OrderedDict[str, dict] = OrderedDict()
        for month in months:
            for m in pre.multi_sku.get((reg, month), []):
                d = by_dist.setdefault(m["distributor_id"],
                                       {"months": [], "skus": set(), "peak": 0,
                                        "region": m["region"]})
                d["months"].append(month)
                d["skus"].update(m["skus"])
                d["peak"] = max(d["peak"], int(m["sku_count"]))
        for dist, d in by_dist.items():
            span = d["months"][0] if len(d["months"]) == 1 else f"{d['months'][0]} to {d['months'][-1]}"
            actions.append(_emit(
                ds, "R-08",
                f"Distributor {dist} reported stock-outs across {d['peak']} SKUs in a single "
                f"month ({', '.join(d['months'])}), {len(d['skus'])} distinct SKUs in total.",
                [{"distributor_id": dist, "region": d["region"], "months": d["months"],
                  "peak_skus_in_a_month": d["peak"], "skus": sorted(d["skus"]),
                  "threshold_skus": MULTI_SKU_COUNT}],
                reg, span))

        # ── R-07 — a mechanic that worked, worth repeating ──
        strong = active_promos[(active_promos["region"] == reg) &
                               (active_promos["uplift_pct"].notna()) &
                               (active_promos["uplift_pct"] > STRONG_UPLIFT_PCT)]
        for _, p in strong.sort_values("uplift_pct", ascending=False).iterrows():
            actions.append(_emit(
                ds, "R-07",
                f"{p['promo_id']} ({p['mechanic']} on {p['sku_name']}, {p['brand']}) in {reg} "
                f"lifted weekly sales {p['uplift_pct']:.1f}% above the four weeks before it.",
                [_promo_ev(p)], reg, f"{p['start_date']} to {p['end_date']}"))

    actions = [a for a in actions if a is not None]
    actions.sort(key=lambda a: (_SEVERITY.get(a["rule_id"], 9), a["finding"]))
    return actions
