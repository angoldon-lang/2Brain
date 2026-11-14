from __future__ import annotations
import httpx, numpy as np

MAX_CHARS = 3000      # un frammento normale è ~1500 caratteri; oltre è testo anomalo (hex, tabelle senza spazi)


class OllamaEmbedder:
    def __init__(self, host, model):
        self.host, self.model = host.rstrip("/"), model

    def __call__(self, texts: list[str]) -> np.ndarray:
        v = self._embed([t[:MAX_CHARS] for t in texts])
        return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)

    def _embed(self, texts: list[str]) -> np.ndarray:
        try:
            r = httpx.post(f"{self.host}/api/embed", json={"model": self.model, "input": texts}, timeout=300)
            r.raise_for_status()
            return np.array(r.json()["embeddings"], dtype=np.float32)
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in (400, 413):
                raise
            if len(texts) > 1:                       # richiesta troppo grande: dividi a metà
                mid = len(texts) // 2
                return np.vstack([self._embed(texts[:mid]), self._embed(texts[mid:])])
            if len(texts[0]) > 200:                  # un solo testo troppo lungo: accorcialo
                return self._embed([texts[0][: len(texts[0]) // 2]])
            raise

    def available(self) -> bool:
        try:
            self(["ping"])
            return True
        except Exception:
            return False
