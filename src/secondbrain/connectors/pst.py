from __future__ import annotations
import shutil, subprocess
from pathlib import Path
from ..parsers import parse_eml_bytes
from .base import RawDoc


class PstConnector:
    """Outlook PST: convertito con `readpst` (brew install libpst) in .eml in cache, poi indicizzato."""

    def __init__(self, source, cache_dir):
        self.source = source
        self.out = Path(cache_dir) / "pst" / source.name.replace("/", "_")

    def roots(self):
        return [self.out]

    def _convert(self):
        pst = self.source.path
        if not pst.exists():
            raise FileNotFoundError(f"{pst} non esiste")
        if not shutil.which("readpst"):
            raise RuntimeError("readpst non trovato: esegui `brew install libpst`")
        stamp = self.out / ".stamp"
        sig = f"{pst.stat().st_mtime}:{pst.stat().st_size}"
        if stamp.exists() and stamp.read_text() == sig:
            return
        shutil.rmtree(self.out, ignore_errors=True)
        self.out.mkdir(parents=True)
        subprocess.run(["readpst", "-r", "-e", "-q", "-o", str(self.out), str(pst)], check=True)
        stamp.write_text(sig)

    def scan(self):
        self._convert()
        pst_name = self.source.path.name
        for p in self.out.rglob("*.eml"):
            st = p.stat()
            rel = p.relative_to(self.out)
            raw = p.read_bytes()
            subject, _ = parse_eml_bytes(raw)
            folder = "/".join(rel.parts[:-1])
            yield RawDoc(self.source.name, str(rel), subject or "(senza oggetto)",
                         f"{self.source.name}/{pst_name}/{folder}", p, "eml",
                         st.st_mtime, st.st_size,
                         lambda raw=raw: [(1, parse_eml_bytes(raw)[1])])
