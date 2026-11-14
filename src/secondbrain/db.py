from __future__ import annotations
import sqlite3, threading, time
import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents(
  id INTEGER PRIMARY KEY, source TEXT NOT NULL, key TEXT NOT NULL,
  title TEXT, location TEXT, file_path TEXT, kind TEXT,
  mtime REAL, size INTEGER, UNIQUE(source, key));
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY, doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  idx INTEGER, page INTEGER, text TEXT NOT NULL, emb BLOB);
CREATE TABLE IF NOT EXISTS issues(
  source TEXT NOT NULL, key TEXT NOT NULL, location TEXT, reason TEXT, at REAL, UNIQUE(source, key));
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, started REAL, finished REAL, state TEXT, scanned INTEGER DEFAULT 0,
  new INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, note TEXT);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, tokenize='unicode61 remove_diacritics 2');
"""


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(documents)")]
        if "indexed_at" not in cols:  # migrazione da 0.1
            self.db.execute("ALTER TABLE documents ADD COLUMN indexed_at REAL")
        if "pv" not in cols:          # versione del parser con cui è stato letto il file
            self.db.execute("ALTER TABLE documents ADD COLUMN pv INTEGER DEFAULT 1")
        self.db.execute("UPDATE runs SET finished=started, state='interrotta' WHERE finished IS NULL")
        self.db.commit()
        self.lock = threading.RLock()

    def doc_state(self, source):
        with self.lock:
            return {r["key"]: (r["id"], r["mtime"], r["size"], r["pv"] or 1) for r in
                    self.db.execute("SELECT id,key,mtime,size,pv FROM documents WHERE source=?", (source,))}

    def delete_doc(self, doc_id):
        with self.lock:
            self.db.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE doc_id=?)", (doc_id,))
            self.db.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            self.db.commit()

    def add_doc(self, d: dict, chunks: list[tuple[int, int, str, np.ndarray | None]]):
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO documents(source,key,title,location,file_path,kind,mtime,size,indexed_at,pv) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (d["source"], d["key"], d["title"], d["location"], d["file_path"], d["kind"], d["mtime"], d["size"], time.time(), d.get("pv", 1)))
            did = cur.lastrowid
            for idx, page, text, emb in chunks:
                c = self.db.execute("INSERT INTO chunks(doc_id,idx,page,text,emb) VALUES(?,?,?,?,?)",
                                    (did, idx, page, text, emb.astype(np.float32).tobytes() if emb is not None else None))
                self.db.execute("INSERT INTO chunks_fts(rowid,text) VALUES(?,?)", (c.lastrowid, text))
            self.db.commit()
            return did

    def fts(self, query: str, limit: int, sources=None):
        sql = ("SELECT c.id FROM chunks_fts f JOIN chunks c ON c.id=f.rowid JOIN documents d ON d.id=c.doc_id "
               "WHERE chunks_fts MATCH ?")
        args: list = [query]
        if sources:
            sql += f" AND d.source IN ({','.join('?' * len(sources))})"
            args += list(sources)
        sql += " ORDER BY bm25(chunks_fts) LIMIT ?"
        args.append(limit)
        with self.lock:
            return [r[0] for r in self.db.execute(sql, args)]

    def load_embeddings(self):
        with self.lock:
            rows = self.db.execute("SELECT c.id, c.emb, d.source FROM chunks c JOIN documents d ON d.id=c.doc_id "
                                   "WHERE c.emb IS NOT NULL").fetchall()
        if not rows:
            return [], [], np.zeros((0, 1), np.float32)
        ids = [r[0] for r in rows]
        srcs = [r[2] for r in rows]
        return ids, srcs, np.vstack([np.frombuffer(r[1], np.float32) for r in rows])

    def chunks_by_ids(self, ids):
        if not ids:
            return {}
        with self.lock:
            rows = self.db.execute(
                f"SELECT c.id chunk_id,c.idx,c.page,c.text,d.id doc_id,d.source,d.title,d.location,d.kind,d.mtime "
                f"FROM chunks c JOIN documents d ON d.id=c.doc_id WHERE c.id IN ({','.join('?' * len(ids))})", ids).fetchall()
        return {r["chunk_id"]: dict(r) for r in rows}

    def document(self, doc_id):
        with self.lock:
            r = self.db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        return dict(r) if r else None

    def doc_chunks(self, doc_id):
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT id,idx,page,text FROM chunks WHERE doc_id=? ORDER BY idx", (doc_id,))]

    def stats(self):
        with self.lock:
            return {"documents": self.db.execute("SELECT count(*) FROM documents").fetchone()[0],
                    "chunks": self.db.execute("SELECT count(*) FROM chunks").fetchone()[0]}

    # --- stato file -------------------------------------------------------
    def set_issue(self, source, key, location, reason):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO issues(source,key,location,reason,at) VALUES(?,?,?,?,?)",
                            (source, key, location, reason, time.time()))
            self.db.commit()

    def sync_issues(self, source, keep_keys):
        """Elimina le segnalazioni di file non più in errore / non più presenti."""
        with self.lock:
            old = [r[0] for r in self.db.execute("SELECT key FROM issues WHERE source=?", (source,))]
            for k in old:
                if k not in keep_keys:
                    self.db.execute("DELETE FROM issues WHERE source=? AND key=?", (source, k))
            self.db.commit()

    def clear_issue(self, source, key):
        with self.lock:
            self.db.execute("DELETE FROM issues WHERE source=? AND key=?", (source, key))
            self.db.commit()

    def issues(self, source=None, limit=500):
        sql, args = "SELECT source,key,location,reason,at FROM issues", []
        if source:
            sql += " WHERE source=?"; args.append(source)
        with self.lock:
            return [dict(r) for r in self.db.execute(sql + " ORDER BY at DESC LIMIT ?", args + [limit])]

    def source_stats(self):
        with self.lock:
            out = {}
            for r in self.db.execute("SELECT d.source, count(DISTINCT d.id) docs, count(c.id) chunks, max(d.indexed_at) last "
                                     "FROM documents d LEFT JOIN chunks c ON c.doc_id=d.id GROUP BY d.source"):
                out[r["source"]] = {"documents": r["docs"], "chunks": r["chunks"], "last_indexed": r["last"], "kinds": {}}
            for r in self.db.execute("SELECT source, kind, count(*) n FROM documents GROUP BY source, kind"):
                out[r["source"]]["kinds"][r["kind"]] = r["n"]
            for r in self.db.execute("SELECT source, count(*) n FROM issues GROUP BY source"):
                out.setdefault(r["source"], {"documents": 0, "chunks": 0, "last_indexed": None, "kinds": {}})["issues"] = r["n"]
            return out

    def list_documents(self, source=None, q=None, limit=50, offset=0):
        where, args = [], []
        if source:
            where.append("source=?"); args.append(source)
        if q:
            where.append("(title LIKE ? OR location LIKE ?)"); args += [f"%{q}%"] * 2
        w = (" WHERE " + " AND ".join(where)) if where else ""
        with self.lock:
            total = self.db.execute(f"SELECT count(*) FROM documents{w}", args).fetchone()[0]
            rows = self.db.execute(
                f"SELECT d.id,d.source,d.title,d.location,d.kind,d.size,d.indexed_at,"
                f"(SELECT count(*) FROM chunks c WHERE c.doc_id=d.id) chunks FROM documents d{w} "
                f"ORDER BY d.indexed_at DESC, d.id DESC LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
        return {"total": total, "items": [dict(r) for r in rows]}

    # --- storico indicizzazioni -------------------------------------------
    def start_run(self):
        with self.lock:
            c = self.db.execute("INSERT INTO runs(started,state) VALUES(?, 'in corso')", (time.time(),))
            self.db.commit()
            return c.lastrowid

    def finish_run(self, run_id, state, scanned, new, failed, note=""):
        with self.lock:
            self.db.execute("UPDATE runs SET finished=?,state=?,scanned=?,new=?,failed=?,note=? WHERE id=?",
                            (time.time(), state, scanned, new, failed, note, run_id))
            self.db.commit()

    def runs(self, limit=15):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]
