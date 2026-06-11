# Constellation

Constellation is a graph-based relationship explorer for U.S. public companies, executives, and board members.

The goal is to build a lightweight investor research tool that allows users to search a company or person and visualize relationships such as CEO, CFO, and board membership.

## V0 Scope

The first validation scope is SOXX constituents only.

V0 should build a standalone Python pipeline that:

- maps tickers to SEC CIKs
- retrieves the latest DEF 14A proxy filing
- downloads and caches filing HTML
- parses CEO, CFO, and board members
- exports graph-ready CSV files

## Graph Model

Node types:

- Company
- Person

Relationship types:

- CEO_OF
- CFO_OF
- BOARD_OF

## Output

V0 should produce:

- company_nodes.csv
- person_nodes.csv
- edges.csv
- parse_log.csv

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
