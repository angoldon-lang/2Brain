from __future__ import annotations
import sqlite3, threading
import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents(
  id INTEGER PRIMARY KEY, source TEXT NOT NULL, key TEXT NOT NULL,
  title TEXT, location TEXT, file_path TEXT, kind TEXT,
  mtime REAL, size INTEGER, UNIQUE(source, key));
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY, doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  idx INTEGER, page INTEGER, text TEXT NOT NULL, emb BLOB);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, tokenize='unicode61 remove_diacritics 2');
"""


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()

    def doc_state(self, source):
        with self.lock:
            return {r["key"]: (r["id"], r["mtime"], r["size"]) for r in
                    self.db.execute("SELECT id,key,mtime,size FROM documents WHERE source=?", (source,))}

    def delete_doc(self, doc_id):
        with self.lock:
            self.db.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE doc_id=?)", (doc_id,))
            self.db.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            self.db.commit()

    def add_doc(self, d: dict, chunks: list[tuple[int, int, str, np.ndarray | None]]):
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO documents(source,key,title,location,file_path,kind,mtime,size) VALUES(?,?,?,?,?,?,?,?)",
                (d["source"], d["key"], d["title"], d["location"], d["file_path"], d["kind"], d["mtime"], d["size"]))
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
