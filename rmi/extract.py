"""LLM extraction of structured contract terms, with verified evidence.

    python -m rmi.extract          # extract all pending documents
    python -m rmi.extract --all    # re-extract everything

For each contract, amendment and supplier email the model returns typed terms plus a
verbatim quote per field. A quote only counts if it appears in the source text; documents
with unverifiable evidence are marked `needs_review` instead of being trusted.

Results:
  documents.extraction   raw extraction, evidence and verification status
  contracts              one document per contract with base terms, amendments and the
                         effective-terms timeline the agent evaluates clauses against
  price_quotes           supplier price notices (kind = supplier_notice, status = claimed)
"""

import argparse
import re
from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from rmi import llm
from rmi.db import get_db

IndexCode = Literal["ALU", "CU", "ZN", "HRC", "PA66", "RUBBER"]
Review = Literal["monthly", "quarterly", "semi-annual", "annual"]


class Evidence(BaseModel):
    field: str = Field(description="Name of the extracted field this quote supports")
    section: str = Field(description="Section heading the quote comes from")
    quote: str = Field(description="Verbatim text copied from the document, 5-40 words")


class PriceAdjustment(BaseModel):
    index_code: IndexCode
    index_name: str
    reference_period: str = Field(description="YYYY-MM of the reference average")
    reference_value: float
    reference_unit: str
    review_frequency: Review
    averaging_months: int = Field(description="Number of months averaged (1 = prior calendar month)")
    trigger_pct: float = Field(description="Deviation in percent that must be exceeded to trigger")
    pass_through_share: float = Field(description="Share of the relative index change passed on, 0-1")
    annual_cap_pct: float | None = Field(description="Cap on cumulative adjustment per contract year, percent")
    notice_days: int = Field(description="Days of written notice the supplier must give before it applies")
    fx_rule: str | None


LeverType = Literal["hardship", "annual_price_review", "price_down_waiver", "extraordinary_review",
                    "cost_reduction_sharing", "meet_competition", "volume_rebate", "renewal"]


class Lever(BaseModel):
    """A clause that opens room to renegotiate price, volume or terms."""
    type: LeverType
    section: str
    threshold_pct: float | None = Field(description="Percentage threshold that activates the lever, if any")
    notice_days: int | None = Field(description="Days of notice or response time stated in the clause")
    deadline_rule: str | None = Field(description="Timing rule as written, e.g. '60 days before 1 January'")
    effect: str = Field(description="What the lever allows, paraphrased in one sentence")


class PiecePrice(BaseModel):
    product_id: str
    price_2025_eur: float | None
    price_2026_eur: float | None


class VendorContract(BaseModel):
    contract_id: str
    supplier_name: str
    material_ids: list[str] = Field(description="Canonical material ids from the provided list")
    signed: str = Field(description="YYYY-MM-DD")
    expires: str = Field(description="YYYY-MM-DD")
    base_price: float
    base_price_unit: str = Field(description="e.g. EUR/kg or EUR/t")
    price_notes: str | None = Field(description="Any extra price rules, e.g. grade extras")
    invoicing_currency: str
    fixed_price: bool
    price_adjustment: PriceAdjustment | None
    annual_volume_t: float | None
    min_offtake_t: float | None
    termination_notice_days: int | None
    renewal_notice_days: int | None = Field(description="Days before expiry renewal talks must start")
    payment_terms: str | None
    levers: list[Lever] = Field(description=(
        "Renegotiation levers from dedicated clauses: competitiveness/meet-competition, volume rebate, renewal "
        "offer or extension-pricing clauses. Do not list the generic renewal timing in the Term section; it "
        "belongs in renewal_notice_days."))
    evidence: list[Evidence]


class Surcharge(BaseModel):
    index_code: IndexCode
    reference_value: float | None
    trigger_pct: float
    pass_through_share: float
    averaging: str = Field(description="Averaging window as written, e.g. '3-month average'")
    averaging_months: int
    timing: str = Field(description="When the adjustment applies, as written")
    cap_pct: float | None = Field(description="Cap per contract year in percent, if any")


class CustomerContract(BaseModel):
    contract_id: str
    customer_name: str
    product_ids: list[str]
    signed: str
    expires: str
    surcharges: list[Surcharge]
    excluded_materials: list[str] = Field(description="Materials explicitly not adjusted, as written")
    claim_rule: str = Field(description="Deadline/procedure for surcharge claims, as written")
    claim_deadline_days: int | None
    claim_deadline_reference: Literal["before_quarter_start", "after_quarter_end", "before_adjustment_date"] | None
    annual_price_down_pct: float | None
    piece_prices: list[PiecePrice] = Field(description="Annex A piece prices per part")
    levers: list[Lever] = Field(description="Hardship, price review, waiver, extraordinary review, cost sharing")
    evidence: list[Evidence]


class Change(BaseModel):
    field: Literal["trigger_pct", "review_frequency", "averaging_months", "pass_through_share",
                   "annual_cap_pct", "notice_days", "base_price", "expires"]
    old_value: str | None
    new_value: str


