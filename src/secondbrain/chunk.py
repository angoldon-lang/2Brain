import re


def chunk_pages(pages, size=180, overlap=30):
    """Spezza per parole mantenendo il numero di pagina. Ritorna [(idx, page, text)]."""
    out, idx = [], 0
    for page, text in pages:
        words = re.sub(r"[ \t]+", " ", text).split()
        if not words:
            continue
        step = size - overlap
        for s in range(0, len(words), step):
            out.append((idx, page, " ".join(words[s:s + size])))
            idx += 1
            if s + size >= len(words):
                break
    return out
