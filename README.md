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
| `contracts` | vendor or customer contract | Built by `python -m rmi.extract`: LLM-extracted terms, amendments applied as `terms_timeline`, `current_terms` |
| `doc_chunks` | document section | Retrieval unit with filters (`kind`, `contract_id`, `party_ids`, `indices`); two search indexes |
| `customer_prices` | SAP sales price condition | Cross-checked against contract Annex A piece prices |
| `negotiations` | past or agent-initiated negotiation | Counterparty behaviour: the agent's long-term memory |
| `plans` / `plan_lines` | plan version / month x customer x part | FY2027 budget; the agent adds latest-estimate versions |
| `agent_cases`, `agent_actions` | case / approved action | Case status, report, forecast; drafts ready to send |
| `contract_changes` | accepted agreement | Re-applied to contracts after every rebuild |
| `ingestion_issues` | data quality finding | rule, severity, status (open / auto_fixed / resolved) |
| `ingestion_runs` | pipeline run | File hashes, row counts in and loaded |

### Contract extraction

`python -m rmi.extract` sends each contract, amendment and supplier email to the LLM with a
typed schema (structured outputs). Every numeric or date field must come with a verbatim
quote. Quotes are checked against the source text, and a document with an unverifiable
quote is marked `needs_review` rather than trusted. Contract numbers come from the
deterministic parse, not from the model. On the sample data: 181/181 quotes verified,
0 mismatches against ground truth.

### Search

`rmi/search.py` builds two indexes on `doc_chunks` during ingestion:

- `chunks_vector`: Vector Search with **automated embeddings** (`autoEmbed`, `voyage-4`).
  Atlas generates and maintains the embeddings, so there is no embedding pipeline in this repo.
- `chunks_text`: Atlas Search with the English analyzer and fuzzy matching.

`hybrid_search()` merges the two with `$rankFusion` and applies the same filters to both.
Try it with `python -m rmi.search "deadline to file a raw material surcharge claim"`.

## The agent

`rmi/engine/` does every calculation deterministically: contract price formulas, margins and
surcharge recovery, opportunities with deadlines, and the FY2027 re-forecast vs budget.
`rmi/agent/` is a LangGraph workflow on top, checkpointed in MongoDB (`agent_checkpoints`):

```
review    sense -> investigate -> propose -> [buyer approval] -> execute -> replan -> report
response  interpret -> [buyer approval] -> apply -> replan -> report
```

- **sense**: margin watch and opportunity scan (supplier claims, decreases owed, meet-competition,
  renewals, hardship, price review, price-down waiver, billing errors)
- **investigate**: clause evidence via hybrid search, plus negotiation memory per counterparty
- **propose**: the LLM drafts one action per finding using only the engine's figures
- **approval**: `interrupt()`; the case waits in MongoDB and resumes from any process
- **execute**: approved emails become drafts ready to send, and internal tasks are opened.
  Nothing is sent automatically.
- **response**: a counterparty reply is read into structured agreements, checked against the
  formula, confirmed by the buyer, applied to the contract (`contract_changes`), and the plan is re-forecast

```bash
.venv/bin/python -m rmi.agent review                    # opens a case, pauses for approval
.venv/bin/python -m rmi.agent approve CASE-...          # or: approve CASE-... 0 3 5
.venv/bin/python -m rmi.agent respond V-100101 data/demo/alucast_reply_2026-09-29.txt
```

## MongoDB resources used

| Resource | Where in this project |
|---|---|
| Atlas Hackathon Sandbox | All data, indexes, checkpoints and memory live in the sandbox cluster (`rmi` database) |
| MongoDB Agent Skills | Installed via the MongoDB Claude plugin; schema-design and search-and-ai skills shaped the data model and indexes |
| MongoDB MCP Server | Connected in Claude Code; used to look up the `autoEmbed` index syntax and inspect the live cluster |
| Natural Language to MongoDB Queries | Chat tool `query_data` (`rmi/agent/chat.py`): the LLM writes read-only aggregation pipelines from questions, given collection and field descriptions |
| Data modeling | Computed pattern (exploded BOM, monthly index averages), polymorphic `parties`/`contracts`, document versioning (`plans`, `terms_timeline`), `$jsonSchema` validation, time series `market_prices` |
| Vector Search + Automated Embeddings | `chunks_vector` and `memory_vector` indexes use `autoEmbed` (voyage-4); no embedding pipeline in the code |
| Atlas Search | `chunks_text` (English analyzer, fuzzy), fused with vector search via `$rankFusion` (`rmi/search.py`) |
| Agent with memory and function calling | Chat assistant with 9 tools (`rmi/agent/chat.py`); long-term semantic memory of negotiations and case reports (`rmi/agent/memory.py`) |
| Chat memory (LangChain + MongoDB) | Chat threads persisted with the MongoDB checkpointer, one thread per conversation |
| LangGraph + MongoDB state | `MongoDBSaver` checkpoints; cases suspend at `interrupt()` for buyer approval and resume from any process (`rmi/agent/case.py`) |
| State vs memory | Short-term: checkpoints per case/thread. Long-term: `agent_memory`, `negotiations`, `contract_changes` |
| Build AI agents with MongoDB | Retrieval (clauses, memory, graph) alongside tools (price engine, re-forecast) and actions |
| GraphRAG | `entity_graph` + `$graphLookup` (`rmi/graph.py`) for relationship-aware context: material -> products -> customer contracts -> customers, and vendor contracts |
| Change streams | `rmi/agent/watcher.py` opens a case when prices or supplier notices change |

## Run the app

```bash
.venv/bin/uvicorn rmi.app:app --port 8000        # http://localhost:8000
.venv/bin/python -m rmi.agent.watcher           # live trigger (change streams), separate terminal
.venv/bin/python scripts/simulate_price_move.py ALU 3.0   # simulate a market move
```

Tabs for Purchasing and Sales: **Materials** (index trends, contract price vs quote and budget),
**Margins** (customer contracts vs vendor contracts per part), **Levers** (customer and vendor
renegotiation with value and deadline), **Forecast vs Budget** (bridge, monthly, accepted changes),
**Agent cases** (approve proposals, record counterparty responses) and **Ask** (chat assistant).

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
| Contracts | 8 vendor, 1 amendment, 3 customer PDFs, 2 emails | Amendment supersedes clause, expiries, overcharge |
| Commercial | SAP customer master and price conditions, standard costs, RFQ responses, negotiation log | SAP still bills a 2025 price |
| Finance | FY2027 budget (assumptions, plan prices, monthly plan P&L) | Plan built on June 2026 prices |

The commercial layer supports four purchasing scenarios: margin watch (contract prices vs.
material cost), customer renegotiation (hardship, price review, waiver clauses), vendor
renegotiation (meet-competition, rebates, renewals, RFQs) and re-planning against the budget.
`scripts/check_extraction.py` checks the extracted contract data against ground truth.

The expected findings are in [data/GROUND_TRUTH.md](data/GROUND_TRUTH.md).
`data/reference/master_data.json` holds the clean master data, for evaluation only.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # add your Atlas sandbox URI and OpenAI key
.venv/bin/python scripts/generate_sample_data.py
.venv/bin/python -m rmi.ingest
.venv/bin/python -m rmi.extract
```
