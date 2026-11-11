import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from sauer_mcp.server import create_server


@pytest.mark.asyncio
async def test_mcp_tools_and_disconnected_error(tmp_path):
  server = create_server(str(tmp_path / "not-running.sock"))
  async with create_connected_server_and_client_session(server) as client:
    tools = await client.list_tools()
    assert len(tools.tools) == 22
    names = {t.name for t in tools.tools}
    assert {"geometry_fill", "map_save_as", "view_capture"} <= names
    result = await client.call_tool("editor_state", {})
    assert result.isError and "NOT_CONNECTED" in result.content[0].text
    invalid = await client.call_tool("region_read", {"unexpected": 1})
    assert invalid.isError and "INVALID_ARGUMENT" in invalid.content[0].text


@pytest.mark.asyncio
async def test_real_stdio_process_protocol(tmp_path):
  from mcp import ClientSession, StdioServerParameters
  from mcp.client.stdio import stdio_client
  from pathlib import Path

  root = Path(__file__).resolve().parents[1]
  params = StdioServerParameters(
    command="uv",
    args=[
      "run",
      "--project",
      str(root),
      "python",
      "-m",
      "sauer_mcp",
      "--socket",
      str(tmp_path / "none.sock"),
    ],
  )
  async with stdio_client(params) as (reader, writer):
    async with ClientSession(reader, writer) as session:
      await session.initialize()
      assert len((await session.list_tools()).tools) == 22
      assert (await session.call_tool("editor_state", {})).isError
