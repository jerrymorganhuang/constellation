# Constellation

Constellation is a graph-based relationship explorer for U.S. public companies, executives, and board members.

The goal is to build a lightweight investor research tool that allows users to search a company or person and visualize relationships such as CEO, CFO, executive-officer, and board membership.


## Company Master V1

The Company Master module builds the primary company reference store at `data/constellation.db` and exports a review CSV at `data/companies.csv`. SQLite is the primary data store for Constellation V1; CSV files are review/export artifacts generated from the builder output. Both generated files are intentionally ignored by git so the repository stays lightweight and reviewable. It supports the `SPX`, `NDX`, `SOXX`, `RUSSELL1000`, and `RUSSELL2000` universes. Universe membership is stored directly on each company as a semicolon-separated string, such as `SPX;NDX;SOXX`; no separate membership table is created.


Repository policy for generated files:

- Commit source code, documentation, dependency manifests, and documented source metadata such as `data/universes/sources.yml`.
- Do not commit generated SQLite databases, CSV exports, caches, or local virtual environments.
- Regenerate `data/constellation.db`, `data/companies.csv`, and `data/universe_audit.json` locally with `python scripts/build_companies.py` when needed.

Run the builder from the repository root:

```bash
python scripts/build_companies.py
```

The script fetches universe membership from documented rule-based public sources in `data/universes/sources.yml`, enriches company name, sector, industry, and description with `yfinance`, derives a deterministic short description, rebuilds the `companies` table in SQLite, exports `data/companies.csv`, writes `data/universe_audit.json`, and validates the generated outputs. Missing yfinance metadata is left blank/null. Source fetch failures are reported as skipped universes; the build completes as long as at least one universe is loaded and never falls back to undocumented local universe files.

SQLite schema:

```sql
CREATE TABLE IF NOT EXISTS companies (
    ticker TEXT PRIMARY KEY,
    company_name TEXT,
    universe TEXT NOT NULL,
    sector TEXT,
    industry TEXT,
    description TEXT,
    description_short TEXT,
    updated_at TEXT NOT NULL
);
```

CSV schema:

```csv
ticker,company_name,universe,sector,industry,description,description_short,updated_at
```

## V0 Scope

The first validation scope was SOXX constituents. The same signature-page extraction path can now also run against an explicit universe CSV, including the checked-in Nasdaq 100 universe at `data/universes/nasdaq100.csv`.

V0 is implemented as a standalone Python pipeline that:

- maps tickers to SEC CIKs, or reads ticker/company/CIK values from a universe CSV
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
python build_constellation_soxx.py --signature-only --output-dir data/test_soxx_signature_only
```

The legacy default still uses the SOXX universe when `--universe-csv` is omitted. The command above writes the validated SOXX signature-page-only output under `data/test_soxx_signature_only/`.

- `company_nodes.csv`
- `person_nodes.csv`
- `edges.csv`
- `parse_log.csv` with signature-page vs. Item 10 extraction counts and success flags
- `qa_summary.csv` with aggregate coverage counts
- `qa_low_coverage.csv` with companies that need review
- `cache/` for downloaded SEC and universe files

Run the checked-in Nasdaq 100 CSV universe with the same signature-page extraction logic:

```bash
python build_constellation_soxx.py \
  --universe-csv data/universes/nasdaq100.csv \
  --signature-only \
  --output-dir data/test_nasdaq100_signature_only
```

Useful development options:

```bash
python build_constellation_soxx.py --tickers NVDA,AMD
python build_constellation_soxx.py --limit 5
python build_constellation_soxx.py --output-dir /tmp/constellation_v0_test
python build_constellation_soxx.py --signature-only
python build_constellation_soxx.py --universe-csv data/universes/nasdaq100.csv --tickers NVDA,AMD,AAPL --signature-only --output-dir /tmp/constellation_nasdaq100_smoke
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
- Use `--signature-only` for validation runs that output only signature-page relationships and skip Item 10 extraction, fallback, and merge logic
- Use `--universe-csv` to run the same extraction logic against any CSV with `ticker`, `company_name`, and `cik` columns
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

## Load V0 CSVs into Neo4j

Layer 2 loading uses the validated Constellation V0 CSV files and writes them to a local Neo4j instance with the official Neo4j Python driver. The loader defaults to `bolt://localhost:7687` because the script and Neo4j Docker container are expected to run on the same GCP VM.

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Load the current validated SOXX signature-page-only CSV output:

