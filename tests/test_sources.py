import httpx, pytest
from fastapi.testclient import TestClient
from secondbrain.config import Config, Source
from secondbrain.api import create_app
from secondbrain.db import Store
from secondbrain.m365 import Outlook365Connector


def app_with(tmp_path, sources=()):
    cfg = Config(data_dir=tmp_path / "data", sources=list(sources))
    cfg.data_dir.mkdir(exist_ok=True)
    return create_app(cfg, embedder=None)


def test_add_remove_folder_source_via_api(tmp_path):
    folder = tmp_path / "OneDrive - Acme"
    folder.mkdir()
    (folder / "nota.txt").write_text("preventivo Rossi 4.200 euro")
    app = app_with(tmp_path)
    c = TestClient(app)
    assert c.get("/api/sources").json() == []
    r = c.post("/api/sources", json={"type": "folder", "path": str(folder)})
    assert r.status_code == 200 and r.json()["name"] == "OneDrive - Acme"
    app.state.indexer._t.join(10)
    assert c.get("/api/search", params={"q": "preventivo Rossi"}).json()
    assert c.post("/api/sources", json={"type": "folder", "path": str(folder)}).status_code == 409   # doppione
    assert c.post("/api/sources", json={"type": "folder", "path": "/non/esiste"}).status_code == 400
    assert c.delete("/api/sources/OneDrive - Acme").status_code == 200
    assert c.get("/api/search", params={"q": "preventivo Rossi"}).json() == []       # indice ripulito
    assert folder.exists()                                                           # file originali intatti


def test_toml_sources_seeded_once_and_only_if_existing(tmp_path):
    real = tmp_path / "docs"
    real.mkdir()
    cfg = Config(data_dir=tmp_path / "d", sources=[Source("Doc", "folder", real),
                                                    Source("Fantasma", "folder", tmp_path / "nope")])
    cfg.data_dir.mkdir()
    app = create_app(cfg, embedder=None)
    assert [s["name"] for s in TestClient(app).get("/api/sources").json()] == ["Doc"]
    TestClient(app).delete("/api/sources/Doc")
    app2 = create_app(cfg, embedder=None)         # riavvio: non reimporta ciò che hai rimosso
    assert TestClient(app2).get("/api/sources").json() == []


def test_detect_onedrive_and_sharepoint(tmp_path, monkeypatch):
    cs = tmp_path / "Library" / "CloudStorage"
    (cs / "OneDrive-Acme").mkdir(parents=True)
    (cs / "OneDrive-SharedLibraries-Acme" / "Team - Documenti").mkdir(parents=True)
    (cs / "GoogleDrive-x").mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    app = app_with(tmp_path / "x" if False else tmp_path)
    labels = {d["label"]: d["group"] for d in TestClient(app).get("/api/sources/detect").json()}
    assert labels == {"OneDrive · Acme": "onedrive", "SharePoint · Team - Documenti": "sharepoint"}


