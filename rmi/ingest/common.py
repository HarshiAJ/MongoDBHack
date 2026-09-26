"""Shared ingestion context: lineage, issue logging, identifier resolution and parsers."""

import bisect
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from rmi.db import ROOT

RAW = ROOT / "data" / "raw"
CROSSWALK = json.loads((ROOT / "config" / "crosswalk.json").read_text())

LB_TO_KG = 0.45359237


def norm(s) -> str:
    """Normalise a free-text identifier for matching: lowercase alphanumerics only."""
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


@dataclass
class Context:
    run_id: str
    started: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    issues: list = field(default_factory=list)
    files: dict = field(default_factory=dict)  # rel path -> {sha256, system, rows_in, rows_loaded}
    docs: dict = field(default_factory=lambda: {
        "materials": {}, "parties": {}, "products": {}, "po_lines": [], "price_quotes": [],
        "forecasts": [], "market_prices": [], "documents": [], "customer_prices": [],
        "negotiations": [], "plans": [], "plan_lines": [],
    })
    # lookups populated as sources load
    fx: dict = field(default_factory=dict)          # "EURUSD" -> (sorted dates, values)
    matnr_to_material: dict = field(default_factory=dict)
    material_alias: dict = field(default_factory=dict)  # norm(name) -> material_id
    vendor_by_lifnr: dict = field(default_factory=dict)
    customer_by_kunnr: dict = field(default_factory=dict)
    customer_alias: dict = field(default_factory=dict)

    def __post_init__(self):
        for mid, aliases in CROSSWALK["material_aliases"].items():
            for a in aliases + [mid]:
                self.material_alias[norm(a)] = mid
        for cid, c in CROSSWALK["customers"].items():
            for a in c["aliases"] + [c["name"], cid]:
                self.customer_alias[norm(a)] = cid

    # -- lineage -----------------------------------------------------------
    def source(self, path: Path, system: str, row=None, sheet=None) -> dict:
        key = rel(path)
        if key not in self.files:
            self.files[key] = {"file": key, "system": system, "sha256": sha256(path),
                               "rows_in": 0, "rows_loaded": 0}
        src = {"system": system, "file": key, "run_id": self.run_id}
        if sheet:
            src["sheet"] = sheet
        if row is not None:
            src["row"] = row
        return src

    def count(self, path: Path, rows_in=0, rows_loaded=0):
        f = self.files[rel(path)]
        f["rows_in"] += rows_in
        f["rows_loaded"] += rows_loaded

    # -- issues ------------------------------------------------------------
    def issue(self, source: dict, rule: str, severity: str, message: str, *,
              auto_fixed=False, record=None, entity=None, fix=None):
        doc = {
            "run_id": self.run_id,
            "source": {k: v for k, v in source.items() if k != "run_id"},
            "rule": rule,
            "severity": severity,
            "message": message,
            "status": "auto_fixed" if auto_fixed else "open",
            "detected_at": self.started,
        }
        if record is not None:
            doc["record"] = record
        if entity:
            doc["entity"] = entity
        if fix:
            doc["fix"] = fix
        self.issues.append(doc)

    # -- resolution --------------------------------------------------------
    def resolve_material(self, name) -> str | None:
        key = norm(name)
        if key in self.material_alias:
            return self.material_alias[key]
        matnr = re.sub(r"\D", "", str(name))
        if matnr and matnr.zfill(18) in self.matnr_to_material:
            return self.matnr_to_material[matnr.zfill(18)]
        return None

    def resolve_vendor(self, name) -> str | None:
        """Match a free-text supplier name to the SAP vendor master by prefix of the normalised name."""
        return self._resolve_party(name, "vendor")

    def _resolve_party(self, name, kind) -> str | None:
        key = norm(name)
        hits = [v["_id"] for v in self.docs["parties"].values()
                if v["type"] == kind and (norm(v["name"]).startswith(key) or key.startswith(norm(v["name"])))]
        return hits[0] if len(hits) == 1 else None

    def resolve_customer(self, name) -> str | None:
        return self.customer_alias.get(norm(name))

    def resolve_party(self, name) -> str | None:
        return self.resolve_customer(name) or self._resolve_party(name, "vendor")

    def fx_rate(self, pair: str, d: date) -> float:
        """Reference rate for the date, or the latest earlier business day."""
        dates, values = self.fx[pair]
        i = bisect.bisect_right(dates, d) - 1
        return values[max(i, 0)]

    def to_eur(self, amount: float, currency: str, d: date) -> float:
        if currency == "EUR":
            return amount
        if currency == "USD":
            return amount / self.fx_rate("EURUSD", d)
        if currency == "PLN":
            return amount / self.fx_rate("EURPLN", d)
        raise ValueError(f"unsupported currency {currency}")


# -- parsers ----------------------------------------------------------------

DATE_FORMATS = [
    ("%Y-%m-%d", "iso"), ("%Y%m%d", "sap"), ("%d.%m.%Y", "de"),
    ("%m/%d/%Y", "us"), ("%d/%m/%Y", "uk"),
]


def parse_date(value, prefer: str | None = None) -> tuple[date | None, str | None, bool]:
    """Parse a date in any known format.

    Returns (date, format name, ambiguous). A slash date like 1/7/2026 is ambiguous between
    US (Jan 7) and UK (1 Jul); `prefer` ("us" or "uk") picks one and the caller should log it.
    """
    if value is None or value == "":
        return None, None, False
    if isinstance(value, datetime):
        return value.date(), "excel", False
    if isinstance(value, date):
        return value, "excel", False
    s = str(value).strip()
    candidates = []
    for fmt, name in DATE_FORMATS:
        try:
            candidates.append((datetime.strptime(s, fmt).date(), name))
        except ValueError:
            pass
    if not candidates:
        return None, None, False
    distinct = {c[0] for c in candidates}
    if len(distinct) > 1:
        chosen = next((c for c in candidates if c[1] == prefer), candidates[0])
        return chosen[0], chosen[1], True
    return candidates[0][0], candidates[0][1], False


def parse_number(value) -> float | None:
    """Parse numbers written with either decimal separator ("3,114", "1.234,5", "3180")."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def qty_to_kg(qty: float, uom: str) -> float | None:
    return {"KG": qty, "G": qty / 1000, "LB": qty * LB_TO_KG, "TO": qty * 1000, "T": qty * 1000}.get(uom.upper())


def price_to_per_kg(price: float, unit: str) -> tuple[float, str] | None:
    """'USD/t' -> (price per kg, 'USD')."""
    m = re.fullmatch(r"\s*([A-Z]{3})\s*/\s*(kg|t)\s*", unit or "", re.I)
    if not m:
        return None
    currency, per = m.group(1).upper(), m.group(2).lower()
    return (price / 1000 if per == "t" else price), currency


def to_dt(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