class Amendment(BaseModel):
    amendment_id: str
    amends_contract_id: str
    effective_date: str
    changes: list[Change]
    evidence: list[Evidence]


class SupplierNotice(BaseModel):
    notice_type: Literal["price_adjustment", "renewal_offer"]
    contract_id: str
    supplier_name: str
    material_id: str
    new_price: float
    currency: str
    unit: Literal["kg", "t"]
    effective_date: str
    offer_valid_until: str | None = Field(description="YYYY-MM-DD if the notice is an offer with a deadline")
    volume_t: float | None
    claimed_basis: str = Field(description="The supplier's stated justification, paraphrased briefly")
    evidence: list[Evidence]


SCHEMAS = {
    "vendor_contract": VendorContract,
    "customer_contract": CustomerContract,
    "contract_amendment": Amendment,
    "supplier_email": SupplierNotice,
}

SYSTEM = """You extract contract terms for a raw-material purchasing team at an automotive supplier.
Rules:
- Extract only what the document states. Use null when a value is not stated; never infer or compute.
- Percentages as numbers (5% -> 5.0). Shares as fractions (85% -> 0.85).
- Dates as YYYY-MM-DD.
- Map materials and products to the canonical ids provided; use only those ids.
- For every extracted numeric or date field give one evidence item whose quote is copied verbatim
  from the document (keep the original wording and punctuation).
- For values from a table, quote the table row as it appears (e.g. the part number, description and
  prices of that row), never a column header joined to a cell."""


def catalog(db) -> str:
    mats = "\n".join(f"- {m['_id']}: {m['description']} (index {m['index']})" for m in db.materials.find())
    prods = "\n".join(f"- {p['_id']}: {p.get('description') or ''}" for p in db.products.find())
    return f"Canonical materials:\n{mats}\n\nCanonical products:\n{prods}"


def squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def verify(evidence: list[Evidence], text: str) -> list[dict]:
    body = squash(text)
    return [{**e.model_dump(), "verified": squash(e.quote) in body} for e in evidence]


def extract_document(db, doc, cat) -> dict:
    schema = SCHEMAS[doc["kind"]]
    user = f"{cat}\n\nDocument type: {doc['kind']}\n\n---\n{doc['text']}"
    result = llm.parse(SYSTEM, user, schema)
    evidence = verify(result.evidence, doc["text"])
    unverified = [e for e in evidence if not e["verified"]]
    extraction = {
        "status": "needs_review" if unverified else "extracted",
        "model": llm.MODEL,
        "extracted_at": datetime.now(timezone.utc),
        "result": result.model_dump(exclude={"evidence"}),
        "evidence": evidence,
        "unverified_fields": [e["field"] for e in unverified],
    }
    db.documents.update_one({"_id": doc["_id"]}, {"$set": {"extraction": extraction}})
    return extraction


def d(s: str | None) -> datetime | None:
    return datetime.combine(date.fromisoformat(s), datetime.min.time(), timezone.utc) if s else None


def build_contracts(db):
    """Assemble contracts from extracted documents and apply amendments as a terms timeline.

    Identifiers come from the deterministic parse (documents.refs), not from the model.
    """
    docs = list(db.documents.find({"extraction.status": {"$in": ["extracted", "needs_review"]}}))
    db.contracts.drop()
    contracts = {}
    for doc in docs:
        r, kind = doc["extraction"]["result"], doc["kind"]
        if kind not in ("vendor_contract", "customer_contract"):
            continue
        party = doc["refs"]["party_ids"][0] if doc["refs"]["party_ids"] else None
        terms = r.get("price_adjustment") if kind == "vendor_contract" else {"surcharges": r["surcharges"]}
        cid = doc["refs"]["contract_id"]
        contracts[cid] = {
            "_id": cid,
            "type": "vendor" if kind == "vendor_contract" else "customer",
            "party_id": party,
            "party_name": r.get("supplier_name") or r.get("customer_name"),
            "material_ids": r.get("material_ids", []),
            "product_ids": r.get("product_ids", []),
            "signed": d(r["signed"]),
            "expires": d(r["expires"]),
            "summary": {k: v for k, v in r.items()
                        if k not in ("price_adjustment", "surcharges", "contract_id", "levers", "piece_prices")},
            "levers": r.get("levers", []),
            "piece_prices": r.get("piece_prices", []),
            "terms_timeline": [{"effective_from": d(r["signed"]), "terms": terms, "document_id": doc["_id"]}],
            "amendments": [],
            "documents": [doc["_id"]],
            "extraction_status": doc["extraction"]["status"],
        }

    for doc in docs:
        if doc["kind"] != "contract_amendment":
            continue
        r = doc["extraction"]["result"]
        c = contracts.get(doc["refs"]["contract_id"])
        if not c:
            continue
        new_terms = dict(c["terms_timeline"][-1]["terms"] or {})
        for ch in r["changes"]:
            old = new_terms.get(ch["field"])
            new_terms[ch["field"]] = coerce(ch["new_value"], old)
        c["amendments"].append({"id": doc["refs"]["amendment_id"], "effective_from": d(r["effective_date"]),
                                "changes": r["changes"], "document_id": doc["_id"]})
        c["terms_timeline"].append({"effective_from": d(r["effective_date"]), "terms": new_terms,
                                    "document_id": doc["_id"]})
        c["documents"].append(doc["_id"])
        if doc["extraction"]["status"] == "needs_review":
            c["extraction_status"] = "needs_review"

    for c in contracts.values():
        c["terms_timeline"].sort(key=lambda t: t["effective_from"])
        c["current_terms"] = c["terms_timeline"][-1]["terms"]
        db.contracts.replace_one({"_id": c["_id"]}, c, upsert=True)
    return contracts


