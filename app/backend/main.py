"""FastAPI entrypoint for the Constellation V1 application."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .graph_service import GraphService

service: GraphService | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global service
    settings = get_settings()
    service = GraphService(settings)
    service.verify_connectivity()
    try:
        yield
    finally:
        service.close()
        service = None


app = FastAPI(title="Constellation V1 API", lifespan=lifespan)
_settings_for_cors = get_settings if False else None
# CORS is configured at import time from explicit .env when available, but startup still validates required Neo4j vars.
try:
    origins = get_settings().cors_allow_origins
except RuntimeError:
    origins = ("http://localhost:5173", "http://127.0.0.1:5173")
app.add_middleware(CORSMiddleware, allow_origins=list(origins), allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


def graph_service() -> GraphService:
    if service is None:
        raise HTTPException(status_code=503, detail="Neo4j service is not initialized")
    return service


def checked_radius(radius: int) -> int:
    if radius not in (1, 2):
        raise HTTPException(status_code=400, detail="radius must be 1 or 2")
    return radius


@app.get("/health")
def health():
    return graph_service().health()


@app.get("/api/graph/full")
def full_graph():
    return graph_service().full_graph()


@app.get("/api/search")
def search(q: str = Query("", min_length=0)):
    if not q.strip():
        return []
    return graph_service().search(q.strip())


@app.get("/api/graph/company/{ticker}")
def company_graph(ticker: str, radius: int = 1):
    return graph_service().company_graph(ticker.upper(), checked_radius(radius))


@app.get("/api/graph/person/{person_id}")
def person_graph(person_id: str, radius: int = 1):
    return graph_service().person_graph(person_id, checked_radius(radius))


@app.get("/api/graph/cross-company")
def cross_company_graph():
    return graph_service().cross_company_graph()
