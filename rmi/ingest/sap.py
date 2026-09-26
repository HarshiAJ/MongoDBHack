"""SAP BW extracts: material master, vendor master and purchase order history."""

import csv
import statistics
from collections import defaultdict

from rmi.ingest.common import CROSSWALK, RAW, Context, parse_date, qty_to_kg, to_dt

DIR = RAW / "sap_bw"
OUTLIER_FACTOR = 3.0


def rows(path):
    with path.open(encoding="utf-8") as f:
        yield from enumerate(csv.DictReader(f, delimiter=";"), start=2)


def load(ctx: Context):
    load_materials(ctx)
    load_vendors(ctx)
    load_customers(ctx)
    load_po_history(ctx)


def load_customers(ctx: Context):
    """SAP customer master; free-text aliases come from the crosswalk."""
    path = DIR / "0CUSTOMER_master.csv"
    for i, r in rows(path):
        src = ctx.source(path, "sap_bw", row=i)
        ctx.count(path, 1)
        cid = ctx.resolve_customer(r["NAME1"])
        if not cid:
            ctx.issue(src, "unmapped_identifier", "error", f"SAP customer {r['KUNNR']} '{r['NAME1']}' not in crosswalk")
            continue
        ctx.docs["parties"][cid] = {
            "_id": cid, "type": "customer", "name": r["NAME1"], "country": r["LAND1"], "currency": r["WAERS"],
            "sap_kunnr": r["KUNNR"], "aliases": CROSSWALK["customers"][cid]["aliases"], "sources": [src],
        }
        ctx.customer_by_kunnr[r["KUNNR"]] = cid
        ctx.count(path, 0, 1)


def load_price_conditions(ctx: Context, as_of):
    """Customer sales price conditions (PR00). Needs products, so it runs after the BOM."""
    path = DIR / "A305_sales_price_conditions.csv"
    latest = {}
    for i, r in rows(path):
        src = ctx.source(path, "sap_bw", row=i)
        ctx.count(path, 1)
        cid, pid = ctx.customer_by_kunnr.get(r["KUNNR"]), r["MATNR"]
        if not cid or pid not in ctx.docs["products"]:
            ctx.issue(src, "unmapped_identifier", "error", f"Price condition for unknown customer/part {r['KUNNR']}/{pid}")
            continue
        valid_from, _, _ = parse_date(r["DATAB"])
        valid_to, _, _ = parse_date(r["DATBI"])
        doc = {
            "customer_id": cid, "product_id": pid, "condition": r["KSCHL"],
            "price_eur": float(r["KBETR"]) / float(r["KPEIN"]), "currency": r["KONWA"],
            "valid_from": to_dt(valid_from), "valid_to": to_dt(valid_to), "source": src,
        }
        ctx.docs["customer_prices"].append(doc)
        ctx.count(path, 0, 1)
        key = (cid, pid)
        if key not in latest or doc["valid_from"] > latest[key]["valid_from"]:
            latest[key] = doc
    year_start = as_of.replace(month=1, day=1)
    for (cid, pid), doc in latest.items():
        if doc["valid_from"] < year_start:
            ctx.issue(doc["source"], "stale_value", "warning",
                      f"{cid} / {pid}: newest SAP price condition dates from {doc['valid_from']:%Y-%m-%d} "
                      f"({doc['price_eur']:.2f} EUR); no {as_of.year} condition although other parts have one. "
                      f"Check the annual price-down against the contract.",
                      entity={"customer_id": cid, "product_id": pid})


def load_materials(ctx: Context):
    path = DIR / "0MATERIAL_master.csv"
    for i, r in rows(path):
        src = ctx.source(path, "sap_bw", row=i)
        ctx.count(path, 1)
        mid = ctx.matnr_to_material.get(r["MATNR"])
        if not mid:
            ctx.issue(src, "unmapped_identifier", "warning",
                      f"SAP material {r['MATNR']} '{r['MAKTX']}' has no RMI Tracker mapping"
                      + (" and looks obsolete" if "OBSOLETE" in r["MAKTX"].upper() else ""),
                      record=r)
            continue
        m = ctx.docs["materials"][mid]
        m["description"] = r["MAKTX"]
        m["material_group"] = r["MATKL"]
        if r["MEINS"] != "KG":
            ctx.issue(src, "unit_mismatch", "error", f"{mid} base unit {r['MEINS']} is not KG")
        m["sources"].append(src)
        ctx.count(path, 0, 1)


