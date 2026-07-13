# Constellation V1

Constellation V1 is a clean FastAPI + React application for exploring the current Neo4j V1 graph. The legacy V0 frontend, backend, and root `load_neo4j.py` have been removed; this application does not preserve V0 compatibility and does not use the old Neo4j schema.

## Architecture

- `app/backend/`: FastAPI API using the official Neo4j Python driver.
- `app/frontend/`: React + Vite + Cytoscape.js graph UI.
- `scripts/load_neo4j.py` and the data-building scripts remain the canonical data pipeline.

## Required environment variables

Create these values in the explicit repository-root `.env` file:

```env
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your-password
NEO4J_DATABASE=neo4j
CORS_ALLOW_ORIGINS=http://your-vm-host:5173
```

`NEO4J_DATABASE` defaults to `neo4j`. `CORS_ALLOW_ORIGINS` is optional and comma-separated; localhost Vite origins are always allowed.

## Backend startup

```bash
cd ~/constellation
source venv/bin/activate
uvicorn app.backend.main:app --host 0.0.0.0 --port 8000
```

The backend verifies Neo4j connectivity during startup and closes the driver during shutdown.

## Frontend startup

```bash
cd ~/constellation/app/frontend
npm install
npm run dev
```

Set `VITE_API_BASE_URL` if the API is not running at `http://localhost:8000`.

## API endpoints

- `GET /health` returns Neo4j connectivity status plus live Company, Person, and relationship counts.
- `GET /api/graph/full` returns the complete V1 graph as Cytoscape `{ nodes, edges }` data.
- `GET /api/search?q=...` searches company tickers, company names, person names, and person IDs.
- `GET /api/graph/company/{ticker}?radius=1|2` returns a company neighborhood.
- `GET /api/graph/person/{person_id}?radius=1|2` returns a person neighborhood.
- `GET /api/graph/cross-company` returns people connected to more than one distinct company and their actual Person-to-Company relationships.

## Frontend behavior

The UI loads the full graph first, keeps the Cytoscape layout stable after data load, supports relationship and node-type filters without rerunning layout, and offers a Cross-company view. Search results focus the existing full-graph node, smoothly center and zoom the camera, select the node, highlight its one-hop neighborhood, and dim unrelated graph elements.
