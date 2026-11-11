import os
from io import BytesIO
from PIL import Image
import pytest
from sauer_mcp.artifacts import ArtifactStore
from sauer_mcp.bridge import BridgeError


def test_image_read_and_symlink_rejection(tmp_path):
  root = tmp_path / "images"
  root.mkdir(mode=0o700)
  aid = "f19acfbb-70e9-46ae-936e-11b822883028"
  image = Image.new("RGB", (20, 10), "blue")
  image.save(root / f"{aid}.png")
  store = ArtifactStore(root)
  assert store.read_image(aid).startswith(b"\x89PNG")
  (root / f"{aid}.png").unlink()
  (root / f"{aid}.png").symlink_to("/etc/passwd")
  with pytest.raises(BridgeError):
    store.read_image(aid)


def test_cleanup_only_owned_artifacts(tmp_path):
  root = tmp_path / "images"
  root.mkdir(mode=0o700)
  aid = "f19acfbb-70e9-46ae-936e-11b822883028"
  Image.new("RGB", (2, 2)).save(root / f"{aid}.png")
  (root / "user-note.txt").write_text("keep")
  os.utime(root / f"{aid}.png", (1, 1))
  ArtifactStore(root).cleanup()
  assert not (root / f"{aid}.png").exists() and (root / "user-note.txt").exists()


def test_parent_symlink_rejected(tmp_path):
  outside = tmp_path / "outside"
  outside.mkdir(mode=0o700)
  (tmp_path / "alias").symlink_to(outside, target_is_directory=True)
  with pytest.raises(BridgeError, match="symlink"):
    ArtifactStore(tmp_path / "alias/images")
