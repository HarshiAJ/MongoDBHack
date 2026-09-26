"""The purchasing case workflow: a LangGraph state machine checkpointed in MongoDB.

Two entry paths share approval, re-planning and reporting:

  review    sense -> investigate -> propose -> approve* -> execute -> replan -> report
  response  interpret -> approve* -> apply -> replan -> report

  * approve pauses the graph with interrupt(); the case waits (for days if need be) in
    MongoDB until a buyer decides, then resumes from exactly that point.

The engine computes every number. The LLM chooses actions, cites clauses and writes the
messages, and is told to use only the figures it is given.
"""

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Literal, TypedDict

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.mongodb import MongoDBSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from rmi import llm
from rmi.agent import changes
from rmi.agent import memory as agent_memory
from rmi.db import client, get_db
from rmi.engine import AS_OF
from rmi.engine.margins import MarginModel
from rmi.engine.opportunities import scan
from rmi.engine.plan import reforecast
from rmi.engine.prices import Market, add_months, month_start, vendor_price
from rmi.search import hybrid_search


class CaseState(TypedDict, total=False):
    case_id: str
    trigger: dict
    findings: list[dict]
    margins: list[dict]
    evidence: dict
    memory: dict
    proposals: list[dict]
    agreements: list[dict]
    decisions: list[dict]
    executed: list[dict]
    forecast: dict
    summary: str


# -- LLM output schemas ----------------------------------------------------------

ActionType = Literal["counter_notice", "request_price_decrease", "invoke_meet_competition", "renewal_negotiation",
                     "request_price_review", "hardship_request", "price_down_waiver_request", "correct_billing",
                     "monitor"]


class ProposedAction(BaseModel):
    finding_id: int
    action_type: ActionType
    counterparty_id: str
    channel: Literal["email", "internal_task"]
    subject: str
    message: str = Field(description="Ready-to-send email draft, or the internal task description")
    rationale: str = Field(description="Why, in 2-3 sentences, citing the clause section and the given figures")
    clause_refs: list[str] = Field(description="Ids of the evidence chunks relied on")


class Proposals(BaseModel):
    actions: list[ProposedAction]


class ResponseReading(BaseModel):
    summary: str = Field(description="One-sentence summary of the counterparty's response")
    agreements: list[changes.Agreement] = Field(description="Only changes the counterparty clearly agreed to")
    open_points: list[str]


def model():
    return ChatOpenAI(model=llm.MODEL, temperature=0)


def _case_update(db, case_id, **fields):
    db.agent_cases.update_one({"_id": case_id}, {"$set": {**fields, "updated_at": datetime.now(timezone.utc)}})


# -- review path -----------------------------------------------------------------

def sense(state: CaseState) -> CaseState:
    db = get_db()
    findings = scan(db)
    for i, f in enumerate(findings):
        f["id"] = i
    mm = MarginModel(db)
    month = add_months(month_start(AS_OF), 1)
    margins = [{k: r[k] for k in ("customer_id", "product_id", "margin_pct", "plan_margin_pct", "target_margin_pct",
                                  "units", "unrecovered_month", "status")} for r in mm.watch(month)]
    _case_update(db, state["case_id"], status="investigating", findings=len(findings))
    return {"findings": findings, "margins": margins}


def investigate(state: CaseState) -> CaseState:
    db = get_db()
    evidence, memory = {}, {}
    for f in actionable(state["findings"]):
        hits = hybrid_search(f["evidence_query"], k=3, contract_id=f["contract_id"]) if f["contract_id"] else []
        evidence[str(f["id"])] = [{"chunk": h["_id"], "heading": h["heading"], "text": h["text"][:600]} for h in hits]
        if f["party_id"] not in memory:
            # long-term semantic memory: what we learned about this counterparty, recalled by meaning
            memory[f["party_id"]] = [m["text"] for m in agent_memory.recall(f["title"], f["party_id"], k=3)]
    return {"evidence": evidence, "memory": memory}


