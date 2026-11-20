"""LLM factory + tolerant structured-output helper (works with tool-less models too)."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, TypeVar

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

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
    """Ambil objek JSON pertama; abaikan teks/objek tambahan setelahnya.

    Model sering menulis `{...}` lalu menempel kalimat atau objek kedua, dan
    `json.loads` menolaknya dengan `Invalid JSON: trailing characters`.
    """
    raw = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    if fenced:
        raw = fenced.group(1)

    decoder = json.JSONDecoder()
    start = raw.find("{")
    while start != -1:
        try:
            obj, _end = decoder.raw_decode(raw[start:])
        except json.JSONDecodeError:
            start = raw.find("{", start + 1)
            continue
        if isinstance(obj, dict):
            return obj
        start = raw.find("{", start + 1)

    raise ValueError("no JSON object in model output")


def _coerce(schema: type[T], payload: Any) -> T:
    """Validasi; kalau gagal, coba perbaikan ringan yang umum dari LLM."""
    try:
        return schema.model_validate(payload)
    except ValidationError:
        if not isinstance(payload, dict):
            raise
        fixed = dict(payload)
        for name, field in schema.model_fields.items():
            # Field opsional yang diisi null: buang supaya default-nya dipakai.
            if name in fixed and fixed[name] is None and not field.is_required():
                fixed.pop(name)
        return schema.model_validate(fixed)


async def structured(prompt: str, schema: type[T], retries: int = 2) -> T:
    """Try native structured output, fall back to JSON-in-text parsing."""
    llm = get_llm()
    last_err: Exception | None = None

    try:
        return await llm.with_structured_output(schema).ainvoke(prompt)
    except Exception as exc:  # noqa: BLE001 - provider may not support tools
        last_err = exc
        # Bukan kegagalan nyata: jalur JSON-text di bawah menangani ini.
        log.debug("structured_output tidak tersedia (%s), pakai JSON text", exc)

    hint = (
        f"\n\nReturn ONLY a JSON object matching this schema:\n"
        f"{json.dumps(schema.model_json_schema(), indent=2)}"
    )
    for _ in range(retries):
        try:
            msg = await llm.ainvoke(prompt + hint)
            return _coerce(schema, _extract_json(str(msg.content)))
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            log.warning("json parse failed: %s", exc)

    raise RuntimeError(f"structured output failed: {last_err}")
