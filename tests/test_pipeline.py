import numpy as np
import pytest
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
    assert c.get("/api/issues").json() == []
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


def test_docx_broken_relationship_fallback(tmp_path):
    import docx, zipfile, shutil
    from secondbrain.parsers import parse
    good = tmp_path / "ok.docx"
    d = docx.Document()
    d.add_paragraph("Offerta switch Tosano importo 9.800 euro")
    d.save(good)
    bad = tmp_path / "bad.docx"
    with zipfile.ZipFile(good) as zi, zipfile.ZipFile(bad, "w") as zo:
        for it in zi.infolist():
            data = zi.read(it.filename)
            if it.filename == "word/_rels/document.xml.rels":
                data = data.replace(b"</Relationships>",
                    b'<Relationship Id="rIdX" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="NULL"/></Relationships>')
            zo.writestr(it, data)
    assert "9.800" in parse(bad)[0][1]


def test_clear_error_for_encrypted_office(tmp_path):
    import pytest
    from secondbrain.parsers import parse
    f = tmp_path / "x.xlsx"
    f.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"0" * 50)
    with pytest.raises(ValueError, match="protetto"):
        parse(f)


def test_old_xls_with_xlsx_extension(tmp_path):
    import pytest
    xlwt = pytest.importorskip("xlwt")
    from secondbrain.parsers import parse
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Marketing")
    ws.write(0, 0, "Campagna")
    ws.write(0, 1, "Budget 5000")
    f = tmp_path / "SAL.xlsx"   # estensione sbagliata, contenuto BIFF
    wb.save(str(f))
    text = parse(f)[0][1]
    assert "Campagna" in text and "Budget 5000" in text


def test_status_issues_and_documents(tmp_path):
    docs, app = make(tmp_path, None)
    (docs / "vuoto.txt").write_text("   ")           # nessun testo -> segnalato
    (docs / "rotto.xlsx").write_bytes(b"non e un file office")
    c = run(app)
    st = c.get("/api/status").json()
    from secondbrain import __version__
    assert st["version"] == __version__
    src = st["sources"][0]
    assert src["documents"] == 3 and src["issues"] == 1
    assert src["kinds"]["txt"] == 1 and src["live"]["state"] == "done"
    assert src["live"]["total"] == 5 and src["live"]["failed"] == 1 and src["live"]["empty"] == 1
    reasons = {i["location"]: i["reason"] for i in c.get("/api/issues").json()}
    assert list(reasons) == ["Test/rotto.xlsx"] and "valido" in reasons["Test/rotto.xlsx"]   # il file vuoto non è un avviso
    lst = c.get("/api/documents", params={"q": "ferie"}).json()
    assert lst["total"] == 1 and lst["items"][0]["chunks"] >= 1
    # un file vuoto che si riempie viene indicizzato al giro successivo
    (docs / "vuoto.txt").write_text("adesso ho del testo")
    app.state.indexer.run()
    st2 = c.get("/api/status").json()
    assert st2["sources"][0]["documents"] == 4 and st2["sources"][0]["issues"] == 1


def test_migration_from_old_db(tmp_path):
    import sqlite3
    from secondbrain.db import Store
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE documents(id INTEGER PRIMARY KEY, source TEXT NOT NULL, key TEXT NOT NULL, title TEXT, location TEXT, file_path TEXT, kind TEXT, mtime REAL, size INTEGER, UNIQUE(source,key));")
    con.close()
    Store(path).source_stats()


def _docx(path, paragraphs):
    import docx
    d = docx.Document()
    for t in paragraphs:
        d.add_paragraph(t)
    d.save(path)


def test_preview_highlights_exact_chunk_and_keeps_paragraphs(tmp_path):
    docs, app = make(tmp_path, None)
    paras = [f"Paragrafo {i}: " + " ".join(f"parola{i}_{j}" for j in range(60)) for i in range(8)]
    paras[6] += " ZEBRAROSA"
    _docx(docs / "lungo.docx", paras)
    c = run(app)
    hit = c.get("/api/search", params={"q": "ZEBRAROSA"}).json()[0]
    pv = c.get(f"/api/preview/{hit['doc_id']}", params={"chunk": hit["chunk_id"]}).json()
    s, e = pv["hl"]
    assert pv["text"][s:e].split() == hit["text"].split()      # evidenziato = frammento trovato
    assert pv["text"].count("\n") >= 7                           # paragrafi conservati


