"""Findings the purchasing team should act on, each with money attached and a deadline.

Vendor side:   supplier claims vs. formula (overcharge, late notice), decreases owed,
               meet-competition triggered by qualified RFQs, renewals and renewal offers.
Customer side: hardship, annual price review, price-down waiver, extraordinary review,
               billing errors against contract prices.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta

from rmi.db import get_db
from rmi.engine import AS_OF
from rmi.engine.margins import MarginModel, piece_price
from rmi.engine.prices import Market, add_months, month_start, vendor_contracts, vendor_price


def _dt(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time())


def monthly_kg(db, material_id, vendor_id=None, as_of=AS_OF, months=6) -> float:
    q = {"material_id": material_id, "quality.status": "ok",
         "order_date": {"$gte": _dt(add_months(month_start(as_of), -months))}}
    if vendor_id:
        q["vendor_id"] = vendor_id
    total = sum(l["qty_kg"] for l in db.po_lines.find(q, {"qty_kg": 1}))
    return total / months


def last_paid(db, material_id, vendor_id):
    return db.po_lines.find_one({"material_id": material_id, "vendor_id": vendor_id, "quality.status": "ok"},
                                sort=[("order_date", -1)])


def finding(kind, side, party_id, title, *, contract_id=None, value_eur=0.0, deadline=None, detail=None,
            evidence_query=None, lever=None, urgency=None):
    days_left = (deadline - AS_OF).days if deadline else None
    return {
        "kind": kind, "side": side, "party_id": party_id, "contract_id": contract_id, "title": title,
        "value_eur": round(value_eur, 2), "deadline": deadline.isoformat() if deadline else None,
        "days_left": days_left, "lever": lever, "detail": detail or {},
        "evidence_query": evidence_query,
        "urgency": urgency or ("high" if days_left is not None and days_left <= 30 else
                               "medium" if days_left is not None and days_left <= 60 else "normal"),
    }


# -- vendor side ---------------------------------------------------------------

def supplier_notices(db, market) -> list[dict]:
    out = []
    for n in db.price_quotes.find({"kind": "supplier_notice", "notice_type": "price_adjustment"}):
        contract = db.contracts.find_one({"_id": n["contract_id"]})
        eff = month_start(n["valid_from"])
        expected = vendor_price(contract, n["material_id"], eff, market)
        if expected.get("agreed"):
            continue  # already settled with the supplier
        claimed = n["price"]["per_kg"]
        terms = contract["current_terms"] or {}
        lead = (n["valid_from"].date() - n["received_at"].date()).days if n.get("received_at") else None
        late = lead is not None and terms.get("notice_days") and lead < terms["notice_days"]
        kg = monthly_kg(db, n["material_id"], contract["party_id"])
        over = claimed - expected["price"]
        value = max(over, 0) * kg + (expected["price"] - expected["base_price"]) * kg * (1 if late else 0)
        issues = []
        if over > 0.005:
            issues.append(f"claims {claimed:.2f} but the formula gives {expected['price']:.2f} "
                          f"{expected['currency']}/kg ({over / expected['price']:+.1%})")
        if late:
            issues.append(f"notice received {lead} days before the effective date; the contract requires "
                          f"{terms['notice_days']} days, so the adjustment only applies from the next review period")
        if not issues:
            continue
        out.append(finding(
            "supplier_claim_check", "vendor", contract["party_id"],
            f"{contract['party_name']}: price notice for {n['material_id']} " + " and ".join(issues),
            contract_id=contract["_id"], value_eur=value * 1.0 / market.fx_eur(expected["currency"], eff),
            deadline=n["valid_from"].date(),
            detail={"claimed": claimed, "expected": expected, "notice_lead_days": lead, "late_notice": bool(late),
                    "monthly_kg": round(kg), "document_id": n.get("document_id")},
            evidence_query=f"{contract['_id']} price adjustment notice trigger", lever="price_adjustment_formula"))
    return out


def decreases_owed(db, market, month=None) -> list[dict]:
    month = month or add_months(month_start(AS_OF), 1)
    out = []
    for c in vendor_contracts(db, on=month):
        if not c["current_terms"]:
            continue
        for mid in c["material_ids"]:
            exp = vendor_price(c, mid, month, market)
            paid = last_paid(db, mid, c["party_id"])
            if not paid or exp.get("agreed") or exp.get("triggered") is None:
                continue
            # compare in EUR: invoices may be in another currency than the contract price (Cuprum bills PLN)
            paid_eur = paid["price"]["per_kg_eur"]
            if exp["price_eur"] < paid_eur * 0.995:
                kg = monthly_kg(db, mid, c["party_id"])
                saving = (paid_eur - exp["price_eur"]) * kg
                out.append(finding(
                    "decrease_owed", "vendor", c["party_id"],
                    f"{c['party_name']}: {mid} should drop to {exp['price']:.3f} {exp['currency']}/kg from "
                    f"{month:%b %Y} (index {exp['change_pct']:+.1f}% vs reference, trigger {exp['trigger_pct']}%); "
                    f"last paid {paid['price']['per_kg']:.3f} {paid['price']['currency']}/kg",
                    contract_id=c["_id"], value_eur=saving, deadline=month,
                    detail={"expected": exp, "last_paid": paid["price"], "last_po": paid["_id"], "monthly_kg": round(kg)},
                    evidence_query=f"{c['_id']} raw material price adjustment trigger review",
                    lever="price_adjustment_formula"))
    return out


def meet_competition(db, market) -> list[dict]:
    out = []
    for c in db.contracts.find({"type": "vendor", "levers.type": "meet_competition"}):
        lever = next(l for l in c["levers"] if l["type"] == "meet_competition")
        for mid in c["material_ids"]:
            base = c["summary"]["base_price"]
            for q in db.price_quotes.find({"kind": "rfq", "material_id": mid}):
                same_basis = "index-linked" in (q.get("pricing_basis") or "").lower()
                # same index basis: compare base prices; fixed offers: compare with next month's formula price
                ours = base if same_basis else vendor_price(c, mid, add_months(month_start(AS_OF), 1), market)["price"]
                theirs = q["price"]["per_kg"] if q["price"]["currency"] == c["summary"]["base_price_unit"][:3] \
                    else q["price"]["per_kg_eur"]
                gap = 1 - theirs / ours
                if gap * 100 <= (lever["threshold_pct"] or 0):
                    continue
                kg = monthly_kg(db, mid, c["party_id"]) * 12
                qualified = q.get("qualified", False)
                out.append(finding(
                    "meet_competition", "vendor", c["party_id"],
                    f"{c['party_name']}: {q['supplier_name']} offers {mid} {gap:.1%} below contract "
                    f"({'qualified' if qualified else 'NOT yet qualified: ' + q['qualification']}); "
                    f"clause threshold {lever['threshold_pct']}%",
                    contract_id=c["_id"], value_eur=gap * ours * kg / market.fx_eur("EUR", AS_OF) if qualified else 0,
                    deadline=q["valid_until"].date() if q.get("valid_until") else None,
                    detail={"rfq": q["rfq_id"], "supplier": q["supplier_name"], "offer": q["price"]["as_entered"],
                            "pricing_basis": q["pricing_basis"], "qualified": qualified,
                            "qualification": q["qualification"], "annual_kg": round(kg), "lever": lever},
                    evidence_query=f"{c['_id']} competitiveness competing offer match", lever="meet_competition",
                    urgency=None if qualified else "normal"))
    return out


def renewals(db, market, horizon_days=120) -> list[dict]:
    out = []
    limit = _dt(AS_OF + timedelta(days=horizon_days))
    for c in db.contracts.find({"type": "vendor", "expires": {"$lte": limit}}):
        mid = c["material_ids"][0]
        lever = next((l for l in c["levers"] if l["type"] == "renewal"), None)
        # without an offer, the renewal clause's notice period sets our deadline; with one, its validity does
        deadline = c["expires"].date() - timedelta(days=(lever or {}).get("notice_days") or 0)
        offer = db.price_quotes.find_one({"kind": "supplier_notice", "notice_type": "renewal_offer",
                                          "contract_id": c["_id"]})
        alts = list(db.price_quotes.find({"kind": "rfq", "material_id": mid, "qualified": True}))
        kg_year = monthly_kg(db, mid, c["party_id"]) * 12
        detail = {"expires": c["expires"].date().isoformat(), "lever": lever, "annual_kg": round(kg_year),
                  "alternatives": [{"supplier": a["supplier_name"], "offer": a["price"]["as_entered"],
                                    "eur_kg": round(a["price"]["per_kg_eur"], 4), "valid_until":
                                        a["valid_until"].date().isoformat()} for a in alts]}
        title = f"{c['party_name']}: contract {c['_id']} expires {c['expires']:%Y-%m-%d}"
        value = 0.0
        if offer:
            best = min(alts, key=lambda a: a["price"]["per_kg_eur"]) if alts else None
            offer_eur = offer["price"]["per_kg"] / market.fx_eur(offer["price"]["currency"], month_start(AS_OF))
            detail["renewal_offer"] = {"price": offer["price"], "eur_kg": round(offer_eur, 4),
                                       "valid_until": offer["offer_valid_until"].date().isoformat()}
            title += f"; renewal offer {offer['price']['per_kg']} {offer['price']['currency']}/kg"
            if best and best["price"]["per_kg_eur"] < offer_eur:
                value = (offer_eur - best["price"]["per_kg_eur"]) * kg_year
                title += f" vs qualified {best['supplier_name']} at {best['price']['as_entered']['value']} " \
                         f"{best['price']['as_entered']['unit']}"
            deadline = offer["offer_valid_until"].date()
        out.append(finding("renewal", "vendor", c["party_id"], title, contract_id=c["_id"], value_eur=value,
                           deadline=deadline, detail=detail, evidence_query=f"{c['_id']} renewal expiry term",
                           lever="renewal"))
    return out


# -- customer side -------------------------------------------------------------

def customer_levers(db, mm: MarginModel, month=None) -> list[dict]:
    month = month or add_months(month_start(AS_OF), 1)
    out = []
    for cid, c in mm.contracts.items():
        parts = [pp["product_id"] for pp in c.get("piece_prices", [])]
        evals = {p: mm.evaluate(cid, p, month) for p in parts}
        annual_units = {p: sum(f["units"] for f in db.forecasts.find({"customer_id": cid, "product_id": p}))
                        for p in parts}
        for lever in c["levers"]:
            t = lever["type"]
            if t == "hardship":
                # the clause tests each raw material not covered by the surcharge against the threshold
                eligible = []
                for p, e in evals.items():
                    hit = [x for x in e["exposure"] if not x["covered_by_surcharge"]
                           and (x["increase_pct"] or 0) > (lever["threshold_pct"] or 0)]
                    if hit:
                        inc = sum(x["increase"] for x in hit)
                        eligible.append({"product_id": p, "indices": [x["index"] for x in hit],
                                         "increase_pct": {x["index"]: x["increase_pct"] for x in hit},
                                         "increase_pc": round(inc, 4), "annual_units": round(annual_units[p])})
                if eligible:
                    value = sum(e["increase_pc"] * e["annual_units"] for e in eligible)
                    out.append(finding(
                        "hardship", "customer", cid,
                        f"{c['party_name']}: hardship clause applies to {', '.join(e['product_id'] for e in eligible)}"
                        f" (uncovered material cost up more than {lever['threshold_pct']}%)",
                        contract_id=c["_id"], value_eur=value, deadline=None,
                        detail={"eligible": eligible, "lever": lever, "response_rule": lever["deadline_rule"]},
                        evidence_query=f"{c['_id']} hardship renegotiation raw materials not covered",
                        lever="hardship", urgency="high"))
            elif t == "annual_price_review":
                deadline = date(AS_OF.year + 1, 1, 1) - timedelta(days=lever["notice_days"] or 0)
                if deadline >= AS_OF:
                    gaps = [{"product_id": p, "margin_pct": e["margin_pct"], "target_margin_pct": e["target_margin_pct"],
                             "unrecovered_pc": round(sum(x["unrecovered"] for x in e["exposure"] if x["unrecovered"] > 0), 4),
                             "annual_units": round(annual_units[p])}
                            for p, e in evals.items() if e["margin_pct"] < e["target_margin_pct"]]
                    value = sum(g["unrecovered_pc"] * g["annual_units"] for g in gaps)
                    out.append(finding(
                        "annual_price_review", "customer", cid,
                        f"{c['party_name']}: request the {AS_OF.year + 1} price review; notice must be received by "
                        f"{deadline:%d %b %Y}", contract_id=c["_id"], value_eur=value, deadline=deadline,
                        detail={"parts_below_target": gaps, "lever": lever},
                        evidence_query=f"{c['_id']} annual price review notice 1 January", lever="annual_price_review"))
            elif t == "price_down_waiver":
                eligible = []
                for p, e in evals.items():
                    base = sum(x["cost_quote"] for x in e["exposure"])
                    inc = sum(x["increase"] for x in e["exposure"])
                    if base and inc / base * 100 > (lever["threshold_pct"] or 0):
                        price = piece_price(c, p, date(2026, 12, 1))
                        down = (c["summary"].get("annual_price_down_pct") or 0) / 100
                        eligible.append({"product_id": p, "material_increase_pct": round(inc / base * 100, 2),
                                         "waiver_value_pc": round(price * down, 4),
                                         "annual_units": round(annual_units[p])})
                if eligible:
                    out.append(finding(
                        "price_down_waiver", "customer", cid,
                        f"{c['party_name']}: raw material cost up more than {lever['threshold_pct']}% on "
                        f"{', '.join(e['product_id'] for e in eligible)}; ask to waive the 2027 price-down",
                        contract_id=c["_id"], value_eur=sum(e["waiver_value_pc"] * e["annual_units"] for e in eligible),
                        deadline=date(AS_OF.year + 1, 1, 1) - timedelta(days=60),
                        detail={"eligible": eligible, "lever": lever},
                        evidence_query=f"{c['_id']} price-down waiver raw material cost increase",
                        lever="price_down_waiver"))
            elif t == "extraordinary_review":
                capped = [r for e in evals.values() for r in e["recovery"] if r.get("capped")]
                out.append(finding(
                    "extraordinary_review", "customer", cid,
                    f"{c['party_name']}: extraordinary review not yet available (cap has not limited two "
                    f"consecutive adjustments; {len(capped)} capped so far). Consider cost-reduction sharing instead.",
                    contract_id=c["_id"], detail={"lever": lever, "capped_now": len(capped)},
                    evidence_query=f"{c['_id']} extraordinary review cap cost reduction sharing",
                    lever="extraordinary_review", urgency="normal"))
    return out


def billing_errors(db) -> list[dict]:
    out = []
    for i in db.ingestion_issues.find({"rule": "contract_price_mismatch", "status": "open"}):
        e = i["entity"]
        c = db.contracts.find_one({"_id": e["contract_id"]})
        pp = next(p for p in c["piece_prices"] if p["product_id"] == e["product_id"])
        sap = db.customer_prices.find_one({"customer_id": e["customer_id"], "product_id": e["product_id"]},
                                          sort=[("valid_from", -1)])
        diff = sap["price_eur"] - pp["price_2026_eur"]
        shipped = sum(f["units"] for f in db.forecasts.find({"customer_id": e["customer_id"],
                                                             "product_id": e["product_id"]})) * 9 / 12
        out.append(finding(
            "billing_error", "customer", e["customer_id"],
            f"{c['party_name']}: SAP bills {e['product_id']} at {sap['price_eur']:.2f} EUR instead of the contract "
            f"price {pp['price_2026_eur']:.2f} since January; correct the condition and prepare a credit note",
            contract_id=c["_id"], value_eur=-diff * shipped,
            detail={"sap_price": sap["price_eur"], "contract_price": pp["price_2026_eur"],
                    "estimated_units_since_jan": round(shipped), "issue_id": str(i["_id"])},
            evidence_query=f"{c['_id']} Annex A piece prices", lever="compliance", urgency="high"))
    return out


def scan(db=None) -> list[dict]:
    db = db if db is not None else get_db()
    market = Market.load(db)
    mm = MarginModel(db)
    findings = (supplier_notices(db, market) + decreases_owed(db, market) + meet_competition(db, market)
                + renewals(db, market) + customer_levers(db, mm) + billing_errors(db))
    order = {"high": 0, "medium": 1, "normal": 2}
    return sorted(findings, key=lambda f: (order[f["urgency"]], -abs(f["value_eur"])))


if __name__ == "__main__":
    for f in scan():
        dl = f"{f['deadline']} ({f['days_left']}d)" if f["deadline"] else "-"
        print(f"[{f['urgency']:<6}] {f['side']:<8} {f['kind']:<22} EUR {f['value_eur']:>10,.0f}  {dl:<18} {f['title']}")
