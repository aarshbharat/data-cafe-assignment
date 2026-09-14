"""Deterministic measurement over the reconciled data.

Nothing in this module calls a model. Every number an answer or an action
carries is produced here, from the CSVs, so it can be recomputed and checked.
"""

from __future__ import annotations

import datetime as dt
import pandas as pd

from src.store import DataStore, month_to_quarter

# A promotion's uplift is its weekly run-rate against the four weeks before it.
BASELINE_WEEKS = 4


# ── Periods ────────────────────────────────────────────────────────────────

def fy26_months() -> list[str]:
    return [f"2025-{m:02d}" for m in range(7, 13)] + [f"2026-{m:02d}" for m in range(1, 7)]


QUARTER_MONTHS = {
    "Q1 FY26": ["2025-07", "2025-08", "2025-09"],
    "Q2 FY26": ["2025-10", "2025-11", "2025-12"],
    "Q3 FY26": ["2026-01", "2026-02", "2026-03"],
    "Q4 FY26": ["2026-04", "2026-05", "2026-06"],
}


def months_in_period(ds: DataStore, period: str | None) -> tuple[list[str], str]:
    """Resolve a period phrase to FY26 months plus a label to show the user.

    'This quarter' is anchored to the last week present in the data, not to the
    wall clock, so the service gives the same answer next year as it does today.
    """
    all_months = fy26_months()
    if not period:
        return all_months, "FY26 (Jul 2025 - Jun 2026)"

    p = str(period).strip().lower()
    today = ds.data_max_date
    cur_month = today.strftime("%Y-%m")
    cur_q = month_to_quarter(cur_month)

    if p in ("this quarter", "current quarter", "the quarter", "qtd", "quarter to date"):
        return QUARTER_MONTHS[cur_q], f"{cur_q} ({_span(QUARTER_MONTHS[cur_q])})"
    if p in ("last quarter", "previous quarter"):
        idx = int(cur_q[1]) - 1
        q = f"Q{idx} FY26" if idx >= 1 else "Q1 FY26"
        return QUARTER_MONTHS[q], f"{q} ({_span(QUARTER_MONTHS[q])})"
    if p in ("this month", "current month", "latest month", "mtd"):
        return [cur_month], _label_month(cur_month)
    if p in ("last month", "previous month"):
        i = all_months.index(cur_month)
        m = all_months[max(i - 1, 0)]
        return [m], _label_month(m)
    if p in ("this week", "latest week", "the week", "this week's", "wtd"):
        # Targets are monthly, so a week has no target of its own to be measured
        # against. Say which window the figures actually cover.
        return [cur_month], (f"the week commencing {today:%d %b %Y} "
                             f"(assessed over {_label_month(cur_month)}, the smallest "
                             f"window with a target)")
    if p in ("fy26", "fy 26", "fy2026", "this year", "full year", "the year", "ytd",
             "year to date", "financial year", "2025-26", "fy25-26"):
        return all_months, "FY26 (Jul 2025 - Jun 2026)"
    if p in ("h1", "h1 fy26", "first half"):
        return all_months[:6], "H1 FY26 (Jul - Dec 2025)"
    if p in ("h2", "h2 fy26", "second half"):
        return all_months[6:], "H2 FY26 (Jan - Jun 2026)"

    for q, months in QUARTER_MONTHS.items():
        if q.lower().replace(" fy26", "") in p.split() or q.lower() in p:
            return months, f"{q} ({_span(months)})"

    # An explicit YYYY-MM, or a month name with an optional year.
    import re
    m = re.search(r"(20\d{2})[-/](\d{1,2})", p)
    if m:
        mm = f"{int(m.group(1))}-{int(m.group(2)):02d}"
        return ([mm], _label_month(mm)) if mm in all_months else ([], period)
    names = ["january", "february", "march", "april", "may", "june", "july",
             "august", "september", "october", "november", "december"]
    for i, name in enumerate(names, 1):
        if name[:3] in p:
            yr = 2025 if "2025" in p else 2026 if "2026" in p else (2025 if i >= 7 else 2026)
            mm = f"{yr}-{i:02d}"
            return ([mm], _label_month(mm)) if mm in all_months else ([], period)

    return [], period            # unrecognised -> caller declines rather than guesses


def _span(months: list[str]) -> str:
    return f"{_label_month(months[0])} - {_label_month(months[-1])}"


def _label_month(m: str) -> str:
    return dt.date(int(m[:4]), int(m[5:7]), 1).strftime("%b %Y")


# ── Performance ────────────────────────────────────────────────────────────

