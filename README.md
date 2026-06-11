# Constellation

Constellation is a graph-based relationship explorer for U.S. public companies, executives, and board members.

The goal is to build a lightweight investor research tool that allows users to search a company or person and visualize relationships such as CEO, CFO, executive-officer, and board membership.

## V0 Scope

The first validation scope is SOXX constituents only.

V0 is implemented as a standalone Python pipeline that:

- maps tickers to SEC CIKs
- retrieves the latest 10-K annual report filing only
- downloads and caches filing HTML
- parses the 10-K signature page as the primary source for CEO, CFO, and director relationships
- compares signature-page extraction with Item 10 / Directors, Executive Officers and Corporate Governance extraction
- extracts CEO, CFO, clearly listed executive officers, and board members
- exports graph-ready CSV files

Constellation V0 intentionally uses 10-K filings as its primary and only SEC filing source. It does not use proxy filings or any fallback filing type because V0 prioritizes consistent SOXX coverage for the most important Person ↔ Company relationships over proxy-level completeness.

## Run V0

Install the Python dependencies, set a SEC-compliant User-Agent, and run the script:

```bash
python -m pip install -r requirements.txt
export CONSTELLATION_SEC_USER_AGENT="ConstellationV0 research@example.com"
python build_constellation_soxx.py
```

The script writes all V0 outputs under `data/constellation_v0/`:

- `company_nodes.csv`
- `person_nodes.csv`
- `edges.csv`
- `parse_log.csv` with signature-page vs. Item 10 extraction counts and success flags
- `cache/` for downloaded SEC and universe files

Useful development options:

```bash
python build_constellation_soxx.py --tickers NVDA,AMD
python build_constellation_soxx.py --limit 5
python build_constellation_soxx.py --output-dir /tmp/constellation_v0_test
```

## Graph Model

Node types:

- Company
- Person

Relationship types:

- CEO_OF
- CFO_OF
- EXECUTIVE_OFFICER_OF
- BOARD_OF

## Output Schema

Company node fields:

- node_id
- node_type
- ticker
- cik
- company_name

Person node fields:

- node_id
- node_type
- name
- normalized_name

Edge fields:

- source_node_id
- target_node_id
- relationship_type
- source_person
- target_company
- ticker
- filing_date
- filing_url

## Rules

- Use SEC EDGAR 10-K annual report filings as the only V0 filing source
- Prefer precision-first 10-K signature-page rows for CEO, CFO, and director extraction before Item 10 fallback rows
- Use the latest 10-K filing for each company
- Keep SEC User-Agent handling and local filing cache
- Keep the V0 outputs as `company_nodes.csv`, `person_nodes.csv`, `edges.csv`, and `parse_log.csv`
- Use Python first
- Keep the pipeline standalone
- Do not build frontend yet
- Do not use GPT API, LLMs, RAG, vector databases, paid data providers, or alternate filing fallbacks
- Future target stack: Neo4j, FastAPI, React, Cytoscape.js

## Long-Term Universe

After validation, expand to:

- S&P 500
- Nasdaq 100
- SOXX
- Russell 2000
