"""Long-term semantic memory: negotiation lessons and case reports, recalled by meaning.

agent_memory holds one document per memory. A Vector Search index with automated embeddings
(voyage-4) lets the agent recall "how did this counterparty react last time?" across runs.
Short-term state lives in the LangGraph checkpoints; this is what the agent learns over time.
"""

import time
from datetime import datetime, timezone

from pymongo.operations import SearchIndexModel

from rmi.db import get_db

INDEX = "memory_vector"


def ensure_index(db=None, wait=True):
    db = db if db is not None else get_db()
    if "agent_memory" not in db.list_collection_names():
        db.create_collection("agent_memory")
    if INDEX not in {i["name"] for i in db.agent_memory.list_search_indexes()}:
        db.agent_memory.create_search_index(SearchIndexModel(name=INDEX, type="vectorSearch", definition={"fields": [
            {"type": "autoEmbed", "path": "text", "model": "voyage-4", "modality": "text"},
            {"type": "filter", "path": "party_id"},
            {"type": "filter", "path": "kind"},
        ]}))
    while wait and not all(i.get("queryable") for i in db.agent_memory.list_search_indexes()):
        time.sleep(5)


def remember(text: str, *, kind: str, party_id: str | None = None, ref: str | None = None, db=None):
    db = db if db is not None else get_db()
    db.agent_memory.update_one({"ref": ref} if ref else {"text": text},
                               {"$set": {"text": text, "kind": kind, "party_id": party_id, "ref": ref,
                                         "updated_at": datetime.now(timezone.utc)}}, upsert=True)


def sync_from_negotiations(db=None) -> int:
    """Turn each negotiation record into a memory sentence."""
    db = db if db is not None else get_db()
    n = 0
    for x in db.negotiations.find():
        party = db.parties.find_one({"_id": x["party_id"]}) or {}
        text = (f"{x['date']:%Y-%m-%d} {party.get('name', x['party_id'])} ({x.get('contract_id')}): {x['topic']}. "
                f"Asked: {x.get('ask') or '-'}. Outcome: {x.get('outcome')}. Lesson: {x.get('lessons') or '-'}")
        remember(text, kind="negotiation", party_id=x["party_id"], ref=f"neg-{x['_id']}", db=db)
        n += 1
    return n


def recall(query: str, party_id: str | None = None, k: int = 4, db=None) -> list[dict]:
    db = db if db is not None else get_db()
    stage = {"index": INDEX, "path": "text", "query": {"text": query}, "numCandidates": 100, "limit": k}
    if party_id:
        stage["filter"] = {"party_id": {"$in": [party_id]}}
    return list(db.agent_memory.aggregate([
        {"$vectorSearch": stage},
        {"$project": {"_id": 0, "text": 1, "kind": 1, "party_id": 1, "score": {"$meta": "vectorSearchScore"}}}]))


if __name__ == "__main__":
    ensure_index()
    print(sync_from_negotiations(), "memories synced")
    for m in recall("how does the customer react when we ask for a price increase?", "C-BRIGHTLINE"):
        print(round(m["score"], 3), m["text"])
