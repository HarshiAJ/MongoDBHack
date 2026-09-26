"""Commercial inputs: product standard costs, supplier RFQ responses and the negotiation log."""

from openpyxl import load_workbook

from rmi.ingest.common import RAW, Context, parse_date, price_to_per_kg, to_dt

COSTS = RAW / "excel_inputs" / "product_cost_structure_2026.xlsx"
RFQ = RAW / "purchasing" / "rfq_responses_2026-09.xlsx"
LOG = RAW / "purchasing" / "negotiation_log.xlsx"


def load(ctx: Context):
    load_costs(ctx)
    load_rfqs(ctx)
    load_negotiations(ctx)


def load_costs(ctx: Context):
    # data_only=True: the workbook was never recalculated, so the SUM formula has no cached value
    ws = load_workbook(COSTS, data_only=True).active
    for i, (part, labour, machine, overhead, logistics, total, target, owner, valid_from) in enumerate(
            ws.iter_rows(min_row=2, values_only=True), start=2):
        src = ctx.source(COSTS, "excel_inputs", row=i, sheet=ws.title)
        ctx.count(COSTS, 1)
        if part not in ctx.docs["products"]:
            ctx.issue(src, "unmapped_identifier", "error", f"Cost structure for unknown part {part}")
            continue
        parts = {"labour": labour, "machine": machine, "overhead": overhead, "logistics": logistics}
        computed = round(sum(parts.values()), 4)
        if total is not None and abs(float(total) - computed) > 0.01:
            ctx.issue(src, "inconsistent_total", "warning",
                      f"{part}: conversion total {total} differs from the sum of components {computed}")
        d, _, _ = parse_date(valid_from)
        ctx.docs["products"][part]["cost"] = {
            "conversion": parts, "conversion_total": computed, "target_margin_pct": target,
            "owner": owner, "valid_from": to_dt(d), "source": src,
        }
        ctx.count(COSTS, 0, 1)
    missing = [p for p, doc in ctx.docs["products"].items() if "cost" not in doc]
    if missing:
        ctx.issue(ctx.source(COSTS, "excel_inputs"), "missing_required_field", "error",
                  f"No standard cost for {missing}; margins cannot be computed for these parts")


def load_rfqs(ctx: Context):
    ws = load_workbook(RFQ, data_only=True).active
    for i, (rfq, material, supplier, offer, unit, basis, volume, qualification, valid_until, buyer, comment) in \
            enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        src = ctx.source(RFQ, "purchasing", row=i, sheet=ws.title)
        ctx.count(RFQ, 1)
        mid = ctx.resolve_material(material)
        if not mid:
            ctx.issue(src, "unmapped_identifier", "error", f"RFQ {rfq}: unknown material '{material}'")
            continue
        per_kg, currency = price_to_per_kg(float(offer), unit)
        until, _, _ = parse_date(valid_until)
        today = ctx.started.date()
        ctx.docs["price_quotes"].append({
            "kind": "rfq", "rfq_id": rfq, "material_id": mid,
            "vendor_id": ctx.resolve_vendor(supplier),  # None for suppliers not yet in SAP
            "supplier_name": supplier,
            "price": {"per_kg": per_kg, "currency": currency,
                      "per_kg_eur": round(ctx.to_eur(per_kg, currency, today), 6),
                      "as_entered": {"value": offer, "unit": unit}},
            "pricing_basis": basis, "annual_volume_t": volume,
            "qualification": qualification,
            "qualified": bool(qualification) and ("approved" in qualification.lower()),
            "valid_until": to_dt(until), "buyer": buyer, "notes": comment,
            "status": "offer", "source": src,
        })
        ctx.count(RFQ, 0, 1)


def load_negotiations(ctx: Context):
    """Past negotiations seed the agent's long-term memory of how each counterparty behaves."""
    ws = load_workbook(LOG, data_only=True).active
    for i, (d, party, contract, topic, ask, outcome, lessons, owner) in enumerate(
            ws.iter_rows(min_row=2, values_only=True), start=2):
        src = ctx.source(LOG, "purchasing", row=i, sheet=ws.title)
        ctx.count(LOG, 1)
        pid = ctx.resolve_party(party)
        if not pid:
            ctx.issue(src, "unmapped_identifier", "error", f"Negotiation with unknown party '{party}'")
            continue
        when, _, _ = parse_date(d)
        ctx.docs["negotiations"].append({
            "date": to_dt(when), "party_id": pid, "party_type": ctx.docs["parties"][pid]["type"],
            "contract_id": contract, "topic": topic, "ask": ask, "outcome": outcome, "lessons": lessons,
            "owner": owner, "origin": "history", "source": src,
        })
        ctx.count(LOG, 0, 1)
