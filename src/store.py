"""Load, reconcile and pre-aggregate the ACPL data pack.

Source files are never modified. Every disagreement between the exports is
resolved here, in code, and recorded in `DataStore.reconciliation` so the
repair log can be printed rather than claimed.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

import pandas as pd

from src.config import DATA_DIR

CANONICAL_REGIONS = ["North", "South", "East", "West"]
FY26_START = dt.date(2025, 7, 1)
FY26_END = dt.date(2026, 6, 30)

# Every region spelling observed across the exports maps to one canonical name.
_REGION_RE = re.compile(r"^(north|south|east|west)(ern)?(\s+region)?$")


def normalise_region(raw) -> str | None:
    """Canonicalise a region string, or None if it does not resolve.

    stockouts.csv is keyed by free text typed into the distributor portal, so it
    carries 'NORTH', 'north', 'North Region' and a literal 'region' header echo.
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    m = _REGION_RE.match(str(raw).strip().lower())
    return m.group(1).capitalize() if m else None


def _drop_excel_padding(df: pd.DataFrame) -> tuple[pd.DataFrame, int, int]:
    """Strip the empty trailing columns and rows Excel leaves behind on export."""
    named = [c for c in df.columns if not str(c).startswith("Unnamed")]
    dropped_cols = len(df.columns) - len(named)
    df = df[named]
    before = len(df)
    df = df.dropna(how="all").reset_index(drop=True)
    return df, dropped_cols, before - len(df)


@dataclass
class Repair:
    """One reconciled disagreement between sources, for the audit log."""

    source: str
    issue: str
    resolution: str
    rows_affected: int = 0


