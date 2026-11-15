from __future__ import annotations
import re
import numpy as np

# Parole che non distinguono un documento da un altro: senza questo filtro un articolo lungo che contiene
# "mi", "le", "dai" batte l'offerta che contiene l'unica parola importante ("bper").
STOP = set("""a ad al alla alle allo agli ai all anche che chi ci col come con cui da dai dal dalla dalle dei del della
delle dello dell dall nell sull di dove e ed è fa fatto fatta fatte fatti gli ha hanno ho il in io la le lo ma mi mia mie
miei mio ne nei nel nella nelle non o per più po quale quali quando quella quelle quello questa queste questi questo se
si sono su sul sulla sulle tra tu un una uno vi tutti tutto cosa cose dimmi dammi dai mostrami trova trovami cerca
cercami fammi elenco lista voglio vorrei puoi può sapere dove qual quanto quanti quante
the of and to in for with on at is are was by an or from what which where who how do does did me my""".split())
RECENT = {"ultima", "ultime", "ultimo", "ultimi", "recente", "recenti", "nuova", "nuove", "nuovo", "nuovi", "scorsa", "scorso"}


def tokens(q: str) -> list[str]:
    return [t for t in re.findall(r"\w+", q.lower()) if (len(t) > 1 or t.isdigit()) and t not in STOP]


def _term(t: str) -> str:
    """Parole lunghe -> prefisso (offerte/offerta/offerto -> offer*): un po' di stemming senza dizionario."""
    return f'"{t[:max(5, len(t) - 2)]}"*' if len(t) >= 6 and not t.isdigit() else f'"{t}"'


class Searcher:
    def __init__(self, store, embedder=None):
        self.store, self.embedder = store, embedder
        self.reload()

    def reload(self):
        ids, srcs, mat = self.store.load_embeddings()
        self.ids, self.srcs, self._n = list(ids), list(srcs), len(ids)
        self._buf = np.array(mat, np.float32) if len(ids) else np.zeros((1024, 1), np.float32)

    def append(self, ids, srcs, vecs):
        """Aggiunge vettori appena calcolati senza ricaricare tutto (buffer che raddoppia)."""
        vecs = np.asarray(vecs, np.float32)
        if self._n == 0 or self._buf.shape[1] != vecs.shape[1]:
            self._buf = np.zeros((max(1024, len(vecs) * 2), vecs.shape[1]), np.float32)
            self._n, self.ids, self.srcs = 0, [], []
        need = self._n + len(vecs)
        if need > len(self._buf):
            grown = np.zeros((max(need, len(self._buf) * 2), self._buf.shape[1]), np.float32)
            grown[: self._n] = self._buf[: self._n]
            self._buf = grown
        self._buf[self._n:need] = vecs
        self._n = need
        self.ids += list(ids)
        self.srcs += list(srcs)

    @property
    def mat(self):
        return self._buf[: self._n]

    def _lexical(self, query, pool, sources):
        toks = tokens(query)
        if not toks:
            return []
        terms = [_term(t) for t in toks]
        out = self.store.fts(" ".join(terms), pool, sources)          # prima chi contiene TUTTE le parole importanti
        if len(terms) > 1 and len(out) < pool:                        # poi chi ne contiene solo alcune
            seen = set(out)
            out += [c for c in self.store.fts(" OR ".join(terms), pool, sources) if c not in seen]
        return out[:pool]

    def search(self, query, k=8, sources=None, per_doc=2, date_from=None, date_to=None):
        pool = k * (25 if (date_from is not None or date_to is not None) else 6)
        lex = self._lexical(query, pool, sources)
        vec = []
        if self.embedder is not None and self._n:
            try:
                q = self.embedder([query])[0]
                sims = self.mat @ q
                if sources:
                    sims = np.where(np.array([s in sources for s in self.srcs]), sims, -1)
                vec = [self.ids[i] for i in np.argsort(-sims)[:pool] if sims[i] > 0]
            except Exception:
                vec = []
        score = {}
        for lst in (lex, vec):
            for r, cid in enumerate(lst):
                score[cid] = score.get(cid, 0) + 1 / (60 + r)
        rows = self.store.chunks_by_ids(list(score))
        if RECENT & set(re.findall(r"\w+", query.lower())) and rows:        # "ultime offerte": premia i documenti recenti
            ts = [r["mtime"] or 0 for r in rows.values()]
            lo, hi = min(ts), max(ts)
            for cid, r in rows.items():
                score[cid] *= 1 + 0.6 * ((r["mtime"] or 0) - lo) / (hi - lo or 1)
        if date_from is not None or date_to is not None:                     # filtro per data di modifica del file
            rows = {c: r for c, r in rows.items()
                    if (date_from is None or (r["mtime"] or 0) >= date_from) and (date_to is None or (r["mtime"] or 0) <= date_to)}
        ranked = sorted((c for c in score if c in rows), key=score.get, reverse=True)
        out, count = [], {}
        for cid in ranked:
            r = rows[cid]
            if count.get(r["doc_id"], 0) >= per_doc:
                continue
            count[r["doc_id"]] = count.get(r["doc_id"], 0) + 1
            r["score"] = round(score[cid], 5)
            out.append(r)
            if len(out) >= k:
                break
        return out
