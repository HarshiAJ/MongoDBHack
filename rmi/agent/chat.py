"""Chat assistant: the same engine and search, exposed as tools, with MongoDB-checkpointed threads."""

import json
from datetime import date

from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from rmi import llm
from rmi.agent.case import checkpointer
from rmi.db import get_db
from rmi.engine import AS_OF
from rmi.engine.margins import MarginModel
from rmi.engine.opportunities import scan
from rmi.engine.prices import Market, vendor_price
from rmi.search import hybrid_search


def _j(x):
    return json.dumps(x, default=str)[:12000]


@tool
def margin_watch(month: str = "2026-10-01", customer_id: str | None = None) -> str:
    """Contribution margin per customer and part for a month (YYYY-MM-DD) vs plan and target."""
    rows = MarginModel().watch(date.fromisoformat(month), customer_id)
    return _j([{k: r[k] for k in ("customer_id", "product_id", "piece_price", "surcharge", "material_cost_pc",
                                  "margin_pct", "plan_margin_pct", "target_margin_pct", "unrecovered_month")}
               for r in rows])


@tool
def opportunities() -> str:
    """Actionable findings with value and deadline: supplier claims, decreases owed, meet-competition,
    renewals, customer hardship / price review / waiver, billing errors."""
    return _j([{k: f[k] for k in ("kind", "side", "party_id", "contract_id", "title", "value_eur", "deadline")}
               for f in scan()])


@tool
def supplier_price(contract_id: str, material_id: str, month: str = "2026-10-01") -> str:
    """What a supplier contract formula gives for a material in a month, with the calculation inputs."""
    c = get_db().contracts.find_one({"_id": contract_id})
    return _j(vendor_price(c, material_id, date.fromisoformat(month), Market.load()))


@tool
def search_contracts(query: str, contract_id: str | None = None) -> str:
    """Hybrid (semantic + keyword) search over contract clauses, supplier emails and analyst notes."""
    return _j(hybrid_search(query, k=5, contract_id=contract_id))


@tool
def forecast_vs_budget() -> str:
    """Latest FY2027 estimate vs the approved budget, with the contribution bridge."""
    p = get_db().plans.find_one({"kind": "latest_estimate"}, sort=[("created_at", -1)])
    return _j({"plan_id": p["_id"], "totals": p["totals"], "bridge": p["variance_vs_budget"]["bridge"],
               "by_customer": p["variance_vs_budget"]["by_customer"]}) if p else "No latest estimate yet."


@tool
def negotiation_history(party_id: str) -> str:
    """Past negotiations and lessons with a customer (C-...) or vendor (V-...)."""
    return _j(list(get_db().negotiations.find({"party_id": party_id}, {"_id": 0, "source": 0}).sort("date", -1)))


SYSTEM = f"""You are the raw material intelligence assistant for Veltra Automotive's purchasing and sales teams.
Today is {AS_OF}. Answer with figures from the tools only, cite contract sections when relevant, be concise.
Parties: customers C-NORDWERK, C-BRIGHTLINE, C-SAKURA; vendors V-100101 AluCast, V-100102 Midwest,
V-100103 Nordic, V-100104 Baltic, V-100105 Cuprum, V-100106 PolyNova, V-100107 Keystone, V-100108 Zenith."""

_agent = None


def agent():
    global _agent
    if _agent is None:
        _agent = create_agent(ChatOpenAI(model=llm.MODEL, temperature=0),
                              [margin_watch, opportunities, supplier_price, search_contracts, forecast_vs_budget,
                               negotiation_history],
                              system_prompt=SYSTEM, checkpointer=checkpointer())
    return _agent


def ask(thread_id: str, message: str) -> str:
    out = agent().invoke({"messages": [("user", message)]}, {"configurable": {"thread_id": f"chat-{thread_id}"}})
    return out["messages"][-1].content


if __name__ == "__main__":
    print(ask("cli", "Which customer parts are furthest below target margin in October and why?"))
