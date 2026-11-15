"""Estrazione testo: ritorna lista di (pagina, testo). Pagina = pagina PDF / foglio / slide."""
from __future__ import annotations
import email, email.policy, html, logging, re, warnings, zipfile
from pathlib import Path

warnings.filterwarnings("ignore", module="openpyxl")
logging.getLogger("pypdf").setLevel(logging.ERROR)

# ATTENZIONE: alzare il numero di un tipo fa RIFARE tutti i file indicizzati di quel tipo (ore di lavoro).
# Va fatto solo se i file già letti con successo sono letti MALE dal vecchio codice. I file che prima fallivano
# (OCR, file protetti…) vengono riprovati comunque, senza toccare questi numeri. Qui: fogli Excel (righe filtrate)
# e RTF (prima letti come testo grezzo).
PARSER_VERSION = {"xlsx": 3, "xls": 3, "rtf": 2}
OLE = b"\xd0\xcf\x11\xe0"


def parser_version(kind: str) -> int:
    return PARSER_VERSION.get(kind, 1)


def _numlike(c: str) -> bool:
    return re.fullmatch(r"[\d\s.,%€$£+\-/:()]+", c.strip()) is not None


def _row(cells) -> str:
    """Riga di foglio senza celle vuote (niente '| | | |')."""
    return " | ".join(str(c).strip() for c in cells if c is not None and str(c).strip() != "")


def _keep_row(r) -> bool:
    return any(isinstance(c, str) and c.strip() and not _numlike(c) for c in r)


def _sheet_text(title, rows) -> str:
    """Tiene solo le righe con almeno una cella di testo: le tabelle di soli numeri non aiutano la ricerca."""
    return f"[Foglio {title}]\n" + "\n".join(_row(r) for r in rows if _keep_row(r))


TEXT_EXT = {".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml"}
SUPPORTED = TEXT_EXT | {".pdf", ".docx", ".xlsx", ".xls", ".pptx", ".html", ".htm", ".eml", ".msg", ".rtf"}


def _strip_html(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"[ \t]+", " ", html.unescape(s))


def parse_eml_bytes(raw: bytes) -> tuple[str, str]:
    m = email.message_from_bytes(raw, policy=email.policy.default)
    head = f"Da: {m.get('from','')}\nA: {m.get('to','')}\nCc: {m.get('cc','')}\nData: {m.get('date','')}\nOggetto: {m.get('subject','')}\n\n"
    body = m.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        text = body.get_content()
        if body.get_content_type() == "text/html":
            text = _strip_html(text)
    names = [p.get_filename() for p in m.iter_attachments() if p.get_filename()]
    if names:
        text += "\n\nAllegati: " + ", ".join(names)
    return str(m.get("subject", "") or ""), head + text


def parse_docx_raw(src) -> list[tuple[int, str]]:
    """Fallback per .docx con relazioni rotte (es. 'word/NULL'): legge direttamente l'XML."""
    with zipfile.ZipFile(src) as z:
        xml = z.read("word/document.xml").decode("utf8", "ignore")
    xml = re.sub(r"</w:p>|<w:br[^>]*/>|<w:tab/>", "\n", xml)
    xml = re.sub(r"</w:tc>", " | ", xml)
    return [(1, html.unescape(re.sub(r"<[^>]+>", "", xml)))]


def _ole_streams(path: Path) -> set[str]:
    import olefile
    try:
        with olefile.OleFileIO(str(path)) as ole:
            return {"/".join(s) for s in ole.listdir()}
    except Exception:
        raise ValueError("file Office non leggibile (protetto da password o danneggiato)")


def _open_office(path: Path, ext: str):
    """Sorgente per le librerie OOXML: il percorso, un file decifrato in memoria, oppure 'xls' per i vecchi fogli.
    Gestisce i file «sola lettura consigliata» (cifrati con la password predefinita di Office)."""
    with open(path, "rb") as f:
        head = f.read(8)
    if head.startswith(b"PK"):
        return str(path)
    if not head.startswith(OLE):
        raise ValueError("non è un file Office valido (corrotto, incompleto o rinominato)")
    names = _ole_streams(path)
    if "EncryptedPackage" in names:
        import io, msoffcrypto
        for pw in ("VelvetSweatshop", ""):
            try:
                of = msoffcrypto.OfficeFile(open(path, "rb"))
                of.load_key(password=pw)
                out = io.BytesIO()
                of.decrypt(out)
                out.seek(0)
                return out
            except Exception:
                continue
        raise ValueError("file protetto da password: non leggibile")
    if ext in (".xlsx", ".xls") and ({"Workbook", "Book"} & names):
        return "xls"
    if ext == ".xlsx" or ext == ".xls":
        raise ValueError("contenitore Office senza un foglio Excel leggibile")
    raise ValueError("formato Office 97-2003 (.doc/.ppt): non supportato, salvalo come .docx/.pptx")


