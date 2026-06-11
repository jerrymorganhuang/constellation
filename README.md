# Constellation

Constellation is a graph-based relationship explorer for U.S. public companies, executives, and board members.

The goal is to build a lightweight investor research tool that allows users to search a company or person and visualize relationships such as CEO, CFO, and board membership.

## V0 Scope

The first validation scope is SOXX constituents only.

V0 is implemented as a standalone Python pipeline that:

- maps tickers to SEC CIKs
- retrieves the latest DEF 14A proxy filing
- downloads and caches filing HTML
- parses CEO, CFO, and board members
- exports graph-ready CSV files

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
- `parse_log.csv`
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

- Use SEC EDGAR as the primary source
- Use Python first
- Keep the pipeline standalone
- Do not build frontend yet
- Do not use GPT API, LLMs, RAG, vector databases, or paid data providers
- Future target stack: Neo4j, FastAPI, React, Cytoscape.js

## Long-Term Universe

After validation, expand to:

- S&P 500
- Nasdaq 100
- SOXX
- Russell 2000
