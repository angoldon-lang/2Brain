from __future__ import annotations
import functools, json, mimetypes, shutil, subprocess, sys, threading
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import __version__
from .db import Store
from .embed import OllamaEmbedder
from .chunk import locate
from .indexer import Indexer
from .parsers import parse
from .llm import build_messages, stream_chat
from .search import Searcher

STATIC = Path(__file__).resolve().parents[2] / "static"
OFFICE_KINDS = {"docx", "xlsx", "xls", "pptx"}


def find_soffice():
    return shutil.which("soffice") or next((p for p in ("/Applications/LibreOffice.app/Contents/MacOS/soffice",)
                                           if Path(p).exists()), None)


@functools.lru_cache(maxsize=4)
def _parsed(path: str, mtime: float):
    return parse(Path(path))


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
    soffice, convert_lock = find_soffice(), threading.Lock()

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
                "office_preview": soffice is not None,
                "indexing": {"running": indexer.status["running"], "current": indexer.status["current"],
                             "started_at": indexer.status["started_at"]},
                "sources": sources}

    @app.get("/api/documents")
    def documents(source: str | None = None, q: str | None = None, limit: int = 50, offset: int = 0):
        return store.list_documents(source, q, min(limit, 200), offset)

    @app.get("/api/runs")
    def runs():
        return store.runs()

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

    @app.get("/api/preview/{doc_id}")
    def preview(doc_id: int, chunk: int | None = None, page: int | None = None):
        """Testo del documento (con a capo originali) e posizione esatta del frammento trovato."""
        d = _doc(doc_id)
        p = Path(d["file_path"])
        if not p.exists():
            raise HTTPException(404, "file non più presente")
        chunks = store.doc_chunks(doc_id)
        target = next((c for c in chunks if c["id"] == chunk), None)
        pg = page or (target["page"] if target else None)
        try:
            pages = _parsed(str(p), p.stat().st_mtime)
        except Exception as e:
            raise HTTPException(422, f"anteprima testuale non disponibile: {e}")
        nums = [x[0] for x in pages]
        if not nums:
            raise HTTPException(422, "nessun testo")
        pg = pg if pg in nums else nums[0]
        text = next(t for n, t in pages if n == pg)
        hl = None
        if target and target["page"] == pg:
            hl = locate(text, [c for c in chunks if c["page"] == pg], target["id"])
        return {"kind": d["kind"], "pages": nums, "page": pg, "text": text, "hl": hl}

    @app.get("/api/pdf/{doc_id}")
    def pdf(doc_id: int):
        """Versione PDF fedele di docx/xlsx/pptx (richiede LibreOffice), in cache."""
        d = _doc(doc_id)
        src = Path(d["file_path"])
        if not soffice:
            raise HTTPException(501, "LibreOffice non installato (brew install --cask libreoffice)")
        if d["kind"] not in OFFICE_KINDS or not src.exists():
            raise HTTPException(404)
        out_dir = cfg.data_dir / "previews"
        out_dir.mkdir(exist_ok=True)
        out = out_dir / f"{doc_id}-{int(d['mtime'])}.pdf"
        if not out.exists():
            with convert_lock:
                tmp = out_dir / f"tmp-{doc_id}"
                shutil.rmtree(tmp, ignore_errors=True)
                tmp.mkdir()
                r = subprocess.run([soffice, f"-env:UserInstallation=file://{out_dir}/lo-profile", "--headless",
                                    "--convert-to", "pdf", "--outdir", str(tmp), str(src)],
                                   capture_output=True, timeout=180)
                made = next(tmp.glob("*.pdf"), None)
                if r.returncode != 0 or not made:
                    raise HTTPException(500, "conversione non riuscita")
                made.replace(out)
                shutil.rmtree(tmp, ignore_errors=True)
        return FileResponse(out, media_type="application/pdf")

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
