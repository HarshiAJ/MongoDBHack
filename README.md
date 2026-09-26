# RMI Agent: raw material intelligence for automotive purchasing

An agent that watches raw material markets, reads supplier and customer contracts, and turns
a price move into concrete purchasing actions: which supplier price increases are valid, what
they cost across the bill of materials, and how much can be recovered from customers before
claim deadlines pass.

It sits on top of the existing data landscape (Sales, BOM, Purchasing, Market Intelligence,
SAP BW, Excel, RMI Tracker) and replaces manual consolidation and after-the-fact validation
with ingestion that validates on arrival.

## The problem

- Inputs come from seven sources in inconsistent formats, units, currencies and identifiers.
- Validation is manual, so errors show up only after the consolidation workflow has run.
- Contract terms (index links, triggers, caps, notice periods, customer pass-through) live in
  PDFs, so nobody sees the full exposure when a metal price spikes.

## Demo scenario

LME aluminium rises about 12% from July to September 2026 and copper climbs to about 8% above its reference. On 22 Sep
AluCast emails a new price of **3.38 EUR/kg**. The agent:

1. Picks up the price move (MongoDB change stream on `market_ticks`) and the email.
2. Checks contract VC-2025-014: the formula gives **3.23 EUR/kg**, so the notice overcharges.
3. Calculates the cost impact through the multi-level BOM and the sales forecast.
4. Works out recovery per customer: Nordwerk recovers 100% next quarter, Brightline 80% of
   aluminium and copper but no PA66, and Sakura aluminium only, capped at 10%.
5. Raises actions (a counter-notice, surcharge claims with deadlines, and renewals for expiring
   contracts) and waits for a buyer to approve. Its state is checkpointed, so it can resume.

## MongoDB usage

| Need | Feature |
|---|---|
| Canonical model of materials, BOM, contracts, prices | Document model, schema validation |
| Clause retrieval from contracts and analyst notes | Atlas Vector Search (automated embeddings) plus Atlas Search, hybrid, reranked |
| React to price moves in real time | Change streams |
| Index history and trends | Time series collections |
| Agent state: suspend, resume, human approval | LangGraph MongoDB checkpointer |
| Long-term memory (past decisions, buyer corrections, vendor behavior) | Memory collection plus vector search |
| Lineage from source row to insight | Provenance fields on every record |

## Data model (database `rmi`)

Built by `python -m rmi.ingest` (use `--dry-run` to validate without writing). Every record
carries a `source` block (system, file, sheet, row, run_id) for lineage.

| Collection | One document per | Notes |
|---|---|---|
| `materials` | raw material | SAP number, tracker name and aliases in `identifiers`; embedded thresholds and plant yields |
| `parties` | vendor or customer | Polymorphic on `type`; SAP vendor number, aliases |
| `products` | finished good | Embedded BOM plus exploded `materials[].kg_per_unit` (computed pattern) |
| `po_lines` | SAP PO line | Normalized to EUR/kg; `quality.status` = ok or quarantined |
| `price_quotes` | buyer price or manual override | Verbal/unapproved status kept, never silently used |
| `forecasts` | customer x product x month | Customer spellings resolved via crosswalk |
| `market_prices` | index observation | Time series collection (`meta.index`, `ts`) |
| `index_monthly` | index x month | Monthly averages, the input to every price clause |
| `documents` | contract, amendment, email, note | Full text plus `sections[]` for search; `extraction.status` for LLM term extraction |
| `ingestion_issues` | data quality finding | rule, severity, status (open / auto_fixed / resolved) |
| `ingestion_runs` | pipeline run | File hashes, row counts in and loaded |

## Sample data

`python scripts/generate_sample_data.py` generates a fictional Tier-1 supplier dataset
(seed 42) in `data/raw/`:

| Source | Files | Deliberate issues |
|---|---|---|
| Sales | EU CSV (`;`, decimal comma), US XLSX | Customer name variants, missing prefix, blank row |
| BOM | Multi-level XLSX | g/lb units, superseded version |
| Purchasing | Buyer price list XLSX | Conflicting and stale prices, mixed units and date formats |
| Market Intelligence | LME CSV, HRC XLSX, PA66 CSV, rubber CSV, FX, analyst notes | Gaps, duplicates, footer rows |
| SAP BW | Material/vendor master, PO history | `PEINH` price units, PLN, 10x typo, duplicate, missing vendor |
| Excel inputs | Yield factors | Fraction vs percent |
| RMI Tracker | Map, overrides, monthly sheet with formulas | Lost leading zeros, unapproved override, empty month |
| Contracts | 8 vendor, 1 amendment, 3 customer PDFs, 1 email | Amendment supersedes clause, expiries, overcharge |

The expected findings are in [data/GROUND_TRUTH.md](data/GROUND_TRUTH.md).
`data/reference/master_data.json` holds the clean master data, for evaluation only.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # add your Atlas sandbox URI and OpenAI key
.venv/bin/python scripts/generate_sample_data.py
.venv/bin/python -m rmi.ingest
```
