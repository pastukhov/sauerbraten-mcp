# Sauerbraten MCP

A local MCP server for editing levels in a running Sauerbraten (Cube 2) game.
It exposes 22 typed tools over stdio and connects to an opt-in native bridge
through a private Unix socket. Linux and single-player editing are supported.
This is an unofficial extension; it is not part of an official Sauerbraten release.

The Python server lives here. Native engine changes are prepared separately for
**official Sauerbraten SVN r6891** and included in [engine-patches](engine-patches/).
The repository contains no game assets, personal maps, profiles or credentials.
An unmodified game does not provide the required socket protocol.

## Build the engine

Use a complete Sauerbraten checkout, including current `data/` and `packages/`:

```sh
svn checkout -r 6891 https://svn.code.sf.net/p/sauerbraten/code/ sauerbraten
cd sauerbraten
# Use an absolute path to this repository:
while IFS= read -r name; do
  patch -p1 < "/absolute/sauerbraten-mcp/engine-patches/$name"
done < /absolute/sauerbraten-mcp/engine-patches/series
make -C src -j2 MCP=1 client
```

Build dependencies: C/C++ compiler, make, SDL2, SDL2_image, SDL2_mixer, zlib,
OpenGL and OpenSSL development headers/libcrypto. The engine keeps its bundled
ENet. The default build and standalone server/master do not include MCP or its
JSON/crypto dependencies. Enabled client objects use a separate `src/.mcp/`
directory, so switching build modes does not require cleaning.

## Install and connect

