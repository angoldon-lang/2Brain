from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class RawDoc:
    source: str
    key: str            # id stabile all'interno della sorgente
    title: str
    location: str       # percorso "umano" mostrato all'utente
    file_path: Path     # file reale (per anteprima/apertura)
    kind: str
    mtime: float
    size: int
    load: Callable[[], list[tuple[int, str]]]
    web_url: str | None = None   # link per aprire l'originale online (es. messaggio Outlook)
