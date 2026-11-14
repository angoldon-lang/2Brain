from __future__ import annotations
import httpx, numpy as np


class OllamaEmbedder:
    def __init__(self, host, model):
        self.host, self.model = host.rstrip("/"), model

    def __call__(self, texts: list[str]) -> np.ndarray:
        r = httpx.post(f"{self.host}/api/embed", json={"model": self.model, "input": texts}, timeout=300)
        r.raise_for_status()
        v = np.array(r.json()["embeddings"], dtype=np.float32)
        return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)

    def available(self) -> bool:
        try:
            self(["ping"])
            return True
        except Exception:
            return False
