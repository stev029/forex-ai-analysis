"""Tolerant JSON extraction/validation for LLM output."""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from forex_agents.llm import _coerce, _extract_json
from forex_agents.schemas import TrendOut


class Inner(BaseModel):
    b: int


class Outer(BaseModel):
    a: Inner


def test_extract_json_ignores_trailing_characters():
    """Kasus nyata .tmp/out.txt: JSON valid + teks tambahan setelah kurung tutup."""
    raw = '{"bias":"range","strength":"weak","key_levels":[1.1],"rationale":"momentum lemah."} catatan'
    assert _extract_json(raw)["bias"] == "range"


def test_extract_json_ignores_second_object():
    raw = '{"bias":"bullish"} {"bias":"bearish"}'
    assert _extract_json(raw) == {"bias": "bullish"}


def test_extract_json_strips_markdown_fence():
    raw = "```json\n{\"a\": 1}\n```"
    assert _extract_json(raw) == {"a": 1}


def test_extract_json_handles_leading_prose_and_nested():
    raw = 'Here you go: {"a": {"b": 2}} done'
    assert _extract_json(raw) == {"a": {"b": 2}}


def test_extract_json_skips_unparsable_brace():
    raw = '{not json} then {"bias": "range"}'
    assert _extract_json(raw) == {"bias": "range"}


def test_extract_json_raises_without_object():
    with pytest.raises(ValueError, match="no JSON object"):
        _extract_json("tidak ada json sama sekali")


def test_coerce_drops_null_optional_field():
    class M(BaseModel):
        a: int
        b: str = "default"

    out = _coerce(M, {"a": 1, "b": None})
    assert out.a == 1
    assert out.b == "default"


def test_coerce_rejects_null_required_field():
    class M(BaseModel):
        a: int
        b: str

    with pytest.raises(Exception):
        _coerce(M, {"a": 1, "b": None})


def test_coerce_validates_nested():
    out = _coerce(Outer, {"a": {"b": 3}})
    assert out.a.b == 3


def test_coerce_still_rejects_bad_payload():
    with pytest.raises(Exception):
        _coerce(Outer, {"a": {"b": "not-an-int"}})


def test_trend_out_accepts_real_world_payload():
    payload = {
        "bias": "range",
        "strength": "weak",
        "key_levels": [1.0976, 1.09803],
        "rationale": "EMA campuran; struktur range.",
    }
    out = _coerce(TrendOut, payload)
    assert out.bias == "range"
    assert out.key_levels == [1.0976, 1.09803]
