#!/usr/bin/env python3
"""Grok API client for Constellation Relationship Layer batch extraction."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable, Mapping

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_MODEL = "grok-4-fast-reasoning"
BASE_URL = "https://api.x.ai/v1"
TEMPERATURE = 0
MAX_TOKENS = 8000

SYSTEM_PROMPT = """You are building a company relationship dataset.

For each company below, identify all current members of the company's official Executive Team / Management Team / Leadership Team / Senior Leadership, and all current members of the Board of Directors.

Output strict JSON only.

JSON schema:
{
  "companies": [
    {
      "ticker": "NVDA",
      "relationships": [
        {
          "person_name": "Jensen Huang",
          "role": "CEO",
          "role_category": "EXECUTIVE"
        },
        {
          "person_name": "Jensen Huang",
          "role": "Director",
          "role_category": "BOARD"
        }
      ]
    }
  ]
}

Rules:
1. role_category must be only EXECUTIVE or BOARD.

2. For executives:
   - CEO / Chief Executive Officer / President and CEO should be standardized as CEO.
   - CFO / Chief Financial Officer should be standardized as CFO.
   - All other executive roles should keep the company's official title as closely as possible.

3. For board members:
   - Chairman / Chair of the Board / Executive Chairman / Independent Chairman should be standardized as Chairman.
   - All other board members should have role = Director.
   - Do not use Founder, Independent Director, Lead Director, or committee roles as role.

4. A person can appear twice if they are both an executive and a board member.

5. Do not include former executives or former directors.

6. Do not include advisors, observers, founders without current executive or board role, or committee-only roles.

7. Do not add explanations, markdown, citations, notes, or blank lines.

8. If uncertain, omit the row.

Return only valid JSON."""


def _format_header_value(name: str, value: str) -> str:
    """Format a header for diagnostics without leaking bearer tokens."""
    if name.lower() == "authorization" and value.startswith("Bearer "):
        token = value.removeprefix("Bearer ")
        value = f"Bearer <GROK_API_KEY length={len(token)}>"
    return f"{name}: {value!r}"


def _print_openai_client_headers(headers: Mapping[str, str]) -> None:
    """Print the exact OpenAI client headers configured by this script."""
    print("OpenAI client headers configured by scripts/grok_client.py:", file=sys.stderr)
    for name, value in headers.items():
        print(f"  {_format_header_value(name, value)}", file=sys.stderr)


def _validate_ascii_headers(headers: Mapping[str, str]) -> None:
    """Fail early with the offending header before httpx normalizes headers."""
    for name, value in headers.items():
        try:
            value.encode("ascii")
        except UnicodeEncodeError as error:
            raise RuntimeError(
                "OpenAI client header contains non-ASCII characters: "
                f"{_format_header_value(name, value)}. httpx encodes header values "
                "as ASCII while constructing the request headers; verify GROK_API_KEY "
                "contains only the API key and not SYSTEM_PROMPT or other prompt text."
            ) from error


def _openai_client_headers(api_key: str) -> dict[str, str]:
    """Return the only OpenAI client header explicitly configured here."""
    return {"Authorization": f"Bearer {api_key}"}


def build_user_prompt(companies: Iterable[tuple[str, str]]) -> str:
    """Build the per-batch user prompt from ticker/company name pairs."""
    lines = ["Process the following companies:"]
    lines.extend(f"{ticker} - {company_name}" for ticker, company_name in companies)
    return "\n".join(lines)


def extract_relationships_raw(companies: Iterable[tuple[str, str]], model: str = DEFAULT_MODEL) -> str:
    """Call Grok with the fixed system prompt and return the raw JSON text."""
    load_dotenv(PROJECT_ROOT / ".env")
    api_key = os.environ.get("GROK_API_KEY")
    if not api_key:
        raise RuntimeError("GROK_API_KEY must be set in the environment or project .env file")

    user_prompt = build_user_prompt(companies)
    headers = _openai_client_headers(api_key)
    _print_openai_client_headers(headers)
    _validate_ascii_headers(headers)
    try:
        from openai import OpenAI
    except ImportError as error:
        raise RuntimeError("The openai package is required to call the Grok API") from error

    client = OpenAI(api_key=api_key, base_url=BASE_URL)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
        extra_body={"cache_prompt": True},
    )
    content = response.choices[0].message.content
    if content is None:
        raise RuntimeError("Grok API returned an empty response")
    return content
