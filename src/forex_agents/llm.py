"""LLM factory + tolerant structured-output helper (works with tool-less models too)."""
from __future__ import annotations

import json
import logging
import re
from typing import TypeVar

from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from .config import get_settings

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


def get_llm() -> ChatOpenAI:
    s = get_settings()
    return ChatOpenAI(
        model=s.llm_model,
        api_key=s.llm_api_key,
        base_url=s.llm_base_url,
        temperature=s.llm_temperature,
        timeout=120,
        max_retries=2,
    )


def _extract_json(text: str) -> dict:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        return json.loads(fenced.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("no JSON object in model output")


async def structured(prompt: str, schema: type[T], retries: int = 2) -> T:
    """Try native structured output, fall back to JSON-in-text parsing."""
    llm = get_llm()
    last_err: Exception | None = None

    try:
        return await llm.with_structured_output(schema).ainvoke(prompt)
    except Exception as exc:  # noqa: BLE001 - provider may not support tools
        last_err = exc
        log.warning("structured_output failed (%s), falling back to JSON text", exc)

    hint = (
        f"\n\nReturn ONLY a JSON object matching this schema:\n"
        f"{json.dumps(schema.model_json_schema(), indent=2)}"
    )
    for _ in range(retries):
        try:
            msg = await llm.ainvoke(prompt + hint)
            return schema.model_validate(_extract_json(str(msg.content)))
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            log.warning("json parse failed: %s", exc)

    raise RuntimeError(f"structured output failed: {last_err}")
