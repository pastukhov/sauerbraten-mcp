import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session


@pytest.mark.asyncio
async def test_sdk_initialize_and_list():
  server = FastMCP("smoke")

  @server.tool()
  def smoke() -> str:
    return "ok"

  async with create_connected_server_and_client_session(server._mcp_server) as client:
    result = await client.list_tools()
    assert [tool.name for tool in result.tools] == ["smoke"]
