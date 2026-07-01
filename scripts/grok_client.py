#!/usr/bin/env python3
"""Grok API client for Constellation Relationship Layer batch extraction."""

from __future__ import annotations

import os
import sys
from pathlib import Path
import json
from typing import Any, Iterable, Mapping, NamedTuple

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_MODEL = "grok-4.20-0309-reasoning"
BASE_URL = "https://api.x.ai/v1"
TEMPERATURE = 0

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



class GrokUsageMetadata(NamedTuple):
    """Usage and response metadata returned by the Responses API."""

    response_id: str | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cached_input_tokens: int | None
    usage_json: str | None


class GrokExtractionResult(NamedTuple):
    """Raw extraction output plus API usage metadata and response diagnostics."""

    raw_response: str | None
    metadata: GrokUsageMetadata
    response_json: str | None


def _as_dict(value: Any) -> dict[str, Any] | None:
    """Best-effort conversion of SDK response objects to dictionaries."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    for method_name in ("model_dump", "dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                result = method()
            except TypeError:
                result = method
            if isinstance(result, dict):
                return result
    return None


def _get_value(value: Any, *names: str) -> Any:
    """Read the first available attribute/key from an SDK object or dict."""
    value_dict = _as_dict(value)
    for name in names:
        if value_dict is not None and name in value_dict:
            return value_dict[name]
        if hasattr(value, name):
            return getattr(value, name)
    return None



def _non_empty_string(value: Any) -> str | None:
    """Return string values with non-whitespace content, otherwise None."""
    if isinstance(value, str) and value.strip():
        return value
    return None


def _iter_items(value: Any) -> list[Any]:
    """Return list-like response fields as a plain list for SDK and dict shapes."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def extract_final_text(response: Any) -> str | None:
    """Extract final assistant text from Responses API SDK/dict response shapes."""
    output_text = _non_empty_string(_get_value(response, "output_text"))
    if output_text is not None:
        return output_text

    fallback_parts: list[str] = []
    for item in _iter_items(_get_value(response, "output")):
        item_type = _get_value(item, "type")
        if item_type != "message":
            continue
        role = _get_value(item, "role")
        if role is not None and role != "assistant":
            continue
        for part in _iter_items(_get_value(item, "content")):
            if _get_value(part, "type") != "output_text":
                continue
            text = _non_empty_string(_get_value(part, "text"))
            if text is not None:
                fallback_parts.append(text)

    if fallback_parts:
        return "\n".join(fallback_parts)
    return None


def _final_text_extraction_method(response: Any, final_text: str | None) -> str | None:
    """Identify which Responses API field supplied the extracted final text."""
    if final_text is None:
        return None
    if _non_empty_string(_get_value(response, "output_text")) is not None:
        return "output_text"
    return "output_fallback"


def _output_item_types(response: Any) -> list[str]:
    """Return response.output item types for lightweight diagnostics."""
    item_types: list[str] = []
    for item in _iter_items(_get_value(response, "output")):
        item_type = _get_value(item, "type")
        if item_type is not None:
            item_types.append(str(item_type))
    return item_types

def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _usage_json(usage: Any, debug_metadata: Mapping[str, Any] | None = None) -> str | None:
    usage_dict = _as_dict(usage)
    if usage_dict is None:
        usage_dict = {}
    if debug_metadata:
        usage_dict.update({key: value for key, value in debug_metadata.items() if value is not None})
    if not usage_dict:
        return None
    return json.dumps(usage_dict, ensure_ascii=False, sort_keys=True)


def _response_json(response: Any) -> str | None:
    """Return the full SDK response object as JSON for debug capture when possible."""
    if response is None:
        return None
    model_dump_json = getattr(response, "model_dump_json", None)
    if callable(model_dump_json):
        try:
            return model_dump_json(indent=2)
        except TypeError:
            return model_dump_json()
    response_dict = _as_dict(response)
    if response_dict is not None:
        return json.dumps(response_dict, ensure_ascii=False, indent=2)
    return None


def _extract_usage_metadata(response: Any, requested_model: str, final_text: str | None = None) -> GrokUsageMetadata:
    usage = _get_value(response, "usage")
    input_tokens = _to_int(_get_value(usage, "input_tokens", "prompt_tokens"))
    output_tokens = _to_int(_get_value(usage, "output_tokens", "completion_tokens"))
    total_tokens = _to_int(_get_value(usage, "total_tokens"))
    input_details = _get_value(usage, "input_tokens_details", "prompt_tokens_details")
    cached_input_tokens = _to_int(_get_value(input_details, "cached_tokens", "cached_input_tokens"))
    return GrokUsageMetadata(
        response_id=_get_value(response, "id", "response_id"),
        model=_get_value(response, "model") or requested_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_input_tokens=cached_input_tokens,
        usage_json=_usage_json(
            usage,
            {
                "extraction_method": _final_text_extraction_method(response, final_text),
                "extracted_text_len": len(final_text) if final_text is not None else None,
                "response_status": _get_value(response, "status"),
                "output_item_types": _output_item_types(response),
            },
        ),
    )


def extract_relationships_raw(companies: Iterable[tuple[str, str]], model: str = DEFAULT_MODEL) -> GrokExtractionResult:
    """Call Grok with the fixed system prompt and return raw JSON text plus metadata."""
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
    )
    final_text = extract_final_text(response)
    return GrokExtractionResult(final_text, _extract_usage_metadata(response, model, final_text), _response_json(response))
