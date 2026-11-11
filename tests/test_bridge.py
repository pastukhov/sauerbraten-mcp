import asyncio
import json
import pytest
from sauer_mcp.bridge import BridgeClient, BridgeError


@pytest.mark.asyncio
async def test_call_frames_and_correlates(tmp_path):
  path = tmp_path / "sock"

  async def serve(reader, writer):
    req = json.loads(await reader.readline())
    reply = (
      json.dumps(
        {"v": 1, "id": req["id"], "ok": True, "result": {"worldsize": 1024}}
      ).encode()
      + b"\n"
    )
    writer.write(reply[:10])
    await writer.drain()
    writer.write(reply[10:])
    await writer.drain()
    writer.close()

  server = await asyncio.start_unix_server(serve, path)
  async with server:
    client = BridgeClient(str(path))
    assert await client.call("editor_state", {}) == {"worldsize": 1024}
    await client.close()


@pytest.mark.asyncio
async def test_timeout_never_retries_mutation(tmp_path):
  count = 0

  async def serve(reader, writer):
    nonlocal count
    await reader.readline()
    count += 1
    await reader.read()
    writer.close()

  server = await asyncio.start_unix_server(serve, tmp_path / "sock")
  async with server:
    client = BridgeClient(str(tmp_path / "sock"))
    with pytest.raises(BridgeError, match="OUTCOME_UNKNOWN"):
      await client.call("geometry_fill", {}, timeout_s=0.02)
    assert count == 1
    await client.close()


@pytest.mark.asyncio
async def test_cancellation_closes_transport_without_replaying(tmp_path):
  received = asyncio.Event()
  requests = []

  async def serve(reader, writer):
    req = json.loads(await reader.readline())
    requests.append(req["method"])
    if req["method"] == "geometry_fill":
      received.set()
      await asyncio.sleep(0.05)
    reply = {"v": 1, "id": req["id"], "ok": True, "result": {"method": req["method"]}}
    try:
      writer.write(json.dumps(reply).encode() + b"\n")
      await writer.drain()
    except ConnectionError:
      pass
    writer.close()

  server = await asyncio.start_unix_server(serve, tmp_path / "sock")
  async with server:
    client = BridgeClient(str(tmp_path / "sock"))
    task = asyncio.create_task(client.call("geometry_fill", {}))
    await received.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
      await task
    assert client.writer is None
    assert await client.call("editor_state", {}) == {"method": "editor_state"}
    assert requests == ["geometry_fill", "editor_state"]
    await client.close()
    await asyncio.sleep(0.06)
