from conftest import CLIENT, DATA
"""Actual SDL/GL integration; no mocked engine globals."""

import asyncio
import os
from pathlib import Path
import signal
import subprocess
import uuid
import pytest
from sauer_mcp.bridge import BridgeClient, BridgeError

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.live


@pytest.fixture
async def game(tmp_path):
  os.chmod(tmp_path, 0o700)
  profile = tmp_path / "profile"
  profile.mkdir()
  log = (tmp_path / "game.log").open("w")
  args = [
    "xvfb-run",
    "-a",
    str(CLIENT),
    f"-q{profile}",
    f"-mcpbridge{tmp_path}/s",
    "-t0",
    "-w640",
    "-h480",
    "-xnewmap 10; edittoggle",
  ]
  process = subprocess.Popen(
    args,
    cwd=DATA,
    stdout=log,
    stderr=log,
    start_new_session=True,
  )
  client = BridgeClient(str(tmp_path / "s"))
  try:
    for _ in range(300):
      if (tmp_path / "s").exists():
        break
      if process.poll() is not None:
        pytest.fail((tmp_path / "game.log").read_text())
      await asyncio.sleep(0.05)
    yield client
  finally:
    await client.close()
    if process.poll() is None:
      os.killpg(process.pid, signal.SIGTERM)
    process.wait(timeout=10)
    log.close()


async def token(game, **params):
  state = await game.call("editor_state", {})
  return dict(
    map_epoch=state["map_epoch"],
    expected_revision=state["revision"],
    request_id=str(uuid.uuid4()),
    **params,
  )


async def test_state_region_catalogs(game):
  state = await game.call("editor_state", {})
  assert state["worldsize"] == 1024 and state["editing"]
  assert state["session_id"] and state["map_epoch"] and state["revision"].isdigit()
  bounds = {"origin": [0, 0, 0], "extent": [64, 64, 64]}
  before = state["allocnodes"]
  region = await game.call("region_read", {"bounds": bounds})
  assert region["leaves"] and len(region["leaves"][0]["edges"]) == 12
  after = await game.call("editor_state", {})
  assert after["allocnodes"] == before and after["revision"] == state["revision"]
  assert (await game.call("texture_catalog", {}))["entries"]
  assert (await game.call("mapmodel_catalog", {}))["entries"]


async def test_geometry_history_stale_and_dedup(game):
  tex = (await game.call("texture_catalog", {"limit": 2}))["entries"][1]["ref"]
  selection = {"origin": [16, 16, 528], "count": [2, 2, 2], "grid": 8, "orient": 5}
  params = await token(game, selection=selection, mode="solid", texture_ref=tex)
  result = await game.call("geometry_fill", params)
  assert result["revision"] != params["expected_revision"]
  assert await game.call("geometry_fill", params) == result
  with pytest.raises(BridgeError, match="REQUEST_ID_REUSED"):
    await game.call("geometry_fill", dict(params, mode="empty"))
  with pytest.raises(BridgeError, match="STALE_STATE"):
    await game.call("geometry_fill", dict(params, request_id=str(uuid.uuid4())))
  bounds = {"origin": [16, 16, 528], "extent": [16, 16, 16]}
  filled = await game.call("region_read", {"bounds": bounds})
  assert all(c["edges"] == [128] * 12 for c in filled["leaves"])
  await game.call("history_undo", await token(game))
  empty = await game.call("region_read", {"bounds": bounds})
  assert all(c["edges"] == [0] * 12 for c in empty["leaves"])
  await game.call("history_redo", await token(game))
  assert all(
    c["edges"] == [128] * 12
    for c in (await game.call("region_read", {"bounds": bounds}))["leaves"]
  )


@pytest.mark.parametrize(
  "grid,count,origin",
  [
    (3, [1, 1, 1], [0, 0, 0]),
    (8, [4097, 1, 1], [0, 0, 0]),
    (8, [1, 1, 1], [1, 0, 0]),
    (8, [-1, 1, 1], [0, 0, 0]),
  ],
)
async def test_geometry_rejects_invalid_without_side_effects(game, grid, count, origin):
  before = await game.call("editor_state", {})
  with pytest.raises(BridgeError):
    await game.call(
      "geometry_fill",
      await token(
        game,
        selection=dict(origin=origin, count=count, grid=grid, orient=0),
        mode="empty",
      ),
    )
  after = await game.call("editor_state", {})
  assert (
    after["revision"] == before["revision"]
    and after["allocnodes"] == before["allocnodes"]
  )