def test_xlsx_compact_and_reindexed_on_parser_bump(tmp_path):
    import openpyxl, sqlite3
    docs, app = make(tmp_path, None)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"], ws["E1"], ws["A3"] = "Campagna", "Budget", "Totale"
    wb.save(docs / "conto.xlsx")
    c = run(app)
    t = c.get("/api/search", params={"q": "Campagna"}).json()[0]["text"]
    assert "| |" not in t and "Campagna | Budget" in t
    # simula un file indicizzato con un parser vecchio: viene rielaborato
    app.state.indexer.store.db.execute("UPDATE documents SET pv=1 WHERE title='conto.xlsx'")
    app.state.indexer.store.db.commit()
    app.state.indexer.run()
    assert app.state.indexer.status["sources"]["Test"]["new"] == 1


def test_runs_history_and_office_pdf(tmp_path):
    docs, app = make(tmp_path, None)
    _docx(docs / "off.docx", ["Offerta rack importo 3.340 euro"])
    c = run(app)
    r = c.get("/api/runs").json()
    assert r[0]["state"] == "completata" and r[0]["new"] == 4
    st = c.get("/api/status").json()
    if st["office_preview"]:
        did = c.get("/api/search", params={"q": "rack"}).json()[0]["doc_id"]
        resp = c.get(f"/api/pdf/{did}")
        assert resp.status_code == 200 and resp.content[:4] == b"%PDF"


def test_rtf_is_converted_to_plain_text(tmp_path):
    from secondbrain.parsers import parse
    f = tmp_path / "off.rtf"
    f.write_text(r"{\rtf1\ansi{\fonttbl\f0 Arial;}\f0 Offerta \b rack\b0  switch\par Totale 3.340 euro}")
    t = parse(f)[0][1]
    assert "Offerta" in t and "3.340" in t and "\\rtf" not in t and "fonttbl" not in t


def test_encrypted_office_with_default_password_is_readable(tmp_path):
    import openpyxl, msoffcrypto, pytest
    from secondbrain.parsers import parse
    plain = tmp_path / "plain.xlsx"
    wb = openpyxl.Workbook(); wb.active["A1"] = "Provvigioni agenti"; wb.active["B1"] = "Rossi"
    wb.save(plain)

    def enc(pw, out):
        with open(plain, "rb") as fi, open(out, "wb") as fo:
            msoffcrypto.OfficeFile(fi).encrypt(pw, fo)
    enc("VelvetSweatshop", tmp_path / "readonly.xlsx")      # «sola lettura consigliata»: si apre senza password
    enc("segretissima", tmp_path / "locked.xlsx")
    assert "Provvigioni agenti" in parse(tmp_path / "readonly.xlsx")[0][1]
    with pytest.raises(ValueError, match="protetto da password"):
        parse(tmp_path / "locked.xlsx")


def test_spreadsheet_keeps_only_rows_with_text(tmp_path):
    import openpyxl
    from secondbrain.parsers import parse
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["Descrizione", "Importo"])
    ws.append(["Switch 48 porte", 1200.5])
    for i in range(50):
        ws.append([i, i * 2.5, "12,5%"])                      # righe di soli numeri: scartate
    wb.save(tmp_path / "ce.xlsx")
    t = parse(tmp_path / "ce.xlsx")[0][1]
    assert "Switch 48 porte | 1200.5" in t and t.count("\n") <= 3


def test_scanned_pdf_without_ocr_gives_clear_reason(tmp_path):
    import pytest
    from pypdf import PdfWriter
    from secondbrain.parsers import parse
    w = PdfWriter(); w.add_blank_page(200, 200)
    f = tmp_path / "scan.pdf"; w.write(f)
    with pytest.raises(ValueError, match="OCR"):
        parse(f)


def test_stopwords_dont_beat_the_rare_term(tmp_path):
    docs, app = make(tmp_path, None)
    (docs / "giornale.txt").write_text(" ".join(["mi dai le ultime fatte offerte lotte avversari"] * 40))
    (docs / "OFF_ADC_Bper_Solarwinds.txt").write_text("Offerta per BPER Banca: importo 22.000 euro.")
    c = run(app)
    top = c.get("/api/search", params={"q": "mi dai le ultime offerte fatte a bper?"}).json()
    assert top[0]["title"] == "OFF_ADC_Bper_Solarwinds.txt"