```bash
python load_neo4j.py \
  --input-dir data/test_soxx_signature_only \
  --uri bolt://localhost:7687 \
  --user neo4j \
  --password constellation123 \
  --clear
```

Load the Nasdaq 100 signature-page-only CSV output:

```bash
python load_neo4j.py \
  --input-dir data/test_nasdaq100_signature_only \
  --uri bolt://localhost:7687 \
  --user neo4j \
  --password constellation123 \
  --clear
```

The `--clear` flag is optional. When provided, the loader runs `MATCH (n) DETACH DELETE n` before creating constraints and loading nodes and relationships.

### Example Cypher Queries

Show all relationships:

```cypher
MATCH (p:Person)-[r]->(c:Company)
RETURN p,r,c
LIMIT 200;
```

Find cross-company directors:

```cypher
MATCH (p:Person)-[:BOARD_OF]->(c:Company)
WITH p, collect(c) AS companies
WHERE size(companies) >= 2
UNWIND companies AS c
MATCH (p)-[r:BOARD_OF]->(c)
RETURN p,r,c;
```

Search a person:

```cypher
MATCH (p:Person)-[r]->(c:Company)
WHERE toLower(p.name) CONTAINS toLower("Victor Peng")
RETURN p,r,c;
```

## QA Output Interpretation

Each extraction run writes two lightweight QA files next to the graph CSVs. `qa_summary.csv` reports total companies, total people, total relationships, `CEO_OF`, `CFO_OF`, and `BOARD_OF` counts, plus counts of companies flagged for low coverage. `qa_low_coverage.csv` lists companies where total relationships are below 5, the CEO edge is missing, the CFO edge is missing, or fewer than 5 board members were extracted. These QA flags are review cues for signature-page coverage; they do not change the edge schema consumed by `load_neo4j.py`.

## Web App V1

Constellation Web App V1 adds a product-shaped graph explorer on top of the existing Layer 1 CSV output and Layer 2 Neo4j loader. The default landing view intentionally loads the full universal graph for the loaded universe so users can inspect the complete network before narrowing to cross-company or local-focus views.

### Load Nasdaq 100 into Neo4j

Start Neo4j locally, then load the checked-in Nasdaq 100 signature-page output:

```bash
python3 load_neo4j.py --clear --data-dir data/test_nasdaq100_signature_only
```

The loader also accepts explicit connection flags when your Neo4j instance is not using local defaults:

```bash
python3 load_neo4j.py \
  --clear \
  --data-dir data/test_nasdaq100_signature_only \
  --uri bolt://localhost:7687 \
  --user neo4j \
  --password "$NEO4J_PASSWORD"
```

Do not commit Neo4j credentials. The web backend reads its password from environment variables.

### Start the FastAPI backend

Install backend dependencies and export Neo4j connection settings:

```bash
python3 -m pip install -r app/backend/requirements.txt
export NEO4J_URI="bolt://localhost:7687"
export NEO4J_USER="neo4j"
export NEO4J_PASSWORD="your-local-password"
uvicorn app.backend.main:app --reload --host 0.0.0.0 --port 8000
```

Available API endpoints:

- `GET /health`
- `GET /graph/universal`
- `GET /search?q=`
- `GET /graph/company/{ticker}?radius=1`
- `GET /graph/person/{name}?radius=1`
- `GET /graph/cross-company`
- `GET /graph/company-network`

Graph endpoints return Cytoscape-compatible JSON with `nodes` and `edges` arrays.

### Start the React frontend

Install frontend dependencies and start Vite:

```bash
cd app/frontend
npm install
npm run dev
```

Open the web app at `http://localhost:5173`. The frontend calls `http://localhost:8000` by default. To use a different API URL, set `VITE_API_BASE_URL` before starting Vite.

### Web App Features

- Full Graph tab loads `GET /graph/universal` automatically on page load.
- Cross-company tab shows people connected to more than one company.
- Company Network tab shows derived company-to-company edges with `shared_people` and `shared_count` metadata.
- Search finds companies by ticker or company name and people by name without replacing the full graph.
- Search results can focus the current graph node or explicitly load a local Search Focus graph.
- Left-panel filters toggle `CEO_OF`, `CFO_OF`, `BOARD_OF`, Company nodes, and Person nodes.
- Clicking a node or edge opens its raw details in the right panel.
