from __future__ import annotations
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Source:
    name: str
    type: str
    path: Path


@dataclass
class Config:
    data_dir: Path = Path("~/.secondbrain").expanduser()
    ollama_host: str = "http://127.0.0.1:11434"
    chat_model: str = "qwen2.5:7b-instruct"
    embed_model: str = "bge-m3"
    host: str = "127.0.0.1"
    port: int = 8765
    top_k: int = 8
    sources: list[Source] = field(default_factory=list)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "index.db"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"


def load(path: str | Path = "config.toml") -> Config:
    p = Path(path)
    raw = tomllib.loads(p.read_text()) if p.exists() else {}
    o, s, se = raw.get("ollama", {}), raw.get("server", {}), raw.get("search", {})
    c = Config(
        data_dir=Path(raw.get("data_dir", "~/.secondbrain")).expanduser(),
        ollama_host=o.get("host", Config.ollama_host),
        chat_model=o.get("chat_model", Config.chat_model),
        embed_model=o.get("embed_model", Config.embed_model),
        host=s.get("host", Config.host),
        port=int(s.get("port", Config.port)),
        top_k=int(se.get("top_k", Config.top_k)),
        sources=[Source(x["name"], x["type"], Path(x["path"]).expanduser())
                 for x in raw.get("sources", [])],
    )
    c.data_dir.mkdir(parents=True, exist_ok=True)
    c.cache_dir.mkdir(parents=True, exist_ok=True)
    return c