def test_prefix_stemming_and_filename_is_searchable(tmp_path):
    docs, app = make(tmp_path, None)
    (docs / "OFF_Acme_Rack.txt").write_text("contenuto neutro senza la parola chiave")
    c = run(app)
    # "offerte" trova "OFF"? no: ma il nome file è contesto: "acme" e "rack" stanno solo nel titolo
    assert c.get("/api/search", params={"q": "acme rack"}).json()[0]["title"] == "OFF_Acme_Rack.txt"
    assert c.get("/api/search", params={"q": "offerte commerciali Rossi"}).json()[0]["title"] == "offerta_rossi.txt"


def test_recent_boost_for_ultime(tmp_path):
    import os
    docs, app = make(tmp_path, None)
    old, new = docs / "vecchia.txt", docs / "nuova.txt"
    old.write_text("offerta Bper importo 1.000"); new.write_text("offerta Bper importo 2.000")
    os.utime(old, (1_500_000_000, 1_500_000_000))
    c = run(app)
    assert c.get("/api/search", params={"q": "ultima offerta bper"}).json()[0]["title"] == "nuova.txt"


def test_two_phase_indexing_keyword_first_then_semantic(tmp_path):
    docs, app = make(tmp_path, fake_embed)
    idx = app.state.indexer
    idx.embedder = None                     # fase 1 da sola: tutto cercabile per parole, nessun vettore
    idx.run()
    c = TestClient(app)
    assert c.get("/api/status").json()["embedded"] == 0
    assert c.get("/api/search", params={"q": "ferie agosto"}).json()[0]["title"] == "ferie.md"
    idx.embedder = fake_embed               # fase 2: i vettori arrivano dopo, senza rifare la scansione
    idx.embed_pending()
    st = c.get("/api/status").json()
    assert st["embedded"] == st["chunks"] > 0 and st["embedding"]["running"] is False
    assert len(app.state.searcher.ids) == st["chunks"]                  # il ricercatore li ha ricevuti senza ricaricare


def test_xlsx_grid_preview_highlights_original_row(tmp_path):
    import openpyxl
    docs, app = make(tmp_path, None)
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["Cliente", "Offerta", "Importo"])
    for i in range(30):
        ws.append([f"Cliente{i}", f"Offerta {i}", 100 * i])
    ws.cell(row=21, column=2).value = "Rinnovo ZEBRAROSA Bper"        # riga Excel 21
    wb.save(docs / "ce.xlsx")
    c = run(app)
    hit = c.get("/api/search", params={"q": "ZEBRAROSA"}).json()[0]
    g = c.get(f"/api/preview/{hit['doc_id']}", params={"chunk": hit["chunk_id"]}).json()["grid"]
    a, b = g["hl"]
    assert any("ZEBRAROSA" in cell for n, cells in g["rows"][a:b + 1] for cell in cells)
    assert 21 in [n for n, _ in g["rows"][a:b + 1]] and g["width"] == 3


def _restart(cfg):
    return create_app(cfg, embedder=None)


def test_restart_does_not_redo_indexed_files_even_from_older_database(tmp_path):
    docs, app = make(tmp_path, None)
    _docx(docs / "a.docx", ["offerta alfa"]); _docx(docs / "b.docx", ["offerta beta"])
    run(app)
    st = app.state.indexer.store
    st.db.execute("UPDATE documents SET pv=1"); st.db.commit()      # database di una versione precedente
    cfg = Config(data_dir=tmp_path / "data", sources=[Source("Test", "folder", docs)])
    app2 = _restart(cfg)
    app2.state.indexer.run()
    s = app2.state.indexer.status["sources"]["Test"]
    assert s["new"] == 0 and s["unchanged"] == 5                      # 5 file: tutti saltati


