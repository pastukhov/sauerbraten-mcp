"""The same valid/invalid wire vectors are consumed by the C++ wire test."""

import json
import math
from pathlib import Path
import pytest

CASES = json.loads(
  (Path(__file__).parents[1] / "contracts/cases/wire.json").read_text()
)


def parse_reference(raw):
  def pairs(values):
    result = {}
    for key, value in values:
      if key in result:
        raise ValueError("duplicate key")
      result[key] = value
    return result

  def reject(value):
    raise ValueError(value)

  value = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject)

  def walk(v, depth=0):
    if depth > 16:
      raise ValueError("depth")
    if isinstance(v, str):
      v.encode("utf-8")
      if "\0" in v:
        raise ValueError("NUL")
    if isinstance(v, float) and not math.isfinite(v):
      raise ValueError("nonfinite")
    if isinstance(v, dict):
      for k, vv in v.items():
        walk(k, depth + 1)
        walk(vv, depth + 1)
    if isinstance(v, list):
      for vv in v:
        walk(vv, depth + 1)

  walk(value)
  if not isinstance(value, dict) or set(value) != {"v", "id", "method", "params"}:
    raise ValueError("envelope")
  if value["v"] != 1 or not isinstance(value["params"], dict):
    raise ValueError("version/params")
  if (
    not isinstance(value["id"], str)
    or not 1 <= len(value["id"]) <= 64
    or any(not 33 <= ord(c) <= 126 for c in value["id"])
  ):
    raise ValueError("id")
  if not isinstance(value["method"], str) or not 1 <= len(value["method"]) <= 64:
    raise ValueError("method")
  return value


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_shared_wire_vectors(case):
  if case["valid"]:
    assert parse_reference(case["request"])
  else:
    with pytest.raises((ValueError, UnicodeError)):
      parse_reference(case["request"])


@pytest.mark.native
def test_engine_wire_vectors_match_sdk_contract():
  from conftest import ENGINE_ROOT
  if ENGINE_ROOT is None:
    pytest.skip("Source checkout required for native fixture conformance")
  native = ENGINE_ROOT / "src/tests/mcp/cases/wire.json"
  sdk = Path(__file__).parents[1] / "contracts/cases/wire.json"
  assert json.loads(native.read_text()) == json.loads(sdk.read_text())