Install Python >=3.11 and [uv](https://docs.astral.sh/uv/), then:

```sh
git clone https://github.com/pastukhov/sauerbraten-mcp.git
cd sauerbraten-mcp
uv sync --locked
```

Map validation also needs `Xvfb` and `xauth`. Validators use a separate SDL/GL
client; use the same patched executable and asset installation as the live game.

Start the patched game in a terminal, from its full resource checkout:

```sh
cd /absolute/sauerbraten
umask 077
sauer_runtime=$(mktemp -d "${XDG_RUNTIME_DIR:-/tmp}/sauer-mcp.XXXXXX")
printf 'Socket: %s/editor.sock\n' "$sauer_runtime"
tools/run-editor.sh \
  --client "$PWD/src/sauer_client" \
  --data-root "$PWD" \
  --profile "$HOME/.local/share/sauerbraten-mcp" \
  --socket "$sauer_runtime/editor.sock"
```

The launcher writes game output to the dedicated profile's `client.log`.
Create/open a map in the UI and enter edit mode. Configure your MCP host with
these stdio command/arguments, replacing paths with absolute paths on your machine:

```json
{
  "command": "uv",
  "args": [
    "run", "--locked", "--project", "/absolute/sauerbraten-mcp", "sauer-mcp",
    "--client", "/absolute/sauerbraten/src/sauer_client",
    "--socket", "/absolute/private-directory/editor.sock"
  ]
}
```

Use the socket path printed by the launcher. The server attaches to an existing
game and does not start or stop it. It can reconnect after restarting. Only one
peer is allowed; another receives `BUSY`. The socket has mode 0600, its parent
must have mode 0700 and the peer must have the same UID. The bridge is disabled
unless the game is started with `-mcpbridge<absolute-path>`.

Always pass `--client` explicitly for a source build. Otherwise the server checks
`SAUER_MCP_CLIENT`, then `sauer_client` on PATH, then the Debian installation path
`/usr/lib/cube2/cube2_client`. An unpatched or different executable can make
validation fail even when the live connection works. MCP stdout carries only
the protocol.

## Tools and editing rules

| Purpose | Tools |
| --- | --- |
| Observe | editor_state, region_read, entity_list, texture_catalog, mapmodel_catalog |
| Geometry | geometry_fill, geometry_face_set, geometry_vertex_set, texture_apply, material_apply |
| Entities | entity_create, entity_update, entity_delete |
| History | history_undo, history_redo |
| View | camera_set, view_capture |
| Maps and recovery | map_catalog, map_new, map_open, map_save_as, operation_status |

Read `editor_state` first. Each mutation requires a fresh UUID `request_id`,
current `map_epoch` and `expected_revision`. Revisions and generations are
canonical decimal strings. Use catalog resource references rather than guessing
IDs. Schemas are advertised through `tools/list` and recorded in
[contracts/v1.json](contracts/v1.json).

If a reply is lost, retain the UUID and exact arguments and query
`operation_status`. An unknown outcome is not permission to repeat an edit. UUID
reuse with different arguments is rejected. Native manual edits and MCP edits
share undo/redo, with one history step per RPC. Pending native drag/onrelease
commands return `BUSY`.

Geometry supports aligned solid/empty fills, face deformation and vertex
movement. `geometry_face_set` sets four absolute depths 0..8 along the selected
face axis in grid/8 units; even negative faces use absolute coordinates. The
order is (R,C)=00,10,01,11: X faces use Y/Z, Y faces use Z/X, Z faces use X/Y.
`geometry_vertex_set` selects a logical corner with XYZ bits and sets its local
XYZ position 0..8. Both affect every selected cell and require existing nonempty
leaf cubes exactly matching the grid; fill explicitly first when establishing
new geometry. They preserve textures/materials. Neighbors and coincident corners
are not moved implicitly. Inverted, nonconvex and zero-volume shapes are rejected
before any change. Each successful change is one shared undo step; no-ops do not
consume history or clear redo. Textures can target one exact face or all faces. Entities support light, playerstart and mapmodel; attached entities/history
are rejected. Reads preserve the native selection and focus. `camera_set` selects
first-person and disables zoom; `view_capture` requires `view_revision` and
returns PNG MCP ImageContent with state tokens.

## Persistence and limits

`map_new` and `map_open` refuse to replace an unsaved world. Opening pins and
validates the source in a separate process before committing in the game.
Native loading is destructive: a failure after reset reports the new state and
recovery information; ordinary edits are blocked while `recovery_required` is set.

`map_save_as` creates an immutable bundle in the dedicated profile:
`packages/mcp/<slug>-<request_uuid>/{level.ogz,level.cfg,manifest.json}`. It does not
overwrite source maps. Exports compare semantic/config fingerprints after reload
and depend on this installation's trusted assets/cfg. They are not standalone
media packages. Changed or shadowed dependencies block reopening before reset.
A stale clean acknowledgement leaves the live map dirty. The persistent export
journal records outcomes across transport failures and server restarts; keep it
and published bundles for recovery.

Custom shader declarations/variants/alternate/fast/deferred definitions are not
supported. Manual custom shader changes disable export/open/new for that process
until a game restart; `editor_state` explains the reason. Standard lazy shaders
remain supported. Other unreconstructible or name-dependent cfg may disable
export. Native save/load is synchronous and can pause frames. Baking lighting,
coop, batch, terrain/blend editing are outside this version.

Mutations allow 4096 cells, 65536 visited nodes, 8 MiB snapshot and 16 MiB inverse
preparation. Reads return at most 256 leaves / 4096 nodes per page. Captures are
at most 1280x720 with a 10-minute TTL and 64 MiB owned cache. There is no arbitrary
CubeScript, shell or file-operation tool.

## Tests

```sh
uv run --locked pytest -q
# Full integration: explicitly select the patched engine with complete assets.
SAUER_MCP_ENGINE=/absolute/sauerbraten uv run --locked pytest -q
# Native-only suite, from the engine checkout:
make -C /absolute/sauerbraten/src MCP=1 test-mcp
```

The first command runs isolated Python tests and explicitly skips live/native
cases. The second runs the complete suite, including initialized engine cases,
stdio/SDK calls, native history, camera/PNG, validated saves/reopen, persistence
and recovery. Integration needs Xvfb/xauth and actual assets. Tests use temporary
profiles/sockets and never attach to an existing game. To test an installed
binary, set both `SAUER_MCP_CLIENT` and `SAUER_MCP_DATA` instead; the source fixture
comparison is then skipped. Explicit invalid paths are errors rather than silent
skips. The native suite needs only Python 3 stdlib, not the MCP SDK.

The Python dependencies are locked in `uv.lock`; the MCP SDK is pinned to
`mcp==1.26.0`. Extension code uses the [zlib license](LICENSE). The engine patch
series preserves Sauerbraten's notices and the MIT license of vendored cJSON.
