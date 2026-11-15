import re

SIZE, OVERLAP = 180, 30
STEP = SIZE - OVERLAP
MAX_CHUNKS = 400          # tetto per file (file enormi, es. fogli con decine di migliaia di righe)


def clean(s):
    """Toglie i caratteri non codificabili in UTF-8 (surrogati isolati che escono da PDF con codifica rovinata
    o da nomi di file con byte non validi): né SQLite né Ollama li accettano."""
    return s.encode("utf-8", "ignore").decode("utf-8") if isinstance(s, str) else s


def chunk_pages(pages, size=SIZE, overlap=OVERLAP):
    """Spezza per parole mantenendo il numero di pagina. Ritorna [(idx, page, text)]."""
    out, idx = [], 0
    for page, text in pages:
        words = clean(text).split()
        if not words:
            continue
        step = size - overlap
        for s in range(0, len(words), step):
            out.append((idx, page, " ".join(words[s:s + size])))
            idx += 1
            if s + size >= len(words):
                break
    return out


def locate(page_text, page_chunks, chunk_id):
    """Posizione (carattere iniziale, finale) di un frammento dentro il testo originale della sua pagina.
    page_chunks: frammenti della stessa pagina in ordine [{id,text}]. None se non combacia."""
    pos = next((i for i, c in enumerate(page_chunks) if c["id"] == chunk_id), None)
    if pos is None:
        return None
    words = list(re.finditer(r"\S+", page_text))
    a = pos * STEP
    b = min(a + SIZE, len(words))
    if a >= len(words):
        return None
    if " ".join(w.group() for w in words[a:b]) != page_chunks[pos]["text"]:
        return None
    return words[a].start(), words[b - 1].end()