def test_pick_not_available_off_mac(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    assert TestClient(app_with(tmp_path)).post("/api/sources/pick", params={"kind": "folder"}).status_code == 501


def test_m365_login_validation_and_flow(tmp_path, monkeypatch):
    app = app_with(tmp_path)
    c = TestClient(app)
    assert c.get("/api/m365").json()["state"] == "none"
    assert c.post("/api/m365/login", json={"client_id": "abc"}).status_code == 400
    assert c.post("/api/sources", json={"type": "outlook365"}).status_code == 400     # serve l'accesso
    from secondbrain import m365 as mod

    def fake_login(self):
        self.state, self.flow = "pending", {"user_code": "ABCD-1234", "verification_uri": "https://microsoft.com/devicelogin"}
    monkeypatch.setattr(mod.M365Auth, "login", fake_login)
    r = c.post("/api/m365/login", json={"client_id": "11111111-2222-3333-4444-555555555555", "tenant": "contoso.com"}).json()
    assert r["state"] == "pending" and r["user_code"] == "ABCD-1234" and r["tenant"] == "contoso.com"


# ---- connettore Graph con server finto ------------------------------------
def fake_graph():
    msgs = [{"id": f"AAA{i}=", "subject": f"Offerta {i}", "lastModifiedDateTime": "2025-03-01T10:00:00Z",
             "receivedDateTime": "2025-03-01T10:00:00Z", "parentFolderId": "F_INBOX" if i < 4 else "F_JUNK",
             "webLink": f"https://outlook.office.com/mail/id/{i}"} for i in range(1, 5)]
    seen = {"calls": []}

    def handler(req: httpx.Request):
        assert req.headers["authorization"] == "Bearer TKN"
        u = req.url
        seen["calls"].append(str(u))
        if u.path.endswith("/mailFolders/deleteditems"):
            return httpx.Response(200, json={"id": "F_DEL"})
        if u.path.endswith("/mailFolders/junkemail"):
            return httpx.Response(200, json={"id": "F_JUNK"})
        if u.path.endswith("/mailFolders"):
            return httpx.Response(200, json={"value": [{"id": "F_INBOX", "displayName": "Posta in arrivo", "childFolderCount": 1}]})
        if u.path.endswith("/childFolders"):
            return httpx.Response(200, json={"value": [{"id": "F_SUB", "displayName": "Clienti", "childFolderCount": 0}]})
        if u.path.endswith("/me/messages") and "page2" not in str(u):
            return httpx.Response(200, json={"value": msgs[:2], "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages?page2"})
        if u.path.endswith("/me/messages"):
            return httpx.Response(200, json={"value": msgs[2:]})
        if "/me/messages/" in u.path:
            assert req.headers["prefer"].startswith("outlook.body-content-type")
            return httpx.Response(200, json={"subject": "Offerta", "from": {"emailAddress": {"name": "Rossi", "address": "r@x.it"}},
                                             "toRecipients": [{"emailAddress": {"name": "Io", "address": "io@x.it"}}],
                                             "receivedDateTime": "2025-03-01T10:00:00Z",
                                             "body": {"content": "importo 7.777 euro entro venerdì"}, "hasAttachments": True})
        return httpx.Response(404, json={"error": {"message": "?"}})
    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_outlook365_connector_scan_filters_and_fetch(tmp_path):
    client, seen = fake_graph()
    conn = Outlook365Connector(Source("Outlook 365", "outlook365", options={"months": 12}), tmp_path, lambda: "TKN", client)
    docs = list(conn.scan())
    assert [d.title for d in docs] == ["Offerta 1", "Offerta 2", "Offerta 3"]    # il 4° è in Posta indesiderata
    assert docs[0].location == "Outlook 365/Posta in arrivo" and docs[0].web_url.endswith("/1")
    pages = docs[0].load()
    assert "7.777" in pages[0][1] and "Rossi" in pages[0][1] and "allegati" in pages[0][1]
    assert docs[0].file_path.exists() and b"7.777" in docs[0].file_path.read_bytes()   # per l'anteprima


def test_outlook365_end_to_end_incremental(tmp_path):
    client, _ = fake_graph()
    app = app_with(tmp_path)
    app.state.m365_fake = True
    store: Store = app.state.indexer.store
    store.add_source("Outlook 365", "outlook365", "", {"months": 12})
    import secondbrain.connectors as cn
    orig = cn.build
    cn.build = lambda s, cd, ctx=None: Outlook365Connector(s, cd, lambda: "TKN", client)
    import secondbrain.indexer as ix
    ix.build = cn.build
    try:
        app.state.indexer.run()
        c = TestClient(app)
        hit = c.get("/api/search", params={"q": "importo 7.777"}).json()[0]
        d = c.get(f"/api/doc/{hit['doc_id']}").json()
        assert d["web_url"].startswith("https://outlook.office.com") and d["kind"] == "eml"
        app.state.indexer.run()                                   # secondo giro: nulla da rifare
        assert app.state.indexer.status["sources"]["Outlook 365"]["new"] == 0
    finally:
        ix.build = orig


def test_friendly_microsoft_errors():
    from secondbrain.m365 import friendly
    raw = "AADSTS700016: Application with identifier 'b1f7' was not found in the directory '427b'. Trace ID: x"
    f = friendly(raw)
    assert "non l'ID del tuo utente" in f and "AADSTS700016" in f and "Trace ID" not in f
    assert "amministratore" in friendly("AADSTS65001: The user or administrator has not consented")
    assert friendly("errore sconosciuto\r\nTrace") == "errore sconosciuto"


def test_exclusions_default_and_per_source(tmp_path):
    folder = tmp_path / "docs"
    for d in ("Personale", "Projects/app/node_modules", "Offerte"):
        (folder / d).mkdir(parents=True)
    (folder / "Personale" / "passaporto.txt").write_text("documento riservato")
    (folder / "Projects/app/node_modules" / "x.txt").write_text("rumore dev")
    (folder / "Projects/app" / "package-lock.json").write_text('{"a": "b"}')
    (folder / "Projects/app" / "debug.log").write_text("log riga")
    (folder / "Offerte" / "o.txt").write_text("offerta rack")
    app = app_with(tmp_path)
    c = TestClient(app)
    c.post("/api/sources", json={"type": "folder", "path": str(folder), "name": "Doc"})
    app.state.indexer._t.join(10)
    titles = lambda: {d["title"] for d in c.get("/api/documents").json()["items"]}
    assert titles() == {"passaporto.txt", "o.txt"}                 # rumore di sviluppo escluso di default
    r = c.patch("/api/sources/Doc", json={"exclude": ["Personale"]}).json()
    app.state.indexer._t.join(10)
    assert titles() == {"o.txt"}                                   # ciò che era già indicizzato esce dall'indice
    assert c.get("/api/sources").json()[0]["exclude"] == ["Personale"]
    assert c.patch("/api/sources/Inesistente", json={"exclude": []}).status_code == 404


def test_partial_warning_survives_next_run_and_failures_not_retried(tmp_path, monkeypatch):
    import secondbrain.indexer as ix
    monkeypatch.setattr(ix, "MAX_CHUNKS", 3)
    folder = tmp_path / "d"; folder.mkdir()
    (folder / "grande.txt").write_text(" ".join(f"w{i}" for i in range(2000)))
    (folder / "rotto.xlsx").write_bytes(b"non office")
    (folder / "vuoto.log2").write_text("")                          # estensione non supportata: ignorato
    (folder / "vuoto.txt").write_text("  ")                         # vuoto: nessun avviso
    app = app_with(tmp_path)
    c = TestClient(app)
    c.post("/api/sources", json={"type": "folder", "path": str(folder), "name": "D"})
    app.state.indexer._t.join(10)
    for _ in range(2):                                              # secondo giro: gli avvisi non spariscono
        lv = {i["location"]: i["level"] for i in c.get("/api/issues").json()}
        assert lv == {"D/grande.txt": "partial", "D/rotto.xlsx": "failed"}
        app.state.indexer.run()
    src = c.get("/api/status").json()["sources"][0]
    assert src["issues"] == 1 and src["partial"] == 1
    assert app.state.indexer.status["sources"]["D"]["failed"] == 0  # il file rotto invariato non è stato rielaborato


def test_embedder_splits_413_and_truncates(monkeypatch):
    import httpx, numpy as np
    from secondbrain.embed import OllamaEmbedder
    calls = []

    def fake_post(url, json, timeout):
        total = sum(len(t) for t in json["input"])
        calls.append(len(json["input"]))
        if total > 2000 or len(json["input"]) > 2:
            req = httpx.Request("POST", url)
            return httpx.Response(413, request=req)
        return httpx.Response(200, json={"embeddings": [[1.0, 0.0] for _ in json["input"]]}, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", fake_post)
    v = OllamaEmbedder("http://x", "m")(["a" * 1500, "b" * 1500, "c" * 10000, "d"])
    assert v.shape == (4, 2) and max(calls) >= 4 and 1 in calls   # batch diviso fino a riuscire
