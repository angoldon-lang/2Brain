import argparse, logging
import uvicorn
from . import config
from .api import create_app

NOISY = ("httpx", "httpcore", "extract_msg", "pypdf", "PIL")


def main():
    ap = argparse.ArgumentParser(prog="secondbrain")
    ap.add_argument("cmd", choices=["serve", "index"])
    ap.add_argument("-c", "--config", default="config.toml")
    ap.add_argument("-v", "--verbose", action="store_true", help="mostra anche ogni richiesta HTTP e i log delle librerie")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not a.verbose:                           # il traffico normale copre gli avvisi che contano
        for n in NOISY:
            logging.getLogger(n).setLevel(logging.ERROR)
    cfg = config.load(a.config)
    app = create_app(cfg)
    if a.cmd == "index":
        app.state.indexer.run()
        print(app.state.indexer.status)
        return
    app.state.indexer.start()
    print(f"\nSecondBrain pronto su http://{cfg.host}:{cfg.port}  (Ctrl+C per fermare; -v per i log completi)\n")
    uvicorn.run(app, host=cfg.host, port=cfg.port, access_log=a.verbose, log_level="info" if a.verbose else "warning")
