from __future__ import annotations
import re
import numpy as np


class Searcher:
    def __init__(self, store, embedder=None):
        self.store, self.embedder = store, embedder
        self.reload()

    def reload(self):
        self.ids, self.srcs, self.mat = self.store.load_embeddings()

    @staticmethod
    def _fts_query(q):
        toks = [t for t in re.findall(r"\w+", q.lower()) if len(t) > 1]
        return " OR ".join(f'"{t}"' for t in toks)

    def search(self, query, k=8, sources=None, per_doc=2):
        pool = k * 6
        fq = self._fts_query(query)
        lex = self.store.fts(fq, pool, sources) if fq else []
        vec = []
        if self.embedder is not None and len(self.ids):
            try:
                q = self.embedder([query])[0]
                sims = self.mat @ q
                mask = np.array([s in sources for s in self.srcs]) if sources else None
                if mask is not None:
                    sims = np.where(mask, sims, -1)
                vec = [self.ids[i] for i in np.argsort(-sims)[:pool] if sims[i] > 0]
            except Exception:
                vec = []
        score = {}
        for lst in (lex, vec):
            for r, cid in enumerate(lst):
                score[cid] = score.get(cid, 0) + 1 / (60 + r)
        ranked = sorted(score, key=score.get, reverse=True)
        rows = self.store.chunks_by_ids(ranked)
        out, count = [], {}
        for cid in ranked:
            r = rows.get(cid)
            if not r or count.get(r["doc_id"], 0) >= per_doc:
                continue
            count[r["doc_id"]] = count.get(r["doc_id"], 0) + 1
            r["score"] = round(score[cid], 5)
            out.append(r)
            if len(out) >= k:
                break
        return out
