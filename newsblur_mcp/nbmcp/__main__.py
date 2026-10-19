from __future__ import annotations

import logging
import signal
import sys
import threading

from .client import NewsBlurClient
from .config import ConfigError, load
from .server import make_server, serve_in_thread
from .tools import Tools


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("nbmcp")
    try:
        cfg = load()
    except ConfigError as e:
        log.error("configuration error: %s", e)
        return 2
    tools = Tools(cfg, NewsBlurClient(cfg.username, cfg.password, cfg.api_base))
    log.info("starting: mark_read=%s folders_allowed=%s", "ON" if cfg.allow_mark_read else "off", sorted(cfg.allowed_folders) or "all")
    server = make_server(cfg, tools)
    serve_in_thread(server)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
