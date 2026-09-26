"""Relationship-aware context (GraphRAG-style) with $graphLookup.

entity_graph links materials -> products -> customers and materials -> vendor contracts ->
vendors, plus customer contracts. impact(material) walks the graph to answer
"who is affected if this material moves?" with the connected contracts as context.
"""

from rmi.db import get_db


def build(db=None) -> int:
    db = db if db is not None else get_db()
    nodes = {}

    def node(_id, type_, name):
        nodes.setdefault(_id, {"_id": _id, "type": type_, "name": name, "edges": []})

    def edge(a, b, rel):
        nodes[a]["edges"].append({"to": b, "rel": rel})

    for p in db.parties.find():
        node(p["_id"], p["type"], p["name"])
    for m in db.materials.find():
        node(m["_id"], "material", m.get("description"))
    for c in db.contracts.find():
        node(c["_id"], f"{c['type']}_contract", c["party_name"])
        edge(c["_id"], c["party_id"], "with")
        for mid in c.get("material_ids", []):
            edge(mid, c["_id"], "bought_under")
    for p in db.products.find():
        node(p["_id"], "product", p.get("description"))
        for m in p["materials"]:
            edge(m["material_id"], p["_id"], "used_in")
    for c in db.contracts.find({"type": "customer"}):
        for pid in c.get("product_ids", []):
            if pid in nodes:
                edge(pid, c["_id"], "sold_under")
    db.entity_graph.drop()
    db.entity_graph.insert_many(list(nodes.values()))
    return len(nodes)


def impact(material_id: str, db=None) -> dict:
    db = db if db is not None else get_db()
    res = list(db.entity_graph.aggregate([
        {"$match": {"_id": material_id}},
        {"$graphLookup": {"from": "entity_graph", "startWith": "$edges.to", "connectFromField": "edges.to",
                          "connectToField": "_id", "as": "reached", "maxDepth": 2, "depthField": "hops"}},
        {"$project": {"reached": {"_id": 1, "type": 1, "name": 1, "hops": 1}}},
    ]))
    if not res:
        return {}
    by_type = {}
    for n in sorted(res[0]["reached"], key=lambda n: n["hops"]):
        by_type.setdefault(n["type"], []).append({"id": n["_id"], "name": n["name"], "hops": n["hops"]})
    return {"material_id": material_id, "connected": by_type}


if __name__ == "__main__":
    print(build(), "nodes")
    print(impact("RM-CU-ROD"))
