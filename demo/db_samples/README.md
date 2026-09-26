# Sample documents from the `rmi` database

Exported by `scripts/export_samples.py` from the Atlas cluster. Long text is shortened.

| File | Collection | What it shows | Documents in collection |
|---|---|---|---|
| [01_materials.json](01_materials.json) | `materials` | Canonical raw material: SAP number, aliases, thresholds, plant yields, lineage | 8 |
| [02_products.json](02_products.json) | `products` | Finished part with embedded BOM and exploded kg per material (computed pattern) | 5 |
| [03_contracts_vendor.json](03_contracts_vendor.json) | `contracts` | Supplier contract: LLM-extracted terms, amendment timeline, levers, agreed prices | 11 |
| [04_contracts_customer.json](04_contracts_customer.json) | `contracts` | Customer contract: surcharge rules, piece prices, renegotiation levers | 11 |
| [05_documents.json](05_documents.json) | `documents` | Source document: full text, sections, verified extraction evidence | 17 |
| [06_doc_chunks.json](06_doc_chunks.json) | `doc_chunks` | Search unit for hybrid search (content is auto-embedded by Atlas) | 98 |
| [07_market_prices.json](07_market_prices.json) | `market_prices` | Time series collection: one index observation | 2,829 |
| [08_index_monthly.json](08_index_monthly.json) | `index_monthly` | Computed monthly index averages used by every price formula | 168 |
| [09_po_lines.json](09_po_lines.json) | `po_lines` | SAP purchase order line normalised to EUR/kg, with quality status | 385 |
| [10_price_quotes.json](10_price_quotes.json) | `price_quotes` | Supplier notice (claimed), RFQ offer and buyer price | 18 |
| [11_forecasts.json](11_forecasts.json) | `forecasts` | Sales forecast line (customer spelling resolved) | 96 |
| [12_customer_prices.json](12_customer_prices.json) | `customer_prices` | SAP sales price condition (cross-checked against contract Annex A) | 15 |
| [13_plans.json](13_plans.json) | `plans` | Plan versions: approved budget and latest estimate with bridge | 2 |
| [14_plan_lines.json](14_plan_lines.json) | `plan_lines` | Plan line: month x customer x part | 192 |
| [15_ingestion_issues.json](15_ingestion_issues.json) | `ingestion_issues` | Data-quality findings with rule, severity and lineage | 33 |
| [16_negotiations.json](16_negotiations.json) | `negotiations` | Negotiation history (seed of long-term memory) | 6 |
| [17_agent_memory.json](17_agent_memory.json) | `agent_memory` | Long-term semantic memory (text is auto-embedded) | 42 |
| [18_entity_graph.json](18_entity_graph.json) | `entity_graph` | Graph node used by $graphLookup | 35 |
| [19_agent_cases.json](19_agent_cases.json) | `agent_cases` | Agent case: trigger, status, report, forecast | 7 |
| [20_agent_actions.json](20_agent_actions.json) | `agent_actions` | Approved action: draft email ready to send, with clause references | 29 |
| [21_contract_changes.json](21_contract_changes.json) | `contract_changes` | Accepted agreement applied to a contract | 2 |
| [22_parties.json](22_parties.json) | `parties` | Vendor and customer (polymorphic collection) | 11 |
| [23_search_indexes.json](23_search_indexes.json) | search indexes | Vector (autoEmbed voyage-4) and Atlas Search index definitions | |