def performance(ds: DataStore, months: list[str], brand: str | None = None,
                region: str | None = None) -> pd.DataFrame:
    """brand x region rows for the period, with sales, target, gap, achievement."""
    f = ds.facts[ds.facts["month"].isin(months)]
    if brand:
        f = f[f["brand"] == brand]
    if region:
        f = f[f["region"] == region]
    if f.empty:
        return f
    out = (f.groupby(["brand", "region"], as_index=False)
            .agg(sales_inr=("sales_inr", "sum"),
                 target_inr=("target_value_inr", "sum"),
                 units=("units", "sum")))
    out["gap_inr"] = out["target_inr"] - out["sales_inr"]
    out["achievement_pct"] = out["sales_inr"] / out["target_inr"].replace(0, pd.NA) * 100
    return out


def totals(ds: DataStore, months: list[str], brand: str | None = None,
           region: str | None = None) -> dict:
    p = performance(ds, months, brand, region)
    if p.empty:
        return {}
    sales = float(p["sales_inr"].sum())
    target = float(p["target_inr"].sum())
    return {
        "sales_inr": round(sales, 2),
        "target_inr": round(target, 2),
        "gap_inr": round(target - sales, 2),
        "achievement_pct": round(sales / target * 100, 1) if target else None,
        "units": int(p["units"].sum()),
    }


def ranked_gaps(ds: DataStore, months: list[str], brand: str | None = None,
                region: str | None = None, top: int = 5, best: bool = False) -> pd.DataFrame:
    p = performance(ds, months, brand, region)
    if p.empty:
        return p
    key = "achievement_pct" if best else "gap_inr"
    return p.sort_values(key, ascending=False).head(top)


# ── Stock-outs ─────────────────────────────────────────────────────────────

def stockouts_in(ds: DataStore, months: list[str], brand: str | None = None,
                 region: str | None = None) -> pd.DataFrame:
    so = ds.stockouts[ds.stockouts["month"].isin(months)]
    if region:
        so = so[so["region"] == region]
    if brand:
        so = so[so["item_code"].isin(ds.skus_for_brand(brand))]
    return so


def chronic_stockouts(ds: DataStore, region: str | None = None,
                      min_weeks: int = 6) -> pd.DataFrame:
    """Distributor x SKU pairs out of stock for more than `min_weeks` weeks (R-04).

    Counted over consecutive weeks, because 'out of stock for more than 6 weeks'
    is a run, not a total across the year.
    """
    so = ds.stockouts if region is None else ds.stockouts[ds.stockouts["region"] == region]
    rows = []
    for (dist, item), grp in so.groupby(["distributor_id", "item_code"]):
        weeks = sorted(grp["week_start"].unique())
        run, run_start = 1, weeks[0]
        best_run, best_start, best_end = 1, weeks[0], weeks[0]
        for prev, cur in zip(weeks, weeks[1:]):
            if (cur - prev) == pd.Timedelta(days=7):
                run += 1
            else:
                run, run_start = 1, cur
            if run > best_run:
                best_run, best_start, best_end = run, run_start, cur
        if best_run > min_weeks:
            rows.append({
                "distributor_id": dist,
                "item_code": item,
                "brand": ds.brand_of(item),
                "region": ds.region_of_distributor(dist) or grp["region"].iloc[0],
                "consecutive_weeks": int(best_run),
                "from_week": str(pd.Timestamp(best_start).date()),
                "to_week": str(pd.Timestamp(best_end).date()),
                "days_out_of_stock": int(grp["days_out_of_stock"].sum()),
            })
    return pd.DataFrame(rows)


def multi_sku_stockouts(ds: DataStore, month: str, region: str | None = None,
                        min_skus: int = 3) -> pd.DataFrame:
    """Distributors out of stock across `min_skus`+ SKUs within one month (R-08)."""
    so = ds.stockouts[ds.stockouts["month"] == month]
    if region:
        so = so[so["region"] == region]
    if so.empty:
        return pd.DataFrame()
    g = (so.groupby("distributor_id")
           .agg(sku_count=("item_code", "nunique"),
                skus=("item_code", lambda s: sorted(set(s))),
                region=("region", "first"))
           .reset_index())
    return g[g["sku_count"] >= min_skus]


# ── Promotions ─────────────────────────────────────────────────────────────

