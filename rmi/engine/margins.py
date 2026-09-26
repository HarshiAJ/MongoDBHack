"""Margin watch: contract selling price + surcharge recovery vs. material and conversion cost.

Per customer, part and month:
  revenue/pc   = contract piece price (with the annual price-down) + surcharge recovery
  cost/pc      = material cost at supplier contract prices + standard conversion cost
  exposure/pc  = material cost increase vs. the quotation basis that the customer contract does not recover
"""

import re
from collections import defaultdict
from datetime import date, datetime

from rmi.db import get_db
from rmi.engine.prices import (Market, add_months, material_cost_per_piece, month_start, quotation_prices,
                               yield_factor)

SEMI_ANNUAL_STARTS = (4, 10)  # adjustment dates written in the Sakura contract: 1 April / 1 October


def surcharge_period(month: date, timing: str) -> tuple[date, int, str]:
    """(period start, lag in months, frequency) interpreted from the contract's timing text."""
    t = timing.lower()
    if "semi-annual" in t or "semi annual" in t:
        starts = [date(month.year - 1, 10, 1), date(month.year, 4, 1), date(month.year, 10, 1)]
        return max(s for s in starts if s <= month), 0, "semi-annual"
    start = date(month.year, 3 * ((month.month - 1) // 3) + 1, 1)
    lag = 3 if re.search(r"lag of one quarter|one quarter", t) else 0
    return start, lag, "quarterly"


def piece_price(contract: dict, product_id: str, month: date, waived_years=()) -> float | None:
    pp = next((p for p in contract.get("piece_prices", []) if p["product_id"] == product_id), None)
    if not pp or pp["price_2026_eur"] is None:
        return None
    agreed = [a for a in contract.get("agreed_piece_prices", [])
              if a["product_id"] == product_id and month_start(a["effective_from"]) <= month]
    if agreed:
        return max(agreed, key=lambda a: a["effective_from"])["price"]
    price = pp["price_2026_eur"]
    down = (contract["summary"].get("annual_price_down_pct") or 0) / 100
    waived = set(waived_years) | {w["year"] for w in contract.get("price_down_waivers", [])}
    for year in range(2027, month.year + 1):
        if year not in waived:
            price *= 1 - down
    return round(price, 4)


def recovery(contract: dict, product: dict, month: date, market: Market, quote_cost_by_index: dict) -> dict:
    """Surcharge per piece the customer contract grants for the month, per index."""
    out, total = [], 0.0
    for s in contract["current_terms"]["surcharges"]:
        start, lag, freq = surcharge_period(month, s["timing"])
        window_end = add_months(start, -lag)
        avg, window = market.window_avg(s["index_code"], window_end, s["averaging_months"])
        ref = s["reference_value"]
        basis = quote_cost_by_index.get(s["index_code"], 0.0)
        item = {"index": s["index_code"], "period_start": start.isoformat(), "frequency": freq,
                "window": window, "reference": ref, "trigger_pct": s["trigger_pct"],
                "share": s["pass_through_share"], "material_cost_basis": round(basis, 4)}
        if avg is None or not basis:
            item.update({"index_avg": avg, "triggered": False, "surcharge": 0.0})
        else:
            change = (avg - ref) / ref
            triggered = abs(change) * 100 > s["trigger_pct"]
            adj = change if triggered else 0.0
            capped = False
            if s.get("cap_pct") is not None and abs(adj) * 100 > s["cap_pct"]:
                adj, capped = s["cap_pct"] / 100 * (1 if adj > 0 else -1), True
            amount = s["pass_through_share"] * adj * basis
            total += amount
            item.update({"index_avg": round(avg, 2), "change_pct": round(change * 100, 2), "triggered": triggered,
                         "capped": capped, "surcharge": round(amount, 4)})
        out.append(item)
    return {"total": round(total, 4), "by_index": out}


class MarginModel:
    """Loads everything once; evaluate() is then cheap per customer/part/month."""

    def __init__(self, db=None):
        self.db = db if db is not None else get_db()
        self.market = Market.load(self.db)
        self.materials = {m["_id"]: m for m in self.db.materials.find()}
        self.products = {p["_id"]: p for p in self.db.products.find()}
        self.contracts = {c["party_id"]: c for c in self.db.contracts.find({"type": "customer"})}
        self.quote_prices = quotation_prices(self.db)
        self._cost_cache = {}

    def quote_cost_by_index(self, product: dict) -> dict:
        out = defaultdict(float)
        for m in product["materials"]:
            mat = self.materials[m["material_id"]]
            out[mat["index"]] += m["kg_per_unit"] / yield_factor(mat) * self.quote_prices[m["material_id"]]
        return dict(out)

    def material_cost(self, product_id: str, month: date, price_override=None) -> dict:
        key = (product_id, month, tuple(sorted((price_override or {}).items())))
        if key not in self._cost_cache:
            self._cost_cache[key] = material_cost_per_piece(
                self.products[product_id], month, self.market, self.db, self.materials, price_override)
        return self._cost_cache[key]

    def evaluate(self, customer_id: str, product_id: str, month, *, waived_years=(), price_override=None) -> dict:
        month = month_start(month)
        contract = self.contracts[customer_id]
        product = self.products[product_id]
        quote_idx = self.quote_cost_by_index(product)
        price = piece_price(contract, product_id, month, waived_years)
        rec = recovery(contract, product, month, self.market, quote_idx)
        mat = self.material_cost(product_id, month, price_override)
        conv = product["cost"]["conversion_total"]
        revenue = price + rec["total"]
        contribution = revenue - mat["total_eur"] - conv

        covered = {s["index_code"] for s in contract["current_terms"]["surcharges"]}
        exposure = []
        for index, cost_now in mat["by_index"].items():
            increase = cost_now - quote_idx.get(index, 0.0)
            recovered = next((r["surcharge"] for r in rec["by_index"] if r["index"] == index), 0.0)
            exposure.append({"index": index, "covered_by_surcharge": index in covered,
                             "cost_quote": round(quote_idx.get(index, 0.0), 4), "cost_now": round(cost_now, 4),
                             "increase": round(increase, 4), "recovered": round(recovered, 4),
                             "unrecovered": round(increase - recovered, 4),
                             "increase_pct": round(increase / quote_idx[index] * 100, 2) if quote_idx.get(index) else None})
        return {
            "customer_id": customer_id, "product_id": product_id, "month": month.isoformat(),
            "contract_id": contract["_id"], "piece_price": price, "surcharge": rec["total"],
            "revenue_pc": round(revenue, 4), "material_cost_pc": round(mat["total_eur"], 4),
            "conversion_cost_pc": conv, "contribution_pc": round(contribution, 4),
            "margin_pct": round(contribution / revenue * 100, 2),
            "target_margin_pct": product["cost"]["target_margin_pct"],
            "recovery": rec["by_index"], "exposure": exposure, "material_lines": mat["lines"],
        }

    def plan_margin(self, customer_id, product_id, month, plan_id="FY2027-BUDGET-v1"):
        line = self.db.plan_lines.find_one({"plan_id": plan_id, "customer_id": customer_id, "product_id": product_id,
                                            "month": datetime.combine(month_start(month), datetime.min.time())})
        return line

    def watch(self, month, customer_id=None) -> list[dict]:
        """Margin of every customer/part for a month, with plan comparison and volume at risk."""
        month = month_start(month)
        month_dt = datetime.combine(month, datetime.min.time())
        rows = []
        for cid, contract in self.contracts.items():
            if customer_id and cid != customer_id:
                continue
            for pp in contract.get("piece_prices", []):
                r = self.evaluate(cid, pp["product_id"], month)
                plan = self.plan_margin(cid, pp["product_id"], month)
                fc = self.db.forecasts.find_one({"customer_id": cid, "product_id": pp["product_id"], "month": month_dt})
                units = fc["units"] if fc else (plan["units"] if plan else 0)
                r.update({
                    "plan_margin_pct": plan["margin_pct"] if plan else None,
                    "margin_gap_vs_plan_pp": round(r["margin_pct"] - plan["margin_pct"], 2) if plan else None,
                    "units": units,
                    "contribution_month": round(r["contribution_pc"] * units, 2),
                    "unrecovered_month": round(sum(e["unrecovered"] for e in r["exposure"] if e["unrecovered"] > 0)
                                               * units, 2),
                    "status": ("below_target" if r["margin_pct"] < r["target_margin_pct"] else "ok"),
                })
                rows.append(r)
        return sorted(rows, key=lambda r: r["margin_pct"])