@dataclass
class DataStore:
    sales: pd.DataFrame = field(default_factory=pd.DataFrame)
    targets: pd.DataFrame = field(default_factory=pd.DataFrame)
    stockouts: pd.DataFrame = field(default_factory=pd.DataFrame)
    promos: pd.DataFrame = field(default_factory=pd.DataFrame)
    sku: pd.DataFrame = field(default_factory=pd.DataFrame)
    geo: pd.DataFrame = field(default_factory=pd.DataFrame)
    distributors: pd.DataFrame = field(default_factory=pd.DataFrame)

    # Derived
    facts: pd.DataFrame = field(default_factory=pd.DataFrame)   # brand x region x month
    playbook: dict = field(default_factory=dict)                # rule_id -> rule
    documents: list = field(default_factory=list)
    reconciliation: list = field(default_factory=list)
    raw_counts: dict = field(default_factory=dict)

    brands: list = field(default_factory=list)
    _brand_skus: dict = field(default_factory=dict)
    _region_territories: dict = field(default_factory=dict)
    _sku_brand: dict = field(default_factory=dict)
    _dist_region: dict = field(default_factory=dict)

    # ── Load ───────────────────────────────────────────────────────────────

    def load_all(self) -> "DataStore":
        self._load_csvs()
        self._reconcile()
        self._build_indexes()
        self._build_facts()
        self._load_playbook()
        self._load_documents()
        return self

    def _load_csvs(self) -> None:
        self.sales = pd.read_csv(DATA_DIR / "fact_primary_sales.csv")
        self.targets = pd.read_csv(DATA_DIR / "fact_targets.csv")
        self.stockouts = pd.read_csv(DATA_DIR / "stockouts.csv")
        self.promos = pd.read_csv(DATA_DIR / "promotions.csv")
        self.sku = pd.read_csv(DATA_DIR / "dim_sku.csv")
        self.geo = pd.read_csv(DATA_DIR / "dim_geo.csv")
        self.distributors = pd.read_csv(DATA_DIR / "dim_distributor.csv")
        self.raw_counts = {
            "fact_primary_sales.csv": len(self.sales),
            "fact_targets.csv": len(self.targets),
            "stockouts.csv": len(self.stockouts),
            "promotions.csv": len(self.promos),
            "dim_sku.csv": len(self.sku),
            "dim_geo.csv": len(self.geo),
            "dim_distributor.csv": len(self.distributors),
        }

    def _reconcile(self) -> None:
        rec = self.reconciliation.append

        # ── promotions.csv / dim_distributor.csv: Excel export padding ──
        for name, attr in (("promotions.csv", "promos"),
                           ("dim_distributor.csv", "distributors")):
            df, n_cols, n_rows = _drop_excel_padding(getattr(self, attr))
            setattr(self, attr, df)
            if n_cols or n_rows:
                rec(Repair(name,
                           f"{n_cols} unnamed columns and {n_rows} fully-empty rows "
                           f"left by the Excel export",
                           "dropped as export padding; no populated row lost",
                           n_rows))

        # ── promotions.csv: DD/MM/YYYY, day and month not zero-padded ──
        for col in ("start_date", "end_date"):
            self.promos[col] = pd.to_datetime(
                self.promos[col], format="%d/%m/%Y", errors="coerce")
        bad_dates = int(self.promos[["start_date", "end_date"]].isna().any(axis=1).sum())
        rec(Repair("promotions.csv",
                   "dates are DD/MM/YYYY with unpadded parts ('1/7/2025'); every other "
                   "file is YYYY-MM-DD",
                   "parsed day-first explicitly so 1/7/2025 reads as 1 July, not 7 January",
                   len(self.promos) - bad_dates))
        if bad_dates:
            self.promos = self.promos.dropna(subset=["start_date", "end_date"]).reset_index(drop=True)

        # ── promotions.csv: region free text ──
        self.promos["region"] = self.promos["region"].map(normalise_region)
        unresolved = int(self.promos["region"].isna().sum())
        if unresolved:
            self.promos = self.promos.dropna(subset=["region"]).reset_index(drop=True)
            rec(Repair("promotions.csv", f"{unresolved} rows with an unresolvable region",
                       "dropped; cannot be attributed to a region", unresolved))
        self.promos["discount_pct"] = pd.to_numeric(self.promos["discount_pct"], errors="coerce")

        # ── stockouts.csv: region typed into the distributor portal ──
        raw_variants = sorted({str(v) for v in self.stockouts["region"].dropna().unique()})
        self.stockouts["region"] = self.stockouts["region"].map(normalise_region)
        junk = self.stockouts["region"].isna()
        n_junk = int(junk.sum())
        rec(Repair("stockouts.csv",
                   f"region typed free-hand: {len(raw_variants)} distinct spellings including "
                   + ", ".join(repr(v) for v in raw_variants[:4]),
                   "case-folded and the ' Region' suffix stripped, to the 4 canonical names "
                   "in dim_geo",
                   len(self.stockouts) - n_junk))
        if n_junk:
            self.stockouts = self.stockouts[~junk].reset_index(drop=True)
            rec(Repair("stockouts.csv",
                       f"{n_junk} rows whose region column repeats the header word 'region'",
                       "dropped; not a region", n_junk))
        self.stockouts["week_start"] = pd.to_datetime(self.stockouts["week_start"])
        self.stockouts["month"] = self.stockouts["week_start"].dt.strftime("%Y-%m")

        # ── stockouts.csv vs the masters: region is duplicated, and can disagree ──
        self.distributors = self.distributors.merge(
            self.geo[["territory_code", "region"]], on="territory_code", how="left")
        dist_region = dict(zip(self.distributors["distributor_id"], self.distributors["region"]))
        logged = self.stockouts["region"]
        master = self.stockouts["distributor_id"].map(dist_region)
        conflict = int(((logged != master) & master.notna()).sum())
        rec(Repair("stockouts.csv x dim_distributor.csv x dim_geo.csv",
                   "the stock-out log carries its own region, which can disagree with the "
                   "distributor's territory in the masters",
                   "dim_geo via dim_distributor is authoritative; the logged region is kept "
                   f"only as a fallback ({conflict} rows disagreed)",
                   conflict))
        self.stockouts["region"] = master.fillna(logged)

        # ── fact_primary_sales.csv: weekly grain, month = month of week_start ──
        self.sales["week_start"] = pd.to_datetime(self.sales["week_start"])
        self.sales["month"] = self.sales["week_start"].dt.strftime("%Y-%m")
        rec(Repair("fact_primary_sales.csv x fact_targets.csv",
                   "sales are weekly by SKU x territory; targets are monthly by brand x region",
                   "sales rolled up to brand x region x month (dim_sku for brand, dim_geo for "
                   "region; a week belongs to the month containing its week_start, per the data "
                   "dictionary) before any comparison",
                   len(self.sales)))

        # ── item_code vs sku_code vs sku ──
        unknown_items = int((~self.stockouts["item_code"].isin(self.sku["sku_code"])).sum())
        rec(Repair("stockouts.csv x dim_sku.csv",
                   "the stock-out log names the product column `item_code`; the master calls "
                   "it `sku_code`",
                   f"joined as the same key; {len(self.stockouts) - unknown_items} of "
                   f"{len(self.stockouts)} rows resolve to a known SKU",
                   unknown_items))
        unknown_promo_sku = int((~self.promos["sku"].isin(self.sku["sku_code"])).sum())
        rec(Repair("promotions.csv x dim_sku.csv",
                   "the promotion calendar names the product column `sku`",
                   f"joined to `sku_code`; {len(self.promos) - unknown_promo_sku} of "
                   f"{len(self.promos)} promotions resolve to a known SKU",
                   unknown_promo_sku))

    def _build_indexes(self) -> None:
        self.brands = sorted(self.sku["brand"].dropna().unique().tolist())
        self._sku_brand = dict(zip(self.sku["sku_code"], self.sku["brand"]))
        for b in self.brands:
            self._brand_skus[b] = self.sku.loc[self.sku["brand"] == b, "sku_code"].tolist()
        for r in CANONICAL_REGIONS:
            self._region_territories[r] = self.geo.loc[
                self.geo["region"] == r, "territory_code"].tolist()
        self._dist_region = dict(zip(self.distributors["distributor_id"],
                                     self.distributors["region"]))

    def _build_facts(self) -> None:
        """One brand x region x month table: the spine every comparison uses."""
        s = (self.sales
             .merge(self.sku[["sku_code", "brand"]], on="sku_code", how="left")
             .merge(self.geo[["territory_code", "region"]], on="territory_code", how="left"))
        agg = (s.groupby(["brand", "region", "month"], as_index=False)
                .agg(sales_inr=("value_inr", "sum"), units=("units", "sum")))

        t = self.targets.rename(columns={"brand_name": "brand", "region_name": "region"})
        t = t.groupby(["brand", "region", "month"], as_index=False)["target_value_inr"].sum()

        facts = agg.merge(t, on=["brand", "region", "month"], how="outer")
        facts["sales_inr"] = facts["sales_inr"].fillna(0.0)
        facts["units"] = facts["units"].fillna(0).astype(int)
        facts["target_value_inr"] = facts["target_value_inr"].fillna(0.0)
        facts["gap_inr"] = facts["target_value_inr"] - facts["sales_inr"]
        facts["achievement_pct"] = (
            facts["sales_inr"] / facts["target_value_inr"].replace(0, pd.NA) * 100)
        facts["quarter"] = facts["month"].map(month_to_quarter)
        self.facts = facts.sort_values(["month", "brand", "region"]).reset_index(drop=True)

    def _load_playbook(self) -> None:
        import openpyxl
        wb = openpyxl.load_workbook(DATA_DIR / "action_playbook.xlsx", data_only=True)
        ws = wb["playbook"]
        rows = list(ws.iter_rows(values_only=True))
        header = [str(h).strip().lower() for h in rows[0]]
        for row in rows[1:]:
            if row[0] is None:
                continue
            r = dict(zip(header, row))
            rid = str(r["rule_id"]).strip()
            self.playbook[rid] = {
                "rule_id": rid,
                "condition": str(r["condition"]).strip(),
                "recommendation": str(r["recommendation"]).strip(),
                "action": str(r["action"]).strip(),
                "needs_approval": str(r["needs_approval"]).strip().lower() in ("yes", "y", "true"),
            }

    def _load_documents(self) -> None:
        """Index the working documents by the entities they actually mention.

        Retrieval is by entity overlap, not by filename: a document earns its way
        into an answer only when it names the brand / region / period in question,
        which is what keeps the HR circular and the routine weekly roll-up out.
        """
        import docx

        month_names = {m: i for i, m in enumerate(
            ["january", "february", "march", "april", "may", "june", "july",
             "august", "september", "october", "november", "december"], 1)}

        for path in sorted((DATA_DIR / "documents").glob("*.docx")):
            try:
                d = docx.Document(str(path))
            except Exception as exc:            # a corrupt file must not stop startup
                self.reconciliation.append(Repair(path.name, f"unreadable: {exc}", "skipped", 0))
                continue
            text = "\n".join(p.text for p in d.paragraphs if p.text.strip())
            # The pack contains U+FFFD where an en-dash was mis-encoded on export.
            text = text.replace("�", "-")
            low = text.lower()
            stem_low = path.stem.lower()

            regions = [r for r in CANONICAL_REGIONS if re.search(rf"\b{r.lower()}\b", low)]
            brands = [b for b in self.brands if b.lower() in low]
            categories = [c for c in self.sku["category"].unique() if c.lower() in low]
            # A note that names a category ("the Beverages 1L line") is about every
            # brand in it, which is how the West supply note reaches Aqualite.
            for c in categories:
                brands += self.sku.loc[self.sku["category"] == c, "brand"].unique().tolist()

            hit_months: list[int] = []
            for name, num in month_names.items():
                # Match the full name in the body, or a 3-letter form at a word
                # boundary in the filename ('..._feb2026'). Without the boundary
                # 'summary' would read as March.
                if name in low or re.search(rf"\b{name[:3]}", stem_low):
                    hit_months.append(num)
            # 'through the April-June quarter' covers the months in between.
            for a, b in re.findall(r"(\w+)\s*[-–—]\s*(\w+)", low):
                if a in month_names and b in month_names:
                    lo, hi = month_names[a], month_names[b]
                    hit_months += list(range(lo, hi + 1)) if lo <= hi else []
            years = [y for y in (2025, 2026) if str(y) in low or str(y) in stem_low]
            months = [f"{y or (2025 if num >= 7 else 2026)}-{num:02d}"
                      for num in set(hit_months) for y in (years or [None])]

            self.documents.append({
                "name": path.stem,
                "file": path.name,
                "text": text,
                "regions": regions,
                "brands": sorted(set(brands)),
                "categories": categories,
                "months": sorted(set(months)),
                "distributors": sorted(set(re.findall(r"\bD\d{3}\b", text))),
            })

    # ── Lookups ────────────────────────────────────────────────────────────

    def resolve_brand(self, name: str) -> str | None:
        n = str(name).strip().lower()
        for b in self.brands:
            if b.lower() == n:
                return b
        for b in self.brands:                   # 'Creme Delight' -> 'CremeDelight'
            if b.lower().replace(" ", "") == n.replace(" ", ""):
                return b
        return None

    def resolve_region(self, name: str) -> str | None:
        return normalise_region(name)

    def skus_for_brand(self, brand: str) -> list[str]:
        return self._brand_skus.get(brand, [])

    def territories_for_region(self, region: str) -> list[str]:
        return self._region_territories.get(region, [])

    def brand_of(self, sku_code: str) -> str | None:
        return self._sku_brand.get(sku_code)

    def region_of_distributor(self, distributor_id: str) -> str | None:
        return self._dist_region.get(distributor_id)

    @property
    def months(self) -> list[str]:
        return sorted(self.facts["month"].unique().tolist())

    @property
    def latest_month(self) -> str:
        return max(self.sales["month"].unique())

    @property
    def data_max_date(self) -> dt.date:
        return self.sales["week_start"].max().date()

    def documents_for(self, brand: str | None = None, region: str | None = None,
                      months: list[str] | None = None) -> list[dict]:
        """Documents that name the given entities. Unmatched documents stay out."""
        hits = []
        for doc in self.documents:
            if brand and brand not in doc["brands"]:
                continue
            if region and region not in doc["regions"]:
                continue
            if months and doc["months"] and not set(months) & set(doc["months"]):
                continue
            if not (doc["brands"] or doc["regions"]):       # names no entity at all
                continue
            hits.append(doc)
        return hits


def month_to_quarter(month: str) -> str:
    """FY26 quarter label for a YYYY-MM month (the financial year starts in July)."""
    m = int(month[5:7])
    return f"Q{(m - 7) % 12 // 3 + 1} FY26"