async def test_entities_explicit_coordinates_refs_and_undo(game):
  attrs = [120, 255, 128, 64, 0]
  created = await game.call(
    "entity_create",
    await token(game, type="light", position=[20, 40, 600], attrs=attrs),
  )
  ref = created["entity_ref"]
  entries = (await game.call("entity_list", {}))["entries"]
  light = next(e for e in entries if e["ref"]["index"] == ref["index"])
  assert light["position"] == [20, 40, 600] and light["attrs"] == attrs
  moved = await game.call(
    "entity_update",
    await token(game, entity_ref=ref, position=[24, 48, 604], attrs=attrs),
  )
  with pytest.raises(BridgeError, match="STALE_STATE"):
    await game.call("entity_delete", await token(game, entity_ref=ref))
  await game.call("entity_delete", await token(game, entity_ref=moved["entity_ref"]))
  assert not (await game.call("entity_list", {}))["entries"]
  await game.call("history_undo", await token(game))
  assert (await game.call("entity_list", {}))["entries"][0]["position"] == [24, 48, 604]
  await game.call("history_undo", await token(game))
  assert (await game.call("entity_list", {}))["entries"][0]["position"] == [20, 40, 600]
  await game.call("history_undo", await token(game))
  assert not (await game.call("entity_list", {}))["entries"]


async def test_camera_and_frame_capture(game):
  result = await game.call(
    "camera_set", await token(game, position=[64, 64, 600], yaw=45, pitch=-20, fov=90)
  )
  image = await game.call(
    "view_capture",
    dict(
      map_epoch=result["map_epoch"],
      expected_revision=result["revision"],
      view_revision=result["view_revision"],
    ),
  )
  from PIL import Image

  state = await game.call("editor_state", {})
  path = Path(state["runtime_dir"]) / "images" / f"{image['artifact_id']}.png"
  with Image.open(path) as picture:
    assert picture.format == "PNG" and picture.size == (640, 480)
  assert image["revision"] == result["revision"]


async def test_staged_save_keeps_history_and_references(game):
  before = await game.call("editor_state", {})
  result = await game.call(
    "save_staged_map", await token(game, staging_id=str(uuid.uuid4()))
  )
  from sauer_mcp.validator import validate_gzip

  validate_gzip(result["payload_path"])
  assert result["snapshot"]["content_digest"] and result["snapshot"]["config_digest"]
  after = await game.call("editor_state", {})
  assert (
    after["revision"] == before["revision"]
    and after["map_epoch"] == before["map_epoch"]
  )


async def test_second_peer_is_busy(game):
  await game.call("editor_state", {})
  second = BridgeClient(game.path)
  try:
    with pytest.raises(BridgeError, match="BUSY"):
      await second.call("editor_state", {})
  finally:
    await second.close()


async def test_sdk_live_edit_capture_save_reopen(game, monkeypatch):
  from mcp.shared.memory import create_connected_server_and_client_session
  from sauer_mcp.server import create_server
  from sauer_mcp.service import EditorService

  monkeypatch.setenv("SAUER_MCP_CLIENT", str(CLIENT))
  server = create_server(game.path)
  async with create_connected_server_and_client_session(server) as client:

    async def call(name, args):
      result = await client.call_tool(name, args)
      assert not result.isError, result.content
      return result.structuredContent

    async def change(name, **kwargs):
      state = await call("editor_state", {})
      return await call(
        name,
        dict(
          map_epoch=state["map_epoch"],
          expected_revision=state["revision"],
          request_id=str(uuid.uuid4()),
          **kwargs,
        ),
      )

    tex = (await call("texture_catalog", dict(limit=2)))["entries"][1]["ref"]
    await change(
      "geometry_fill",
      selection=dict(origin=[32, 32, 528], count=[4, 4, 1], grid=8, orient=5),
      mode="solid",
      texture_ref=tex,
    )
    await change(
      "entity_create",
      type="light",
      position=[64, 64, 600],
      attrs=[120, 255, 255, 255, 0],
    )
    await change(
      "entity_create", type="playerstart", position=[64, 64, 552], attrs=[0, 0, 0, 0, 0]
    )
    model = (await call("mapmodel_catalog", dict(limit=1)))["entries"][0]["ref"]
    await change(
      "entity_create",
      type="mapmodel",
      position=[40, 40, 536],
      attrs=[0, model["index"], 0, 0, 0],
      model_ref=model,
    )
    view = await change(
      "camera_set", position=[100, 20, 600], yaw=45, pitch=-25, fov=90
    )
    capture = await client.call_tool(
      "view_capture",
      dict(
        map_epoch=view["map_epoch"],
        expected_revision=view["revision"],
        view_revision=view["view_revision"],
      ),
    )
    assert (
      not capture.isError
      and capture.content[0].type == "image"
      and capture.content[0].mimeType == "image/png"
    )
    saved = await change("map_save_as", slug="sdk-live")
    assert saved["manifest_sha256"] and saved["clean_ack"] == "applied"
    await change("map_open", artifact_id=saved["artifact_id"])
