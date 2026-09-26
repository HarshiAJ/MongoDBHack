"""Contract price formulas: what each supplier contract says a material should cost in a month.

All calculations are deterministic and return their inputs (window, index average, trigger
test, cap), so every number the agent shows can be traced back to data and clause.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from rmi.db import get_db
from rmi.engine import AS_OF


def month_start(d) -> date:
    d = d.date() if isinstance(d, datetime) else d
    return date(d.year, d.month, 1)


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def period_start(month: date, review: str) -> date:
    """First month of the adjustment period that contains `month`."""
    if review == "quarterly":
        return date(month.year, 3 * ((month.month - 1) // 3) + 1, 1)
    if review == "semi-annual":
        return date(month.year, 1 if month.month < 7 else 7, 1)
    if review == "annual":
        return date(month.year, 1, 1)
    return month


# Per-process caches for contract and PO lookups; cleared whenever a Market is loaded,
# i.e. at the start of every calculation run, so accepted changes are always picked up.
_CACHE: dict = {}


def clear_cache():
    _CACHE.clear()


@dataclass
class Market:
    """Monthly index averages from index_monthly, held in memory."""
    avgs: dict = field(default_factory=dict)  # (index, date) -> avg
    last: dict = field(default_factory=dict)  # index -> latest month with data

    @classmethod
    def load(cls, db=None):
        db = db if db is not None else get_db()
        clear_cache()
        m = cls()
        for doc in db.index_monthly.find():
            key = month_start(doc["month"])
            m.avgs[(doc["index"], key)] = doc["avg"]
            m.last[doc["index"]] = max(m.last.get(doc["index"], key), key)
        return m

    def avg(self, index: str, month: date, flat_after: bool = True) -> float | None:
        """Monthly average; for months beyond the data, hold the latest value flat (spot-flat outlook)."""
        if (index, month) in self.avgs:
            return self.avgs[(index, month)]
        if flat_after and index in self.last and month > self.last[index]:
            return self.avgs[(index, self.last[index])]
        return None

    def window_avg(self, index: str, end_exclusive: date, months: int) -> tuple[float | None, list[str]]:
        window = [add_months(end_exclusive, -k) for k in range(months, 0, -1)]
        vals = [self.avg(index, w) for w in window]
        if any(v is None for v in vals):
            return None, [w.strftime("%Y-%m") for w in window]
        return sum(vals) / len(vals), [w.strftime("%Y-%m") for w in window]

    def fx_eur(self, currency: str, month: date) -> float:
        """Units of `currency` per EUR, using the latest monthly average up to `month`."""
        if currency == "EUR":
            return 1.0
        pair = {"USD": "EURUSD", "PLN": "EURPLN"}[currency]
        for k in range(0, 24):
            v = self.avg(pair, add_months(month, -k), flat_after=False)
            if v:
                return v
        raise ValueError(f"no FX rate for {pair}")


def terms_at(contract: dict, month: date) -> tuple[dict | None, dict]:
    """The price-adjustment terms in force for a month, and the timeline entry they come from."""
    entries = [t for t in contract["terms_timeline"] if month_start(t["effective_from"]) <= month]
    entry = entries[-1] if entries else contract["terms_timeline"][0]
    return entry["terms"], entry


def base_price(contract: dict, material_id: str) -> tuple[float, str]:
    """Base price per kg in the price currency, including grade extras written in the price notes."""
    s = contract["summary"]
    unit = s["base_price_unit"].split(";")[0]
    currency = unit.strip()[:3].upper()
    per_kg = s["base_price"] / (1000 if re.search(r"/\s*t\b", unit) else 1)
    notes = s.get("price_notes") or ""
    grade = material_id.split("-")[-1]  # e.g. CRC
    m = re.search(rf"{grade}\s*=\s*\w+\s*\+\s*([\d.]+)\s*{currency}\s*/\s*t", notes)
    if m:
        per_kg += float(m.group(1)) / 1000
    return per_kg, currency


def vendor_price(contract: dict, material_id: str, month: date, market: Market) -> dict:
    """Price per kg the contract formula gives for deliveries in `month`."""
    month = month_start(month)
    base, currency = base_price(contract, material_id)
    terms, entry = terms_at(contract, month)
    out = {
        "contract_id": contract["_id"], "vendor_id": contract["party_id"], "material_id": material_id,
        "month": month.isoformat(), "currency": currency, "base_price": round(base, 6),
        "terms_effective_from": entry["effective_from"].date().isoformat(), "indexed": bool(terms),
    }
    price = base
    agreed = [a for a in contract.get("agreed_prices", [])
              if a["material_id"] == material_id and month_start(a["effective_from"]) <= month]
    if agreed:
        # a negotiated price overrides the formula from its effective date
        a = max(agreed, key=lambda x: x["effective_from"])
        fx = market.fx_eur(a["currency"], month)
        out.update({"price": a["price"], "price_eur": round(a["price"] / fx, 6), "fx": fx,
                    "agreed": {k: a[k] for k in ("change_id", "reason") if k in a}})
        return out
    if terms:
        start = period_start(month, terms["review_frequency"])
        avg, window = market.window_avg(terms["index_code"], start, terms["averaging_months"])
        ref = terms["reference_value"]
        out.update({"index": terms["index_code"], "window": window, "reference": ref,
                    "trigger_pct": terms["trigger_pct"], "share": terms["pass_through_share"],
                    "cap_pct": terms["annual_cap_pct"], "review": terms["review_frequency"]})
        if avg is None:
            out.update({"index_avg": None, "triggered": None, "note": "index data missing for window"})
        else:
            change = (avg - ref) / ref
            triggered = abs(change) * 100 > terms["trigger_pct"]
            adj = change * terms["pass_through_share"] if triggered else 0.0
            capped = False
            if terms["annual_cap_pct"] is not None and abs(adj) * 100 > terms["annual_cap_pct"]:
                adj = terms["annual_cap_pct"] / 100 * (1 if adj > 0 else -1)
                capped = True
            price = base * (1 + adj)
            out.update({"index_avg": round(avg, 4), "change_pct": round(change * 100, 2),
                        "triggered": triggered, "adjustment_pct": round(adj * 100, 2), "capped": capped})
    fx = market.fx_eur(currency, month)
    out.update({"price": round(price, 6), "price_eur": round(price / fx, 6), "fx": fx})
    return out


def vendor_contracts(db=None, material_id: str | None = None, on: date | None = None) -> list[dict]:
    """Vendor contracts, optionally for one material and active on a date (all loaded once per run)."""
    if "vendor_contracts" not in _CACHE:
        db = db if db is not None else get_db()
        _CACHE["vendor_contracts"] = list(db.contracts.find({"type": "vendor"}))
    contracts = _CACHE["vendor_contracts"]
    if material_id:
        contracts = [c for c in contracts if material_id in c["material_ids"]]
    if on:
        on_dt = datetime.combine(on, datetime.min.time())
        contracts = [c for c in contracts if c["expires"] >= on_dt]
    return contracts


def sourcing_split(material_id: str, as_of: date, db=None, months: int = 12) -> dict:
    """Volume share per vendor over the last `months` of clean PO lines."""
    key = ("split", material_id, month_start(as_of), months)
    if key not in _CACHE:
        _CACHE[key] = _sourcing_split(material_id, as_of, db, months)
    return _CACHE[key]


def _sourcing_split(material_id, as_of, db, months):
    db = db if db is not None else get_db()
    since = datetime.combine(add_months(month_start(as_of), -months), datetime.min.time())
    rows = db.po_lines.aggregate([
        {"$match": {"material_id": material_id, "quality.status": "ok", "order_date": {"$gte": since}}},
        {"$group": {"_id": "$vendor_id", "kg": {"$sum": "$qty_kg"}}},
    ])
    kg = {r["_id"]: r["kg"] for r in rows}
    total = sum(kg.values())
    return {v: q / total for v, q in kg.items()} if total else {}


def material_price(material_id: str, month: date, market: Market, db=None, as_of: date | None = None) -> dict:
    """Blended EUR/kg across active supplier contracts, weighted by recent PO volumes."""
    db = db if db is not None else get_db()
    month = month_start(month)
    contracts = vendor_contracts(db, material_id, on=month) or vendor_contracts(db, material_id)
    split = sourcing_split(material_id, as_of or AS_OF, db)
    by_vendor = {c["party_id"]: c for c in contracts}
    weights = {v: w for v, w in split.items() if v in by_vendor} or {c["party_id"]: 1.0 for c in contracts}
    total_w = sum(weights.values())
    parts = []
    for vid, w in weights.items():
        p = vendor_price(by_vendor[vid], material_id, month, market)
        parts.append({**p, "weight": w / total_w})
    return {"material_id": material_id, "month": month.isoformat(),
            "price_eur": round(sum(p["price_eur"] * p["weight"] for p in parts), 6), "sources": parts}


def yield_factor(material: dict) -> float:
    vals = [y["yield_pct"] for y in material.get("yield", [])]
    return (sum(vals) / len(vals) / 100) if vals else 1.0


def material_cost_per_piece(product: dict, month: date, market: Market, db=None, materials=None,
                            price_override: dict | None = None) -> dict:
    """Material cost of one finished part: exploded BOM kg / yield x blended contract price."""
    db = db if db is not None else get_db()
    materials = materials or {m["_id"]: m for m in db.materials.find()}
    lines, by_index = [], defaultdict(float)
    for m in product["materials"]:
        mat = materials[m["material_id"]]
        price = (price_override or {}).get(m["material_id"])
        if price is None:
            price = material_price(m["material_id"], month, market, db)["price_eur"]
        gross_kg = m["kg_per_unit"] / yield_factor(mat)
        cost = gross_kg * price
        by_index[mat["index"]] += cost
        lines.append({"material_id": m["material_id"], "index": mat["index"], "net_kg": m["kg_per_unit"],
                      "gross_kg": round(gross_kg, 6), "price_eur_kg": round(price, 6), "cost_eur": round(cost, 6)})
    return {"product_id": product["_id"], "month": month_start(month).isoformat(),
            "total_eur": round(sum(l["cost_eur"] for l in lines), 6),
            "by_index": {k: round(v, 6) for k, v in by_index.items()}, "lines": lines}


def quotation_prices(db=None) -> dict:
    """EUR/kg at the contracts' base prices (the quotation basis customers' prices were built on)."""
    db = db if db is not None else get_db()
    market = Market.load(db)
    out = {}
    for mat in db.materials.find():
        contracts = vendor_contracts(db, mat["_id"])
        split = sourcing_split(mat["_id"], AS_OF, db)
        weights = {c["party_id"]: split.get(c["party_id"], 0) for c in contracts}
        if not sum(weights.values()):
            weights = {c["party_id"]: 1 for c in contracts}
        total = sum(weights.values())
        price = 0.0
        for c in contracts:
            base, currency = base_price(c, mat["_id"])
            price += weights[c["party_id"]] / total * base / market.fx_eur(currency, date(2025, 1, 1))
        out[mat["_id"]] = price
    return out
