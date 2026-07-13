"""Backend configuration loaded from the repository root .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / ".env"


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


@dataclass(frozen=True)
class Settings:
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    neo4j_database: str = "neo4j"
    cors_allow_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )


def get_settings(env_path: Path = ENV_PATH) -> Settings:
    file_values = _parse_env(env_path)

    def read(name: str, default: str | None = None) -> str:
        value = file_values.get(name, os.environ.get(name, default))
        if value is None or value == "":
            raise RuntimeError(f"Missing required environment variable: {name}")
        return value

    extra_origins = [o.strip() for o in read("CORS_ALLOW_ORIGINS", "").split(",") if o.strip()]
    origins = ["http://localhost:5173", "http://127.0.0.1:5173", *extra_origins]
    return Settings(
        neo4j_uri=read("NEO4J_URI"),
        neo4j_user=read("NEO4J_USER"),
        neo4j_password=read("NEO4J_PASSWORD"),
        neo4j_database=read("NEO4J_DATABASE", "neo4j"),
        cors_allow_origins=tuple(dict.fromkeys(origins)),
    )
