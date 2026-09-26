"""Live trigger: a MongoDB change stream opens a review case when prices move or a supplier writes.

    python -m rmi.agent.watcher        # keep running; opens a case per event
Watches index_monthly (updated when new market prices arrive) and price_quotes (supplier notices).
"""

from rmi.agent import case
from rmi.db import get_db

PIPELINE = [{"$match": {"$or": [
    {"ns.coll": "index_monthly", "operationType": {"$in": ["insert", "update", "replace"]}},
    {"ns.coll": "price_quotes", "operationType": "insert", "fullDocument.kind": "supplier_notice"},
]}}]


def main():
    db = get_db()
    print("Watching index_monthly and price_quotes for changes...")
    with db.watch(PIPELINE, full_document="updateLookup") as stream:
        for ev in stream:
            doc = ev.get("fullDocument") or {}
            coll = ev["ns"]["coll"]
            trigger = ({"type": "market_move", "index": doc.get("index"), "avg": doc.get("avg")}
                       if coll == "index_monthly" else
                       {"type": "supplier_notice", "contract_id": doc.get("contract_id")})
            print(f"Event on {coll}: {trigger} -> opening case")
            case_id, payload = case.start(trigger)
            print(f"{case_id}: {len(payload['items']) if payload else 0} proposals waiting for approval")


if __name__ == "__main__":
    main()
