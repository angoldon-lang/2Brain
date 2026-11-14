from __future__ import annotations
from .base import RawDoc
from .localfs import FolderConnector
from .pst import PstConnector


def build(source, cache_dir, ctx=None):
    if source.type == "folder":
        return FolderConnector(source)
    if source.type == "pst":
        return PstConnector(source, cache_dir)
    if source.type == "outlook365":
        from ..m365 import Outlook365Connector
        if ctx is None or getattr(ctx, "m365", None) is None:
            raise RuntimeError("Microsoft 365 non configurato")
        return Outlook365Connector(source, cache_dir, ctx.m365.token)
    raise ValueError(f"tipo sorgente sconosciuto: {source.type}")
