from __future__ import annotations
import logging, threading, time
from .chunk import MAX_CHUNKS, chunk_pages
from .connectors import build
from .parsers import parser_version

log = logging.getLogger("secondbrain.indexer")
BATCH = 32


class Indexer:
    def __init__(self, cfg, store, embedder, on_done=None):
        self.cfg, self.store, self.embedder, self.on_done = cfg, store, embedder, on_done
        self.status = {"running": False, "current": None, "started_at": None, "sources": {}}
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
        self.status.update(running=True, started_at=time.time(), sources={
            s.name: {"state": "waiting", "total": 0, "processed": 0, "new": 0, "failed": 0, "error": None}
            for s in self.cfg.sources})
        run_id = self.store.start_run()
        notes = []
        try:
            for s in self.cfg.sources:
                st = self.status["sources"][s.name]
                self.status["current"] = s.name
                try:
                    self.index_source(build(s, self.cfg.cache_dir), st)
                    st["state"] = "done"
                except Exception as e:  # una sorgente rotta non blocca le altre
                    log.exception("sorgente %s", s.name)
                    st.update(state="error", error=str(e))
                    notes.append(f"{s.name}: {e}")
                if self.on_done:
                    self.on_done()
        finally:
            ss = self.status["sources"].values()
            self.store.finish_run(run_id, "completata" if not notes else "con errori",
                                  sum(x["processed"] for x in ss), sum(x["new"] for x in ss),
                                  sum(x["failed"] for x in ss), "; ".join(notes))
            self.status.update(running=False, current=None)

    def index_source(self, conn, st):
        name = conn.source.name
        st["state"] = "scanning"
        known = self.store.doc_state(name)
        docs = sorted(conn.scan(), key=lambda d: d.mtime, reverse=True)   # prima i file più recenti
        st.update(state="indexing", total=len(docs), t0=time.time())
        seen, bad = set(), set()
        for key, loc, reason in getattr(conn, "skipped", []):
            self.store.set_issue(name, key, loc, reason)
            bad.add(key)
        for d in docs:
            seen.add(d.key)
            st["processed"] += 1
            old = known.get(d.key)
            pv = parser_version(d.kind)
            if old and old[1] == d.mtime and old[2] == d.size and old[3] >= pv:
                continue
            if old:
                self.store.delete_doc(old[0])
            try:
                chunks = chunk_pages(d.load())
                if not chunks:
                    raise ValueError("nessun testo estraibile (es. PDF scansionato/immagini)")
                partial = len(chunks) > MAX_CHUNKS
                chunks = chunks[:MAX_CHUNKS]
                embs = self._embed([c[2] for c in chunks])
            except Exception as e:
                log.warning("skip %s: %s", d.location, e)
                self.store.set_issue(name, d.key, d.location, str(e))
                bad.add(d.key)
                st["failed"] += 1
                continue
            rows = [(i, pg, t, embs[n] if embs is not None else None) for n, (i, pg, t) in enumerate(chunks)]
            self.store.add_doc({"source": name, "key": d.key, "title": d.title, "location": d.location,
                                "file_path": str(d.file_path), "kind": d.kind, "mtime": d.mtime,
                                "size": d.size, "pv": pv}, rows)
            if partial:
                self.store.set_issue(name, d.key, d.location,
                                     f"indicizzato solo in parte: primi {MAX_CHUNKS} frammenti (file molto grande)")
                bad.add(d.key)
            st["new"] += 1
        for key, (doc_id, *_) in known.items():
            if key not in seen:
                self.store.delete_doc(doc_id)
        self.store.sync_issues(name, bad)

    def _embed(self, texts):
        if not texts or self.embedder is None:
            return None
        import numpy as np
        return np.vstack([self.embedder(texts[i:i + BATCH]) for i in range(0, len(texts), BATCH)])
