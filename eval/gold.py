"""Ground truth, recomputed from the CSVs with plain pandas.

Deliberately independent of `src/` — it re-reads the source files and redoes the
joins by hand. If this module and the service ever disagree, one of them is
wrong, which is the whole point of checking against it rather than against the
service's own output.
"""

from __future__ import annotations

import pandas as pd

from src.config import DATA_DIR

Q4 = ["2026-04", "2026-05", "2026-06"]


def _facts() -> pd.DataFrame:
    sales = pd.read_csv(DATA_DIR / "fact_primary_sales.csv", parse_dates=["week_start"])
    sku = pd.read_csv(DATA_DIR / "dim_sku.csv")
    geo = pd.read_csv(DATA_DIR / "dim_geo.csv")
    tgt = pd.read_csv(DATA_DIR / "fact_targets.csv")

    sales["month"] = sales["week_start"].dt.strftime("%Y-%m")
    s = (sales.merge(sku[["sku_code", "brand"]], on="sku_code")
              .merge(geo[["territory_code", "region"]], on="territory_code"))
    agg = s.groupby(["brand", "region", "month"], as_index=False)["value_inr"].sum()
    t = tgt.rename(columns={"brand_name": "brand", "region_name": "region"})
    f = agg.merge(t, on=["brand", "region", "month"], how="outer").fillna(0.0)
    f["gap"] = f["target_value_inr"] - f["value_inr"]
    return f


def _slice(f: pd.DataFrame, months, brand=None, region=None) -> dict:
    x = f[f["month"].isin(months)]
    if brand:
        x = x[x["brand"] == brand]
    if region:
        x = x[x["region"] == region]
    sales = float(x["value_inr"].sum())
    target = float(x["target_value_inr"].sum())
    return {"sales_inr": round(sales, 2), "target_inr": round(target, 2),
            "gap_inr": round(target - sales, 2),
            "achievement_pct": round(sales / target * 100, 1) if target else None}


def compute() -> dict:
    f = _facts()
    all_months = sorted(f["month"].unique())
    sales_raw = pd.read_csv(DATA_DIR / "fact_primary_sales.csv")
    so = pd.read_csv(DATA_DIR / "stockouts.csv")
    dist = pd.read_csv(DATA_DIR / "dim_distributor.csv").dropna(subset=["distributor_id"])
    geo = pd.read_csv(DATA_DIR / "dim_geo.csv")
    dist = dist.merge(geo[["territory_code", "region"]], on="territory_code", how="left")
    so["region_master"] = so["distributor_id"].map(
        dict(zip(dist["distributor_id"], dist["region"])))
    so["month"] = pd.to_datetime(so["week_start"]).dt.strftime("%Y-%m")

    q4 = f[f["month"].isin(Q4)].groupby(["brand", "region"], as_index=False)[
        ["value_inr", "target_value_inr"]].sum()
    q4["gap"] = q4["target_value_inr"] - q4["value_inr"]
    worst = q4.sort_values("gap", ascending=False).iloc[0]

    west_so = so[(so["region_master"] == "West")]

    return {
        "national_fy26": {"sales_inr": round(float(sales_raw["value_inr"].sum()), 2)},
        "glucojoy_south_fy26": _slice(f, all_months, "GlucoJoy", "South"),
        "west_q4": _slice(f, Q4, None, "West"),
        "cremedelight_north_feb": _slice(f, ["2026-02"], "CremeDelight", "North"),
        "aqualite_west_q4": _slice(f, Q4, "Aqualite", "West"),
        "worst_gap_q4": {"brand": worst["brand"], "region": worst["region"],
                         "gap_inr": round(float(worst["gap"]), 2),
                         "sales_inr": round(float(worst["value_inr"]), 2),
                         "target_inr": round(float(worst["target_value_inr"]), 2)},
        "stockouts_west_fy26": {"distributor_weeks": int(len(west_so)),
                                "distributors": int(west_so["distributor_id"].nunique()),
                                "days_out_of_stock": int(west_so["days_out_of_stock"].sum())},
        "actions_west_q4": {"brand": "Aqualite", "region": "West"},
        "row_counts": {
            "fact_primary_sales.csv": len(sales_raw),
            "fact_targets.csv": len(pd.read_csv(DATA_DIR / "fact_targets.csv")),
            "stockouts.csv": len(so),
            "dim_sku.csv": len(pd.read_csv(DATA_DIR / "dim_sku.csv")),
            "dim_geo.csv": len(geo),
            "dim_distributor.csv": len(dist),
        },
    }


if __name__ == "__main__":
    import json
    print(json.dumps(compute(), indent=2, default=str))
