from __future__ import annotations
import os
from pathlib import Path
from ..parsers import SUPPORTED, parse, parse_eml_bytes
from .base import RawDoc

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".Trash", "Library", ".venv"}


class FolderConnector:
    """Cartelle locali, OneDrive e SharePoint (sincronizzati dal client OneDrive)."""

    def __init__(self, source):
        self.source = source
        self.root = source.path

    def roots(self):
        return [self.root]

    def scan(self):
        if not self.root.exists():
            raise FileNotFoundError(f"{self.root} non esiste")
        for dp, dns, fns in os.walk(self.root):
            dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".")]
            for fn in fns:
                p = Path(dp) / fn
                ext = p.suffix.lower()
                if ext not in SUPPORTED or fn.startswith(("~$", ".")):
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                # file OneDrive "solo online" (placeholder): saltati per non scatenare il download
                if getattr(st, "st_blocks", 1) == 0 and st.st_size > 0:
                    continue
                rel = p.relative_to(self.root)
                title = p.name
                if ext == ".eml":
                    title = parse_eml_bytes(p.read_bytes())[0] or p.name
                yield RawDoc(self.source.name, str(rel), title, f"{self.source.name}/{rel}", p,
                             ext.lstrip("."), st.st_mtime, st.st_size, lambda p=p: parse(p))
