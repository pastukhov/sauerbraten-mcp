import io
import os
from pathlib import Path
import stat
import time
from PIL import Image, UnidentifiedImageError
from .bridge import BridgeError
from .export_journal import UUID_RE, private_dir


class ArtifactStore:
  def __init__(self, root):
    self.root = private_dir(root)

  def cleanup(self):
    files = []
    for path in self.root.iterdir():
      info = path.lstat()
      if (
        path.suffix == ".png"
        and UUID_RE.fullmatch(path.stem)
        and stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
      ):
        files.append((info.st_mtime, info.st_size, path))
    files.sort()
    total = sum(size for _, size, _ in files)
    for modified, size, path in files:
      if time.time() - modified > 600 or total > 64 * 1024 * 1024:
        path.unlink()
        total -= size

  def read_image(self, aid):
    if not UUID_RE.fullmatch(aid):
      raise BridgeError("INVALID_ARGUMENT", "invalid image artifact ID")
    self.cleanup()
    path = self.root / f"{aid}.png"
    try:
      fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
      with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (
          not stat.S_ISREG(info.st_mode)
          or info.st_uid != os.getuid()
          or not 32 <= info.st_size <= 4 * 1024 * 1024
        ):
          raise ValueError("invalid artifact size/owner")
        data = handle.read(4 * 1024 * 1024 + 1)
      with Image.open(io.BytesIO(data)) as image:
        if image.format != "PNG" or image.width > 1280 or image.height > 720:
          raise ValueError("unexpected PNG dimensions")
        image.verify()
      return data
    except (OSError, ValueError, UnidentifiedImageError) as exc:
      raise BridgeError("IO_ERROR", f"invalid image artifact: {exc}") from exc
