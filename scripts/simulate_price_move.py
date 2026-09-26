"""Simulate a live market move: add aluminium ticks for Sep 2026 and refresh the monthly average.

    python scripts/simulate_price_move.py ALU 3.0     # +3% on the latest price
The change to index_monthly is what the watcher's change stream reacts to.
"""

import sys
from datetime import datetime, timedelta, timezone

from rmi.db import get_db

index, pct = (sys.argv[1], float(sys.argv[2])) if len(sys.argv) > 2 else ("ALU", 3.0)
db = get_db()
last = db.market_prices.find_one({"meta.index": index}, sort=[("ts", -1)])
ts = last["ts"] + timedelta(days=1)
value = last["value"] * (1 + pct / 100)
db.market_prices.insert_one({"ts": ts, "meta": {**last["meta"], "source": "live-feed-simulator"}, "value": value})
month = datetime(ts.year, ts.month, 1)
avg = list(db.market_prices.aggregate([
    {"$match": {"meta.index": index, "ts": {"$gte": month}}},
    {"$group": {"_id": None, "avg": {"$avg": "$value"}, "n": {"$sum": 1}}}]))[0]
db.index_monthly.update_one({"_id": f"{index}:{month:%Y-%m}"},
                            {"$set": {"avg": avg["avg"], "observations": avg["n"],
                                      "updated_at": datetime.now(timezone.utc)}})
print(f"{index} {ts:%Y-%m-%d} {value:,.2f} ({pct:+.1f}%); {month:%Y-%m} average now {avg['avg']:,.2f}")
