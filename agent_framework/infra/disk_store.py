from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class DiskStore:
    """Local filesystem storage utility."""

    def write_json(self, path: str | Path, data: Any) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def read_json(self, path: str | Path) -> Any:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def write_text(self, path: str | Path, text: str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def read_text(self, path: str | Path) -> str:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def ensure_directory(self, path: str | Path) -> Path:
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def list_files(self, directory: str | Path, pattern: str = "*") -> list[Path]:
        d = Path(directory)
        if not d.exists():
            return []
        return sorted(d.glob(pattern))

    def atomic_write(self, path: str | Path, content: str) -> None:
        """Write atomically using tempfile + rename."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
