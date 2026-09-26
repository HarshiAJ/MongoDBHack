"""Export sample documents from every collection to demo/db_samples/ for offline viewing.

    .venv/bin/python scripts/export_samples.py
"""

import json
from pathlib import Path

from bson import json_util

from rmi.db import get_db

OUT = Path(__file__).resolve().parent.parent / "demo" / "db_samples"
db = get_db()


def trim(x, limit=600):
    """Shorten long strings and lists so samples stay readable."""
    if isinstance(x, str):
        return x if len(x) <= limit else x[:limit] + f"... [{len(x) - limit} more chars]"
    if isinstance(x, list):
        head = [trim(v, limit) for v in x[:4]]
        return head + ([f"... [{len(x) - 4} more items]"] if len(x) > 4 else [])
    if isinstance(x, dict):
        return {k: trim(v, limit) for k, v in x.items()}
    return x


SAMPLES = [
    ("01_materials", "Canonical raw material: SAP number, aliases, thresholds, plant yields, lineage",
     "materials", {"_id": "RM-AL-A380"}, 1),
    ("02_products", "Finished part with embedded BOM and exploded kg per material (computed pattern)",
     "products", {"_id": "FG-WH-EV1"}, 1),
    ("03_contracts_vendor", "Supplier contract: LLM-extracted terms, amendment timeline, levers, agreed prices",
     "contracts", {"_id": {"$in": ["VC-2025-007", "VC-2025-014"]}}, 2),
    ("04_contracts_customer", "Customer contract: surcharge rules, piece prices, renegotiation levers",
     "contracts", {"_id": "CC-BL-2024-07"}, 1),
    ("05_documents", "Source document: full text, sections, verified extraction evidence",
     "documents", {"_id": "VC-2025-014_AluCast"}, 1),
    ("06_doc_chunks", "Search unit for hybrid search (content is auto-embedded by Atlas)",
     "doc_chunks", {"_id": "CC-BL-2024-07_Brightline#8"}, 1),
    ("07_market_prices", "Time series collection: one index observation",
     "market_prices", {"meta.index": "ALU"}, 2),
    ("08_index_monthly", "Computed monthly index averages used by every price formula",
     "index_monthly", {"index": "ALU", "_id": {"$gte": "ALU:2026-07"}}, 3),
    ("09_po_lines", "SAP purchase order line normalised to EUR/kg, with quality status",
     "po_lines", {"quality.status": {"$in": ["ok", "quarantined"]}}, 2),
    ("10_price_quotes", "Supplier notice (claimed), RFQ offer and buyer price",
     "price_quotes", {"kind": {"$in": ["supplier_notice", "rfq"]}}, 3),
    ("11_forecasts", "Sales forecast line (customer spelling resolved)",
     "forecasts", {}, 2),
    ("12_customer_prices", "SAP sales price condition (cross-checked against contract Annex A)",
     "customer_prices", {"customer_id": "C-BRIGHTLINE"}, 2),
    ("13_plans", "Plan versions: approved budget and latest estimate with bridge",
     "plans", {}, 2),
    ("14_plan_lines", "Plan line: month x customer x part",
     "plan_lines", {"customer_id": "C-SAKURA"}, 2),
    ("15_ingestion_issues", "Data-quality findings with rule, severity and lineage",
     "ingestion_issues", {"severity": "error"}, 3),
    ("16_negotiations", "Negotiation history (seed of long-term memory)",
     "negotiations", {"origin": "history"}, 2),
    ("17_agent_memory", "Long-term semantic memory (text is auto-embedded)",
     "agent_memory", {}, 3),
    ("18_entity_graph", "Graph node used by $graphLookup",
     "entity_graph", {"_id": "RM-CU-ROD"}, 1),
    ("19_agent_cases", "Agent case: trigger, status, report, forecast",
     "agent_cases", {"status": "done"}, 1),
    ("20_agent_actions", "Approved action: draft email ready to send, with clause references",
     "agent_actions", {}, 2),
    ("21_contract_changes", "Accepted agreement applied to a contract",
     "contract_changes", {}, 2),
    ("22_parties", "Vendor and customer (polymorphic collection)",
     "parties", {"_id": {"$in": ["V-100101", "C-BRIGHTLINE"]}}, 2),
]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    index = ["# Sample documents from the `rmi` database", "",
             "Exported by `scripts/export_samples.py` from the Atlas cluster. Long text is shortened.", "",
             "| File | Collection | What it shows | Documents in collection |", "|---|---|---|---|"]
    for name, desc, coll, query, n in SAMPLES:
        docs = [trim(d) for d in db[coll].find(query).limit(n)]
        (OUT / f"{name}.json").write_text(json_util.dumps(
            {"collection": coll, "description": desc, "documents": docs},
            indent=2, json_options=json_util.RELAXED_JSON_OPTIONS))
        index.append(f"| [{name}.json]({name}.json) | `{coll}` | {desc} | {db[coll].estimated_document_count():,} |")
        print(f"{name}: {len(docs)} docs")

    indexes = {}
    for coll in ("doc_chunks", "agent_memory"):
        indexes[coll] = [{"name": i["name"], "type": i.get("type"), "definition": i.get("latestDefinition")}
                         for i in db[coll].list_search_indexes()]
    (OUT / "23_search_indexes.json").write_text(json.dumps(indexes, indent=2))
    index.append("| [23_search_indexes.json](23_search_indexes.json) | search indexes | Vector (autoEmbed voyage-4) "
                 "and Atlas Search index definitions | |")
    (OUT / "README.md").write_text("\n".join(index) + "\n")
    print(f"\nWritten to {OUT}")


if __name__ == "__main__":
    main()
