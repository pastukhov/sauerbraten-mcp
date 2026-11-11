"""Single-writer bounded connection to the opt-in game bridge."""

import asyncio
import json
import uuid
from pathlib import Path


class BridgeError(Exception):
  def __init__(self, code, message="", details=None):
    self.code, self.message, self.details = code, message, details or {}
    super().__init__(f"{code}: {message}")


class BridgeClient:
  def __init__(self, socket_path: str):
    if not Path(socket_path).is_absolute():
      raise ValueError("absolute socket path required")
    self.path = socket_path
    self.reader = self.writer = None
    self.lock = asyncio.Lock()

  async def close(self, abort=False):
    writer = self.writer
    self.reader = self.writer = None
    if writer:
      if abort:
        writer.transport.abort()
      else:
        writer.close()
      try:
        await writer.wait_closed()
      except (OSError, ConnectionError):
        pass

  async def call(self, method: str, params: dict, timeout_s: float = 5.0) -> dict:
    async with self.lock:
      sent = False
      try:
        async with asyncio.timeout(timeout_s):
          if self.writer is None:
            self.reader, self.writer = await asyncio.open_unix_connection(
              self.path, limit=1024 * 1024
            )
          request_id = str(uuid.uuid4())
          packet = (
            json.dumps(
              {"v": 1, "id": request_id, "method": method, "params": params},
              allow_nan=False,
              separators=(",", ":"),
              sort_keys=True,
            ).encode()
            + b"\n"
          )
          if len(packet) > 1024 * 1024:
            raise BridgeError("LIMIT_EXCEEDED", "request exceeds 1 MiB")
          self.writer.write(packet)
          sent = True
          await self.writer.drain()
          raw = await self.reader.readline()
          if not raw:
            raise ConnectionError("bridge disconnected")
          response = json.loads(raw)
          if not isinstance(response, dict):
            raise BridgeError("OUTCOME_UNKNOWN", "invalid bridge envelope")
          if response.get("error", {}).get("code") == "BUSY":
            raise BridgeError("BUSY", "bridge already attached")
          if response.get("id") != request_id or response.get("v") != 1:
            raise BridgeError("OUTCOME_UNKNOWN", "unexpected bridge response")
          if not response.get("ok"):
            err = response.get("error", {})
            raise BridgeError(
              err.get("code", "IO_ERROR"), err.get("message", ""), err.get("details")
            )
          return response["result"]
      except asyncio.CancelledError:
        await self.close(abort=True)
        raise
      except BridgeError as exc:
        if exc.code in ("BUSY", "OUTCOME_UNKNOWN"):
          await self.close(abort=True)
        raise
      except (
        TimeoutError,
        ConnectionError,
        OSError,
        ValueError,
        asyncio.LimitOverrunError,
      ) as exc:
        await self.close(abort=True)
        # Never reconnect-and-repeat a possibly executed command.
        raise BridgeError(
          "OUTCOME_UNKNOWN" if sent else "NOT_CONNECTED", str(exc)
        ) from exc
