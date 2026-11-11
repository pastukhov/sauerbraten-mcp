"""Public tools and durable export/open orchestration outside the render loop."""

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import uuid
from collections import OrderedDict
from pydantic import ValidationError
from .bridge import BridgeError
from .models import validate, MODELS, Mutation
from .export_journal import ExportJournal, atomic_json, canonical, private_dir
from .validator import Validator, sha256_file, validate_gzip

TOKEN_KEYS = ("map_epoch", "expected_revision", "request_id")


def phase_id(rid, phase):
  return str(uuid.uuid5(uuid.UUID(rid), phase))


def token_from(params, request_id=None):
  return dict(
    map_epoch=params["map_epoch"],
    expected_revision=params["expected_revision"],
    request_id=request_id or params["request_id"],
  )


def dependencies_unchanged(deps):
  for dep in deps:
    path = Path(dep["resolved"])
    if path.is_symlink() or not path.is_file() or sha256_file(path) != dep["sha256"]:
      raise BridgeError(
        "DEPENDENCY_CHANGED", f"config dependency changed: {dep['logical']}"
      )


def wrapper_for(config):
  if not config:
    return "// Generated MCP map configuration; default settings are loaded by Cube2.\n"
  if not re.fullmatch(r"[A-Za-z0-9_./-]+", config) or ".." in Path(config).parts:
    raise BridgeError("UNSUPPORTED_CONFIG", "map config has unsupported logical name")
  return f'// Generated MCP map configuration.\nexec "{config}"\n'


