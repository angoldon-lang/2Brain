import argparse, logging
import uvicorn
from . import config
from .api import create_app


def main():
    ap = argparse.ArgumentParser(prog="secondbrain")
    ap.add_argument("cmd", choices=["serve", "index"])
    ap.add_argument("-c", "--config", default="config.toml")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    cfg = config.load(a.config)
    app = create_app(cfg)
    if a.cmd == "index":
        app.state.indexer.run()
        print(app.state.indexer.status)
        return
    app.state.indexer.start()
    uvicorn.run(app, host=cfg.host, port=cfg.port)
