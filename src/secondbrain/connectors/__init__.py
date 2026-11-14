from __future__ import annotations
from .base import RawDoc
from .localfs import FolderConnector
from .pst import PstConnector


def build(source, cache_dir):
    if source.type == "folder":
        return FolderConnector(source)
    if source.type == "pst":
        return PstConnector(source, cache_dir)
    raise ValueError(f"tipo sorgente sconosciuto: {source.type}")
