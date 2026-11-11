"""Standard SDK MCP stdio server; game process stdout never enters this stream."""

import argparse
import asyncio
import json
import os
import shutil
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types
from .bridge import BridgeClient, BridgeError
from .models import schemas
from .service import EditorService

DESCRIPTIONS = {
  "editor_state": "Read current map/session/revision, camera and editor capabilities. Read before mutations.",
  "operation_status": "Look up a mutation UUID; unknown means do not blindly repeat it.",
  "map_catalog": "List trusted local maps. Use opaque map_id values with map_open.",
  "map_open": "Validate and open a catalog map or saved artifact; refuses an unsaved map.",
  "map_new": "Create a native map (bottom half solid). Scale 10..16; refuses unsaved changes.",
  "region_read": "Read octree leaves in half-open world bounds, without subdividing geometry.",
  "texture_catalog": "List texture references. Preserve map_epoch and vslot_generation when applying.",
  "mapmodel_catalog": "List mapmodel registry references for entity_create/update.",
  "geometry_fill": "Replace an aligned selection with solid or empty cubes. count is in cells, grid in world units.",
  "geometry_face_set": "Set four absolute face-axis depths (0..8 in grid/8 units) on each exact nonempty leaf. Face=selection.orient; order (R,C)=00,10,01,11; X uses Y/Z, Y uses Z/X, Z uses X/Y. Atomic and undoable; invalid/zero-volume shapes rejected.",
  "geometry_vertex_set": "Set one logical corner (XYZ bits) to absolute local XYZ coordinates 0..8 in grid/8 units on each exact nonempty leaf. Neighbors/coincident corners are not moved implicitly. Atomic and undoable; invalid shapes rejected.",
  "texture_apply": "Set exactly one orientation (0..5) or all six faces of an aligned selection.",
  "material_apply": "Apply a built-in volume/clip material to an aligned selection.",
  "entity_list": "List entities and generation-bound references, optionally in world bounds.",
  "entity_create": "Create light/playerstart/mapmodel at explicit coordinates with five attrs; no camera/drop fixups.",
  "entity_update": "Replace position/attrs of a referenced entity. Attached entities are unsupported.",
  "entity_delete": "Delete a referenced entity as one undoable step.",
  "history_undo": "Undo the top shared manual/MCP history step; read state afterwards.",
  "history_redo": "Redo the top shared manual/MCP history step; read state afterwards.",
  "camera_set": "Set edit camera coordinates, yaw/pitch and FOV; returns a view_revision for capture.",
  "view_capture": "Capture the requested map/view revision as PNG image content.",
  "map_save_as": "Validate reload and publish an immutable map snapshot; returns revision_saved and conditional clean_ack.",
}
READONLY = {
  "editor_state",
  "operation_status",
  "map_catalog",
  "region_read",
  "texture_catalog",
  "mapmodel_catalog",
  "entity_list",
  "view_capture",
}


def create_server(socket_path, client=None, service=None):
  server = Server("sauerbraten-editor")
  if service is None:
    if client is None:
      client = (
        os.environ.get("SAUER_MCP_CLIENT")
        or shutil.which("sauer_client")
        or "/usr/lib/cube2/cube2_client"
      )
    service = EditorService(BridgeClient(socket_path), client)

  @server.list_tools()
  async def list_tools():
    return [
      types.Tool(
        name=name,
        description=DESCRIPTIONS[name],
        inputSchema=schema,
        annotations=types.ToolAnnotations(
          readOnlyHint=name in READONLY,
          destructiveHint=name
          in {"geometry_fill", "geometry_face_set", "geometry_vertex_set", "entity_delete", "map_new", "map_open"},
          idempotentHint=name in READONLY,
          openWorldHint=False,
        ),
      )
      for name, schema in schemas().items()
    ]

  @server.call_tool(validate_input=False)
  async def call_tool(name, arguments):
    try:
      result = await service.call(name, arguments or {})
      contents = []
      if name == "view_capture" and "image_b64" in result:
        result = dict(result)
        image = result.pop("image_b64")
        contents.append(
          types.ImageContent(type="image", data=image, mimeType="image/png")
        )
      contents.append(
        types.TextContent(
          type="text", text=json.dumps(result, ensure_ascii=False, allow_nan=False)
        )
      )
      return types.CallToolResult(
        content=contents, structuredContent=result, isError=False
      )
    except BridgeError as exc:
      data = {"code": exc.code, "message": exc.message, "details": exc.details}
      return types.CallToolResult(
        content=[
          types.TextContent(type="text", text=json.dumps(data, ensure_ascii=False))
        ],
        structuredContent={"error": data},
        isError=True,
      )
    except (OSError, ValueError) as exc:
      data = {"code": "IO_ERROR", "message": str(exc)}
      return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(data))], isError=True
      )

  return server


async def main_async():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--socket", required=True)
  parser.add_argument("--client")
  args = parser.parse_args()
  server = create_server(args.socket, args.client)
  async with stdio_server() as (reader, writer):
    await server.run(reader, writer, server.create_initialization_options())


def main():
  asyncio.run(main_async())
