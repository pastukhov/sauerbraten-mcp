"""Durable public-export outcomes, independent of the engine's bounded LRU."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid
from .bridge import BridgeError

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def canonical(value):
  return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def private_dir(path):
  path = Path(path).absolute()
  current = Path(path.anchor)
  for component in path.parts[1:]:
    if component in (".", ".."):
      raise BridgeError("IO_ERROR", "directory must be canonical")
    current = current / component
    if current.is_symlink():
      raise BridgeError("IO_ERROR", "symlink component in owned directory")
  path.mkdir(parents=True, exist_ok=True, mode=0o700)
  info = path.lstat()
  if (
    not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
  ):
    raise BridgeError("IO_ERROR", "owned directory must have mode 0700")
  return path


def atomic_json(path, value):
  path = Path(path)
  if path.is_symlink():
    raise BridgeError("IO_ERROR", "refusing journal symlink")
  temporary = path.with_name(f".{path.name}.{uuid.uuid4()}.tmp")
  fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
  try:
    with os.fdopen(fd, "w") as handle:
      handle.write(canonical(value) + "\n")
      handle.flush()
      os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
      os.fsync(directory)
    finally:
      os.close(directory)
  finally:
    temporary.unlink(missing_ok=True)


class ExportJournal:
  def __init__(self, root):
    self.root = private_dir(root)

  def path(self, rid):
    if not UUID_RE.fullmatch(rid):
      raise BridgeError("INVALID_ARGUMENT", "invalid request UUID")
    return self.root / f"{rid}.json"

  def read(self, rid):
    path = self.path(rid)
    if path.is_symlink():
      raise BridgeError("IO_ERROR", "journal symlink")
    if not path.exists():
      return None
    return json.loads(path.read_text())

  def begin(self, rid, body):
    fingerprint = hashlib.sha256(canonical(body).encode()).hexdigest()
    record = self.read(rid)
    if record:
      if record["input_hash"] != fingerprint:
        raise BridgeError(
          "REQUEST_ID_REUSED", "export UUID already has different input"
        )
      return record
    record = {
      "request_id": rid,
      "input_hash": fingerprint,
      "input": body,
      "phase": "accepted",
    }
    atomic_json(self.path(rid), record)
    return record

  def update(self, rid, **updates):
    record = self.read(rid)
    if record is None:
      raise BridgeError("IO_ERROR", "missing export journal entry")
    record.update(updates)
    atomic_json(self.path(rid), record)
    return record

  def status(self, rid):
    record = self.read(rid)
    if not record:
      return {"status": "unknown"}
    if record["phase"] in ("published", "acked"):
      return {"status": "completed", "result": record["result"]}
    if record["phase"] == "failed":
      return {"status": "failed", "error": record["error"]}
    if record["phase"] == "unknown":
      return {"status": "unknown", "phase": "stage-outcome-unknown"}
    return {"status": "pending", "phase": record["phase"]}
