import pytest
from pydantic import ValidationError
from sauer_mcp.models import validate, schemas


def test_contract_is_closed_and_typed():
  assert len(schemas()) == 20
  with pytest.raises(ValidationError):
    validate("editor_state", {"execute": "quit"})
  with pytest.raises(ValidationError):
    validate("region_read", {"bounds": {"origin": [True, 0, 0], "extent": [1, 1, 1]}})
  with pytest.raises(ValidationError):
    validate("camera_set", {"position": [float("inf"), 0, 0]})


def test_valid_read_params():
  assert validate(
    "region_read", {"bounds": {"origin": [0, 0, 0], "extent": [8, 8, 8]}}
  )["bounds"]["extent"] == [8, 8, 8]
