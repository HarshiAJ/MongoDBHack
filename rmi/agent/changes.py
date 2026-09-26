"""Accepted contract changes: stored in contract_changes and applied onto contracts.

contract_changes is agent-owned (never dropped by ingestion), and rmi.extract re-applies
it whenever contracts are rebuilt, so an agreement survives a full data reload.
"""

from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from rmi.db import get_db

ChangeType = Literal["vendor_price", "customer_piece_price", "price_down_waiver"]


class Agreement(BaseModel):
    """A change both parties agreed to, as captured from a counterparty response."""
    type: ChangeType
    contract_id: str
    material_id: str | None = Field(None, description="For vendor_price")
    product_id: str | None = Field(None, description="For customer_piece_price")
    price: float | None = Field(None, description="Agreed price per kg (vendor) or per piece (customer)")
    currency: str | None = "EUR"
    effective_from: str | None = Field(None, description="YYYY-MM-DD")
    year: int | None = Field(None, description="For price_down_waiver: the calendar year waived")
    reason: str = Field(description="One sentence: what was agreed and why")


def _dt(s: str) -> datetime:
    return datetime.combine(date.fromisoformat(s), datetime.min.time())


def apply_to_contract(db, change: dict) -> None:
    """Idempotently write one change onto its contract document."""
    cid, t = change["contract_id"], change["type"]
    if t == "vendor_price":
        entry = {"change_id": change["_id"], "material_id": change["material_id"], "price": change["price"],
                 "currency": change["currency"], "effective_from": _dt(change["effective_from"]),
                 "reason": change["reason"]}
        field = "agreed_prices"
    elif t == "customer_piece_price":
        entry = {"change_id": change["_id"], "product_id": change["product_id"], "price": change["price"],
                 "effective_from": _dt(change["effective_from"]), "reason": change["reason"]}
        field = "agreed_piece_prices"
    else:
        entry = {"change_id": change["_id"], "year": change["year"], "reason": change["reason"]}
        field = "price_down_waivers"
    db.contracts.update_one({"_id": cid}, {"$pull": {field: {"change_id": change["_id"]}}})
    db.contracts.update_one({"_id": cid}, {"$push": {field: entry}})


def record(agreement: Agreement, *, case_id: str | None = None, approved_by: str = "buyer", db=None) -> dict:
    db = db if db is not None else get_db()
    contract = db.contracts.find_one({"_id": agreement.contract_id})
    if not contract:
        raise ValueError(f"unknown contract {agreement.contract_id}")
    change = {
        "_id": f"CHG-{datetime.now(timezone.utc):%Y%m%d%H%M%S%f}",
        **agreement.model_dump(),
        "party_id": contract["party_id"], "case_id": case_id, "approved_by": approved_by,
        "recorded_at": datetime.now(timezone.utc), "status": "accepted",
    }
    db.contract_changes.insert_one(change)
    apply_to_contract(db, change)
    db.negotiations.insert_one({
        "date": change["recorded_at"], "party_id": contract["party_id"], "party_type": contract["type"],
        "contract_id": contract["_id"], "topic": agreement.type.replace("_", " "), "ask": None,
        "outcome": agreement.reason, "lessons": None, "owner": approved_by, "origin": "agent",
        "change_id": change["_id"], "case_id": case_id,
    })
    return change


def reapply_all(db=None) -> int:
    db = db if db is not None else get_db()
    n = 0
    for change in db.contract_changes.find({"status": "accepted"}).sort("recorded_at", 1):
        apply_to_contract(db, change)
        n += 1
    return n
