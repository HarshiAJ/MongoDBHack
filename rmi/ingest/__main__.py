"""Run the full ingestion: parse all seven sources, validate, and load the canonical model.

    python -m rmi.ingest            # load into MongoDB (replaces the ingested collections)
    python -m rmi.ingest --dry-run  # parse and validate only, print the issue report
"""

import argparse
import uuid
from collections import Counter
from datetime import datetime, timezone

from pymongo import InsertOne

from rmi.db import get_db, reset_ingested
from rmi.ingest import bom, documents, excel_inputs, market, purchasing, sales, sap, tracker
from rmi.ingest.common import Context

AS_OF = datetime(2026, 9, 26, tzinfo=timezone.utc)  # the dataset's "today"


def run(dry_run=False) -> Context:
    ctx = Context(run_id=f"ingest-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}")
    # Order matters: FX before prices, the tracker's crosswalk before SAP, BOM before sales.
    market.load(ctx)
    tracker.load(ctx)
    sap.load(ctx)
    bom.load(ctx)
    sales.load(ctx)
    purchasing.load(ctx, AS_OF)
    excel_inputs.load(ctx)
    documents.load(ctx)

    report(ctx)
    if not dry_run:
        write(ctx)
    return ctx


def write(ctx: Context):
    db = get_db()
    reset_ingested(db)
    for name, docs in ctx.docs.items():
        docs = list(docs.values()) if isinstance(docs, dict) else docs
        if docs:
            db[name].bulk_write([InsertOne(d) for d in docs], ordered=False)
    if ctx.issues:
        db.ingestion_issues.insert_many(ctx.issues)

    # Computed pattern: monthly index averages, the input to every price-adjustment clause
    db.market_prices.aggregate([
        {"$group": {
            "_id": {"index": "$meta.index", "month": {"$dateTrunc": {"date": "$ts", "unit": "month"}}},
            "avg": {"$avg": "$value"}, "min": {"$min": "$value"}, "max": {"$max": "$value"},
            "observations": {"$sum": 1}, "unit": {"$first": "$meta.unit"},
        }},
        {"$project": {"_id": {"$concat": ["$_id.index", ":", {"$dateToString": {"date": "$_id.month", "format": "%Y-%m"}}]},
                      "index": "$_id.index", "month": "$_id.month", "avg": 1, "min": 1, "max": 1,
                      "observations": 1, "unit": 1}},
        {"$merge": {"into": "index_monthly", "whenMatched": "replace"}},
    ])

    db.ingestion_runs.insert_one({
        "_id": ctx.run_id, "started": ctx.started, "finished": datetime.now(timezone.utc),
        "files": list(ctx.files.values()),
        "counts": {k: len(v) for k, v in ctx.docs.items()},
        "issues": dict(Counter(i["severity"] for i in ctx.issues)),
    })
    print(f"\nLoaded into database '{db.name}' (run {ctx.run_id}).")


def report(ctx: Context):
    print(f"Run {ctx.run_id}")
    print("\nFiles:")
    for f in ctx.files.values():
        print(f"  {f['file']:<62} {f['rows_in']:>5} in {f['rows_loaded']:>5} loaded")
    print("\nCanonical records:", {k: len(v) for k, v in ctx.docs.items()})
    sev = Counter(i["severity"] for i in ctx.issues)
    print(f"\nIssues: {len(ctx.issues)} ({dict(sev)})")
    order = {"error": 0, "warning": 1, "info": 2}
    for i in sorted(ctx.issues, key=lambda i: (order[i["severity"]], i["source"]["system"])):
        fixed = " [auto-fixed]" if i["status"] == "auto_fixed" else ""
        print(f"  {i['severity']:<7} {i['source']['system']:<19} {i['rule']:<28} {i['message']}{fixed}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    run(parser.parse_args().dry_run)
