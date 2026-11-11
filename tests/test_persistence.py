from conftest import CLIENT, DATA
import uuid
import pytest
from pathlib import Path
from sauer_mcp.service import EditorService
from sauer_mcp.bridge import BridgeError

pytestmark = pytest.mark.live
from test_live_editor import game, token, ROOT


async def test_public_save_reopen_and_replay(game):
  service = EditorService(game, CLIENT)
  args = await token(game, slug="roundtrip")
  saved = await service.call("map_save_as", args)
  assert saved["artifact_id"] == args["request_id"]
  assert saved["clean_ack"] == "applied"
  assert await service.call("map_save_as", args) == saved
  status = await service.call("operation_status", dict(request_id=args["request_id"]))
  assert status["status"] == "completed"
  opened = await service.call(
    "map_open", await token(game, artifact_id=saved["artifact_id"])
  )
  assert opened["map_epoch"] != args["map_epoch"]
  assert not (await game.call("editor_state", {}))["dirty"]


async def test_artifact_dependency_drift_rejected_before_reset(game):
  service = EditorService(game, CLIENT)
  saved = await service.call("map_save_as", await token(game, slug="drift"))
  state = await game.call("editor_state", {})
  manifest = (
    Path(state["profile_root"])
    / "packages/mcp"
    / f"drift-{saved['artifact_id']}"
    / "level.cfg"
  )
  manifest.write_text(manifest.read_text() + "// changed\n")
  with pytest.raises(BridgeError, match="DEPENDENCY_CHANGED"):
    await service.call("map_open", await token(game, artifact_id=saved["artifact_id"]))
  assert (await game.call("editor_state", {}))["map_epoch"] == state["map_epoch"]


@pytest.mark.parametrize("map_name", ["complex", "ot"])
async def test_stock_config_roundtrip(game, map_name):
  service = EditorService(game, CLIENT)
  await service.call("map_save_as", await token(game, slug="initial"))
  catalog = await service.call(
    "map_catalog", dict(search=map_name, offset=0, limit=256)
  )
  target = next(row for row in catalog["entries"] if row["name"] == map_name)
  await service.call("map_open", await token(game, map_id=target["map_id"]))
  await game.call(
    "material_apply",
    await token(
      game,
      selection=dict(origin=[16, 16, 16], count=[1, 1, 1], grid=8, orient=5),
      material="air",
    ),
  )
  saved = await service.call("map_save_as", await token(game, slug=map_name))
  assert saved["clean_ack"] == "applied"
  await service.call("map_open", await token(game, artifact_id=saved["artifact_id"]))
  assert (await game.call("editor_state", {}))["lighting_dirty"] == saved[
    "lighting_dirty"
  ]


async def test_name_dependent_config_export_rejected(game):
  import shutil

  service = EditorService(game, CLIENT)
  saved = await service.call("map_save_as", await token(game, slug="initial"))
  state = await game.call("editor_state", {})
  profile = Path(state["profile_root"])
  base = profile / "packages/base"
  base.mkdir(exist_ok=True)
  shutil.copyfile(
    profile / "packages/mcp" / f"initial-{saved['artifact_id']}" / "level.ogz",
    base / "namecase.ogz",
  )
  (base / "namecase.cfg").write_text("maptitle (mapname)\n")
  target = (await service.call("map_catalog", dict(search="namecase")))["entries"][0]
  await service.call("map_open", await token(game, map_id=target["map_id"]))
  with pytest.raises(BridgeError, match="UNSUPPORTED_CONFIG"):
    await service.call("map_save_as", await token(game, slug="namecase"))


