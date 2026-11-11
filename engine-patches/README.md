# Native engine patch series

Prepared against the official repository:
`https://svn.code.sf.net/p/sauerbraten/code/` at **r6891** (verified 2026-11-11).
The local source/data baseline is Git `747c28be4459fddacfccc4f98d5977d3d911a060`;
the resulting tree is `522cda246694a158bdd222e2cd313d143d503b9f`.
`series` records patch order; `SHA256SUMS` verifies the patch bytes.

This is a proposed opt-in Linux editor bridge, not an accepted upstream change.
There is no Python SDK or game media in these patches. The game exposes a typed
private Unix-socket protocol on its main thread; the separate Python server
provides the MCP SDK, validation supervisor and persistent export journal.

1. Native bridge, editor adapter, build gating, cJSON and native regression tests.
2. English usage documentation and portable build/launch helpers.

## Apply and verify

From a complete, untouched r6891 checkout, with this repository elsewhere:

```sh
cd /absolute/sauerbraten-mcp/engine-patches
sha256sum -c SHA256SUMS
cd /absolute/sauerbraten
while IFS= read -r name; do
  patch -p1 < "/absolute/sauerbraten-mcp/engine-patches/$name"
done < /absolute/sauerbraten-mcp/engine-patches/series
make -C src -j2 client server master
make -C src -j2 MCP=1 client server master
make -C src MCP=1 test-mcp
sh tools/tests/run_editor_smoke.sh
sha256sum -c src/vendor/cjson/SHA256SUMS
```

Alternatively import the pristine source/data snapshot into Git and apply the
ordered files with `git am --whitespace=nowarn`. `baseline.sha256` records the
original 297 source/data files; media are excluded and must come from Sauerbraten.
Do not use unrelated release cfg/media as proof of current-upstream acceptance.
The stock r6891 data references missing `data/menus/browse_textures2.cfg`; the
same warning occurs on the untouched baseline and is outside these patches.

## Review notes

Ordinary builds retain bundled ENet and do not require cJSON or libcrypto.
Only `MCP=1` client builds enable new integration; standalone server/master stay
unchanged. Enabled objects/dependencies are separate from default objects, and
toggling modes rebuilds the appropriate client. Linux-only support is explicit;
Windows/macOS project files and ordinary platform paths remain unchanged.

The native suite uses production compiler flags and initialized SDL/GL cases.
The separate server suite additionally checks real SDK/stdio calls, validated
immutable saves/reopen, history boundaries, screenshots and recovery. Integration
uses owned temporary profiles, not a running user's game. Native loading remains
destructive after reset and save/load may pause rendering. Custom shaders and
other unreconstructible cfg are explicitly unsupported. See the included
`docs/mcp-editor.md` for protocol constraints, failure handling and budgets.

Existing Sauerbraten source notices and vendored cJSON MIT notices are preserved.
The surrounding Python repository's LICENSE does not replace their licenses.
