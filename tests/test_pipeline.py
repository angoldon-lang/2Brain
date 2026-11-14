import numpy as np
from fastapi.testclient import TestClient
from secondbrain.config import Config, Source
from secondbrain.api import create_app


def fake_embed(texts):
    # bag-of-letters deterministico: sufficiente per verificare il flusso vettoriale
    out = np.zeros((len(texts), 26), np.float32)
    for i, t in enumerate(texts):
        for ch in t.lower():
            if "a" <= ch <= "z":
                out[i, ord(ch) - 97] += 1
    return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-9)


def make(tmp_path, embedder):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "offerta_rossi.txt").write_text("Offerta commerciale per Rossi SpA: importo 12.500 euro, validità 30 giorni.")
    (docs / "ferie.md").write_text("Il piano ferie estive prevede chiusura dal 10 al 20 agosto.")
    (docs / "mail.eml").write_text("From: a@x.it\nTo: b@x.it\nSubject: Riunione budget\nDate: Mon, 1 Jan 2024 10:00:00 +0000\n\nCi vediamo giovedì per il budget 2025.")
    cfg = Config(data_dir=tmp_path / "data", sources=[Source("Test", "folder", docs)])
    cfg.data_dir.mkdir()
    return docs, create_app(cfg, embedder=embedder)


def run(app):
    app.state.indexer.run()
    return TestClient(app)


def test_search_and_preview_keyword_only(tmp_path):
    _, app = make(tmp_path, None)
    c = run(app)
    assert app.state.indexer.status["errors"] == []
    assert c.get("/api/status").json()["documents"] == 3
    hits = c.get("/api/search", params={"q": "offerta Rossi importo"}).json()
    assert hits[0]["title"] == "offerta_rossi.txt"
    assert hits[0]["location"] == "Test/offerta_rossi.txt"
    d = c.get(f"/api/doc/{hits[0]['doc_id']}").json()
    assert "12.500" in d["chunks"][0]["text"]
    assert b"Rossi" in c.get(f"/api/file/{hits[0]['doc_id']}").content
    mail = c.get("/api/search", params={"q": "budget"}).json()[0]
    assert mail["title"] == "Riunione budget"


def test_hybrid_and_incremental(tmp_path):
    docs, app = make(tmp_path, fake_embed)
    c = run(app)
    assert c.get("/api/search", params={"q": "ferie agosto"}).json()[0]["title"] == "ferie.md"
    (docs / "ferie.md").unlink()
    (docs / "nuovo.txt").write_text("contenuto nuovo")
    app.state.indexer.run()
    titles = {h["title"] for h in c.get("/api/search", params={"q": "contenuto ferie"}).json()}
    assert "nuovo.txt" in titles and "ferie.md" not in titles


def test_unknown_doc_404(tmp_path):
    _, app = make(tmp_path, None)
    assert TestClient(app).get("/api/file/999").status_code == 404
