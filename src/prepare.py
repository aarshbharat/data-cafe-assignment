"""One-command data preparation.

    python -m src.prepare

Reads the seven CSVs, the playbook workbook and the Word documents exactly as
provided, applies every reconciliation in `src/store.py`, and writes the derived
tables plus a manifest to `build/`. The provided files are only ever read.

The service does this same preparation in memory at startup, so running this is
not a prerequisite for serving — it exists so the preparation is reproducible
and inspectable, and it is where the figures in ARTEFACT.md come from.
"""

from __future__ import annotations

import json
import sys
import time

from src import metrics as M
from src.config import BUILD_DIR, DATA_DIR
from src.store import DataStore


def main() -> int:
    if not DATA_DIR.exists():
        print(f"ERROR: data pack not found at {DATA_DIR}", file=sys.stderr)
        print("Unzip 'ACPL Data Pack.zip' into data/ and re-run.", file=sys.stderr)
        return 1

    t0 = time.perf_counter()
    ds = DataStore().load_all()
    pre = M.Precomputed(ds)
    elapsed = (time.perf_counter() - t0) * 1000

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    ds.facts.to_csv(BUILD_DIR / "facts_brand_region_month.csv", index=False)
    ds.stockouts.to_csv(BUILD_DIR / "stockouts_clean.csv", index=False)
    ds.promos.to_csv(BUILD_DIR / "promotions_clean.csv", index=False)
    pre.uplifts.to_csv(BUILD_DIR / "promo_uplift.csv", index=False)

    national = float(ds.sales["value_inr"].sum())
    manifest = {
        "prepared_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "prepare_ms": round(elapsed, 1),
        "source_dir": str(DATA_DIR),
        "rows_raw": ds.raw_counts,
        "rows_after_preparation": {
            "fact_primary_sales.csv": len(ds.sales),
            "fact_targets.csv": len(ds.targets),
            "stockouts.csv": len(ds.stockouts),
            "promotions.csv": len(ds.promos),
            "dim_sku.csv": len(ds.sku),
            "dim_geo.csv": len(ds.geo),
            "dim_distributor.csv": len(ds.distributors),
            "action_playbook.xlsx": len(ds.playbook),
            "documents/": len(ds.documents),
        },
        "derived": {
            "facts_brand_region_month": len(ds.facts),
            "promo_uplift": len(pre.uplifts),
            "promo_uplift_measurable": int(pre.uplifts["uplift_pct"].notna().sum()),
            "chronic_stockout_pairs": sum(len(c) for c in pre.chronic.values()),
        },
        "coverage": {
            "weeks": int(ds.sales["week_start"].nunique()),
            "first_week": str(ds.sales["week_start"].min().date()),
            "last_week": str(ds.sales["week_start"].max().date()),
            "months": len(ds.months),
            "brands": ds.brands,
            "regions": sorted(ds.geo["region"].unique().tolist()),
        },
        "national_fy26_primary_sales_inr": round(national, 2),
        "national_fy26_target_inr": float(ds.targets["target_value_inr"].sum()),
        "reconciliation": [r.__dict__ for r in ds.reconciliation],
        "documents": [{k: d[k] for k in ("file", "regions", "brands", "categories",
                                         "months", "distributors")} for d in ds.documents],
        "playbook": list(ds.playbook.values()),
    }
    (BUILD_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8")

    print(f"Prepared in {elapsed:.0f} ms -> {BUILD_DIR}")
    print(f"  sales {len(ds.sales):,} rows over {manifest['coverage']['weeks']} weeks "
          f"({manifest['coverage']['first_week']} to {manifest['coverage']['last_week']})")
    print(f"  facts {len(ds.facts)} brand x region x month rows")
    print(f"  national FY26 primary sales INR {national:,.2f}")
    print(f"  {len(ds.playbook)} playbook rules, {len(ds.documents)} documents")
    print(f"  {len(ds.reconciliation)} reconciliations applied:")
    for r in ds.reconciliation:
        print(f"    - {r.source}: {r.issue}")
        print(f"        -> {r.resolution}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