class EditorService:
  def __init__(self, bridge, client):
    self.bridge = bridge
    self.client = Path(client).resolve()
    self.environment = None
    self.journal = None
    self.tasks = {}
    self.open_tasks = {}
    self.init_lock = asyncio.Lock()
    self.admission_lock = asyncio.Lock()
    self.admissions = OrderedDict()
    self.active_admissions = {}
    self.validation_slots = asyncio.Semaphore(1)

  async def initialize(self):
    async with self.init_lock:
      if self.environment is not None:
        state = await self.bridge.call("editor_state", {})
        if state["session_id"] != self.environment["session_id"]:
          raise BridgeError(
            "NOT_CONNECTED",
            "game session changed; restart the MCP server before file operations",
          )
        return
      state = await self.bridge.call("editor_state", {})
      profile = Path(state["profile_root"])
      self.private = private_dir(profile / ".mcp-private")
      self.inputs = private_dir(profile / ".mcp-inputs")
      self.bundles = private_dir(profile / "packages/mcp")
      self.journal = ExportJournal(self.private / "export-journal")
      self.environment = state

  async def call(self, name, arguments):
    try:
      params = validate(name, arguments)
    except (ValidationError, KeyError) as exc:
      raise BridgeError("INVALID_ARGUMENT", str(exc)) from exc
    # Durable historical outcomes remain readable when the game is offline.
    if self.journal is not None and name == "operation_status":
      if self.journal.read(params["request_id"]) is not None:
        return self.journal.status(params["request_id"])
    if self.journal is not None and name == "map_save_as":
      cached = self.journal.read(params["request_id"])
      if cached is not None and cached["phase"] in ("published", "acked"):
        self.journal.begin(params["request_id"], params)
        if cached["result"].get("clean_ack") == "pending":
          return await self.acknowledge(params, cached["result"])
        return cached["result"]
    mutation = issubclass(MODELS[name], Mutation)
    if mutation or name in ("map_catalog", "operation_status", "view_capture"):
      await self.initialize()
    rid = params.get("request_id")
    if mutation:
      key = name + "|" + canonical(params)
      async with self.admission_lock:
        record = self.journal.read(rid)
        if name != "map_save_as" and record:
          raise BridgeError("REQUEST_ID_REUSED", "UUID belongs to an export operation")
        if rid in self.admissions and self.admissions[rid] != key:
          raise BridgeError(
            "REQUEST_ID_REUSED", "UUID belongs to another public mutation"
          )
        if name == "map_save_as" and record is None:
          existing = await self.bridge.call("operation_status", {"request_id": rid})
          if existing["status"] != "unknown":
            raise BridgeError(
              "REQUEST_ID_REUSED", "UUID already belongs to an engine operation"
            )
        self.admissions[rid] = key
        self.active_admissions[rid] = self.active_admissions.get(rid, 0) + 1
    try:
      return await self.dispatch(name, params)
    finally:
      if mutation:
        async with self.admission_lock:
          self.active_admissions[rid] -= 1
          if not self.active_admissions[rid]:
            del self.active_admissions[rid]
          while len(self.admissions) > 256:
            removable = next(
              (
                k
                for k in self.admissions
                if k not in self.active_admissions and k not in self.open_tasks
              ),
              None,
            )
            if removable is None:
              break
            del self.admissions[removable]

  async def dispatch(self, name, params):
    if name == "operation_status":
      status = self.journal.status(params["request_id"])
      if status["status"] != "unknown" or self.journal.read(params["request_id"]):
        return status
      return await self.bridge.call(name, params)
    if name == "map_catalog":
      return self.catalog(params)
    if name == "map_open":
      rid = params["request_id"]
      task = self.open_tasks.get(rid)
      if task is None:
        task = asyncio.create_task(self.open_map(params))
        self.open_tasks[rid] = task

        def finished(worker):
          self.open_tasks.pop(rid, None)
          # A caller may disconnect; still finish/reconcile the single native commit.
          if not worker.cancelled():
            worker.exception()

        task.add_done_callback(finished)
      return await asyncio.shield(task)
    if name == "map_save_as":
      rid = params["request_id"]
      record = self.journal.begin(rid, params)
      if record["phase"] in ("published", "acked"):
        if record["result"].get("clean_ack") == "pending":
          return await self.acknowledge(params, record["result"])
        return record["result"]
      if record["phase"] == "failed":
        raise BridgeError(**record["error"])
      if rid in self.tasks and not self.tasks[rid].done():
        return self.journal.status(rid)
      task = asyncio.create_task(self.export(params, record))
      self.tasks[rid] = task
      return await asyncio.shield(task)
    if name == "view_capture":
      result = await self.bridge.call(name, params)
      from .artifacts import ArtifactStore

      store = ArtifactStore(Path(self.environment["runtime_dir"]) / "images")
      return dict(
        result,
        image_b64=base64.b64encode(store.read_image(result["artifact_id"])).decode(),
      )
    return await self.bridge.call(name, params)

  def maps(self):
    roots = [
      Path(self.environment["profile_root"]),
      Path(self.environment["data_root"]),
    ]
    result = {}
    for root in roots:
      folder = root / "packages/base"
      for path in sorted(folder.glob("*.ogz")):
        if path.is_symlink():
          continue
        logical = "base/" + path.stem
        if any(row["logical"] == logical for row in result.values()):
          continue
        map_id = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:24]
        result[map_id] = {"logical": logical, "path": path.resolve(), "name": path.stem}
    return result

  def catalog(self, params):
    rows = [
      dict(map_id=k, name=v["name"])
      for k, v in self.maps().items()
      if params["search"].casefold() in v["name"].casefold()
    ]
    start = params["offset"]
    end = start + params["limit"]
    return {"entries": rows[start:end], "next_offset": end if end < len(rows) else None}

  async def validate(self, staged, logical, wrapper, work):
    validate_gzip(staged["payload_path"])
    if sha256_file(staged["payload_path"]) != staged["payload_sha256"]:
      raise BridgeError("IO_ERROR", "staged payload hash changed")
    mount = private_dir(work / "mount")
    cfg = mount / "packages" / f"{logical}.cfg"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(wrapper)
    snapshot = staged["snapshot"]
    dependencies_unchanged(snapshot["dependency_manifest"])
    validator = Validator(
      self.client,
      self.environment["data_root"],
      [self.environment["profile_root"], str(mount)],
    )
    async with self.validation_slots:
      report = await validator.run(
        dict(
          mode="compare",
          logical_name=logical,
          payload_path=staged["payload_path"],
          payload_sha256=staged["payload_sha256"],
          content_digest=snapshot["content_digest"],
          config_digest=snapshot["config_digest"],
        ),
        work / "validator",
      )
    observed = {d["logical"]: d for d in report["fingerprint"]["dependency_manifest"]}
    for dep in snapshot["dependency_manifest"]:
      # An older generated wrapper is not a source dependency of the flattened export.
      if dep["logical"].startswith("packages/mcp/") and dep["logical"].endswith(
        "/level.cfg"
      ):
        continue
      if observed.get(dep["logical"]) != dep:
        raise BridgeError(
          "DEPENDENCY_CHANGED", "validator resolved different source dependencies"
        )
    dependencies_unchanged(snapshot["dependency_manifest"])
    return report

  async def acknowledge(self, params, result):
    try:
      ack = await self.bridge.call(
        "save_ack", token_from(params, phase_id(params["request_id"], "save_ack"))
      )
      result = dict(result, clean_ack=ack["clean_ack"])
      self.journal.update(params["request_id"], phase="acked", result=result)
    except BridgeError:
      pass
    return result

  async def export(self, params, record):
    rid = params["request_id"]
    stage_id = phase_id(rid, "save_staged_map")
    logical = f"mcp/{params['slug']}-{rid}/level"
    stage_dir = self.bundles / f"staging-{stage_id}"
    destination = self.bundles / f"{params['slug']}-{rid}"
    work = self.private / f"validate-{rid}"
    try:
      if destination.exists():
        manifest = self.read_manifest(destination, check_dependencies=False)
        self.sync_bundle(destination)
        if (
          manifest["request_id"] != rid
          or manifest["input_hash"] != record["input_hash"]
        ):
          raise BridgeError("IO_ERROR", "existing destination has different identity")
        result = dict(
          manifest["result"], manifest_sha256=sha256_file(destination / "manifest.json")
        )
        self.journal.update(rid, phase="published", result=result)
        return await self.acknowledge(params, result)
      staged = record.get("staged")
      if staged is None and record.get("stage_requested"):
        outcome = await self.bridge.call("operation_status", {"request_id": stage_id})
        if outcome["status"] == "completed":
          staged = outcome["result"]
        else:
          completion = stage_dir / "level.ogz.engine.json"
          if completion.is_file() and not completion.is_symlink():
            staged = json.loads(completion.read_text())
            if staged.get("operation_id") != stage_id:
              staged = None
        if staged is None:
          self.journal.update(rid, phase="unknown")
          raise BridgeError(
            "OUTCOME_UNKNOWN", "engine staging outcome is unknown; no retry was sent"
          )
      if staged is None:
        private_dir(stage_dir)
        self.journal.update(rid, stage_requested=True)
        staged = await self.bridge.call(
          "save_staged_map",
          dict(token_from(params, stage_id), staging_id=stage_id),
          timeout_s=120,
        )
      self.journal.update(rid, phase="staged", staged=staged)
      source_config = staged["snapshot"]["source_config"]
      source_deps = [
        d
        for d in staged["snapshot"]["dependency_manifest"]
        if not (
          d["logical"].startswith("packages/mcp/")
          and d["logical"].endswith("/level.cfg")
        )
      ]
      # A map-specific cfg can legitimately be absent; don't synthesize a failing exec.
      if source_config and not any(d["logical"] == source_config for d in source_deps):
        source_config = ""
      wrapper = wrapper_for(source_config)
      report = await self.validate(staged, logical, wrapper, work)
      source_deps = [
        d
        for d in report["fingerprint"]["dependency_manifest"]
        if d["logical"] != "packages/" + logical + ".cfg"
      ]
      self.journal.update(rid, phase="validated", validation=report)
      (stage_dir / "level.cfg").write_text(wrapper)
      result = dict(
        artifact_id=rid,
        logical_map_name=logical,
        map_epoch=staged["map_epoch"],
        revision_saved=staged["revision"],
        lighting_dirty=staged["lighting_dirty"],
        clean_ack="pending",
      )
      manifest = dict(
        schema_version=1,
        request_id=rid,
        input_hash=record["input_hash"],
        payload_sha256=staged["payload_sha256"],
        wrapper_sha256=hashlib.sha256(wrapper.encode()).hexdigest(),
        source_config=source_config,
        dependency_manifest=source_deps,
        snapshot=staged["snapshot"],
        result=result,
      )
      atomic_json(stage_dir / "manifest.json", manifest)
      result = dict(result, manifest_sha256=sha256_file(stage_dir / "manifest.json"))
      for name in ("level.ogz", "level.cfg", "manifest.json"):
        with (stage_dir / name).open("rb") as handle:
          os.fsync(handle.fileno())
      directory = os.open(stage_dir, os.O_RDONLY | os.O_DIRECTORY)
      try:
        os.fsync(directory)
      finally:
        os.close(directory)
      dependencies_unchanged(source_deps)
      os.rename(stage_dir, destination)
      directory = os.open(self.bundles, os.O_RDONLY | os.O_DIRECTORY)
      try:
        os.fsync(directory)
      finally:
        os.close(directory)
      self.journal.update(rid, phase="published", result=result)
      return await self.acknowledge(params, result)
    except OSError as exc:
      failure = BridgeError("IO_ERROR", str(exc))
      if not destination.exists():
        self.journal.update(
          rid,
          phase="failed",
          error=dict(code=failure.code, message=failure.message, details={}),
        )
      raise failure from exc
    except BridgeError as exc:
      if not destination.exists():
        if exc.code == "OUTCOME_UNKNOWN":
          self.journal.update(rid, phase="unknown")
        else:
          self.journal.update(
            rid,
            phase="failed",
            error=dict(code=exc.code, message=exc.message, details=exc.details),
          )
      raise
    finally:
      if destination.exists() and work.exists() and not work.is_symlink():
        shutil.rmtree(work)

  def sync_bundle(self, bundle):
    for name in ("level.ogz", "level.cfg", "manifest.json"):
      with (bundle / name).open("rb") as handle:
        os.fsync(handle.fileno())
    for path in (bundle, bundle.parent):
      fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
      try:
        os.fsync(fd)
      finally:
        os.close(fd)

  def read_manifest(self, bundle, check_dependencies=True):
    if bundle.is_symlink():
      raise BridgeError("DEPENDENCY_CHANGED", "bundle is a symlink")
    path = bundle / "manifest.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
      raise BridgeError("DEPENDENCY_CHANGED", "invalid artifact manifest")
    manifest = json.loads(path.read_text())
    for name, key in [("level.ogz", "payload_sha256"), ("level.cfg", "wrapper_sha256")]:
      file = bundle / name
      if file.is_symlink() or sha256_file(file) != manifest[key]:
        raise BridgeError("DEPENDENCY_CHANGED", f"artifact {name} changed")
    if check_dependencies:
      dependencies_unchanged(manifest["dependency_manifest"])
    return manifest

  async def open_map(self, params):
    rid = params["request_id"]
    public_hash = hashlib.sha256(canonical(params).encode()).hexdigest()
    previous = await self.bridge.call("operation_status", {"request_id": rid})
    if previous["status"] != "unknown":
      if previous.get("public_input_hash") != public_hash:
        raise BridgeError("REQUEST_ID_REUSED", "open UUID has different input")
      if previous["status"] == "completed":
        return previous["result"]
      raise BridgeError(**previous["error"])
    provenance = ""
    manifest = None
    if params.get("artifact_id"):
      aid = params["artifact_id"]
      if not re.fullmatch(r"[0-9a-f-]{36}", aid):
        raise BridgeError("INVALID_ARGUMENT", "invalid artifact ID")
      bundles = list(self.bundles.glob(f"*-{aid}"))
      if len(bundles) != 1:
        raise BridgeError("RESOURCE_NOT_FOUND", "artifact not found")
      bundle = bundles[0]
      manifest = self.read_manifest(bundle)
      payload = bundle / "level.ogz"
      logical = manifest["result"]["logical_map_name"]
      provenance = manifest["source_config"]
    else:
      row = self.maps().get(params["map_id"])
      if row is None:
        raise BridgeError("RESOURCE_NOT_FOUND", "map not found in catalog")
      payload = row["path"]
      logical = row["logical"]
    state = await self.bridge.call("editor_state", {})
    if (
      state["map_epoch"] != params["map_epoch"]
      or state["revision"] != params["expected_revision"]
    ):
      raise BridgeError("STALE_STATE", "active map changed")
    if state["dirty"] and not state["recovery_required"]:
      raise BridgeError("UNSAVED_CHANGES", "save current map before opening another")
    validate_gzip(payload)
    digest = sha256_file(payload)
    pinned = self.inputs / f"{rid}.ogz"
    if pinned.exists():
      if pinned.is_symlink() or sha256_file(pinned) != digest:
        raise BridgeError("REQUEST_ID_REUSED", "ticket has different payload")
    else:
      with payload.open("rb") as source, pinned.open("xb") as target:
        shutil.copyfileobj(source, target)
        target.flush()
        os.fsync(target.fileno())
    if sha256_file(pinned) != digest or sha256_file(payload) != digest:
      raise BridgeError("DEPENDENCY_CHANGED", "map changed while pinning")
    work = self.private / f"open-{rid}"
    try:
      validator = Validator(
        self.client, self.environment["data_root"], [self.environment["profile_root"]]
      )
      validation_input = dict(
        mode="inspect",
        logical_name=logical,
        payload_path=str(pinned),
        payload_sha256=digest,
      )
      if manifest:
        validation_input.update(
          mode="compare",
          content_digest=manifest["snapshot"]["content_digest"],
          config_digest=manifest["snapshot"]["config_digest"],
        )
      try:
        async with self.validation_slots:
          report = await validator.run(validation_input, work)
      except BridgeError as exc:
        if manifest and exc.code == "UNSUPPORTED_CONFIG":
          raise BridgeError(
            "DEPENDENCY_CHANGED",
            "saved artifact no longer reloads with its recorded semantics",
            exc.details,
          ) from exc
        raise
      if manifest:
        observed = {
          d["logical"]: d for d in report["fingerprint"]["dependency_manifest"]
        }
        expected = {d["logical"]: d for d in manifest["dependency_manifest"]}
        wrapper = "packages/" + logical + ".cfg"
        actual = {k: v for k, v in observed.items() if k != wrapper}
        if actual != expected:
          raise BridgeError(
            "DEPENDENCY_CHANGED", "saved artifact dependencies resolve differently"
          )
        if observed.get(wrapper, {}).get("sha256") != manifest["wrapper_sha256"]:
          raise BridgeError("DEPENDENCY_CHANGED", "generated wrapper identity changed")
      deps = report["fingerprint"]["dependency_manifest"]
      dependencies_unchanged(deps)
      if manifest:
        self.read_manifest(bundle)
      command = dict(
        token_from(params),
        ticket_id=rid,
        logical_name=logical,
        payload_sha256=digest,
        dependency_manifest=deps,
        provenance_config=provenance,
        public_input_hash=public_hash,
        content_digest=report["fingerprint"]["content_digest"],
        config_digest=report["fingerprint"]["config_digest"],
      )
      if manifest:
        command["lighting_dirty"] = manifest["result"]["lighting_dirty"]
      return await self.bridge.call("commit_open", command, timeout_s=120)
    finally:
      if work.exists() and not work.is_symlink():
        shutil.rmtree(work)