def promo_uplift(ds: DataStore, promo: pd.Series) -> dict:
    """Weekly run-rate inside the promo window vs the `BASELINE_WEEKS` before it.

    Returns uplift_pct=None when there is no baseline to compare against — the
    rules that need an uplift figure then decline rather than assume one.
    """
    terr = ds.territories_for_region(promo["region"])
    sku_rows = ds.sales[(ds.sales["sku_code"] == promo["sku"]) &
                        (ds.sales["territory_code"].isin(terr))]
    start, end = promo["start_date"], promo["end_date"]
    base_start = start - pd.Timedelta(weeks=BASELINE_WEEKS)

    during = sku_rows[(sku_rows["week_start"] >= start) & (sku_rows["week_start"] <= end)]
    before = sku_rows[(sku_rows["week_start"] >= base_start) & (sku_rows["week_start"] < start)]

    n_during = during["week_start"].nunique()
    n_before = before["week_start"].nunique()
    rate_during = during["value_inr"].sum() / n_during if n_during else None
    rate_before = before["value_inr"].sum() / n_before if n_before else None

    uplift = None
    if rate_during is not None and rate_before:
        uplift = round((rate_during / rate_before - 1) * 100, 1)

    return {
        "promo_id": promo["promo_id"],
        "sku": promo["sku"],
        "sku_name": ds.sku.set_index("sku_code")["sku_name"].get(promo["sku"]),
        "brand": ds.brand_of(promo["sku"]),
        "region": promo["region"],
        "mechanic": promo["mechanic"],
        "discount_pct": None if pd.isna(promo["discount_pct"]) else float(promo["discount_pct"]),
        "start_date": str(start.date()),
        "end_date": str(end.date()),
        "weeks_in_promo": int(n_during),
        "weekly_sales_during_inr": round(rate_during, 2) if rate_during is not None else None,
        "weekly_sales_before_inr": round(rate_before, 2) if rate_before else None,
        "uplift_pct": uplift,
    }


def all_promo_uplifts(ds: DataStore) -> pd.DataFrame:
    """Uplift for every promotion. Computed once at startup and reused."""
    return pd.DataFrame([promo_uplift(ds, p) for _, p in ds.promos.iterrows()])


# ── Startup pre-computation ────────────────────────────────────────────────

class Precomputed:
    """Everything a request would otherwise recompute from scratch.

    Built once at startup so `/actions` is a lookup over small frames rather
    than a scan of the 74,880-row sales ledger per brand.
    """

    def __init__(self, ds: DataStore) -> None:
        self.uplifts = all_promo_uplifts(ds)

        # Stock-out weeks per brand x region x month, for the has-stock-out test.
        so = ds.stockouts.copy()
        so["brand"] = so["item_code"].map(ds.brand_of)
        self.stockout_index = (so.groupby(["brand", "region", "month"], as_index=False)
                                 .agg(weeks=("week_start", "count"),
                                      skus=("item_code", "nunique")))
        self._so_keys = set(map(tuple, self.stockout_index[["brand", "region", "month"]].values))

        # R-04 runs do not depend on the request window, so cost them once.
        self.chronic = {r: chronic_stockouts(ds, r) for r in ds.facts["region"].unique()}

        # R-08 candidates for every month, so a request is a dict lookup.
        self.multi_sku: dict[tuple[str, str], list[dict]] = {}
        for month in ds.facts["month"].unique():
            hits = multi_sku_stockouts(ds, month)
            for rec in hits.to_dict("records") if not hits.empty else []:
                self.multi_sku.setdefault((rec["region"], month), []).append(rec)

        # Which promotions were live for a brand x region in a given month.
        self.promo_months: dict[tuple[str, str, str], list[dict]] = {}
        for rec in (self.uplifts.to_dict("records") if not self.uplifts.empty else []):
            if not rec["brand"]:
                continue
            cur = pd.Timestamp(rec["start_date"]).to_period("M")
            last = pd.Timestamp(rec["end_date"]).to_period("M")
            while cur <= last:
                key = (rec["brand"], rec["region"], str(cur))
                self.promo_months.setdefault(key, []).append(rec)
                cur += 1

    def has_stockout(self, brand: str, region: str, month: str) -> bool:
        return (brand, region, month) in self._so_keys

    def promos_live(self, brand: str, region: str, month: str) -> list[dict]:
        return self.promo_months.get((brand, region, month), [])


def promos_overlapping(uplifts: pd.DataFrame, months: list[str],
                       brand: str | None = None, region: str | None = None) -> pd.DataFrame:
    """Promotions whose window overlaps any of `months`."""
    if uplifts.empty or not months:
        return uplifts.iloc[0:0]
    lo = pd.Timestamp(f"{months[0]}-01")
    hi = pd.Timestamp(f"{months[-1]}-01") + pd.offsets.MonthEnd(0)
    m = (pd.to_datetime(uplifts["start_date"]) <= hi) & (pd.to_datetime(uplifts["end_date"]) >= lo)
    out = uplifts[m]
    if brand:
        out = out[out["brand"] == brand]
    if region:
        out = out[out["region"] == region]
    return out