def actionable(findings):
    return [f for f in findings if f["urgency"] in ("high", "medium") or abs(f["value_eur"]) > 0]


PROPOSE_SYSTEM = """You are the purchasing agent of Veltra Automotive, a Tier-1 supplier.
For each finding, propose exactly one action. Rules:
- Use only the figures in the findings; never compute or invent numbers, prices or dates.
- Cite the contract section from the evidence in the rationale and in customer/supplier emails.
- Use the negotiation memory to choose tone and approach (what worked with this counterparty before).
- Emails are from the purchasing team, factual and firm, with the deadline and the requested reply.
- Use 'internal_task' for internal fixes (e.g. SAP price corrections) and 'monitor' when no action is due yet.
- counterparty_id must be the finding's party_id."""


def propose(state: CaseState) -> CaseState:
    items = []
    for f in actionable(state["findings"]):
        items.append({
            "finding_id": f["id"], "kind": f["kind"], "side": f["side"], "party_id": f["party_id"],
            "contract_id": f["contract_id"], "title": f["title"], "value_eur": f["value_eur"],
            "deadline": f["deadline"], "days_left": f["days_left"], "detail": compact(f["detail"]),
            "evidence": state["evidence"].get(str(f["id"]), []),
            "negotiation_memory": state["memory"].get(f["party_id"], []),
        })
    prompt = (f"Today is {AS_OF.isoformat()}.\n\nMargin watch for next month:\n{json.dumps(state['margins'])}\n\n"
              f"Findings:\n{json.dumps(items, default=str)}")
    out = model().with_structured_output(Proposals).invoke([("system", PROPOSE_SYSTEM), ("human", prompt)])
    by_id = {f["id"]: f for f in state["findings"]}
    proposals = []
    for a in out.actions:
        f = by_id.get(a.finding_id)
        if not f:
            continue
        proposals.append({**a.model_dump(), "value_eur": f["value_eur"], "deadline": f["deadline"],
                          "kind": f["kind"], "contract_id": f["contract_id"], "title": f["title"]})
    return {"proposals": proposals}


def compact(detail: dict) -> dict:
    """Drop bulky engine internals the LLM does not need."""
    drop = {"material_lines", "sources"}
    return {k: v for k, v in detail.items() if k not in drop}


def execute(state: CaseState) -> CaseState:
    db = get_db()
    decisions = {d["finding_id"]: d for d in state.get("decisions", [])}
    executed = []
    for p in state["proposals"]:
        d = decisions.get(p["finding_id"], {"decision": "reject"})
        if d["decision"] != "approve" or p["action_type"] == "monitor":
            continue
        message = d.get("message") or p["message"]
        action = {
            "case_id": state["case_id"], "finding_id": p["finding_id"], "kind": p["kind"],
            "action_type": p["action_type"], "counterparty_id": p["counterparty_id"],
            "contract_id": p["contract_id"], "channel": p["channel"], "subject": p["subject"], "message": message,
            "rationale": p["rationale"], "clause_refs": p["clause_refs"], "value_eur": p["value_eur"],
            "deadline": p["deadline"],
            # nothing leaves the building from here: emails become approved drafts for the buyer to send
            "status": "ready_to_send" if p["channel"] == "email" else "task_open",
            "approved_by": d.get("by", "buyer"), "approved_at": datetime.now(timezone.utc),
        }
        action["_id"] = db.agent_actions.insert_one(action).inserted_id
        if p["channel"] == "email":
            db.negotiations.insert_one({
                "date": datetime.now(timezone.utc), "party_id": p["counterparty_id"],
                "party_type": "customer" if p["counterparty_id"].startswith("C-") else "vendor",
                "contract_id": p["contract_id"], "topic": p["subject"], "ask": p["rationale"],
                "outcome": "initiated", "lessons": None, "owner": "agent", "origin": "agent",
                "case_id": state["case_id"], "action_id": action["_id"],
            })
        executed.append({k: v for k, v in action.items() if k not in ("_id", "approved_at")})
    return {"executed": executed}


# -- response path ---------------------------------------------------------------

