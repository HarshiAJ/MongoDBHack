"""Approved FY2027 budget -> plans (one document per plan version) and plan_lines.

The agent later writes latest-estimate versions into the same two collections, so budget
and forecasts are compared with the same queries (document versioning pattern).
"""

from datetime import datetime

from openpyxl import load_workbook

from rmi.ingest.common import RAW, Context, parse_date, to_dt

PATH = RAW / "finance" / "budget_FY2027.xlsx"


def load(ctx: Context):
    wb = load_workbook(PATH, data_only=True)
    ctx.source(PATH, "finance")  # register the file for lineage before counting rows

    approval = list(wb["Approval"].iter_rows(min_row=2, values_only=True))[0]
    version, approved_on, approved_by, comment = approval
    plan_id = "FY2027-BUDGET-v1"

    assumptions, fx = {}, None
    for i, (item, value, unit, basis) in enumerate(wb["Assumptions"].iter_rows(min_row=2, values_only=True), start=2):
        ctx.count(PATH, 1, 1)
        if item == "EURUSD":
            fx = value
        elif unit != "%":
            assumptions[item] = {"value": value, "unit": unit, "basis": basis}

    prices = {}
    for i, (mat, price, basis) in enumerate(wb["Material prices"].iter_rows(min_row=2, values_only=True), start=2):
        src = ctx.source(PATH, "finance", row=i, sheet="Material prices")
        ctx.count(PATH, 1)
        if mat not in ctx.docs["materials"]:
            ctx.issue(src, "unmapped_identifier", "error", f"Budget price for unknown material {mat}")
            continue
        prices[mat] = price
        ctx.count(PATH, 0, 1)

    d, _, _ = parse_date(approved_on)
    ctx.docs["plans"].append({
        "_id": plan_id, "kind": "budget", "fiscal_year": "FY2027", "version": version,
        "period": {"from": datetime(2026, 10, 1), "to": datetime(2027, 9, 1)},
        "status": "approved", "approved_on": to_dt(d), "approved_by": approved_by, "comment": comment,
        "assumptions": {"indices": assumptions, "EURUSD": fx, "customer_price_down_pct": 2},
        "material_prices_eur_kg": prices,
        "source": ctx.source(PATH, "finance", sheet="Approval"),
    })

    ws = wb["Plan"]
    for i, (month, customer, part, units, price, revenue, mat, conv, contrib, margin) in enumerate(
            ws.iter_rows(min_row=2, values_only=True), start=2):
        src = ctx.source(PATH, "finance", row=i, sheet=ws.title)
        ctx.count(PATH, 1)
        cid = ctx.resolve_customer(customer)
        if not cid or part not in ctx.docs["products"]:
            ctx.issue(src, "unmapped_identifier", "error", f"Plan line for unknown customer/part {customer}/{part}")
            continue
        if abs(revenue - mat - conv - contrib) > 0.05:
            ctx.issue(src, "inconsistent_total", "warning", f"Plan {month} {cid}/{part}: contribution does not add up")
        ctx.docs["plan_lines"].append({
            "plan_id": plan_id, "month": datetime.strptime(month, "%Y-%m"), "customer_id": cid, "product_id": part,
            "units": units, "piece_price": price, "revenue": revenue, "material_cost": mat,
            "conversion_cost": conv, "contribution": contrib, "margin_pct": margin, "source": src,
        })
        ctx.count(PATH, 0, 1)