def load_vendors(ctx: Context):
    path = DIR / "0VENDOR_master.csv"
    for i, r in rows(path):
        src = ctx.source(path, "sap_bw", row=i)
        vid = f"V-{r['LIFNR'].lstrip('0')}"
        ctx.docs["parties"][vid] = {
            "_id": vid, "type": "vendor", "name": r["NAME1"], "country": r["LAND1"],
            "currency": r["WAERS"], "sap_lifnr": r["LIFNR"], "aliases": [], "sources": [src],
        }
        ctx.vendor_by_lifnr[r["LIFNR"]] = vid
        ctx.count(path, 1, 1)


def load_po_history(ctx: Context):
    path = DIR / "2LIS_02_ITM_po_history.csv"
    lines, seen = [], {}
    for i, r in rows(path):
        src = ctx.source(path, "sap_bw", row=i)
        ctx.count(path, 1)
        key = f"{r['EBELN']}-{r['EBELP']}"
        if key in seen:
            same = seen[key] == r
            ctx.issue(src, "duplicate_record", "warning" if same else "error",
                      f"PO line {key} appears again (row {i})" + ("" if same else " with different values"),
                      auto_fixed=same, entity={"po_line": key})
            continue
        seen[key] = r

        flags = []
        mid = ctx.matnr_to_material.get(r["MATNR"])
        if not mid:
            ctx.issue(src, "unmapped_identifier", "error", f"PO {key}: unknown MATNR {r['MATNR']}")
            continue
        vid = ctx.vendor_by_lifnr.get(r["LIFNR"])
        if not vid:
            flags.append("missing_vendor")
            ctx.issue(src, "missing_required_field", "error",
                      f"PO {key}: vendor (LIFNR) is empty; line quarantined until the vendor is confirmed",
                      entity={"po_line": key, "material_id": mid})

        qty = float(r["MENGE"])
        qty_kg = qty_to_kg(qty, r["MEINS"])
        if r["MEINS"] != "KG":
            ctx.issue(src, "unit_conversion", "info",
                      f"PO {key}: quantity {qty} {r['MEINS']} converted to {qty_kg:,.0f} KG",
                      auto_fixed=True, entity={"po_line": key})

        d, _, _ = parse_date(r["BEDAT"])
        peinh = float(r["PEINH"])
        per_kg = float(r["NETPR"]) / peinh  # NETPR is the price per PEINH units of the order unit (KG)
        lines.append({
            "_id": key,
            "po_number": r["EBELN"],
            "material_id": mid,
            "vendor_id": vid,
            "plant": r["WERKS"],
            "qty_kg": qty_kg,
            "price": {"per_kg": round(per_kg, 6), "currency": r["WAERS"],
                      "per_kg_eur": round(ctx.to_eur(per_kg, r["WAERS"], d), 6)},
            "order_date": to_dt(d),
            "raw": {"NETPR": r["NETPR"], "PEINH": r["PEINH"], "MENGE": r["MENGE"], "MEINS": r["MEINS"]},
            "quality": {"status": "quarantined" if flags else "ok", "flags": flags},
            "source": src,
        })
        ctx.count(path, 0, 1)

    # Price outliers against the median for the same material and vendor
    groups = defaultdict(list)
    for line in lines:
        groups[(line["material_id"], line["vendor_id"])].append(line["price"]["per_kg_eur"])
    medians = {k: statistics.median(v) for k, v in groups.items()}
    for line in lines:
        med = medians[(line["material_id"], line["vendor_id"])]
        ratio = line["price"]["per_kg_eur"] / med
        if ratio > OUTLIER_FACTOR or ratio < 1 / OUTLIER_FACTOR:
            line["quality"] = {"status": "quarantined", "flags": line["quality"]["flags"] + ["price_outlier"]}
            ctx.issue(line["source"], "price_outlier", "error",
                      f"PO {line['_id']}: {line['price']['per_kg_eur']:.2f} EUR/kg is {ratio:.1f}x the median "
                      f"{med:.2f} EUR/kg for {line['material_id']} (likely decimal slip)",
                      entity={"po_line": line["_id"], "material_id": line["material_id"]},
                      record=line["raw"])
    ctx.docs["po_lines"].extend(lines)