def interpret(state: CaseState) -> CaseState:
    db = get_db()
    t = state["trigger"]
    party = t.get("party_id")
    contracts = list(db.contracts.find({"party_id": party} if party else {}))
    context = [{"contract_id": c["_id"], "party": c["party_name"], "type": c["type"],
                "materials": c.get("material_ids"), "products": c.get("product_ids"),
                "piece_prices": c.get("piece_prices"), "base_price": c["summary"].get("base_price"),
                "base_price_unit": c["summary"].get("base_price_unit")} for c in contracts]
    prompt = (f"Today is {AS_OF.isoformat()}. Contracts:\n{json.dumps(context, default=str)}\n\n"
              f"Counterparty response:\n{t['text']}")
    reading = model().with_structured_output(ResponseReading).invoke([
        ("system", "Extract only changes the counterparty clearly agreed to, mapped to the contract ids given. "
                   "Include every price the counterparty confirms for a period, also a price that stays unchanged "
                   "for a period (e.g. 'October stays at X' is an agreement effective from the first of October). "
                   "Do not treat proposals, conditions or questions as agreements."),
        ("human", prompt)])
    # check each agreement against what the contract formula would give, so the buyer sees the difference
    market = Market.load(db)
    agreements = []
    for a in reading.agreements:
        item = a.model_dump()
        if a.type == "vendor_price" and a.effective_from:
            c = db.contracts.find_one({"_id": a.contract_id})
            exp = vendor_price(c, a.material_id, month_start(datetime.fromisoformat(a.effective_from)), market)
            item["formula_price"] = exp["price"]
            item["vs_formula_pct"] = round((a.price / exp["price"] - 1) * 100, 2)
        agreements.append(item)
    return {"agreements": agreements, "summary": reading.summary,
            "proposals": [{"open_points": reading.open_points}]}


def apply(state: CaseState) -> CaseState:
    decisions = {d["finding_id"]: d for d in state.get("decisions", [])}
    executed = []
    for i, a in enumerate(state["agreements"]):
        if decisions.get(i, {}).get("decision") != "approve":
            continue
        agreement = changes.Agreement(**{k: v for k, v in a.items() if k in changes.Agreement.model_fields})
        ch = changes.record(agreement, case_id=state["case_id"], approved_by=decisions[i].get("by", "buyer"))
        executed.append({k: v for k, v in ch.items() if k != "recorded_at"})
    return {"executed": executed}


# -- shared ----------------------------------------------------------------------

def approve(state: CaseState) -> CaseState:
    db = get_db()
    is_response = state["trigger"]["type"] == "counterparty_response"
    items = state["agreements"] if is_response else state["proposals"]
    _case_update(db, state["case_id"], status="awaiting_approval", pending=len(items))
    # Pauses here. The payload is what the buyer reviews; the resume value is their decisions.
    decision = interrupt({"case_id": state["case_id"], "kind": "agreements" if is_response else "proposals",
                          "items": items})
    if decision.get("approve_all"):
        ids = range(len(items)) if is_response else [p["finding_id"] for p in items]
        decisions = [{"finding_id": i, "decision": "approve", "by": decision.get("by", "buyer")} for i in ids]
    else:
        decisions = decision.get("decisions", [])
    _case_update(db, state["case_id"], status="executing")
    return {"decisions": decisions}


def replan(state: CaseState) -> CaseState:
    db = get_db()
    previous = db.plans.find_one({"kind": "latest_estimate"}, sort=[("created_at", -1)])
    plan = reforecast(db, reason=f"case {state['case_id']}: {state['trigger']['type']}",
                      changes=[e.get("_id") for e in state.get("executed", []) if e.get("_id")])
    t = plan["totals"]
    forecast = {
        "plan_id": plan["_id"],
        "budget_contribution": t["budget"]["contribution"], "le_contribution": t["le"]["contribution"],
        "delta_vs_budget": t["contribution_delta"], "le_margin_pct": t["le"]["margin_pct"],
        "budget_margin_pct": t["budget"]["margin_pct"], "bridge": plan["variance_vs_budget"]["bridge"],
        "previous_plan_id": previous["_id"] if previous else None,
        "delta_vs_previous": round(t["le"]["contribution"] - previous["totals"]["le"]["contribution"], 2)
        if previous else None,
    }
    return {"forecast": forecast}


