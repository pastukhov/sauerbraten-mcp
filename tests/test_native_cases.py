from conftest import CLIENT, DATA
import json
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.native


@pytest.mark.parametrize(
  "case",
  [
    "deformation_validation",
    "boot",
    "exact_faces",
    "undo_interleave",
    "read_boundaries",
    "entity_boundary_selected",
    "entity_boundary_hover",
    "attached_history",
    "history_accounting",
    "allocation_failure",
    "native_hooks",
    "aux_fingerprint",
    "capture_error",
    "remote_native_history",
    "config_edits",
    "camera_modes",
    "rpc_view_state",
    "shader_registry",
  ],
)
def test_initialized_engine_cases(tmp_path, case):
  report = tmp_path / "report.json"
  result = subprocess.run(
    [
      "xvfb-run",
      "-a",
      str(CLIENT),
      f"-q{tmp_path}/profile",
      "-t0",
      "-w640",
      "-h480",
      "--mcp-test-case",
      case,
      "--mcp-report",
      str(report),
    ],
    cwd=DATA,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    timeout=30,
  )
  assert result.returncode == 0, result.stdout.decode(errors="replace")[-1000:]
  assert json.loads(report.read_text())["ok"] is True
