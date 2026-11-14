"""Estrazione testo: ritorna lista di (pagina, testo). Pagina = pagina PDF / foglio / slide."""
from __future__ import annotations
import email, email.policy, html, logging, re, warnings, zipfile
from pathlib import Path

warnings.filterwarnings("ignore", module="openpyxl")
logging.getLogger("pypdf").setLevel(logging.ERROR)

TEXT_EXT = {".txt", ".md", ".csv", ".log", ".json", ".xml", ".yaml", ".yml", ".rtf"}
SUPPORTED = TEXT_EXT | {".pdf", ".docx", ".xlsx", ".xls", ".pptx", ".html", ".htm", ".eml", ".msg"}


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


def parse_docx_raw(path: Path) -> list[tuple[int, str]]:
    """Fallback per .docx con relazioni rotte (es. 'word/NULL'): legge direttamente l'XML."""
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf8", "ignore")
    xml = re.sub(r"</w:p>|<w:br[^>]*/>|<w:tab/>", "\n", xml)
    xml = re.sub(r"</w:tc>", " | ", xml)
    return [(1, html.unescape(re.sub(r"<[^>]+>", "", xml)))]


def _check_container(path: Path, ext: str) -> None:
    """Spiega perché un file OOXML non è leggibile, invece del generico 'not a zip file'."""
    if ext not in {".docx", ".xlsx", ".pptx"}:
        return
    with open(path, "rb") as f:
        head = f.read(8)
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        if ext == ".xlsx":
            return  # vecchio .xls rinominato: lo gestisce parse_xls
        raise ValueError("file protetto da password/IRM o in formato Office 97-2003 (OLE), non leggibile")
    if not head.startswith(b"PK"):
        raise ValueError("non è un file Office valido (corrotto, incompleto o rinominato)")


def parse_xls(path: Path) -> list[tuple[int, str]]:
    import xlrd
    try:
        wb = xlrd.open_workbook(str(path), on_demand=True)
    except xlrd.XLRDError as e:
        raise ValueError(f"foglio Excel non leggibile (protetto da password o corrotto): {e}")
    out = []
    for i, ws in enumerate(wb.sheets()):
        rows = (" | ".join(str(c) for c in ws.row_values(r)) for r in range(ws.nrows))
        out.append((i + 1, f"[Foglio {ws.name}]\n" + "\n".join(rows)))
    return out


def _is_ole(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(4) == b"\xd0\xcf\x11\xe0"


def parse(path: Path) -> list[tuple[int, str]]:
    ext = path.suffix.lower()
    _check_container(path, ext)
    if ext == ".xls" or (ext == ".xlsx" and _is_ole(path)):
        return parse_xls(path)
    if ext == ".pdf":
        from pypdf import PdfReader
        r = PdfReader(str(path))
        return [(i + 1, (p.extract_text() or "")) for i, p in enumerate(r.pages)]
    if ext == ".docx":
        import docx
        try:
            d = docx.Document(str(path))
        except KeyError:
            return parse_docx_raw(path)
        parts = [p.text for p in d.paragraphs]
        for t in d.tables:
            for row in t.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return [(1, "\n".join(parts))]
    if ext == ".xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        out = []
        for i, ws in enumerate(wb.worksheets):
            rows = (" | ".join("" if c is None else str(c) for c in r) for r in ws.iter_rows(values_only=True))
            out.append((i + 1, f"[Foglio {ws.title}]\n" + "\n".join(rows)))
        return out
    if ext == ".pptx":
        from pptx import Presentation
        out = []
        for i, s in enumerate(Presentation(str(path)).slides):
            out.append((i + 1, "\n".join(sh.text_frame.text for sh in s.shapes if sh.has_text_frame)))
        return out
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