def test_interrupted_run_resumes_and_is_recorded_as_interrupted(tmp_path, monkeypatch):
    import secondbrain.indexer as ix
    docs, app = make(tmp_path, None)
    for i in range(6):
        (docs / f"extra{i}.txt").write_text(f"documento extra numero {i}")
    real, n = ix.chunk_pages, {"n": 0}

    def boom(pages, *a, **k):
        n["n"] += 1
        if n["n"] == 5:
            raise KeyboardInterrupt
        return real(pages, *a, **k)
    monkeypatch.setattr(ix, "chunk_pages", boom)
    with pytest.raises(KeyboardInterrupt):
        app.state.indexer.run()
    done = app.state.indexer.store.stats()["documents"]
    assert 0 < done < 9 and app.state.indexer.store.runs()[0]["state"] == "interrotta"
    monkeypatch.setattr(ix, "chunk_pages", real)
    cfg = Config(data_dir=tmp_path / "data", sources=[Source("Test", "folder", docs)])
    app2 = _restart(cfg)
    app2.state.indexer.run()
    s = app2.state.indexer.status["sources"]["Test"]
    assert s["new"] == 9 - done and app2.state.indexer.store.stats()["documents"] == 9


def test_failed_scan_is_retried_when_ocr_becomes_available(tmp_path):
    from pypdf import PdfWriter
    docs, app = make(tmp_path, None)
    w = PdfWriter(); w.add_blank_page(100, 100); w.write(docs / "scan.pdf")
    run(app)
    reasons = {i["location"]: i["reason"] for i in TestClient(app).get("/api/issues").json()}
    assert "OCR non disponibile" in reasons["Test/scan.pdf"]
    cfg = Config(data_dir=tmp_path / "data", sources=[Source("Test", "folder", docs)])
    app2 = _restart(cfg)
    app2.state.indexer.run()
    assert app2.state.indexer.status["sources"]["Test"]["failed"] == 1     # riprovato (non memorizzato come definitivo)


def _pdf_with_text(path, text):
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path)); c.drawString(72, 720, text); c.save()


def test_pdf_with_malformed_encrypt_falls_back_to_pdfium(tmp_path):
    from secondbrain.parsers import parse
    f = tmp_path / "ok.pdf"
    _pdf_with_text(f, "CWNA study guide wireless offerta Bper importo 9800")
    raw = f.read_bytes()
    (tmp_path / "bad.pdf").write_bytes(raw.replace(b"trailer\n<<", b"trailer\n<</Encrypt 99 0 R ", 1))
    import pypdf
    with pytest.raises(Exception, match="Encrypt"):
        pypdf.PdfReader(str(tmp_path / "bad.pdf"))                 # il caso reale: pypdf rinuncia
    assert "Bper importo 9800" in parse(tmp_path / "bad.pdf")[0][1]  # la riserva lo legge


def test_partial_message_says_how_far_it_got(tmp_path, monkeypatch):
    import secondbrain.indexer as ix
    monkeypatch.setattr(ix, "MAX_CHUNKS", 2)
    docs, app = make(tmp_path, None)
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(docs / "libro.pdf"))
    for pg in range(1, 7):
        c.drawString(72, 720, " ".join(f"p{pg}w{i}" for i in range(250))[:1500]); c.showPage()
    c.save()
    cl = run(app)
    r = next(i["reason"] for i in cl.get("/api/issues").json() if i["location"].endswith("libro.pdf"))
    assert "fino a pag." in r and " di " in r


def test_text_with_lone_surrogate_is_cleaned_and_indexed(tmp_path, monkeypatch):
    import secondbrain.connectors.localfs as lf
    docs, app = make(tmp_path, None)
    (docs / "libro.pdf").write_bytes(b"x")
    real = lf.parse
    monkeypatch.setattr(lf, "parse", lambda p: [(1, "Capitolo CWNA wireless rotto\udc04qui fine")]
                        if p.suffix == ".pdf" else real(p))
    c = run(app)
    assert c.get("/api/issues").json() == []
    hit = c.get("/api/search", params={"q": "CWNA wireless"}).json()[0]
    assert "\udc04" not in hit["text"] and "rottoqui" in hit["text"]


def test_add_doc_is_atomic(tmp_path):
    from secondbrain.db import Store
    st = Store(tmp_path / "x.db")
    d = {"source": "S", "key": "k", "title": "t", "location": "l", "file_path": "/x", "kind": "txt", "mtime": 1.0, "size": 1}
    with pytest.raises(UnicodeEncodeError):
        st.add_doc(d, [(0, 1, "ok", None), (1, 1, "male\udc04", None)])
    assert st.stats()["documents"] == 0 and st.stats()["chunks"] == 0      # niente documento a metà
    st.add_doc(d, [(0, 1, "ok", None)])
    assert st.stats()["documents"] == 1