def coerce(value: str, like):
    """Amendment values arrive as strings; match the type of the value they replace."""
    if isinstance(like, (int, float)) and not isinstance(like, bool):
        num = float(re.sub(r"[^\d.\-]", "", value))
        return int(num) if isinstance(like, int) else num
    return value


def build_notices(db):
    for doc in db.documents.find({"kind": "supplier_email", "extraction.status": {"$ne": "pending"}}):
        r = doc["extraction"]["result"]
        per_kg = r["new_price"] / (1000 if r["unit"] == "t" else 1)
        eur = per_kg if r["currency"] == "EUR" else None
        contract = db.contracts.find_one({"_id": doc["refs"]["contract_id"]})
        db.price_quotes.replace_one({"kind": "supplier_notice", "document_id": doc["_id"]}, {
            "kind": "supplier_notice",
            "document_id": doc["_id"],
            "contract_id": doc["refs"]["contract_id"],
            "vendor_id": contract["party_id"] if contract else None,
            "material_id": r["material_id"],
            "price": {"per_kg": per_kg, "currency": r["currency"], "per_kg_eur": eur},
            "valid_from": d(r["effective_date"]),
            "received_at": doc.get("email", {}).get("sent_at"),
            "claimed_basis": r["claimed_basis"],
            "notice_type": r["notice_type"],
            "offer_valid_until": d(r["offer_valid_until"]),
            "volume_t": r["volume_t"],
            # a price-adjustment claim is verified against the contract formula; a renewal offer is negotiated
            "status": "claimed" if r["notice_type"] == "price_adjustment" else "offer",
            "source": doc["source"],
        }, upsert=True)


def cross_check_customer_prices(db, as_of=datetime(2026, 9, 26, tzinfo=timezone.utc)):
    """Compare the SAP price condition in force today with the contract's Annex A price."""
    run_id = f"extract-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
    db.ingestion_issues.delete_many({"rule": "contract_price_mismatch"})
    found = 0
    for c in db.contracts.find({"type": "customer"}):
        for pp in c.get("piece_prices", []):
            contract_price = pp["price_2026_eur"]
            sap = db.customer_prices.find_one(
                {"customer_id": c["party_id"], "product_id": pp["product_id"],
                 "valid_from": {"$lte": as_of.replace(tzinfo=None)}, "valid_to": {"$gte": as_of.replace(tzinfo=None)}},
                sort=[("valid_from", -1)])
            if not sap or contract_price is None or abs(sap["price_eur"] - contract_price) < 0.005:
                continue
            found += 1
            db.ingestion_issues.insert_one({
                "run_id": run_id, "source": {k: v for k, v in sap["source"].items() if k != "run_id"},
                "rule": "contract_price_mismatch", "severity": "error", "status": "open",
                "message": f"{c['party_id']} / {pp['product_id']}: SAP bills {sap['price_eur']:.2f} EUR but contract "
                           f"{c['_id']} Annex A price from 2026-01-01 is {contract_price:.2f} EUR "
                           f"({sap['price_eur'] - contract_price:+.2f} EUR/pc)",
                "entity": {"customer_id": c["party_id"], "product_id": pp["product_id"], "contract_id": c["_id"]},
                "detected_at": datetime.now(timezone.utc),
            })
    return found


def main(all_docs=False):
    db = get_db()
    cat = catalog(db)
    query = {"kind": {"$in": list(SCHEMAS)}}
    if not all_docs:
        query["extraction.status"] = "pending"
    # contracts first so amendments and notices can refer to them
    order = {"vendor_contract": 0, "customer_contract": 0, "contract_amendment": 1, "supplier_email": 2}
    for doc in sorted(db.documents.find(query), key=lambda x: order[x["kind"]]):
        ex = extract_document(db, doc, cat)
        flag = f"  unverified: {ex['unverified_fields']}" if ex["unverified_fields"] else ""
        print(f"{doc['_id']:<34} {ex['status']:<13} {len(ex['evidence'])} evidence{flag}")
    contracts = build_contracts(db)
    build_notices(db)
    print(f"{cross_check_customer_prices(db)} SAP price conditions disagree with contract piece prices.")
    print(f"\n{len(contracts)} contracts assembled; "
          f"{sum(len(c['amendments']) for c in contracts.values())} amendments applied.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--all", action="store_true")
    main(p.parse_args().all)
