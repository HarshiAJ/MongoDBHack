"""Re-forecast FY2027 at current contract terms and compare it with the approved budget.

A latest estimate (LE) is stored like the budget: one `plans` document plus `plan_lines`,
so the variance is a plain comparison of two plan versions. The market outlook is spot-flat
(latest monthly index averages held constant), and accepted contract changes flow in
automatically because the engine reads them from the contracts.
"""

from collections import defaultdict
from datetime import datetime, timezone

from rmi.db import get_db
from rmi.engine.margins import MarginModel
from rmi.engine.prices import month_start

BUDGET = "FY2027-BUDGET-v1"


def reforecast(db=None, label: str | None = None, reason: str = "", changes: list | None = None,
               save: bool = True) -> dict:
    db = db if db is not None else get_db()
    mm = MarginModel(db)
    budget = db.plans.find_one({"_id": BUDGET})
    budget_lines = list(db.plan_lines.find({"plan_id": BUDGET}))
    now = datetime.now(timezone.utc)
    plan_id = label or f"FY2027-LE-{now:%Y%m%dT%H%M%S}"

    forecast = {(f["customer_id"], f["product_id"], f["month"]): f["units"] for f in db.forecasts.find()}
    lines = []
    for b in budget_lines:
        month = month_start(b["month"])
        units = forecast.get((b["customer_id"], b["product_id"], b["month"]), b["units"])
        e = mm.evaluate(b["customer_id"], b["product_id"], month)
        revenue = units * e["revenue_pc"]
        mat = units * e["material_cost_pc"]
        conv = units * e["conversion_cost_pc"]
        lines.append({
            "plan_id": plan_id, "month": b["month"], "customer_id": b["customer_id"], "product_id": b["product_id"],
            "units": round(units, 2), "piece_price": e["piece_price"], "surcharge_pc": e["surcharge"],
            "revenue": round(revenue, 2), "material_cost": round(mat, 2), "conversion_cost": round(conv, 2),
            "contribution": round(revenue - mat - conv, 2),
            "margin_pct": round((revenue - mat - conv) / revenue * 100, 2) if revenue else None,
        })

    variance = compare(budget_lines, lines)
    plan = {
        "_id": plan_id, "kind": "latest_estimate", "fiscal_year": "FY2027", "base_plan": BUDGET,
        "created_at": now, "status": "draft", "reason": reason, "changes": changes or [],
        "assumptions": {
            "market": "spot-flat: latest monthly index averages held constant",
            "index_levels": {i: round(mm.market.avg(i, mm.market.last[i]), 4) for i in mm.market.last},
            "volumes": "latest sales forecast", "prices": "contract formulas incl. accepted changes",
        },
        "totals": variance["totals"],
        "variance_vs_budget": variance,
        "period": budget["period"],
    }
    if save:
        db.plans.insert_one(plan)
        db.plan_lines.insert_many(lines)
    return plan


def compare(budget_lines, le_lines) -> dict:
    """Contribution bridge budget -> LE: volume, price & surcharge, material, conversion."""
    b_by = {(l["month"], l["customer_id"], l["product_id"]): l for l in budget_lines}
    bridge = defaultdict(float)
    by_customer = defaultdict(lambda: defaultdict(float))
    totals = {"budget": defaultdict(float), "le": defaultdict(float)}
    for le in le_lines:
        b = b_by[(le["month"], le["customer_id"], le["product_id"])]
        for k in ("revenue", "material_cost", "conversion_cost", "contribution", "units"):
            totals["budget"][k] += b[k]
            totals["le"][k] += le[k]
        bu, lu = b["units"], le["units"]
        b_contrib_pc = b["contribution"] / bu if bu else 0
        volume = (lu - bu) * b_contrib_pc
        price = lu * (le["revenue"] / lu - b["revenue"] / bu) if lu and bu else 0
        material = -lu * (le["material_cost"] / lu - b["material_cost"] / bu) if lu and bu else 0
        conversion = -lu * (le["conversion_cost"] / lu - b["conversion_cost"] / bu) if lu and bu else 0
        for k, v in (("volume", volume), ("price_and_surcharge", price), ("material", material),
                     ("conversion", conversion)):
            bridge[k] += v
            by_customer[le["customer_id"]][k] += v
        by_customer[le["customer_id"]]["budget_contribution"] += b["contribution"]
        by_customer[le["customer_id"]]["le_contribution"] += le["contribution"]
    for side in totals.values():
        side["margin_pct"] = side["contribution"] / side["revenue"] * 100 if side["revenue"] else None
    r = lambda d: {k: round(v, 2) for k, v in d.items()}
    return {
        "totals": {"budget": r(totals["budget"]), "le": r(totals["le"]),
                   "contribution_delta": round(totals["le"]["contribution"] - totals["budget"]["contribution"], 2)},
        "bridge": r(bridge),
        "by_customer": {c: r(v) for c, v in by_customer.items()},
    }


if __name__ == "__main__":
    p = reforecast(save=False, reason="preview")
    t = p["totals"]
    print(f"Budget contribution {t['budget']['contribution']:>14,.0f}  margin {t['budget']['margin_pct']:.1f}%")
    print(f"LE contribution     {t['le']['contribution']:>14,.0f}  margin {t['le']['margin_pct']:.1f}%")
    print(f"Delta               {t['contribution_delta']:>14,.0f}")
    print("Bridge:", p["variance_vs_budget"]["bridge"])
    for c, v in p["variance_vs_budget"]["by_customer"].items():
        print(f"  {c:<13} budget {v['budget_contribution']:>12,.0f}  LE {v['le_contribution']:>12,.0f}  "
              f"volume {v['volume']:>10,.0f}  price {v['price_and_surcharge']:>10,.0f}  material {v['material']:>10,.0f}")