async def test_concurrent_edit_preserves_new_dirty_revision(game):
  import asyncio

  service = EditorService(game, CLIENT)
  entered = asyncio.Event()
  release = asyncio.Event()
  original = service.validate

  async def paused(*args, **kwargs):
    result = await original(*args, **kwargs)
    entered.set()
    await release.wait()
    return result

  service.validate = paused
  args = await token(game, slug="concurrent")
  task = asyncio.create_task(service.call("map_save_as", args))
  await asyncio.wait_for(entered.wait(), 15)
  status = await service.call("operation_status", {"request_id": args["request_id"]})
  assert status["status"] == "pending"
  assert (await service.call("map_save_as", args))["status"] == "pending"
  tex = (await game.call("texture_catalog", {"limit": 2}))["entries"][1]["ref"]
  await game.call(
    "geometry_fill",
    await token(
      game,
      selection=dict(origin=[16, 16, 528], count=[1, 1, 1], grid=8, orient=5),
      mode="solid",
      texture_ref=tex,
    ),
  )
  release.set()
  saved = await task
  assert (
    saved["clean_ack"] == "stale" and (await game.call("editor_state", {}))["dirty"]
  )


async def test_publish_io_failure_does_not_clear_dirty(game, monkeypatch):
  import sauer_mcp.service as module

  service = EditorService(game, CLIENT)
  original = module.os.rename

  def fail(source, target):
    raise OSError("injected publish failure")

  monkeypatch.setattr(module.os, "rename", fail)
  args = await token(game, slug="iofailure")
  with pytest.raises(BridgeError, match="IO_ERROR"):
    await service.call("map_save_as", args)
  assert not (service.bundles / f"iofailure-{args['request_id']}").exists()
  assert (await game.call("editor_state", {}))["dirty"]
  assert (await service.call("operation_status", {"request_id": args["request_id"]}))[
    "status"
  ] == "failed"


async def test_recover_after_publish_before_journal_update(game, monkeypatch):
  service = EditorService(game, CLIENT)
  await service.initialize()
  update = service.journal.update

  def fail(rid, **kwargs):
    if kwargs.get("phase") == "published":
      raise OSError("injected crash window after rename")
    return update(rid, **kwargs)

  monkeypatch.setattr(service.journal, "update", fail)
  args = await token(game, slug="recover")
  with pytest.raises(BridgeError, match="IO_ERROR"):
    await service.call("map_save_as", args)
  recovered = EditorService(game, CLIENT)
  saved = await recovered.call("map_save_as", args)
  assert saved["artifact_id"] == args["request_id"] and saved["clean_ack"] == "applied"
  assert len(list(recovered.bundles.glob(f"*-{args['request_id']}"))) == 1


async def test_new_map_dirty_gate_and_native_bottom_half(game):
  service = EditorService(game, CLIENT)
  await service.call("map_save_as", await token(game, slug="before-new"))
  previous = await game.call("editor_state", {})
  await service.call("map_new", await token(game, scale=11))
  state = await game.call("editor_state", {})
  assert state["worldsize"] == 2048 and state["map_epoch"] != previous["map_epoch"]
  bottom = await game.call(
    "region_read", dict(bounds=dict(origin=[0, 0, 0], extent=[8, 8, 8]))
  )
  assert all(c["edges"] == [128] * 12 for c in bottom["leaves"])
  with pytest.raises(BridgeError, match="UNSAVED_CHANGES"):
    await service.call("map_new", await token(game, scale=10))


async def test_recompressed_truncated_payload_rejected_before_reset(game):
  import gzip

  service = EditorService(game, CLIENT)
  await service.call("map_save_as", await token(game, slug="before-corrupt"))
  state = await game.call("editor_state", {})
  base = Path(state["profile_root"]) / "packages/base"
  base.mkdir(exist_ok=True)
  with gzip.open(
    DATA / "packages/base/complex.ogz", "rb"
  ) as handle:
    payload = handle.read()
  (base / "corrupt-lm.ogz").write_bytes(gzip.compress(payload[:-32]))
  (base / "corrupt-lm.cfg").write_text('exec "packages/base/complex.cfg"\n')
  target = (await service.call("map_catalog", dict(search="corrupt-lm")))["entries"][0]
  with pytest.raises(BridgeError):
    await service.call("map_open", await token(game, map_id=target["map_id"]))
  assert (await game.call("editor_state", {}))["map_epoch"] == state["map_epoch"]


