"""Market Intelligence: index prices and FX in four formats, loaded into one time series."""

import csv
from collections import defaultdict
from datetime import datetime

from openpyxl import load_workbook

from rmi.ingest.common import CROSSWALK, RAW, Context, parse_date, parse_number, to_dt

DIR = RAW / "market_intelligence"
METALS = {"Aluminium": "ALU", "Copper": "CU", "Zinc": "ZN"}


def load(ctx: Context):
    points = defaultdict(dict)  # index -> {date: (value, source)}

    def add(index, d, value, src, frequency):
        if d in points[index]:
            prev = points[index][d][0]
            if abs(prev - value) > 1e-9:
                ctx.issue(src, "duplicate_conflicting_value", "warning",
                          f"{index} {d} appears twice with different values ({prev} vs {value}); kept the first",
                          auto_fixed=True, entity={"index": index, "date": d.isoformat()},
                          record={"kept": prev, "dropped": value})
            return False
        points[index][d] = (value, src, frequency)
        return True

    # LME metals: ISO dates, long format
    path = DIR / "lme_metals_daily.csv"
    with path.open() as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            src = ctx.source(path, "market_intelligence", row=i)
            d, _, _ = parse_date(row["date"])
            loaded = add(METALS[row["metal"]], d, float(row["cash_usd_per_t"]), src, "daily")
            ctx.count(path, 1, int(loaded))
    check_gaps(ctx, points, ["ALU", "CU", "ZN"], path)

    # Steel HRC: weekly Excel with a footer row
    path = DIR / "steel_hrc_weekly.xlsx"
    ws = load_workbook(path, data_only=True).active
    for i, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        src = ctx.source(path, "market_intelligence", row=i, sheet=ws.title)
        d, _, _ = parse_date(row[0], prefer="uk")
        value = parse_number(row[1])
        ctx.count(path, 1)
        if d is None or value is None:
            ctx.issue(src, "non_data_row", "info", f"Skipped non-data row: {[c for c in row if c]}",
                      auto_fixed=True)
            continue
        ctx.count(path, 0, int(add("HRC", d, value, src, "weekly")))

    # PA66: monthly, 'Sep-26' labels, decimal comma
    path = DIR / "pa66_europe_monthly.csv"
    with path.open() as f:
        for i, row in enumerate(csv.DictReader(f, delimiter=";"), start=2):
            src = ctx.source(path, "market_intelligence", row=i)
            d = datetime.strptime(row["Month"], "%b-%y").date()
            ctx.count(path, 1, int(add("PA66", d, parse_number(row["PA66 EUR/kg"]), src, "monthly")))

    # Rubber: US dates
    path = DIR / "sgx_smr20_rubber.csv"
    with path.open() as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            src = ctx.source(path, "market_intelligence", row=i)
            d, _, _ = parse_date(row["Date"], prefer="us")
            ctx.count(path, 1, int(add("RUBBER", d, float(row["Settle (USc/kg)"]), src, "daily")))

    # FX reference rates
    path = DIR / "fx_reference_rates.csv"
    with path.open() as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            src = ctx.source(path, "market_intelligence", row=i)
            d, _, _ = parse_date(row["date"])
            add("EURUSD", d, float(row["EURUSD"]), src, "daily")
            ctx.count(path, 1, int(add("EURPLN", d, float(row["EURPLN"]), src, "daily")))

    for index, series in points.items():
        for d, (value, src, frequency) in series.items():
            ctx.docs["market_prices"].append({
                "ts": to_dt(d),
                "meta": {"index": index, "unit": CROSSWALK["index_units"][index],
                         "frequency": frequency, "source": src["file"]},
                "value": value,
            })
    for pair in ("EURUSD", "EURPLN"):
        dates = sorted(points[pair])
        ctx.fx[pair] = (dates, [points[pair][d][0] for d in dates])


def check_gaps(ctx, points, indices, path):
    """A daily series is missing a day if another series from the same feed has it."""
    all_days = set().union(*(points[i].keys() for i in indices))
    for index in indices:
        for d in sorted(all_days - points[index].keys()):
            ctx.issue(ctx.source(path, "market_intelligence"), "missing_observation", "warning",
                      f"{index} has no price for {d} although the feed published other metals that day",
                      entity={"index": index, "date": d.isoformat()})
