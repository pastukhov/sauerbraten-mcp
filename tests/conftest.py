"""Integration tests require an explicit engine checkout or client/data pair."""
import os
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE_ROOT = Path(os.environ["SAUER_MCP_ENGINE"]).resolve() if os.environ.get("SAUER_MCP_ENGINE") else None
CLIENT = Path(os.environ.get("SAUER_MCP_CLIENT", (ENGINE_ROOT / "src/sauer_client") if ENGINE_ROOT else "/usr/lib/cube2/cube2_client")).resolve()
DATA = Path(os.environ.get("SAUER_MCP_DATA", ENGINE_ROOT or "/usr/share/games/sauerbraten")).resolve()


def pytest_collection_modifyitems(items):
  enabled = bool(ENGINE_ROOT or (os.environ.get("SAUER_MCP_CLIENT") and os.environ.get("SAUER_MCP_DATA")))
  integration = [i for i in items if i.get_closest_marker("live") or i.get_closest_marker("native")]
  if not enabled:
    for item in integration:
      item.add_marker(pytest.mark.skip(reason="Set SAUER_MCP_ENGINE or both SAUER_MCP_CLIENT/SAUER_MCP_DATA for integration tests"))
  elif integration and (not CLIENT.is_file() or not (DATA / "data/default_map_settings.cfg").is_file()):
    raise pytest.UsageError("Explicit integration client/data root does not exist")
