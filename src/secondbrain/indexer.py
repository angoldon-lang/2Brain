from __future__ import annotations
import logging, threading
from .chunk import chunk_pages
from .connectors import build

log = logging.getLogger("secondbrain.indexer")
BATCH = 32


class Indexer:
    def __init__(self, cfg, store, embedder, on_done=None):
        self.cfg, self.store, self.embedder, self.on_done = cfg, store, embedder, on_done
        self.status = {"running": False, "source": None, "done": 0, "errors": []}
        self._t = None

    def roots(self):
        return [r for s in self.cfg.sources for r in build(s, self.cfg.cache_dir).roots()]

    def start(self):
        if self._t and self._t.is_alive():
            return False
        self._t = threading.Thread(target=self.run, daemon=True)
        self._t.start()
        return True

    def run(self):
        self.status.update(running=True, done=0, errors=[])
        try:
            for s in self.cfg.sources:
                self.status["source"] = s.name
                try:
                    self.index_source(build(s, self.cfg.cache_dir))
                except Exception as e:  # una sorgente rotta non blocca le altre
                    log.exception("sorgente %s", s.name)
                    self.status["errors"].append(f"{s.name}: {e}")
        finally:
            self.status.update(running=False, source=None)
            if self.on_done:
                self.on_done()

    def index_source(self, conn):
        name = conn.source.name
        known = self.store.doc_state(name)
        seen = set()
        for d in conn.scan():
            seen.add(d.key)
            old = known.get(d.key)
            if old and old[1] == d.mtime and old[2] == d.size:
                continue
            if old:
                self.store.delete_doc(old[0])
            try:
                chunks = chunk_pages(d.load())
                embs = self._embed([c[2] for c in chunks])
            except Exception as e:
                log.warning("skip %s: %s", d.location, e)
                self.status["errors"].append(f"{d.location}: {e}")
                continue
            rows = [(i, pg, t, embs[n] if embs is not None else None) for n, (i, pg, t) in enumerate(chunks)]
            self.store.add_doc({"source": name, "key": d.key, "title": d.title, "location": d.location,
                                "file_path": str(d.file_path), "kind": d.kind, "mtime": d.mtime, "size": d.size}, rows)
            self.status["done"] += 1
        for key, (doc_id, *_ ) in known.items():
            if key not in seen:
                self.store.delete_doc(doc_id)

    def _embed(self, texts):
        if not texts or self.embedder is None:
            return None
        import numpy as np
        return np.vstack([self.embedder(texts[i:i + BATCH]) for i in range(0, len(texts), BATCH)])
