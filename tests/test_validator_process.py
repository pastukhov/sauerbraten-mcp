import pytest
from sauer_mcp.validator import Validator
from sauer_mcp.bridge import BridgeError


@pytest.mark.asyncio
async def test_supervisor_rejects_missing_report(tmp_path):
  validator = Validator("/bin/true", str(tmp_path), [])
  with pytest.raises(BridgeError, match="IO_ERROR"):
    await validator.run({}, tmp_path / "work", timeout_s=1)


@pytest.mark.asyncio
async def test_supervisor_reaps_timeout(tmp_path):
  fake = tmp_path / "client"
  fake.write_text("#!/bin/sh\nsleep 60\n")
  fake.chmod(0o700)
  validator = Validator(str(fake), str(tmp_path), [])
  with pytest.raises(BridgeError, match="IO_ERROR"):
    await validator.run({}, tmp_path / "work", timeout_s=0.05)
