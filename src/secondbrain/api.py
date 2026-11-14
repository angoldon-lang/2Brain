from __future__ import annotations
import json, mimetypes, subprocess, sys
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import __version__
from .db import Store
from .embed import OllamaEmbedder
from .indexer import Indexer
from .llm import build_messages, stream_chat
from .search import Searcher

STATIC = Path(__file__).resolve().parents[2] / "static"


class AskReq(BaseModel):
    question: str
    history: list[dict] = []
    sources: list[str] | None = None


def create_app(cfg, embedder="auto") -> FastAPI:
    store = Store(cfg.db_path)
    if embedder == "auto":
        e = OllamaEmbedder(cfg.ollama_host, cfg.embed_model)
        embedder = e if e.available() else None
    searcher = Searcher(store, embedder)
    indexer = Indexer(cfg, store, embedder, on_done=searcher.reload)
    app = FastAPI(title="SecondBrain")

    @app.get("/api/status")
    def status():
        stats = store.source_stats()
        live = indexer.status["sources"]
        sources = []
        for s in cfg.sources:
            st = stats.get(s.name, {"documents": 0, "chunks": 0, "last_indexed": None, "kinds": {}})
            sources.append({"name": s.name, "type": s.type, "path": str(s.path), "issues": st.get("issues", 0),
                            **{k: st[k] for k in ("documents", "chunks", "last_indexed", "kinds")},
                            "live": live.get(s.name)})
        return {"version": __version__, **store.stats(), "semantic": embedder is not None,
                "chat_model": cfg.chat_model, "embed_model": cfg.embed_model,
                "indexing": {"running": indexer.status["running"], "current": indexer.status["current"]},
                "sources": sources}

    @app.get("/api/documents")
    def documents(source: str | None = None, q: str | None = None, limit: int = 50, offset: int = 0):
        return store.list_documents(source, q, min(limit, 200), offset)

    @app.get("/api/issues")
    def issues(source: str | None = None):
        return store.issues(source)

    @app.post("/api/reindex")
    def reindex():
        return {"started": indexer.start()}

    @app.get("/api/search")
    def search(q: str, k: int = 8, source: str | None = None):
        return searcher.search(q, k, [source] if source else None)

    @app.post("/api/ask")
    def ask(req: AskReq):
        hits = searcher.search(req.question, cfg.top_k, req.sources)
        msgs = build_messages(req.question, hits, req.history)

        def gen():
            yield f"data: {json.dumps({'sources': hits})}\n\n"
            try:
                for tok in stream_chat(cfg.ollama_host, cfg.chat_model, msgs):
                    yield f"data: {json.dumps({'token': tok})}\n\n"
            except Exception as ex:
                yield f"data: {json.dumps({'error': f'Ollama non raggiungibile o modello assente: {ex}'})}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    def _doc(doc_id):
        d = store.document(doc_id)
        if not d:
            raise HTTPException(404)
        return d

    @app.get("/api/doc/{doc_id}")
    def doc(doc_id: int):
        d = _doc(doc_id)
        return {**d, "chunks": store.doc_chunks(doc_id)}

    @app.get("/api/file/{doc_id}")
    def file(doc_id: int):
        p = Path(_doc(doc_id)["file_path"])
        if not p.exists():
            raise HTTPException(404, "file non più presente")
        mt = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        return FileResponse(p, media_type=mt, headers={"Content-Disposition": f'inline; filename="{p.name}"'})

    @app.post("/api/reveal/{doc_id}")
    def reveal(doc_id: int):
        p = _doc(doc_id)["file_path"]
        if sys.platform != "darwin":
            raise HTTPException(501, "disponibile solo su macOS")
        subprocess.run(["open", "-R", p], check=False)
        return {"ok": True}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.state.indexer, app.state.searcher = indexer, searcher
    return app
