"""Unstructured sources: contract PDFs, amendments, supplier emails and analyst notes.

Each document is stored with its full text and split into sections, which become the
retrieval units for Atlas Search / Vector Search. Structured contract terms are extracted
later by the agent's LLM step, with the section text as evidence.
"""

import email
import re
from email.utils import parsedate_to_datetime

from pypdf import PdfReader

from rmi.ingest.common import RAW, Context, norm, rel

CONTRACT_ID = re.compile(r"\b((?:VC|CC)-\d{4}-\d{2,3}|CC-[A-Z]{2}-\d{4}-\d{2})(-A\d+)?\b")
SECTION = re.compile(r"^(\d+\.\s+\S.*|Annex [A-Z]\..*)$")
PRODUCT = re.compile(r"([A-Z][A-Za-z0-9 -]+?) \((FG-[A-Z0-9-]+)\)")


def load(ctx: Context):
    for path in sorted((RAW / "contracts").rglob("*.pdf")):
        text = "\n".join(page.extract_text() for page in PdfReader(path).pages)
        kind = ("contract_amendment" if "amendment" in path.stem.lower()
                else "customer_contract" if "customer" in path.parts else "vendor_contract")
        add(ctx, path, kind, text, title=text.splitlines()[0])

    for path in sorted((RAW / "contracts" / "inbox").glob("*.eml")):
        msg = email.message_from_bytes(path.read_bytes())
        body = msg.get_payload()
        add(ctx, path, "supplier_email", body, title=msg["Subject"], extra={
            "email": {"from": msg["From"], "to": msg["To"], "subject": msg["Subject"],
                      "sent_at": parsedate_to_datetime(msg["Date"])}})

    for path in sorted((RAW / "market_intelligence" / "analyst_notes").glob("*.md")):
        text = path.read_text()
        add(ctx, path, "analyst_note", text, title=text.splitlines()[0].lstrip("# ").strip())


def add(ctx, path, kind, text, title, extra=None):
    src = ctx.source(path, "documents")
    ctx.count(path, 1, 1)
    ids = [m.group(0) for m in CONTRACT_ID.finditer(text + " " + path.stem)]
    contract_id = next((i for i in ids if not re.search(r"-A\d+$", i)), None)
    refs = {"contract_id": contract_id}
    if kind == "contract_amendment":
        refs["amendment_id"] = next((i for i in ids if re.search(r"-A\d+$", i)), None)
    refs["party_ids"] = parties_mentioned(ctx, text)
    for d in ("expires on", "Valid until"):
        m = re.search(rf"{d} (\d{{4}}-\d{{2}}-\d{{2}})", text)
        if m:
            refs["expires"] = m.group(1)

    if kind == "customer_contract":
        for name, pid in PRODUCT.findall(" ".join(text.split())):
            if pid in ctx.docs["products"] and not ctx.docs["products"][pid]["description"]:
                ctx.docs["products"][pid]["description"] = name.strip()
        for pid in refs["party_ids"]:
            if pid in ctx.docs["parties"] and ctx.docs["parties"][pid]["type"] == "customer":
                ctx.docs["parties"][pid]["sources"].append(src)

    if kind in ("vendor_contract", "customer_contract", "contract_amendment") and not contract_id:
        ctx.issue(src, "missing_required_field", "error", f"No contract number found in {rel(path)}")

    ctx.docs["documents"].append({
        "_id": path.stem,
        "kind": kind,
        "title": title,
        "text": text,
        "sections": split_sections(text),
        "refs": refs,
        "extraction": {"status": "pending"},  # structured terms come from the LLM extraction step
        "source": src,
        **(extra or {}),
    })


def parties_mentioned(ctx, text):
    flat = norm(text)
    return [p["_id"] for p in ctx.docs["parties"].values()
            if norm(p["name"]) in flat or any(norm(a) in flat for a in p.get("aliases", []) if len(a) > 6)]


def split_sections(text):
    sections, current = [], {"heading": "Preamble", "lines": []}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if SECTION.match(line):
            sections.append(current)
            current = {"heading": line, "lines": []}
        else:
            current["lines"].append(line)
    sections.append(current)
    return [{"n": i, "heading": s["heading"], "text": " ".join(s["lines"])}
            for i, s in enumerate(sections) if s["lines"]]