def report(state: CaseState) -> CaseState:
    db = get_db()
    facts = {"trigger": state["trigger"].get("type"), "executed": state.get("executed", []),
             "forecast": state.get("forecast"), "response_summary": state.get("summary")}
    text = model().invoke([
        ("system", "Write a short case report for the head of purchasing: what happened, what was approved, and the "
                   "effect on the FY2027 forecast vs budget. Use only the given figures. Approved emails are drafts "
                   "ready to send and tasks are opened, not completed: do not describe outcomes that have not "
                   "happened. Recorded agreements (type vendor_price, customer_piece_price, price_down_waiver) are "
                   "accepted changes. Max 150 words."),
        ("human", json.dumps(facts, default=str))]).content
    _case_update(db, state["case_id"], status="done", report=text, forecast=state.get("forecast"))
    agent_memory.remember(text, kind="case_report", ref=state["case_id"], db=db)
    agent_memory.sync_from_negotiations(db)
    return {"summary": text}


def route_start(state: CaseState) -> str:
    return "interpret" if state["trigger"]["type"] == "counterparty_response" else "sense"


def route_after_approve(state: CaseState) -> str:
    return "apply" if state["trigger"]["type"] == "counterparty_response" else "execute"


def route_after_execution(state: CaseState) -> str:
    """Nothing approved means nothing changed: skip the re-forecast."""
    return "replan" if state.get("executed") else "report"


def build(checkpointer=None):
    g = StateGraph(CaseState)
    for name, fn in [("sense", sense), ("investigate", investigate), ("propose", propose), ("approve", approve),
                     ("execute", execute), ("interpret", interpret), ("apply", apply), ("replan", replan),
                     ("report", report)]:
        g.add_node(name, fn)
    g.add_conditional_edges(START, route_start, ["sense", "interpret"])
    g.add_edge("sense", "investigate")
    g.add_edge("investigate", "propose")
    g.add_edge("propose", "approve")
    g.add_edge("interpret", "approve")
    g.add_conditional_edges("approve", route_after_approve, ["execute", "apply"])
    g.add_conditional_edges("execute", route_after_execution, ["replan", "report"])
    g.add_conditional_edges("apply", route_after_execution, ["replan", "report"])
    g.add_edge("replan", "report")
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer)


def checkpointer():
    return MongoDBSaver(client(), db_name=os.environ.get("MONGODB_DB", "rmi"),
                        checkpoint_collection_name="agent_checkpoints",
                        writes_collection_name="agent_checkpoint_writes")


def start(trigger: dict) -> tuple[str, dict]:
    """Open a case; runs until the approval interrupt. Returns (case_id, pending payload)."""
    db = get_db()
    case_id = f"CASE-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
    db.agent_cases.insert_one({"_id": case_id, "trigger": trigger, "status": "running",
                               "created_at": datetime.now(timezone.utc)})
    graph = build(checkpointer())
    result = graph.invoke({"case_id": case_id, "trigger": trigger}, {"configurable": {"thread_id": case_id}})
    return case_id, pending(result)


def resume(case_id: str, decision: dict) -> dict:
    """Continue a paused case with the buyer's decision. Works from a fresh process."""
    graph = build(checkpointer())
    return graph.invoke(Command(resume=decision), {"configurable": {"thread_id": case_id}})


def pending(result: dict) -> dict | None:
    intr = result.get("__interrupt__")
    return intr[0].value if intr else None


def state(case_id: str) -> dict:
    graph = build(checkpointer())
    snap = graph.get_state({"configurable": {"thread_id": case_id}})
    return {"values": snap.values, "next": snap.next,
            "pending": snap.tasks[0].interrupts[0].value if snap.tasks and snap.tasks[0].interrupts else None}
