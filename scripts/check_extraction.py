"""Compare LLM-extracted contract data with the generator's ground truth (data/reference/master_data.json)."""

import json
from pathlib import Path

from rmi.db import get_db

ROOT = Path(__file__).resolve().parent.parent
truth = json.loads((ROOT / "data" / "reference" / "master_data.json").read_text())
db = get_db()
AVG = {"prior calendar month": 1, "prior 3 months": 3}
EXPECTED_LEVERS = {
    "VC-2025-014": {"meet_competition"}, "VC-2025-026": {"meet_competition"}, "VC-2024-031": {"volume_rebate"},
    "VC-2025-022": {"renewal"}, "VC-2024-040": {"renewal"},
    "CC-NW-2025-01": {"hardship"}, "CC-BL-2024-07": {"annual_price_review", "price_down_waiver"},
    "CC-SK-2025-03": {"extraordinary_review", "cost_reduction_sharing"},
}
errors, checked = [], 0


def check(cid, field, got, exp):
    global checked
    checked += 1
    if got != exp:
        errors.append(f"{cid} {field}: got {got!r}, expected {exp!r}")


for t in truth["vendor_contracts"]:
    c = db.contracts.find_one({"_id": t["id"]})
    s, p = c["summary"], c["terms_timeline"][0]["terms"]
    check(t["id"], "expires", c["expires"].date().isoformat(), t["expires"])
    check(t["id"], "base_price", s["base_price"], t["base_price"])
    check(t["id"], "materials", sorted(s["material_ids"]), sorted(t["material"].split(",")))
    if t["index"]:
        for f, exp in (("index_code", t["index"]), ("reference_value", t["base_index_value"]),
                       ("trigger_pct", t["trigger_pct"]), ("pass_through_share", t["share"]),
                       ("annual_cap_pct", t["cap_pct"]), ("notice_days", t["notice_days"]),
                       ("averaging_months", AVG[t["averaging"]]), ("review_frequency", t["review"])):
            check(t["id"], f, p[f], exp)
    else:
        check(t["id"], "fixed_price", s["fixed_price"], True)

for pp in truth["piece_prices"]:
    c = db.contracts.find_one({"type": "customer", "party_id": pp["customer"]})
    got = next((x for x in c["piece_prices"] if x["product_id"] == pp["product"]), {})
    check(c["_id"], f"{pp['product']} 2025", got.get("price_2025_eur"), pp["2025"])
    check(c["_id"], f"{pp['product']} 2026", got.get("price_2026_eur"), pp["2026"])

for cid, exp in EXPECTED_LEVERS.items():
    c = db.contracts.find_one({"_id": cid})
    check(cid, "levers", {l["type"] for l in c["levers"]}, exp)
for c in db.contracts.find({"_id": {"$nin": list(EXPECTED_LEVERS)}}):
    check(c["_id"], "levers", {l["type"] for l in c["levers"]}, set())

notices = {q["contract_id"]: q for q in db.price_quotes.find({"kind": "supplier_notice"})}
check("AluCast notice", "type/status", (notices["VC-2025-014"]["notice_type"], notices["VC-2025-014"]["status"]),
      ("price_adjustment", "claimed"))
k = notices["VC-2024-040"]
check("Keystone offer", "fields", (k["notice_type"], k["price"]["per_kg"], k["price"]["currency"],
                                   k["offer_valid_until"].date().isoformat(), k["volume_t"]),
      ("renewal_offer", 2.45, "USD", "2026-10-15", 60.0))

print(f"{checked} checks, {len(errors)} mismatches")
for e in errors:
    print("  " + e)