def test_one_unsavable_file_does_not_stop_the_source(tmp_path, monkeypatch):
    docs, app = make(tmp_path, None)
    store = app.state.indexer.store
    real = store.add_doc

    def flaky(d, chunks):
        if d["title"] == "ferie.md":
            raise RuntimeError("disco pieno")
        return real(d, chunks)
    monkeypatch.setattr(store, "add_doc", flaky)
    c = run(app)
    assert {d["title"] for d in c.get("/api/documents").json()["items"]} == {"offerta_rossi.txt", "Riunione budget"}
    assert "non salvabile" in c.get("/api/issues").json()[0]["reason"]
    assert app.state.indexer.status["sources"]["Test"]["state"] == "done"   # la sorgente non risulta in errore


def test_failed_files_are_retried_when_the_reader_improves(tmp_path):
    from secondbrain.parsers import READER_GEN
    docs, app = make(tmp_path, None)
    _pdf_with_text(docs / "ok.pdf", "CWNA wireless importo 9800")
    raw = (docs / "ok.pdf").read_bytes()
    (docs / "rotto.pdf").write_bytes(raw.replace(b"trailer\n<<", b"trailer\n<</Encrypt 99 0 R ", 1))
    st = app.state.indexer.store
    stat = (docs / "rotto.pdf").stat()
    # come se fosse fallito con una versione vecchia del programma (stesso file, stessa dimensione)
    st.set_issue("Test", "rotto.pdf", "Test/rotto.pdf", "/Encrypt entry of the trailer is not a dictionary",
                 stat.st_mtime, stat.st_size, READER_GEN - 1)
    c = run(app)
    assert "rotto.pdf" in {d["title"] for d in c.get("/api/documents").json()["items"]}     # riprovato e letto
    assert [i for i in c.get("/api/issues").json() if "rotto.pdf" in i["location"]] == []


def test_encrypted_pdf_with_empty_password_and_empty_file(tmp_path):
    from pypdf import PdfReader, PdfWriter
    from secondbrain.parsers import parse
    _pdf_with_text(tmp_path / "plain.pdf", "contratto riservato importo 4200")
    w = PdfWriter(clone_from=PdfReader(str(tmp_path / "plain.pdf")))
    w.encrypt(user_password="", owner_password="segreta", algorithm="RC4-128")
    w.write(tmp_path / "enc.pdf")
    assert "importo 4200" in parse(tmp_path / "enc.pdf")[0][1]
    (tmp_path / "vuoto.pdf").write_bytes(b"")
    assert parse(tmp_path / "vuoto.pdf") == []                                  # 0 byte: nessun errore


def test_misnamed_office_files_are_read_by_content(tmp_path):
    from secondbrain.parsers import parse
    (tmp_path / "a.docx").write_text(r"{\rtf1\ansi Accordo di riservatezza firmato\par Parti: Alfa e Beta}")
    (tmp_path / "b.docx").write_text("<html><body><p>Verbale riunione budget</p></body></html>")
    _pdf_with_text(tmp_path / "c_src.pdf", "SOW per la migrazione NSX dal cliente")
    (tmp_path / "c.docx").write_bytes((tmp_path / "c_src.pdf").read_bytes())
    assert "riservatezza" in parse(tmp_path / "a.docx")[0][1]
    assert "Verbale riunione" in parse(tmp_path / "b.docx")[0][1]
    assert "migrazione NSX" in parse(tmp_path / "c.docx")[0][1]
    (tmp_path / "d.docx").write_bytes(b"\x00\x00\x00\x00\x00\x00 zeri")
    with pytest.raises(ValueError, match="inizia con 00 00"):
        parse(tmp_path / "d.docx")


