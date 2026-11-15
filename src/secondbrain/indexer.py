from __future__ import annotations
import logging, threading, time
from .chunk import MAX_CHUNKS, chunk_pages, clean
from .connectors import build
from .parsers import parser_version

log = logging.getLogger("secondbrain.indexer")
BATCH = 32
MAX_CHUNKS_BY_KIND = {"xlsx": 150, "xls": 150}      # i fogli di costi sono enormi e pieni di numeri: bastano i primi frammenti


class Indexer:
    def __init__(self, cfg, store, embedder, on_done=None, ctx=None, on_vectors=None):
        self.cfg, self.store, self.embedder, self.on_done, self.ctx = cfg, store, embedder, on_done, ctx
        self.on_vectors = on_vectors
        self.status = {"running": False, "current": None, "started_at": None, "sources": {}, "embedding": None}
        self._t = None
        self._rescan = False

    def roots(self):
        return [r for s in self.store.sources() for r in build(s, self.cfg.cache_dir, self.ctx).roots()]

    def start(self):
        """True se parte ora; False se era già in corso (in tal caso rifà la scansione appena possibile)."""
        if self._t and self._t.is_alive():
            self._rescan = True
            return False
        self._t = threading.Thread(target=self.run, daemon=True)
        self._t.start()
        return True

    def run(self):
        self.status.update(running=True, started_at=time.time())
        run_id = self.store.start_run()
        notes, tot, finished = [], {"processed": 0, "new": 0, "failed": 0}, False
        try:
            while True:
                self._rescan = False
                self._scan_all(notes, tot)
                self.embed_pending()
                if not self._rescan:
                    break
            finished = True
        finally:
            self.store.finish_run(run_id, "interrotta" if not finished else ("completata" if not notes else "con errori"),
                                  tot["processed"], tot["new"], tot["failed"], "; ".join(notes))
            self.status.update(running=False, current=None)

    def _scan_all(self, notes, tot):
        """Fase 1: legge e indicizza tutto per parole chiave (veloce). Gli embedding vengono dopo."""
        sources = self.store.sources()
        self.status["sources"] = {
            s.name: {"state": "waiting", "total": 0, "processed": 0, "new": 0, "failed": 0, "error": None}
            for s in sources}
        for s in sources:
            st = self.status["sources"][s.name]
            self.status["current"] = s.name
            try:
                self.index_source(build(s, self.cfg.cache_dir, self.ctx), st)
                st["state"] = "done"
            except Exception as e:  # una sorgente rotta non blocca le altre
                log.exception("sorgente %s", s.name)
                st.update(state="error", error=str(e))
                notes.append(f"{s.name}: {e}")
            for k in tot:
                tot[k] += st.get(k, 0)
            if self.on_done:
                self.on_done()
        self.status["current"] = None

    def embed_pending(self):
        """Fase 2: calcola gli embedding mancanti, dai file più recenti. Riprende da dove era dopo un riavvio."""
        if self.embedder is None:
            return
        e = self.status["embedding"] = {"running": True, "done": 0, "total": self.store.count_pending(),
                                        "t0": time.time(), "error": None}
        try:
            while not self._rescan and e["done"] < e["total"] + 10_000:
                rows = self.store.pending_chunks(BATCH)
                if not rows:
                    break
                try:
                    vecs = self.embedder([f"{r['title']} — {r['location']}\n{r['text']}" for r in rows])
                except Exception as ex:
                    log.warning("embedding interrotto: %s", ex)
                    e["error"] = str(ex)
                    break
                ids = [r["id"] for r in rows]
                self.store.set_embeddings(ids, vecs)
                if self.on_vectors:
                    self.on_vectors(ids, [r["source"] for r in rows], vecs)
                e["done"] += len(rows)
        finally:
            e["running"] = False

    def index_source(self, conn, st):
        name = conn.source.name
        st["state"] = "scanning"
        known = self.store.doc_state(name)
        issue_known = self.store.issue_state(name)
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
                st["unchanged"] = st.get("unchanged", 0) + 1       # già indicizzato e invariato: si salta
                if d.key in issue_known:          # file parziale: l'avviso resta finché il file non cambia
                    bad.add(d.key)
                continue
            if old and old[1] == d.mtime and old[2] == d.size:
                st["stale"] = st.get("stale", 0) + 1               # riletto solo perché il lettore è migliorato
            ik = issue_known.get(d.key)
            if not old and ik and ik[0] == d.mtime and ik[1] == d.size and ik[2] >= pv \
                    and "OCR non disponibile" not in ik[3]:        # (se l'OCR si installa, i PDF si riprovano)
                bad.add(d.key)                    # già fallito e invariato: non ritento ogni volta
                continue
            if old:
                self.store.delete_doc(old[0])
            try:
                chunks = chunk_pages(d.load())
                if not chunks:                        # file vuoto (log, __init__, .egg-info…): non è un problema
                    st["empty"] = st.get("empty", 0) + 1
                    continue
                cap = MAX_CHUNKS_BY_KIND.get(d.kind, MAX_CHUNKS)
                partial = len(chunks) > cap
                if partial:
                    unit = {"xlsx": "foglio", "xls": "foglio", "pptx": "slide"}.get(d.kind, "pag.")
                    reached = f"fino a {unit} {chunks[cap - 1][1]} di {max(c[1] for c in chunks)}"
                chunks = chunks[:cap]
            except Exception as e:
                log.warning("skip %s: %s", d.location, e)
                self.store.set_issue(name, clean(d.key), clean(d.location), clean(str(e)), d.mtime, d.size, pv)
                bad.add(d.key)
                st["failed"] += 1
                continue
            rows = [(i, pg, t, None) for i, pg, t in chunks]
            try:
                self.store.add_doc({"source": name, "key": clean(d.key), "title": clean(d.title),
                                    "location": clean(d.location), "file_path": clean(str(d.file_path)),
                                    "kind": d.kind, "mtime": d.mtime, "size": d.size, "pv": pv,
                                    "web_url": d.web_url}, rows)
            except Exception as e:               # un file che non si può salvare non deve fermare gli altri
                log.warning("skip %s: %s", d.location, e)
                self.store.set_issue(name, clean(d.key), clean(d.location), f"non salvabile: {e}", d.mtime, d.size, pv)
                bad.add(d.key)
                st["failed"] += 1
                continue
            if partial:
                self.store.set_issue(name, d.key, d.location,
                                     f"indicizzato solo in parte: primi {cap} frammenti ({reached})", d.mtime, d.size, pv)
                bad.add(d.key)
            st["new"] += 1
        for key, (doc_id, *_) in known.items():
            if key not in seen:
                self.store.delete_doc(doc_id)
        self.store.sync_issues(name, bad)
