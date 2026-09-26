"""Retrieval over contract clauses, supplier emails and analyst notes.

doc_chunks holds one document per section. Two search indexes cover it:
  chunks_vector  Vector Search with automated embeddings (Voyage, generated inside Atlas)
  chunks_text    Atlas Search, typo-tolerant keyword matching
hybrid_search() merges both with $rankFusion (MongoDB 8.0+).

    python -m rmi.search "which contracts let us pass aluminium costs to the customer?"
"""

import re
import sys
import time

from pymongo.operations import SearchIndexModel

from rmi.db import get_db

EMBED_MODEL = "voyage-4"
VECTOR_INDEX, TEXT_INDEX = "chunks_vector", "chunks_text"

INDEX_TERMS = {
    "ALU": r"alumin", "CU": r"copper|\bcu\b", "ZN": r"\bzinc\b", "HRC": r"steel|\bhrc\b",
    "PA66": r"pa66|polyamide|resin|polymer", "RUBBER": r"rubber|elastomer",
}


def build_chunks(documents: list[dict]) -> list[dict]:
    chunks = []
    for doc in documents:
        for s in doc["sections"]:
            text = s["text"]
            chunks.append({
                "_id": f"{doc['_id']}#{s['n']}",
                "document_id": doc["_id"],
                "kind": doc["kind"],
                "contract_id": doc["refs"].get("contract_id"),
                "party_ids": doc["refs"].get("party_ids", []),
                "indices": [k for k, pat in INDEX_TERMS.items() if re.search(pat, text, re.I)],
                "title": doc["title"],
                "heading": s["heading"],
                "text": text,
                # what gets embedded and keyword-indexed: section text with its context
                "content": f"{doc['title']} - {s['heading']}: {text}",
                "source": doc["source"],
            })
    return chunks


def ensure_indexes(db=None, wait=True, timeout=300):
    db = db if db is not None else get_db()
    coll = db.doc_chunks
    existing = {i["name"] for i in coll.list_search_indexes()}
    models = []
    if VECTOR_INDEX not in existing:
        models.append(SearchIndexModel(name=VECTOR_INDEX, type="vectorSearch", definition={"fields": [
            {"type": "autoEmbed", "path": "content", "model": EMBED_MODEL, "modality": "text"},
            {"type": "filter", "path": "kind"},
            {"type": "filter", "path": "contract_id"},
            {"type": "filter", "path": "party_ids"},
            {"type": "filter", "path": "indices"},
        ]}))
    if TEXT_INDEX not in existing:
        models.append(SearchIndexModel(name=TEXT_INDEX, type="search", definition={"mappings": {
            "dynamic": False,
            "fields": {
                "content": {"type": "string", "analyzer": "lucene.english"},
                "heading": {"type": "string"},
                "kind": {"type": "token"},
                "contract_id": {"type": "token"},
                "party_ids": {"type": "token"},
                "indices": {"type": "token"},
            },
        }}))
    if models:
        coll.create_search_indexes(models)
    if wait:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = {i["name"]: i.get("queryable", False) for i in coll.list_search_indexes()}
            if status.get(VECTOR_INDEX) and status.get(TEXT_INDEX):
                return status
            time.sleep(5)
        raise TimeoutError(f"search indexes not queryable after {timeout}s: {status}")


def _filters(kind=None, contract_id=None, party_id=None, index=None):
    """Same filter expressed for $vectorSearch (MQL) and $search (compound.filter)."""
    mql, lexical = {}, []
    for field, value in (("kind", kind), ("contract_id", contract_id), ("party_ids", party_id), ("indices", index)):
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        mql[field] = {"$in": values}
        lexical.append({"in": {"path": field, "value": values}})
    return mql, lexical


def hybrid_search(query: str, k=5, *, kind=None, contract_id=None, party_id=None, index=None,
                  vector_weight=1.0, text_weight=1.0, db=None):
    """Top-k chunks by reciprocal rank fusion of semantic and keyword search."""
    db = db if db is not None else get_db()
    mql, lexical = _filters(kind, contract_id, party_id, index)
    vector = {"index": VECTOR_INDEX, "path": "content", "query": {"text": query},
              "numCandidates": max(100, 20 * k), "limit": 4 * k}
    if mql:
        vector["filter"] = mql
    text = {"index": TEXT_INDEX, "compound": {
        "should": [
            {"text": {"query": query, "path": "content", "fuzzy": {"maxEdits": 1}}},
            {"text": {"query": query, "path": "heading", "score": {"boost": {"value": 2}}}},
        ],
        "minimumShouldMatch": 1,
        **({"filter": lexical} if lexical else {}),
    }}
    pipeline = [
        {"$rankFusion": {
            "input": {"pipelines": {
                "semantic": [{"$vectorSearch": vector}],
                "keyword": [{"$search": text}, {"$limit": 4 * k}],
            }},
            "combination": {"weights": {"semantic": vector_weight, "keyword": text_weight}},
            "scoreDetails": True,
        }},
        {"$limit": k},
        {"$project": {"_id": 1, "document_id": 1, "kind": 1, "contract_id": 1, "heading": 1, "text": 1,
                      "indices": 1, "score": {"$meta": "score"}}},
    ]
    return list(db.doc_chunks.aggregate(pipeline))


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "which customer contracts pass aluminium cost increases through?"
    for hit in hybrid_search(q):
        print(f"{hit['score']:.4f}  {hit['_id']:<40} {hit['heading']}\n        {hit['text'][:160]}")
