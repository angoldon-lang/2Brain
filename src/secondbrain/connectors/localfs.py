from __future__ import annotations
import fnmatch, os
from pathlib import Path
from ..parsers import SUPPORTED, parse, parse_eml_bytes
from .base import RawDoc

SKIP_DIRS = {".Trash", "Library"}
# Esclusi sempre (rumore di sviluppo); a questi si aggiungono quelli scelti per ogni sorgente.
DEFAULT_EXCLUDES = ["node_modules", ".git", "__pycache__", ".venv", "venv", "*.egg-info", ".cache",
                    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "*.lock", "*.log", "*.map", "*.min.js", "*.pyc"]


def is_excluded(rel: Path, patterns) -> bool:
    """Un pattern vale per il nome di una qualsiasi cartella/file del percorso o per il percorso relativo intero."""
    full = str(rel).lower()
    return any(fnmatch.fnmatch(part.lower(), p.lower()) for part in rel.parts for p in patterns) or \
        any(fnmatch.fnmatch(full, p.lower()) for p in patterns)


class FolderConnector:
    """Cartelle locali, OneDrive e SharePoint (sincronizzati dal client OneDrive)."""

    def __init__(self, source):
        self.source = source
        self.root = source.path
        self.skipped: list[tuple[str, str, str]] = []
        self.excludes = DEFAULT_EXCLUDES + [x.strip() for x in source.options.get("exclude", []) if x.strip()]

    def roots(self):
        return [self.root]

    def scan(self):
        self.skipped = []
        if not self.root.exists():
            raise FileNotFoundError(f"{self.root} non esiste")
        for dp, dns, fns in os.walk(self.root):
            rel_dir = Path(dp).relative_to(self.root)
            dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".")
                      and not is_excluded(rel_dir / d, self.excludes)]
            for fn in fns:
                p = Path(dp) / fn
                ext = p.suffix.lower()
                if is_excluded(rel_dir / fn, self.excludes):
                    continue
                if ext not in SUPPORTED or fn.startswith(("~$", ".")):
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                # file OneDrive "solo online" (placeholder): saltati per non scatenare il download
                if getattr(st, "st_blocks", 1) == 0 and st.st_size > 0:
                    rel = p.relative_to(self.root)
                    self.skipped.append((str(rel), f"{self.source.name}/{rel}", "solo online (OneDrive): non scaricato sul Mac"))
                    continue
                rel = p.relative_to(self.root)
                title = p.name
                if ext == ".eml":
                    title = parse_eml_bytes(p.read_bytes())[0] or p.name
                yield RawDoc(self.source.name, str(rel), title, f"{self.source.name}/{rel}", p,
                             ext.lstrip("."), st.st_mtime, st.st_size, lambda p=p: parse(p))
