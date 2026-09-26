"""Web app for purchasing and sales: materials, margins, levers, forecast, cases and chat.

    .venv/bin/uvicorn rmi.app:app --port 8000
"""

from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from rmi.agent import case, chat
from rmi.db import get_db
from rmi.engine import AS_OF
from rmi.engine.margins import MarginModel
from rmi.engine.opportunities import scan
from rmi.engine.plan import reforecast
from rmi.engine.prices import Market, material_price, quotation_prices

app = FastAPI(title="RMI Agent")
NEXT_MONTH = date(2026, 10, 1)


def clean(x):
    if isinstance(x, dict):
        return {k: clean(v) for k, v in x.items() if k != "_id" or isinstance(v, str)}
    if isinstance(x, list):
        return [clean(v) for v in x]
    if isinstance(x, datetime):
        return x.date().isoformat()
    return x


@app.get("/", response_class=HTMLResponse)
def index():
    return (Path(__file__).parent / "static" / "index.html").read_text()


@app.get("/api/materials")
def materials():
    db = get_db()
    market = Market.load(db)
    quote = quotation_prices(db)
    out = []
    for m in db.materials.find():
        trend = [{"month": d["month"].strftime("%Y-%m"), "avg": round(d["avg"], 3)}
                 for d in db.index_monthly.find({"index": m["index"], "month": {"$gte": datetime(2025, 10, 1)}}).sort("month", 1)]
        now = material_price(m["_id"], NEXT_MONTH, market, db)["price_eur"]
        budget = db.plans.find_one({"_id": "FY2027-BUDGET-v1"})["material_prices_eur_kg"].get(m["_id"])
        out.append({"id": m["_id"], "description": m.get("description"), "index": m["index"],
                    "quote_eur_kg": round(quote[m["_id"]], 4), "budget_eur_kg": round(budget, 4),
                    "oct_eur_kg": round(now, 4), "vs_budget_pct": round((now / budget - 1) * 100, 2),
                    "trend": trend})
    return out


@app.get("/api/margins")
def margins(month: str = "2026-10-01"):
    rows = MarginModel().watch(date.fromisoformat(month))
    return [{k: r[k] for k in ("customer_id", "product_id", "piece_price", "surcharge", "material_cost_pc",
                               "conversion_cost_pc", "contribution_pc", "margin_pct", "plan_margin_pct",
                               "target_margin_pct", "units", "unrecovered_month", "exposure")} for r in rows]


@app.get("/api/opportunities")
def opportunities():
    return clean([{k: f[k] for k in ("kind", "side", "party_id", "contract_id", "title", "value_eur", "deadline",
                                     "days_left", "urgency")} for f in scan()])


@app.get("/api/forecast")
def forecast():
    db = get_db()
    le = db.plans.find_one({"kind": "latest_estimate"}, sort=[("created_at", -1)])
    if not le:
        # e.g. after a data reload: build the latest estimate from current contracts and forecast
        reforecast(db, reason="baseline latest estimate")
        le = db.plans.find_one({"kind": "latest_estimate"}, sort=[("created_at", -1)])
    budget_m = {}
    for l in db.plan_lines.aggregate([{"$match": {"plan_id": {"$in": ["FY2027-BUDGET-v1", le["_id"]]}}},
                                      {"$group": {"_id": {"p": "$plan_id", "m": "$month"},
                                                  "c": {"$sum": "$contribution"}}}]):
        budget_m.setdefault(l["_id"]["m"].strftime("%Y-%m"), {})["budget" if "BUDGET" in l["_id"]["p"] else "le"] = round(l["c"])
    return clean({"plan_id": le["_id"], "created_at": le["created_at"], "totals": le["totals"],
                  "bridge": le["variance_vs_budget"]["bridge"], "by_customer": le["variance_vs_budget"]["by_customer"],
                  "monthly": [{"month": k, **v} for k, v in sorted(budget_m.items())],
                  "changes": list(db.contract_changes.find({}, {"_id": 1, "type": 1, "contract_id": 1, "price": 1,
                                                                "effective_from": 1, "reason": 1}))})


@app.get("/api/cases")
def cases():
    return clean(list(get_db().agent_cases.find({}, {"findings": 0}).sort("created_at", -1).limit(10)))


@app.get("/api/cases/{case_id}")
def case_detail(case_id: str):
    s = case.state(case_id)
    db = get_db()
    return clean({"case": db.agent_cases.find_one({"_id": case_id}), "pending": s["pending"],
                  "actions": list(db.agent_actions.find({"case_id": case_id}, {"_id": 0}))})


class Trigger(BaseModel):
    type: str = "scheduled_review"
    party_id: str | None = None
    text: str | None = None


@app.post("/api/cases")
def open_case(t: Trigger):
    case_id, payload = case.start(t.model_dump(exclude_none=True))
    return clean({"case_id": case_id, "pending": payload})


class Decision(BaseModel):
    approve: list[int] = []
    edits: dict[int, str] = {}


@app.post("/api/cases/{case_id}/decide")
def decide(case_id: str, d: Decision):
    decisions = [{"finding_id": i, "decision": "approve", "by": "buyer",
                  **({"message": d.edits[i]} if i in d.edits else {})} for i in d.approve]
    result = case.resume(case_id, {"decisions": decisions})
    return {"summary": result.get("summary"), "forecast": result.get("forecast")}


class Chat(BaseModel):
    thread_id: str = "web"
    message: str


@app.post("/api/chat")
def ask(c: Chat):
    return {"answer": chat.ask(c.thread_id, c.message)}