@pytest.mark.parametrize(
  "suffix", ["// same semantics, different identity\n", "ambient 17\n"]
)
async def test_shadow_dependency_rejected_before_reset(game, suffix):
  import shutil

  service = EditorService(game, CLIENT)
  saved = await service.call("map_save_as", await token(game, slug="shadow"))
  state = await game.call("editor_state", {})
  shadow = Path(state["profile_root"]) / "data/default_map_settings.cfg"
  shadow.parent.mkdir(exist_ok=True)
  original = DATA / "data/default_map_settings.cfg"
  shadow.write_text(original.read_text() + "\n" + suffix)
  with pytest.raises(BridgeError, match="DEPENDENCY_CHANGED"):
    await service.call("map_open", await token(game, artifact_id=saved["artifact_id"]))
  assert (await game.call("editor_state", {}))["map_epoch"] == state["map_epoch"]


async def test_export_uuid_reuse_rejected_after_service_restart(game):
  service = EditorService(game, CLIENT)
  saved = await service.call("map_save_as", await token(game, slug="uuid-check"))
  replacement = EditorService(game, CLIENT)
  args = await token(
    game,
    selection=dict(origin=[16, 16, 16], count=[1, 1, 1], grid=8, orient=5),
    mode="empty",
  )
  args["request_id"] = saved["artifact_id"]
  with pytest.raises(BridgeError, match="REQUEST_ID_REUSED"):
    await replacement.call("geometry_fill", args)
  open_args = await token(game, artifact_id=saved["artifact_id"])
  open_args["request_id"] = saved["artifact_id"]
  with pytest.raises(BridgeError, match="REQUEST_ID_REUSED"):
    await replacement.call("map_open", open_args)


@pytest.mark.parametrize("unreadable", [False, True])
async def test_missing_nested_config_rejected_before_reset(game, unreadable):
  import shutil

  service = EditorService(game, CLIENT)
  saved = await service.call("map_save_as", await token(game, slug="before-missing"))
  state = await game.call("editor_state", {})
  profile = Path(state["profile_root"])
  base = profile / "packages/base"
  base.mkdir(exist_ok=True)
  shutil.copyfile(
    profile / "packages/mcp" / f"before-missing-{saved['artifact_id']}" / "level.ogz",
    base / "missing-include.ogz",
  )
  (base / "missing-include.cfg").write_text(
    'exec "packages/base/mandatory-missing.cfg"\n'
  )
  if unreadable:
    included = base / "mandatory-missing.cfg"
    included.write_text("ambient 17\n")
    included.chmod(0)
  row = (await service.call("map_catalog", dict(search="missing-include")))["entries"][
    0
  ]
  with pytest.raises(BridgeError):
    await service.call("map_open", await token(game, map_id=row["map_id"]))
  assert (await game.call("editor_state", {}))["map_epoch"] == state["map_epoch"]


async def test_engine_uuid_cannot_become_export(game):
  service = EditorService(game, CLIENT)
  args = await token(
    game,
    selection=dict(origin=[16, 16, 16], count=[1, 1, 1], grid=8, orient=5),
    mode="empty",
  )
  original = await service.call("geometry_fill", args)
  export = await token(game, slug="collision")
  export["request_id"] = args["request_id"]
  with pytest.raises(BridgeError, match="REQUEST_ID_REUSED"):
    await service.call("map_save_as", export)
  status = await service.call("operation_status", dict(request_id=args["request_id"]))
  assert status["status"] == "completed" and status["result"] == original

async def test_published_journal_available_without_live_game(game,monkeypatch):
  service=EditorService(game,CLIENT)
  args=await token(game,slug='offline-status');saved=await service.call('map_save_as',args)
  async def offline(*args,**kwargs):raise BridgeError('NOT_CONNECTED','game stopped')
  monkeypatch.setattr(game,'call',offline)
  assert await service.call('map_save_as',args)==saved
  status=await service.call('operation_status',dict(request_id=args['request_id']))
  assert status['status']=='completed' and status['result']==saved

