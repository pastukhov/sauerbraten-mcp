"""Supervise an isolated, initialized SDL/GL validation client."""

import asyncio
import gzip
import hashlib
import json
import os
from pathlib import Path
import signal
import struct
import uuid
from .bridge import BridgeError


def sha256_file(path):
  with Path(path).open("rb") as handle:
    return hashlib.file_digest(handle, "sha256").hexdigest()


def validate_gzip(path):
  try:
    with gzip.open(path, "rb") as handle:
      header = handle.read(40)
      if len(header) < 28 or header[:4] != b"OCTA":
        raise ValueError("invalid map header")
      version, header_size, worldsize, numents = struct.unpack_from("<4i", header, 4)
      if (
        not 1 <= version <= 33
        or not 2 <= worldsize <= 65536
        or worldsize & (worldsize - 1)
        or not 0 <= numents <= 10000
      ):
        raise ValueError("unsupported map header")
      total = len(header)
      while chunk := handle.read(1024 * 1024):
        total += len(chunk)
        if total > 256 * 1024 * 1024:
          raise ValueError("decompressed map exceeds 256 MiB")
  except (OSError, EOFError, ValueError) as exc:
    raise BridgeError("IO_ERROR", f"map validation failed: {exc}") from exc


class Validator:
  def __init__(self, client, data_root, package_roots):
    self.client = str(Path(client).resolve())
    self.data_root = str(Path(data_root).resolve())
    self.roots = [str(Path(p).resolve()) for p in package_roots]

  async def run(self, params, work: Path, timeout_s: float = 120.0):
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile = work / "profile"
    profile.mkdir(mode=0o700, exist_ok=True)
    input_path = work / "input.json"
    report = work / "report.json"
    input_path.write_text(
      json.dumps(dict(v=1, id=str(uuid.uuid4()), method="validate", params=params))
    )
    args = [
      "xvfb-run",
      "-a",
      self.client,
      f"-q{profile}",
      *[f"-k{r}" for r in self.roots],
      "-t0",
      "-w640",
      "-h480",
      "--mcp-validate-input",
      str(input_path),
      "--mcp-report",
      str(report),
    ]
    with (work / "client.log").open("wb") as log:
      process = await asyncio.create_subprocess_exec(
        *args, cwd=self.data_root, stdout=log, stderr=log, start_new_session=True
      )
      try:
        await asyncio.wait_for(process.wait(), timeout_s)
      except (TimeoutError, asyncio.CancelledError):
        try:
          os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
          pass
        try:
          await asyncio.wait_for(process.wait(), 1)
        except TimeoutError:
          try:
            os.killpg(process.pid, signal.SIGKILL)
          except ProcessLookupError:
            pass
          await process.wait()
        raise BridgeError("IO_ERROR", "validator timed out or cancelled")
    if (
      not report.is_file() or report.is_symlink() or report.stat().st_size > 256 * 1024
    ):
      raise BridgeError("IO_ERROR", "validator produced no valid report")
    try:
      result = json.loads(report.read_text())
    except (OSError, ValueError) as exc:
      raise BridgeError("IO_ERROR", "invalid validator report") from exc
    if (
      process.returncode
      or result.get("schema_version") != 1
      or result.get("ok") is not True
    ):
      raise BridgeError(
        "UNSUPPORTED_CONFIG", "map reload or semantic comparison failed", result
      )
    return result
