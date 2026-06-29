#!/usr/bin/env python3
"""Grok API client for Constellation Relationship Layer batch extraction."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_MODEL = "grok-4-fast-reasoning"
BASE_URL = "https://api.x.ai/v1"
TEMPERATURE = 0
MAX_TOKENS = 8000

SYSTEM_PROMPT = """You are building a company relationship dataset.

For each company below, identify all current members of the company’s official Executive Team / Management Team / Leadership Team / Senior Leadership, and all current members of the Board of Directors.

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
   - All other executive roles should keep the company’s official title as closely as possible.

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