async def test_lost_stage_reply_recovers_without_reapplying(game,monkeypatch):
  service=EditorService(game,CLIENT)
  original=game.call;staging_calls=0
  async def lose(method,params,**kwargs):
    nonlocal staging_calls
    result=await original(method,params,**kwargs)
    if method=='save_staged_map':
      staging_calls+=1
      raise BridgeError('OUTCOME_UNKNOWN','simulated lost reply after native commit')
    return result
  monkeypatch.setattr(game,'call',lose)
  args=await token(game,slug='lost-reply')
  with pytest.raises(BridgeError,match='OUTCOME_UNKNOWN'):await service.call('map_save_as',args)
  assert (await service.call('operation_status',dict(request_id=args['request_id'])))['status']=='unknown'
  saved=await service.call('map_save_as',args)
  assert saved['clean_ack']=='applied' and staging_calls==1


@pytest.mark.parametrize("cancel_first", [False, True])
async def test_identical_concurrent_open_shares_validation_and_commit(
  game, monkeypatch, cancel_first
):
  import asyncio
  from sauer_mcp.validator import Validator

  service = EditorService(game, CLIENT)
  saved = await service.call("map_save_as", await token(game, slug="singleflight"))
  args = await token(game, artifact_id=saved["artifact_id"])
  entered = asyncio.Event()
  release = asyncio.Event()
  validations = 0
  commits = 0
  original_run = Validator.run
  original_call = game.call

  async def paused(self, *args, **kwargs):
    nonlocal validations
    validations += 1
    entered.set()
    await release.wait()
    return await original_run(self, *args, **kwargs)

  async def counted(name, *args, **kwargs):
    nonlocal commits
    if name == "commit_open":
      commits += 1
    return await original_call(name, *args, **kwargs)

  monkeypatch.setattr(Validator, "run", paused)
  monkeypatch.setattr(game, "call", counted)
  first = asyncio.create_task(service.call("map_open", args))
  await asyncio.wait_for(entered.wait(), 10)
  second = asyncio.create_task(service.call("map_open", args))
  await asyncio.sleep(0.1)
  if cancel_first:
    first.cancel()
  release.set()
  results = await asyncio.gather(first, second, return_exceptions=True)
  if cancel_first:
    assert isinstance(results[0], asyncio.CancelledError)
    assert await service.call("map_open", args) == results[1]
  else:
    assert all(isinstance(result, dict) for result in results), results
    assert results[0] == results[1]
  assert validations == commits == 1


async def test_custom_shader_configs_rejected_before_reset(game):
  import shutil

  service = EditorService(game, CLIENT)
  saved = await service.call("map_save_as", await token(game, slug="shader-source"))
  state = await game.call("editor_state", {})
  profile = Path(state["profile_root"])
  base = profile / "packages/base"
  base.mkdir(exist_ok=True)
  vs = "attribute vec4 vvertex; uniform mat4 camprojmatrix; void main(){ gl_Position = camprojmatrix * vvertex; }"
  definitions = [
    f"shader 0 mcp_same_name [{vs}] [void main(){{ gl_FragColor = vec4(1,0,0,1); }}]",
    f"shader 0 mcp_same_name [{vs}] [void main(){{ gl_FragColor = vec4(0,0,1,1); }}]",
    "shader 0 stdworld [] []",
    "variantshader 0 stdworld 999 [] []",
    "altshader mcp_missing stdworld",
    "fastshader mcp_missing stdworld 0",
    "defershader 0 mcp_deferred []",
  ]
  for i, definition in enumerate(definitions):
    name = f"customshader{i}"
    shutil.copyfile(
      profile / "packages/mcp" / f"shader-source-{saved['artifact_id']}" / "level.ogz",
      base / f"{name}.ogz",
    )
    (base / f"{name}.cfg").write_text(definition + "\n")
    row = (await service.call("map_catalog", dict(search=name)))["entries"][0]
    with pytest.raises(BridgeError, match="UNSUPPORTED_CONFIG"):
      await service.call("map_open", await token(game, map_id=row["map_id"]))
    assert (await game.call("editor_state", {}))["map_epoch"] == state["map_epoch"]
