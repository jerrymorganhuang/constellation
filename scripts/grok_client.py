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

SYSTEM_PROMPT = """You are building a high-quality company relationship dataset.

Your task is to identify the CURRENT executive leadership team and board of directors for every company provided by the user.

Use ONLY official company sources whenever possible.

Search official sources in the following priority order:

1. Leadership / Management page
2. Investor Relations website
3. Corporate Governance page
4. Proxy Statement (DEF 14A)
5. Annual Report

Use web search to locate these sources.

Your objective is to maximize recall while maintaining reasonable precision.

Return STRICT JSON only.

JSON schema:

{
  "companies": [
    {
      "ticker": "NVDA",
      "relationships": [
        {
          "person_name": "Jensen Huang",
          "role": "President and Chief Executive Officer",
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

Requirements

1. Return exactly one company object for every ticker provided.

2. For each company, identify every CURRENT member of the official:
   - Executive Team
   - Management Team
   - Leadership Team
   - Senior Leadership
   - Board of Directors

3. If no current leadership or board members can be identified after searching official company sources, return the company with:

   "relationships": []

4. role_category must be exactly one of:
   - EXECUTIVE
   - BOARD

5. Preserve each person's official job title exactly as shown on the official company source.

6. Do not standardize, simplify, abbreviate, or reinterpret job titles.

7. A person may appear multiple times if they currently hold multiple qualifying roles.

8. Include only CURRENT executives and CURRENT directors.

9. Exclude:
   - Former executives
   - Former directors
   - Advisors
   - Observers
   - Committee memberships by themselves
   - Honorary titles
   - Founders without a current executive or board position

10. If one individual cannot be verified, omit only that individual. Never omit the rest of the company.

11. Before returning the final JSON, verify that the executive team and board of directors appear reasonably complete based on the official company sources you found. If the results appear incomplete, continue searching before returning your answer.

Return only valid JSON.

Do not include explanations, markdown, citations, comments, or any text outside the JSON.
"""


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
    load_dotenv(PROJECT_ROOT / ".env", override=True)
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
    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        tools=[{"type": "web_search"}],
        temperature=TEMPERATURE,
        max_output_tokens=MAX_TOKENS,
    )
    return response.output_text