def parse_xls(path: Path) -> list[tuple[int, str]]:
    import xlrd
    try:
        wb = xlrd.open_workbook(str(path), on_demand=True)
    except Exception as e:
        raise ValueError(f"foglio Excel non leggibile (protetto da password o corrotto): {e}")
    return [(i + 1, _sheet_text(ws.name, (ws.row_values(r) for r in range(ws.nrows))))
            for i, ws in enumerate(wb.sheets())]


def _pdf_pages_pypdf(path: Path):
    from pypdf import PdfReader
    return [(i + 1, (p.extract_text() or "")) for i, p in enumerate(PdfReader(str(path)).pages)]


def _pdf_pages_pdfium(path: Path):
    """Riserva per i PDF con struttura rovinata o protezioni mal formate (es. «/Encrypt … is not a dictionary»)."""
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(str(path))
    try:
        return [(i + 1, pdf[i].get_textpage().get_text_range()) for i in range(len(pdf))]
    finally:
        pdf.close()


def parse_pdf(path: Path) -> list[tuple[int, str]]:
    pages, errors = None, []
    for reader in (_pdf_pages_pypdf, _pdf_pages_pdfium):
        try:
            got = reader(path)
        except Exception as e:
            errors.append(str(e))
            continue
        if sum(len(t.strip()) for _, t in got) >= 20:
            return got
        pages = got if pages is None else pages
        break                                   # letto bene ma senza testo: è una scansione, non un errore del lettore
    if pages is None:
        msg = " ".join(errors)
        if "password" in msg.lower() or "decrypt" in msg.lower():
            raise ValueError("PDF protetto da password: non leggibile")
        raise ValueError(f"PDF non leggibile: {errors[-1][:120] if errors else 'errore sconosciuto'}")
    from . import ocr
    if not ocr.available():
        raise ValueError("scansione senza testo: OCR non disponibile (macOS: pip install -e . installa pyobjc)")
    got = ocr.ocr_pdf(path)
    if not got:
        raise ValueError("nessun testo riconosciuto neppure con OCR (immagine vuota o illeggibile)")
    return got


def _resolve(path: Path, ext: str):
    return _open_office(path, ext) if ext != ".xls" else ("xls" if path.read_bytes()[:4] == OLE else str(path))


def grid_rows(path: Path, page: int) -> list[tuple[int, list[str]]]:
    """Righe di un foglio con la POSIZIONE ORIGINALE delle celle, nello stesso ordine/filtro usato per l'indice:
    [(numero_riga_excel, [celle])]. Serve all'anteprima a tabella."""
    ext = path.suffix.lower()
    src = _resolve(path, ext)
    out = []
    if src == "xls" or ext == ".xls":
        import xlrd
        ws = xlrd.open_workbook(str(path), on_demand=True).sheets()[page - 1]
        for r in range(ws.nrows):
            cells = ws.row_values(r)
            if _keep_row(cells):
                out.append((r + 1, ["" if c is None else str(c) for c in cells]))
        return out
    import openpyxl
    ws = openpyxl.load_workbook(src, read_only=True, data_only=True).worksheets[page - 1]
    for i, cells in enumerate(ws.iter_rows(values_only=True), 1):
        if _keep_row(cells):
            out.append((i, ["" if c is None else str(c) for c in cells]))
    return out


def parse(path: Path) -> list[tuple[int, str]]:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return parse_pdf(path)
    if ext in (".docx", ".xlsx", ".xls", ".pptx"):
        src = _resolve(path, ext)
        if src == "xls":
            return parse_xls(path)
        if ext == ".docx":
            import docx
            try:
                d = docx.Document(src)
            except KeyError:
                if hasattr(src, "seek"):
                    src.seek(0)
                return parse_docx_raw(src)
            parts = [p.text for p in d.paragraphs]
            for t in d.tables:
                for row in t.rows:
                    parts.append(" | ".join(c.text for c in row.cells))
            return [(1, "\n".join(parts))]
        if ext == ".xlsx":
            import openpyxl
            wb = openpyxl.load_workbook(src, read_only=True, data_only=True)
            return [(i + 1, _sheet_text(ws.title, ws.iter_rows(values_only=True))) for i, ws in enumerate(wb.worksheets)]
        if ext == ".pptx":
            from pptx import Presentation
            return [(i + 1, "\n".join(sh.text_frame.text for sh in s.shapes if sh.has_text_frame))
                    for i, s in enumerate(Presentation(src).slides)]
    if ext == ".rtf":
        from striprtf.striprtf import rtf_to_text
        return [(1, rtf_to_text(path.read_text(errors="ignore"), errors="ignore"))]
    if ext in {".html", ".htm"}:
        return [(1, _strip_html(path.read_text(errors="ignore")))]
    if ext == ".eml":
        return [(1, parse_eml_bytes(path.read_bytes())[1])]
    if ext == ".msg":
        import extract_msg
        m = extract_msg.Message(str(path))
        return [(1, f"Da: {m.sender}\nA: {m.to}\nData: {m.date}\nOggetto: {m.subject}\n\n{m.body or ''}")]
    if ext in TEXT_EXT:
        return [(1, path.read_text(errors="ignore"))]
    return []