def test_docx_with_bad_part_and_pptx_with_bad_zip_entry_still_give_text(tmp_path, monkeypatch):
    import docx, zipfile, pptx
    from secondbrain import parsers
    good = tmp_path / "ok.docx"
    d = docx.Document(); d.add_paragraph("Design content importo 777"); d.save(good)
    bad = tmp_path / "bad.docx"
    with zipfile.ZipFile(good) as zi, zipfile.ZipFile(bad, "w") as zo:
        for it in zi.infolist():
            zo.writestr(it, b"questo non e xml" if it.filename == "word/settings.xml" else zi.read(it.filename))
    with pytest.raises(Exception):
        docx.Document(str(bad))                                                  # la libreria si arrende
    assert "importo 777" in parsers.parse(bad)[0][1]                             # il fallback legge il corpo
    p = pptx.Presentation(); s = p.slides.add_slide(p.slide_layouts[5]); s.shapes.title.text = "Webinar VMware Broadcom"
    p.save(tmp_path / "w.pptx")
    monkeypatch.setattr(pptx, "Presentation", lambda *a, **k: (_ for _ in ()).throw(zipfile.BadZipFile("Bad CRC-32 for file x")))
    assert "Webinar VMware" in parsers.parse(tmp_path / "w.pptx")[0][1]


def test_encrypted_xls_without_known_password_gives_clear_message(tmp_path, monkeypatch):
    import xlrd
    from secondbrain import parsers
    f = tmp_path / "x.xls"
    f.write_bytes(parsers.OLE + b"\x00" * 100)
    monkeypatch.setattr(xlrd, "open_workbook", lambda *a, **k: (_ for _ in ()).throw(xlrd.XLRDError("Workbook is encrypted")))
    with pytest.raises(ValueError, match="protetto da password"):
        parsers.parse_xls(f)


def test_spreadsheet_cap_is_per_sheet_so_big_data_sheet_does_not_hide_others(tmp_path, monkeypatch):
    import openpyxl
    import secondbrain.indexer as ix
    monkeypatch.setattr(ix, "PER_SHEET_CAP", {"xlsx": 2})
    docs, app = make(tmp_path, None)
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Dati"
    for i in range(400):
        ws.append([f"voce numero {i} descrizione lunga del prodotto", f"codice{i}", "nota"])
    wb.create_sheet("Offerta").append(["Cliente BPER", "totale offerta ZEBRAROSA"])
    wb.save(docs / "ce.xlsx")
    c = run(app)
    r = next(i for i in c.get("/api/issues").json() if i["location"].endswith("ce.xlsx"))
    assert r["level"] == "partial" and "per foglio" in r["reason"]
    assert c.get("/api/search", params={"q": "ZEBRAROSA"}).json()                 # il foglio 2 è comunque indicizzato


def test_issues_filter_by_level(tmp_path, monkeypatch):
    import secondbrain.indexer as ix
    monkeypatch.setattr(ix, "MAX_CHUNKS", 2)
    docs, app = make(tmp_path, None)
    (docs / "grande.txt").write_text(" ".join(f"w{i}" for i in range(2000)))
    (docs / "rotto.xlsx").write_bytes(b"non office")
    c = run(app)
    assert [i["location"] for i in c.get("/api/issues", params={"level": "failed"}).json()] == ["Test/rotto.xlsx"]
    assert [i["location"] for i in c.get("/api/issues", params={"level": "partial"}).json()] == ["Test/grande.txt"]


def test_zero_byte_files_are_not_errors_for_any_type(tmp_path):
    from secondbrain.parsers import parse
    for name in ("a.docx", "b.xlsx", "c.pptx", "d.pdf", "e.xls"):
        (tmp_path / name).write_bytes(b"")
        assert parse(tmp_path / name) == []


def test_damaged_pdf_falls_back_to_macos_reader_when_available(tmp_path, monkeypatch):
    from secondbrain import ocr, parsers
    f = tmp_path / "rotto.pdf"
    f.write_bytes(b"%PDF-1.4 questo file e danneggiato e illeggibile per entrambi i lettori")
    monkeypatch.setattr(ocr, "available", lambda: False)
    with pytest.raises(ValueError, match="PDF non leggibile"):
        parsers.parse(f)
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "ocr_pdf", lambda p: [(1, "testo recuperato dal lettore di macOS")])
    assert "recuperato" in parsers.parse(f)[0][1]


def test_empty_office_file_leaves_no_warning(tmp_path):
    docs, app = make(tmp_path, None)
    (docs / "vuoto.docx").write_bytes(b"")
    c = run(app)
    assert c.get("/api/issues").json() == []
