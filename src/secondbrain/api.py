from __future__ import annotations
import functools, json, re, time, mimetypes, shutil, subprocess, sys, threading
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from types import SimpleNamespace

from . import __version__
from .db import Store
from .embed import OllamaEmbedder
from .chunk import locate
from .indexer import Indexer
from .m365 import M365Auth
from .parsers import grid_rows, parse
from .llm import build_messages, stream_chat
from .search import Searcher, tokens

STATIC = Path(__file__).resolve().parents[2] / "static"
OFFICE_KINDS = {"docx", "xlsx", "xls", "pptx"}


def find_soffice():
    return shutil.which("soffice") or next((p for p in ("/Applications/LibreOffice.app/Contents/MacOS/soffice",)
                                           if Path(p).exists()), None)


@functools.lru_cache(maxsize=4)
def _parsed(path: str, mtime: float):
    return parse(Path(path))


class SourceReq(BaseModel):
    type: str
    path: str | None = None
    name: str | None = None
    months: int = 12


class ExcludeReq(BaseModel):
    exclude: list[str] = []


class M365Req(BaseModel):
    client_id: str
    tenant: str = "organizations"


def detect_cloud():
    """OneDrive/SharePoint sincronizzati dal client OneDrive (macOS: ~/Library/CloudStorage)."""
    out, seen = [], set()

    def add(label, p, group):
        rp = str(p.resolve())
        if rp not in seen:
            seen.add(rp)
            out.append({"label": label, "path": str(p), "group": group})
    base = Path.home() / "Library" / "CloudStorage"
    if base.exists():
        for d in sorted(base.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            if d.name.startswith("OneDrive-SharedLibraries"):
                for c in sorted(d.iterdir()):
                    if c.is_dir() and not c.name.startswith("."):
                        add(f"SharePoint · {c.name}", c, "sharepoint")
            elif d.name.startswith("OneDrive"):
                add(d.name.replace("OneDrive-", "OneDrive · ", 1), d, "onedrive")
    for d in sorted(Path.home().glob("OneDrive*")):
        if d.is_dir():
            add(d.name, d, "onedrive")
    return out


def pick_path(kind: str):
    """Finestra nativa di macOS per scegliere cartella/file. None se annullato."""
    if sys.platform != "darwin":
        raise HTTPException(501, "selettore disponibile solo su macOS: inserisci il percorso a mano")
    what = 'choose folder with prompt "Scegli la cartella da indicizzare"' if kind == "folder" \
        else 'choose file with prompt "Scegli l\'archivio Outlook (.pst)"'
    r = subprocess.run(["osascript", "-e", f"POSIX path of ({what})"], capture_output=True, text=True, timeout=600)
    return r.stdout.strip().rstrip("/") or None if r.returncode == 0 else None


class AskReq(BaseModel):
    question: str
    history: list[dict] = []
    sources: list[str] | None = None


def create_app(cfg, embedder="auto") -> FastAPI:
    store = Store(cfg.db_path)
    if embedder == "auto":
        e = OllamaEmbedder(cfg.ollama_host, cfg.embed_model)
        embedder = e if e.available() else None
    if not store.get_setting("seeded"):     # sorgenti di config.toml: importate una sola volta, se esistono
        for src in cfg.sources:
            if src.path.exists():
                store.add_source(src.name, src.type, src.path, src.options)
        store.set_setting("seeded", "1")
    m365 = M365Auth(store, cfg.data_dir)
    searcher = Searcher(store, embedder)
    indexer = Indexer(cfg, store, embedder, on_done=searcher.reload, ctx=SimpleNamespace(m365=m365),
                      on_vectors=searcher.append)
    app = FastAPI(title="SecondBrain")
    soffice, convert_lock = find_soffice(), threading.Lock()

    @app.get("/api/status")
    def status():
        stats = store.source_stats()
        live = indexer.status["sources"]
        sources = []
        for s in store.sources():
            st = stats.get(s.name, {"documents": 0, "chunks": 0, "last_indexed": None, "kinds": {}})
            sources.append({"name": s.name, "type": s.type, "path": s.display(), "issues": st.get("issues", 0), "partial": st.get("partial", 0),
                            **{k: st[k] for k in ("documents", "chunks", "last_indexed", "kinds")},
                            "live": live.get(s.name)})
        stt = store.stats()
        e = indexer.status.get("embedding") or {}
        rate = e["done"] / max(time.time() - e["t0"], 1) if e.get("running") and e.get("done") else 0
        embedding = None if embedder is None else {
            "running": bool(e.get("running")), "embedded": stt["embedded"], "total": stt["chunks"],
            "eta_s": (stt["chunks"] - stt["embedded"]) / rate if rate else None, "error": e.get("error")}
        return {"version": __version__, **stt, "embedding": embedding, "semantic": embedder is not None,
                "chat_model": cfg.chat_model, "embed_model": cfg.embed_model,
                "office_preview": soffice is not None,
                "indexing": {"running": indexer.status["running"], "current": indexer.status["current"],
                             "started_at": indexer.status["started_at"]},
                "sources": sources}

    # --- gestione sorgenti ----------------------------------------------------
    @app.get("/api/sources")
    def list_sources():
        return [{"name": s.name, "type": s.type, "path": s.display(), "exclude": s.options.get("exclude", [])}
                for s in store.sources()]

    @app.patch("/api/sources/{name}")
    def edit_source(name: str, req: ExcludeReq):
        src = next((s for s in store.sources() if s.name == name), None)
        if not src or src.type != "folder":
            raise HTTPException(404)
        pats = [p.strip() for p in req.exclude if p.strip()][:100]
        store.update_source_options(name, {**src.options, "exclude": pats})
        return {"exclude": pats, "reindexing": indexer.start()}

    @app.get("/api/sources/detect")
    def detect():
        have = {str(s.path) for s in store.sources()}
        return [{**d, "added": d["path"] in have} for d in detect_cloud()]

    @app.post("/api/sources/pick")
    def pick(kind: str):
        return {"path": pick_path(kind)}

    def _unique(name):
        name = name.replace("/", "-")
        names = {s.name for s in store.sources()}
        n, i = name, 2
        while n in names:
            n, i = f"{name} ({i})", i + 1
        return n

    @app.post("/api/sources")
    def add_source(req: SourceReq):
        if req.type in ("folder", "pst"):
            p = Path((req.path or "").strip()).expanduser()
            if req.type == "folder" and not p.is_dir():
                raise HTTPException(400, f"cartella non trovata: {p}")
            if req.type == "pst" and (not p.is_file() or p.suffix.lower() != ".pst"):
                raise HTTPException(400, "scegli un file .pst esistente")
            if any(s.path == p for s in store.sources()):
                raise HTTPException(409, "questa sorgente è già presente")
            name = _unique((req.name or p.stem if req.type == "pst" else req.name or p.name).strip() or "Sorgente")
            store.add_source(name, req.type, p)
        elif req.type == "outlook365":
            if m365.info()["state"] != "signed_in":
                raise HTTPException(400, "accedi prima a Microsoft 365")
            if any(s.type == "outlook365" for s in store.sources()):
                raise HTTPException(409, "la posta Microsoft 365 è già presente")
            name = _unique(req.name or "Outlook 365")
            store.add_source(name, "outlook365", "", {"months": max(1, min(req.months, 120))})
        else:
            raise HTTPException(400, "tipo non supportato")
        indexer.start()
        return {"name": name, "indexing": True}

    @app.delete("/api/sources/{name}")
    def remove_source(name: str):
        if indexer.status["current"] is not None:
            raise HTTPException(409, "è in corso la lettura dei file: riprova tra qualche istante")
        store.remove_source(name)
        searcher.reload()
        return {"ok": True}

    @app.get("/api/m365")
    def m365_info():
        return m365.info()

    @app.post("/api/m365/login")
    def m365_login(req: M365Req):
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", req.client_id.strip()):
            raise HTTPException(400, "l'ID applicazione ha il formato 00000000-0000-0000-0000-000000000000")
        m365.configure(req.client_id, req.tenant)
        try:
            m365.login()
        except Exception as e:
            raise HTTPException(400, str(e))
        return m365.info()

    @app.post("/api/m365/logout")
    def m365_logout():
        m365.logout()
        return {"ok": True}

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
    def preview(doc_id: int, chunk: int | None = None, page: int | None = None, q: str | None = None):
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
        out = {"kind": d["kind"], "pages": nums, "page": pg, "text": text, "hl": hl,
               "terms": [t[: max(5, len(t) - 2)] if len(t) >= 6 else t for t in tokens(q or "")]}
        if d["kind"] in ("xlsx", "xls"):
            try:
                out["grid"] = _grid(p, pg, text, hl)
            except Exception:
                pass                                        # resta l'anteprima a testo
        return out

    def _grid(p, pg, text, hl):
        rows = grid_rows(p, pg)
        a = b = None
        if hl:                                              # carattere -> riga: l'intestazione "[Foglio X]" è la riga 0
            a = max(0, text[: hl[0]].count("\n") - 1)
            b = min(len(rows) - 1, max(a, text[: hl[1]].count("\n") - 1))
        lo = 0 if a is None else max(0, a - 40)
        hi = min(len(rows), (300 if a is None else b + 200))
        width = min(40, max((len(c) for _, c in rows[lo:hi]), default=0))
        win = [[n, [(x[:160]) for x in cells[:width]]] for n, cells in rows[lo:hi]]
        return {"rows": win, "width": width, "total": len(rows), "offset": lo,
                "hl": None if a is None else [a - lo, b - lo]}

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
